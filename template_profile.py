from __future__ import annotations
"""
template_profile.py — Structural parser for arbitrary CV DOCX templates
=======================================================================

The generation pipeline needs to understand what's in a user's template
*without* pattern-matching against known English section names like
"Experience" or "Skills". Different users have different sections (Research,
Volunteer, Board positions, Patents, Media appearances, custom-named
projects, ...), different bullet formats (bold subheading vs. plain), and
different skills layouts (comma-single vs. line-per-item vs. categorised).

This module walks the DOCX body, classifies every paragraph/table by its
visual role, and groups consecutive elements into `Group` objects that the
downstream planner and renderer consume.

It does NOT:
    • modify any part of the existing cv_engine pipeline
    • import from cv_engine (avoids circular imports; shares only
      namespace constants via minimal re-declaration)
    • make any assumption about section names

Public API:
    build_profile(template_path: Path) -> TemplateProfile

    # Convenience for diagnostics:
    classify_elements(template_path: Path) -> list[Element]
    summarise_profile(profile: TemplateProfile) -> str
"""

import re, zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from lxml import etree


# ─── Constants ────────────────────────────────────────────────────────────────
WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Year patterns: any 4-digit year 1900–2099 present in text is a strong hint
# that the line is a date / meta row. We use a word-boundary match.
_YEAR_RE    = re.compile(r"\b(?:19|20)\d{2}\b")
_DATE_SEPS  = re.compile(r"[\u2013\u2014\-–—/|·•]")   # em/en-dash, pipe, bullet, slash
_ALL_DATE   = re.compile(r"^[\d\s\u2013\u2014\-–—/\.\(\),\|:]+$")
_PAGE_NO    = re.compile(r"^\s*page\s*\d+\s*(of\s*\d+)?\s*$", re.I)

# Characters-per-line estimation (copied intentionally so this module is
# standalone — same table as cv_engine._AVG_CHAR_EM)
_AVG_CHAR_EM: dict[str, float] = {
    "verdana": 0.560, "tahoma": 0.520, "calibri": 0.450, "carlito": 0.450,
    "arial": 0.490, "helvetica": 0.490, "liberation sans": 0.490,
    "times": 0.445, "times new roman": 0.445, "georgia": 0.490,
    "cambria": 0.475, "caladea": 0.475, "garamond": 0.440,
    "book antiqua": 0.480, "palatino": 0.490, "courier": 0.600,
    "courier new": 0.600,
}
_DEFAULT_CHAR_EM = 0.500


# ─── Element + Group data model ──────────────────────────────────────────────

class ElementKind(str, Enum):
    HEADING_MAJOR = "HEADING_MAJOR"    # top-level section header (EXPERIENCE / SKILLS / AWARDS / ...)
    META          = "META"             # company / role / date / location row
    DESCRIPTION   = "DESCRIPTION"      # short prose about the preceding meta (company tagline, etc.)
    BULLET        = "BULLET"           # list item
    CONTENT       = "CONTENT"          # anything else that carries user-visible text
    SPACER        = "SPACER"           # empty or whitespace-only paragraph


class GroupKind(str, Enum):
    ENTITY_LIST = "ENTITY_LIST"   # Heading + repeating (META → optional DESC → BULLETs) — Experience, Projects, Volunteer, Research, etc.
    SIMPLE_LIST = "SIMPLE_LIST"   # Heading + BULLETs only — Awards, Publications, Certifications
    PROSE       = "PROSE"         # Heading + CONTENT paragraphs — Summary, Profile
    SKILLS      = "SKILLS"        # Heading + CONTENT; sub-layout detected
    CONTACT     = "CONTACT"       # Pre-first-heading block (name, email, phone, LinkedIn) — preserved verbatim


class SkillLayout(str, Enum):
    COMMA_SINGLE  = "comma_single"    # comma-separated single paragraph
    LINE_PER_ITEM = "line_per_item"   # soft-break or hard-return per skill
    CATEGORISED   = "categorised"     # "Technical: X, Y / Languages: A, B"
    BULLETED      = "bulleted"        # one bullet per skill
    UNKNOWN       = "unknown"


@dataclass
class ElementStyle:
    """Minimal Word-formatting descriptor used by the classifier."""
    font_name:       Optional[str] = None
    font_size_pt:    Optional[float] = None
    bold:            bool = False           # majority of runs bold
    italic:          bool = False           # majority of runs italic
    all_caps:        bool = False           # text is ALL CAPS (heuristic)
    has_numpr:       bool = False           # paragraph has list numbering
    para_style_id:   Optional[str] = None   # pPr/pStyle val (e.g. "Heading1")
    alignment:       Optional[str] = None   # left / center / right / justify

    def is_heading_like(self, body_size_pt: float) -> bool:
        """Heuristic: this element looks like a heading."""
        if self.para_style_id and "heading" in self.para_style_id.lower():
            return True
        big = (self.font_size_pt or 0) >= body_size_pt * 1.15
        return bool(
            (big and self.bold)
            or (self.all_caps and self.bold)
            or (big and self.all_caps)
        )


@dataclass
class Element:
    """One classified paragraph / table cell at top-level of the body."""
    idx:   int
    kind:  ElementKind
    text:  str
    style: ElementStyle
    # Preserved references for rendering. `xml` is the original lxml Element
    # so the renderer can replace content in place without losing formatting.
    xml:   object = None
    # For BULLET: list of (text, is_bold) runs, preserved so the renderer can
    # decide whether a bullet already has a bold subheading prefix.
    runs:  list[tuple[str, bool]] = field(default_factory=list)

    @property
    def has_subhead(self) -> bool:
        """True if this bullet's text has a bold run ending with ':'."""
        if self.kind is not ElementKind.BULLET:
            return False
        for run_text, is_bold in self.runs:
            if is_bold and ":" in run_text:
                return True
        return False


@dataclass
class EntityItem:
    """One entity inside an ENTITY_LIST (a single job, project, role, etc.)."""
    label:                str                  # literal META text, used as human-readable handle
    key:                  str                  # unique slug
    meta_indices:         list[int] = field(default_factory=list)
    description_indices:  list[int] = field(default_factory=list)
    bullet_indices:       list[int] = field(default_factory=list)
    # Per-bullet format memory — parallel to bullet_indices
    bullet_has_subhead:   list[bool] = field(default_factory=list)


@dataclass
class Group:
    """A contiguous section of the document."""
    label:        str                  # literal heading text (or synthesised, e.g. "__contact__")
    key:          str                  # unique slug
    kind:         GroupKind
    heading_idx:  Optional[int] = None
    start_idx:    int = 0
    end_idx:      int = 0
    # For ENTITY_LIST:
    items:        list[EntityItem] = field(default_factory=list)
    # For SIMPLE_LIST:
    bullet_indices: list[int] = field(default_factory=list)
    bullet_has_subhead: list[bool] = field(default_factory=list)
    # For PROSE / SKILLS:
    content_indices: list[int] = field(default_factory=list)
    # For SKILLS:
    skill_layout:    SkillLayout = SkillLayout.UNKNOWN
    # For CONTACT:
    contact_indices: list[int] = field(default_factory=list)


@dataclass
class TemplateProfile:
    """The full structural understanding of a template."""
    elements:       list[Element]
    groups:         list[Group]
    # Body metrics for downstream sizing decisions
    body_font_size_pt: float = 10.0
    body_font_name:    str   = "Verdana"
    usable_twips:      int   = 11361
    chars_per_line:    int   = 90


# ─── Primitive helpers (DOCX XML walking) ────────────────────────────────────

def _para_text(p) -> str:
    return "".join(x.text or "" for x in p.iter(f"{{{WNS}}}t"))


def _table_text(tbl) -> str:
    return "".join(t.text or "" for t in tbl.iter(f"{{{WNS}}}t"))


def _get_usable_twips(body) -> int:
    sectPr = body.find(f"{{{WNS}}}sectPr")
    if sectPr is None:
        return 11361
    pgSz  = sectPr.find(f"{{{WNS}}}pgSz")
    pgMar = sectPr.find(f"{{{WNS}}}pgMar")
    if pgSz is None or pgMar is None:
        return 11361
    try:
        w     = int(pgSz.get(f"{{{WNS}}}w",     0))
        left  = int(pgMar.get(f"{{{WNS}}}left",  0))
        right = int(pgMar.get(f"{{{WNS}}}right", 0))
        return max(3000, w - left - right)
    except (TypeError, ValueError):
        return 11361


def _estimate_cpl(font_name: str, font_size_pt: float, usable_twips: int) -> int:
    size = float(font_size_pt) if font_size_pt else 10.0
    if size <= 0:
        size = 10.0
    em   = _AVG_CHAR_EM.get((font_name or "").strip().lower(), _DEFAULT_CHAR_EM)
    char_w_twips = size * 20.0 * em
    if char_w_twips <= 0:
        return 100
    return max(60, min(180, int(usable_twips / char_w_twips)))


def _collect_runs(p) -> list[tuple[str, bool]]:
    """Return [(run_text, is_bold), ...] preserving order."""
    out: list[tuple[str, bool]] = []
    for r in p.findall(f"{{{WNS}}}r"):
        rPr = r.find(f"{{{WNS}}}rPr")
        bold = False
        if rPr is not None:
            if rPr.find(f"{{{WNS}}}b") is not None:
                bold = True
        txt = "".join(t.text or "" for t in r.findall(f"{{{WNS}}}t"))
        if txt:
            out.append((txt, bold))
    return out


def _has_numpr(p) -> bool:
    return p.find(f".//{{{WNS}}}numPr") is not None


def _para_style_id(p) -> Optional[str]:
    pPr = p.find(f"{{{WNS}}}pPr")
    if pPr is None:
        return None
    pStyle = pPr.find(f"{{{WNS}}}pStyle")
    if pStyle is None:
        return None
    return pStyle.get(f"{{{WNS}}}val")


def _alignment(p) -> Optional[str]:
    pPr = p.find(f"{{{WNS}}}pPr")
    if pPr is None:
        return None
    jc = pPr.find(f"{{{WNS}}}jc")
    return jc.get(f"{{{WNS}}}val") if jc is not None else None


def _primary_font_name(rPr_chain) -> Optional[str]:
    """Pick an rFonts/@ascii out of a list of rPr candidates, first match wins."""
    for rPr in rPr_chain:
        if rPr is None:
            continue
        fonts_el = rPr.find(f"{{{WNS}}}rFonts")
        if fonts_el is not None:
            for attr in ("ascii", "hAnsi", "cs"):
                name = fonts_el.get(f"{{{WNS}}}{attr}")
                if name:
                    return name
    return None


def _primary_font_size_pt(rPr_chain) -> Optional[float]:
    for rPr in rPr_chain:
        if rPr is None:
            continue
        sz = rPr.find(f"{{{WNS}}}sz")
        if sz is not None:
            try:
                return int(sz.get(f"{{{WNS}}}val", "20")) / 2.0
            except (TypeError, ValueError):
                continue
    return None


def _majority_bold(runs: list[tuple[str, bool]]) -> bool:
    if not runs:
        return False
    bold_chars = sum(len(t) for t, b in runs if b)
    total      = sum(len(t) for t, _ in runs)
    return total > 0 and bold_chars >= total * 0.6


def _majority_italic(p) -> bool:
    runs = p.findall(f"{{{WNS}}}r")
    if not runs:
        return False
    it_len = 0
    tot    = 0
    for r in runs:
        rPr = r.find(f"{{{WNS}}}rPr")
        is_it = rPr is not None and rPr.find(f"{{{WNS}}}i") is not None
        txt   = "".join(t.text or "" for t in r.findall(f"{{{WNS}}}t"))
        L = len(txt)
        tot += L
        if is_it:
            it_len += L
    return tot > 0 and it_len >= tot * 0.6


def _element_style(p, body_font_name: str, body_size_pt: float) -> ElementStyle:
    """Build ElementStyle for a paragraph."""
    text  = _para_text(p)
    runs  = p.findall(f"{{{WNS}}}r")
    rPr_chain: list = []
    for r in runs:
        rPr_chain.append(r.find(f"{{{WNS}}}rPr"))
    pPr = p.find(f"{{{WNS}}}pPr")
    if pPr is not None:
        rPr_chain.append(pPr.find(f"{{{WNS}}}rPr"))

    font = _primary_font_name(rPr_chain) or body_font_name
    size = _primary_font_size_pt(rPr_chain) or body_size_pt
    run_pairs = _collect_runs(p)
    bold      = _majority_bold(run_pairs)
    italic    = _majority_italic(p)
    # ALL-CAPS heuristic: majority of alphabetic chars are upper and there's ≥3 alphas
    alpha     = [c for c in text if c.isalpha()]
    all_caps  = bool(alpha) and len(alpha) >= 3 and sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.9

    return ElementStyle(
        font_name       = font,
        font_size_pt    = size,
        bold            = bold,
        italic          = italic,
        all_caps        = all_caps,
        has_numpr       = _has_numpr(p),
        para_style_id   = _para_style_id(p),
        alignment       = _alignment(p),
    )


# ─── Body-metric estimation ──────────────────────────────────────────────────

def _estimate_body_metrics(body) -> tuple[str, float]:
    """
    Guess the body font name + size by finding the most common (font, size)
    pair among paragraphs that have a numPr (bullets) — those are almost
    always set in the body size. Falls back to the first sub-20pt run.
    """
    from collections import Counter
    counts: Counter = Counter()
    for p in body.iter(f"{{{WNS}}}p"):
        if not _has_numpr(p):
            continue
        for r in p.findall(f"{{{WNS}}}r"):
            rPr = r.find(f"{{{WNS}}}rPr")
            if rPr is None:
                continue
            sz_el = rPr.find(f"{{{WNS}}}sz")
            fonts_el = rPr.find(f"{{{WNS}}}rFonts")
            size = None
            if sz_el is not None:
                try:
                    size = int(sz_el.get(f"{{{WNS}}}val", "20")) / 2.0
                except (TypeError, ValueError):
                    pass
            name = None
            if fonts_el is not None:
                for attr in ("ascii", "hAnsi", "cs"):
                    name = fonts_el.get(f"{{{WNS}}}{attr}") or name
            if size and name:
                counts[(name, size)] += 1
    if counts:
        (name, size), _ = counts.most_common(1)[0]
        return name, size
    # fallback: scan any run with a size
    for p in body.iter(f"{{{WNS}}}p"):
        for r in p.findall(f"{{{WNS}}}r"):
            rPr = r.find(f"{{{WNS}}}rPr")
            if rPr is None:
                continue
            sz = rPr.find(f"{{{WNS}}}sz")
            fonts = rPr.find(f"{{{WNS}}}rFonts")
            if sz is None:
                continue
            try:
                size = int(sz.get(f"{{{WNS}}}val", "20")) / 2.0
            except (TypeError, ValueError):
                continue
            name = None
            if fonts is not None:
                for attr in ("ascii", "hAnsi", "cs"):
                    name = fonts.get(f"{{{WNS}}}{attr}") or name
            if name and size and size < 20:
                return name, size
    return "Verdana", 10.0


# ─── Element classification ──────────────────────────────────────────────────

def _looks_like_contact(text: str) -> bool:
    """Contact-line heuristic: contains @ (email), phone digits, or linkedin URL."""
    t = text.lower()
    if "@" in text and "." in text:
        return True
    if "linkedin.com" in t or "github.com" in t or "http://" in t or "https://" in t:
        return True
    # Phone-shaped: has ≥7 digits
    digits = sum(1 for c in text if c.isdigit())
    return digits >= 7 and len(text) <= 120


def _looks_like_date_only(text: str) -> bool:
    """Text is only dates / separators — e.g. '2022 – Present'."""
    t = text.strip()
    return bool(t) and bool(_ALL_DATE.match(t)) and len(t) <= 50


def _classify_paragraph(
    idx:            int,
    p,
    body_font_name: str,
    body_size_pt:   float,
    prev_kind:      Optional[ElementKind],
    prev_prev_kind: Optional[ElementKind],
) -> Element:
    """Classify one paragraph into an Element with a ElementKind tag."""
    text  = _para_text(p).strip()
    style = _element_style(p, body_font_name, body_size_pt)
    runs  = _collect_runs(p)

    # SPACER: empty line
    if not text:
        return Element(idx, ElementKind.SPACER, text, style, xml=p, runs=runs)

    # BULLET: numPr wins regardless of formatting
    if style.has_numpr:
        return Element(idx, ElementKind.BULLET, text, style, xml=p, runs=runs)

    # Very long non-bullet line → CONTENT (body prose; Summary-like)
    is_short = len(text) <= 140

    # HEADING_MAJOR: heading-shaped formatting OR bold+short+standalone
    if style.is_heading_like(body_size_pt) and is_short:
        return Element(idx, ElementKind.HEADING_MAJOR, text, style, xml=p, runs=runs)

    # META: has a year pattern, or is a short line immediately following a
    # HEADING_MAJOR, or is a short bold line (company row) in a structured CV.
    has_year = bool(_YEAR_RE.search(text))
    if has_year and is_short:
        return Element(idx, ElementKind.META, text, style, xml=p, runs=runs)

    # A short bold line that isn't heading-sized but follows a heading, meta,
    # description, OR the previous entity's BULLETs → treat as META.
    # (The post-BULLET case is how we detect a new entity starting inside an
    # ENTITY_LIST: e.g. after Fivetran's last bullet, "ZS Associates — Pune"
    # is the meta row of the next item, not freeform content.)
    if is_short and style.bold and prev_kind in {
        ElementKind.HEADING_MAJOR, ElementKind.META, ElementKind.DESCRIPTION,
        ElementKind.BULLET,
    }:
        return Element(idx, ElementKind.META, text, style, xml=p, runs=runs)

    # DESCRIPTION: non-bullet paragraph that sits between a META and either
    # another META or a BULLET, and reads like a sentence (≤220 chars, has a
    # space, no year, typically italic / gray). We commit to DESCRIPTION when
    # the PREVIOUS element was META and the paragraph is short-ish prose.
    if (
        prev_kind is ElementKind.META
        and len(text) <= 250
        and " " in text
        and not has_year
    ):
        return Element(idx, ElementKind.DESCRIPTION, text, style, xml=p, runs=runs)

    # DESCRIPTION (2nd case): italic short line directly under META or HEADING
    if (
        prev_kind in {ElementKind.META, ElementKind.HEADING_MAJOR}
        and style.italic
        and is_short
    ):
        return Element(idx, ElementKind.DESCRIPTION, text, style, xml=p, runs=runs)

    # Fallback: CONTENT (skill lines, summary prose, freeform)
    return Element(idx, ElementKind.CONTENT, text, style, xml=p, runs=runs)


def _classify_table(idx: int, tbl, body_font_name: str, body_size_pt: float) -> Element:
    """
    Tables in CV templates are most often used to position (company, date)
    on the same line. We treat the concatenated table text as a META element
    unless it contains a numbered/bulleted list (rare), in which case we
    still classify as META — individual bullets beneath remain separate
    paragraphs and will be classified on their own.
    """
    text = _table_text(tbl).strip()
    if not text:
        return Element(idx, ElementKind.SPACER, "", ElementStyle(), xml=tbl)
    # Approximate style from the first paragraph found in the table
    first_p = tbl.find(f".//{{{WNS}}}p")
    style = _element_style(first_p, body_font_name, body_size_pt) if first_p is not None else ElementStyle()
    kind  = ElementKind.META if len(text) <= 250 else ElementKind.CONTENT
    return Element(idx, kind, text, style, xml=tbl, runs=[(text, style.bold)])


def classify_elements(template_path: Path) -> list[Element]:
    """Public: walk the DOCX body and return an ordered list of Elements."""
    with zipfile.ZipFile(template_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)
    body = tree.find(f"{{{WNS}}}body")
    if body is None:
        return []

    body_font_name, body_size_pt = _estimate_body_metrics(body)

    elements: list[Element] = []
    prev_kind:      Optional[ElementKind] = None
    prev_prev_kind: Optional[ElementKind] = None

    idx = 0

    def _emit(el: Element):
        nonlocal prev_kind, prev_prev_kind, idx
        elements.append(el)
        prev_prev_kind = prev_kind
        prev_kind = el.kind if el.kind is not ElementKind.SPACER else prev_kind
        idx += 1

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            _emit(_classify_paragraph(idx, child, body_font_name, body_size_pt, prev_kind, prev_prev_kind))
        elif tag == "tbl":
            # Tables in CV templates serve two distinct purposes:
            #   (a) layout helper for a single row (e.g. "Company  …  Date" with
            #       2 columns of 1 paragraph each) — keep as a single META.
            #   (b) container for an entire experience/section block, with
            #       company rows + descriptions + bullets as inner paragraphs —
            #       flatten so the classifier sees each inner paragraph.
            inner_paras  = list(child.iter(f"{{{WNS}}}p"))
            text_paras   = [p for p in inner_paras if _para_text(p).strip()]
            has_bullets  = any(_has_numpr(p) for p in text_paras)
            if has_bullets or len(text_paras) > 2:
                for p in inner_paras:
                    _emit(_classify_paragraph(idx, p, body_font_name, body_size_pt, prev_kind, prev_prev_kind))
            else:
                _emit(_classify_table(idx, child, body_font_name, body_size_pt))
        # else: skip unrelated children (sectPr, bookmarks, etc.)

    return elements


# ─── Group formation ─────────────────────────────────────────────────────────

def _slugify(text: str, used: set[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    s = s[:40] or "section"
    base = s
    i = 2
    while s in used:
        s = f"{base}_{i}"; i += 1
    used.add(s)
    return s


def _classify_group_kind(elements: list[Element], start: int, end: int) -> GroupKind:
    """Given a slice [start:end] of classified elements, decide the group kind."""
    kinds = [e.kind for e in elements[start:end]]
    has_meta    = ElementKind.META   in kinds
    has_bullet  = ElementKind.BULLET in kinds
    has_content = ElementKind.CONTENT in kinds

    # ENTITY_LIST: metas + bullets (date+bullets structure)
    if has_meta and has_bullet:
        return GroupKind.ENTITY_LIST
    # SIMPLE_LIST: only bullets (no metas)
    if has_bullet and not has_meta:
        return GroupKind.SIMPLE_LIST
    # ENTITY_LIST with descriptions but no bullets yet (rare) → still entity_list
    if has_meta:
        return GroupKind.ENTITY_LIST
    # PROSE vs SKILLS: content-only. We disambiguate using the heading text
    # and the comma density — handled by caller.
    return GroupKind.PROSE


def _detect_skill_layout(elements: list[Element], indices: list[int]) -> SkillLayout:
    """
    Look at the CONTENT paragraphs under a SKILLS heading and decide layout.
    - If any element has_numpr → BULLETED
    - If >= 2 elements → LINE_PER_ITEM
    - Single element:
        - Many colons + semicolons/newlines → CATEGORISED
        - Comma-separated list → COMMA_SINGLE
        - Default → UNKNOWN
    """
    if not indices:
        return SkillLayout.UNKNOWN
    if any(elements[i].style.has_numpr for i in indices):
        return SkillLayout.BULLETED
    if len(indices) >= 2:
        return SkillLayout.LINE_PER_ITEM
    text = elements[indices[0]].text
    colon_count = text.count(":")
    comma_count = text.count(",")
    if colon_count >= 2 and colon_count >= comma_count // 4:
        return SkillLayout.CATEGORISED
    if comma_count >= 2:
        return SkillLayout.COMMA_SINGLE
    return SkillLayout.UNKNOWN


_SKILLS_HINTS = (
    "skill", "competenc", "tool", "technolog", "languag", "stack",
    "proficienc", "expertise",
)


def _heading_hints_skills(label: str) -> bool:
    lo = label.lower()
    return any(h in lo for h in _SKILLS_HINTS)


_PROSE_HINTS = (
    "summary", "profile", "about", "objective", "interests", "hobbies",
    "references",
)


def _heading_hints_prose(label: str) -> bool:
    lo = label.lower()
    return any(h in lo for h in _PROSE_HINTS)


def _build_contact_group(elements: list[Element], upto: int, used: set[str]) -> Optional[Group]:
    """
    Everything before the first HEADING_MAJOR is the contact block.
    Preserved verbatim by the renderer.
    """
    if upto <= 0:
        return None
    indices = [i for i in range(upto) if elements[i].kind is not ElementKind.SPACER]
    if not indices:
        return None
    return Group(
        label           = "__contact__",
        key             = _slugify("__contact__", used),
        kind            = GroupKind.CONTACT,
        heading_idx     = None,
        start_idx       = 0,
        end_idx         = upto,
        contact_indices = indices,
    )


def _is_contact_shaped_group(g: Group, elements: list[Element]) -> bool:
    """
    True if this group is really just name + contact rows masquerading as a
    section. Heuristic: it's PROSE (or SKILLS) group whose heading is short
    and whose content is entirely contact-shaped (email / URL / phone) OR
    empty. Used to rescue CV templates where the candidate's NAME is rendered
    as a large bold line (often classified as HEADING_MAJOR) at the top of
    the doc.
    """
    if g.kind not in {GroupKind.PROSE, GroupKind.SKILLS}:
        return False
    if g.content_indices:
        # Every content paragraph must be contact-shaped
        for i in g.content_indices:
            if not _looks_like_contact(elements[i].text):
                return False
        return True
    # No content at all → treat as degenerate contact (a stray name line)
    return True


def _merge_leading_contact_blocks(
    groups: list[Group], elements: list[Element], used_keys: set[str],
) -> list[Group]:
    """
    Walk from the start of `groups` and merge any leading groups whose content
    is entirely name/contact-shaped into a single CONTACT group. Stops at the
    first "real" section (ENTITY_LIST, SIMPLE_LIST, or PROSE/SKILLS that has
    non-contact content).

    Preserves the order of all underlying elements; the merged CONTACT group's
    contact_indices include both the heading paragraph(s) and any content
    paragraphs, so the renderer outputs them verbatim in their original slots.
    """
    if not groups:
        return groups

    # Start point: existing CONTACT group (from _build_contact_group) or none.
    absorbed_indices: list[int] = []
    start_idx = groups[0].start_idx
    i0 = 0
    if groups[0].kind is GroupKind.CONTACT:
        absorbed_indices.extend(groups[0].contact_indices)
        start_idx = groups[0].start_idx
        i0 = 1

    absorbed = 0
    end_idx = groups[0].end_idx
    for g in groups[i0:]:
        if not _is_contact_shaped_group(g, elements):
            break
        # Add heading paragraph and all content of this group
        if g.heading_idx is not None:
            absorbed_indices.append(g.heading_idx)
        absorbed_indices.extend(g.content_indices)
        end_idx = g.end_idx
        absorbed += 1

    if absorbed == 0 and i0 == 0:
        return groups  # nothing to merge

    # Build unified CONTACT group (reuse existing key if present, else new)
    if i0 == 1:
        merged = groups[0]
        merged.contact_indices = absorbed_indices
        merged.end_idx         = end_idx
    else:
        merged = Group(
            label           = "__contact__",
            key             = _slugify("__contact__", used_keys),
            kind            = GroupKind.CONTACT,
            heading_idx     = None,
            start_idx       = start_idx,
            end_idx         = end_idx,
            contact_indices = absorbed_indices,
        )

    return [merged] + groups[i0 + absorbed:]


def _split_entity_items(elements: list[Element], indices: list[int], group_key: str) -> list[EntityItem]:
    """
    Walk the [indices] of a group (post-heading) and split into EntityItems.

    An item starts on each run of META elements. The first META in a run sets
    the item label; any trailing META elements in the same run get appended
    to meta_indices. A DESCRIPTION immediately after META goes to the item's
    description_indices. BULLETs in the run go to bullet_indices. The item
    closes when we hit the next META-after-BULLET, or run out of indices.
    """
    items: list[EntityItem] = []
    used_sub: set[str] = set()
    cur: Optional[EntityItem] = None
    phase: str = "meta"   # 'meta' → 'desc' → 'bullets'

    def _open(label: str, first_idx: int) -> EntityItem:
        slug = f"{group_key}_{_slugify(label, used_sub)}"
        return EntityItem(
            label = label,
            key   = slug,
            meta_indices = [first_idx],
        )

    for i in indices:
        el = elements[i]
        if el.kind is ElementKind.SPACER:
            continue
        if el.kind is ElementKind.META:
            if cur is None:
                cur = _open(el.text[:80], i)
                phase = "meta"
            elif phase == "meta":
                # continuation of the meta row (e.g., second line: "Role | 2022–24")
                cur.meta_indices.append(i)
            else:
                # New item starts — flush previous
                items.append(cur)
                cur = _open(el.text[:80], i)
                phase = "meta"
        elif el.kind is ElementKind.DESCRIPTION:
            if cur is None:
                # orphan description — skip (shouldn't happen with classifier)
                continue
            cur.description_indices.append(i)
            phase = "desc"
        elif el.kind is ElementKind.BULLET:
            if cur is None:
                # Bullets under a heading but no META row (e.g., SIMPLE_LIST) —
                # caller should not route here. Create a synthetic item.
                cur = EntityItem(
                    label = "(unnamed)",
                    key   = f"{group_key}_unnamed",
                )
            cur.bullet_indices.append(i)
            cur.bullet_has_subhead.append(el.has_subhead)
            phase = "bullets"
        elif el.kind is ElementKind.CONTENT:
            # Non-bullet content inside an entity list is unusual; if we're in
            # bullets phase, treat as a trailing CONTENT to ignore for now.
            # If before any bullets under a meta, treat as a description extension.
            if cur is not None and phase in {"meta", "desc"}:
                cur.description_indices.append(i)
                phase = "desc"
        # HEADING_MAJOR should not appear inside a group's indices (caller splits on them)

    if cur is not None:
        items.append(cur)
    return items


def build_profile(template_path: Path) -> TemplateProfile:
    """
    Public entry point: parse the template and return a TemplateProfile.
    This function does NOT mutate any template state; it's read-only.
    """
    elements = classify_elements(template_path)
    if not elements:
        return TemplateProfile(elements=[], groups=[])

    # Compute body metrics again (cheap) for the profile
    with zipfile.ZipFile(template_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)
    body = tree.find(f"{{{WNS}}}body")
    body_font_name, body_size_pt = _estimate_body_metrics(body)
    usable_twips   = _get_usable_twips(body)
    chars_per_line = _estimate_cpl(body_font_name, body_size_pt, usable_twips)

    # Find heading positions
    heading_positions = [
        i for i, el in enumerate(elements) if el.kind is ElementKind.HEADING_MAJOR
    ]

    groups: list[Group] = []
    used_keys: set[str] = set()

    # 1) Contact group (everything before first heading)
    first_heading = heading_positions[0] if heading_positions else len(elements)
    contact = _build_contact_group(elements, first_heading, used_keys)
    if contact is not None:
        groups.append(contact)

    # 2) One group per heading
    for hi, h_idx in enumerate(heading_positions):
        next_h = heading_positions[hi + 1] if hi + 1 < len(heading_positions) else len(elements)
        label  = elements[h_idx].text[:80]
        key    = _slugify(label, used_keys)
        inner  = list(range(h_idx + 1, next_h))
        kind   = _classify_group_kind(elements, h_idx + 1, next_h)

        # Prose vs Skills disambiguation based on content and heading hint
        if kind is GroupKind.PROSE:
            if _heading_hints_skills(label):
                kind = GroupKind.SKILLS
            elif not _heading_hints_prose(label):
                # Content-only, heading gives no hint: default to PROSE unless
                # there's clearly a comma-separated skills-like single line.
                only_content_idx = [i for i in inner if elements[i].kind is ElementKind.CONTENT]
                if only_content_idx and len(only_content_idx) == 1:
                    t = elements[only_content_idx[0]].text
                    if t.count(",") >= 3 and len(t) <= 400:
                        kind = GroupKind.SKILLS

        g = Group(
            label       = label,
            key         = key,
            kind        = kind,
            heading_idx = h_idx,
            start_idx   = h_idx,
            end_idx     = next_h,
        )

        if kind is GroupKind.ENTITY_LIST:
            g.items = _split_entity_items(elements, inner, key)
        elif kind is GroupKind.SIMPLE_LIST:
            g.bullet_indices     = [i for i in inner if elements[i].kind is ElementKind.BULLET]
            g.bullet_has_subhead = [elements[i].has_subhead for i in g.bullet_indices]
        elif kind is GroupKind.SKILLS:
            g.content_indices = [
                i for i in inner
                if elements[i].kind in (ElementKind.CONTENT, ElementKind.BULLET)
            ]
            g.skill_layout    = _detect_skill_layout(elements, g.content_indices)
        elif kind is GroupKind.PROSE:
            g.content_indices = [
                i for i in inner if elements[i].kind is ElementKind.CONTENT
            ]

        groups.append(g)

    # Post-process: merge leading name/contact-shaped groups into CONTACT.
    # Fixes templates where the candidate's name is a large bold line that the
    # classifier reads as HEADING_MAJOR.
    groups = _merge_leading_contact_blocks(groups, elements, used_keys)

    return TemplateProfile(
        elements          = elements,
        groups            = groups,
        body_font_name    = body_font_name,
        body_font_size_pt = body_size_pt,
        usable_twips      = usable_twips,
        chars_per_line    = chars_per_line,
    )


# ─── Diagnostic printer ──────────────────────────────────────────────────────

def summarise_profile(profile: TemplateProfile) -> str:
    """Pretty-print a profile for debugging — used by tests and ad-hoc runs."""
    lines: list[str] = []
    lines.append(f"TemplateProfile ({len(profile.elements)} elements, {len(profile.groups)} groups)")
    lines.append(
        f"  body: font={profile.body_font_name!r} size={profile.body_font_size_pt}pt "
        f"usable_twips={profile.usable_twips} cpl={profile.chars_per_line}"
    )
    lines.append("")
    for g in profile.groups:
        lines.append(f"─ GROUP [{g.kind.value}] {g.label!r}  (key={g.key})")
        if g.kind is GroupKind.CONTACT:
            lines.append(f"    contact lines: {len(g.contact_indices)}")
            for i in g.contact_indices[:4]:
                lines.append(f"      • {profile.elements[i].text[:80]!r}")
        elif g.kind is GroupKind.ENTITY_LIST:
            for item in g.items:
                lines.append(
                    f"    • item {item.label!r} "
                    f"meta={len(item.meta_indices)} "
                    f"desc={len(item.description_indices)} "
                    f"bullets={len(item.bullet_indices)} "
                    f"subhead={item.bullet_has_subhead}"
                )
        elif g.kind is GroupKind.SIMPLE_LIST:
            lines.append(
                f"    bullets={len(g.bullet_indices)} subhead={g.bullet_has_subhead}"
            )
        elif g.kind is GroupKind.SKILLS:
            lines.append(
                f"    content_paras={len(g.content_indices)} layout={g.skill_layout.value}"
            )
            for i in g.content_indices[:2]:
                lines.append(f"      • {profile.elements[i].text[:100]!r}")
        elif g.kind is GroupKind.PROSE:
            lines.append(f"    content_paras={len(g.content_indices)}")
            for i in g.content_indices[:2]:
                lines.append(f"      • {profile.elements[i].text[:100]!r}")
    return "\n".join(lines)


# ─── __main__: ad-hoc diagnostic ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: python template_profile.py <template.docx>")
        sys.exit(2)
    p = build_profile(Path(sys.argv[1]))
    print(summarise_profile(p))
