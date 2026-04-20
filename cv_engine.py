from __future__ import annotations
#!/usr/bin/env python3
"""
cv_engine.py — Self-contained DOCX + PDF generation engine
============================================================
Completely standalone — does NOT import from cv_automation.
Section detection is template-first: the DOCX itself is scanned to discover all
sections and bullet counts with zero prior knowledge. The master_bank (optional)
is used only to map discovered section titles → user-defined section keys.

Works for ANY user's CV template — no hardcoded section names anywhere.

Public API:
    discover_template_sections(template_path)  → {title: bullet_count}
    read_template_slots(template_path, master_bank=None)  → {section_key: bullet_count}
    extract_template_format_rules(template_path)  → format rules dict
    modify_docx(sections, skills_text, template_path, output_path,
                master_bank=None, project_overrides=None)
    convert_to_pdf(docx_path)  → Path | None
    check_one_page(pdf_path)   → bool
"""

import os, re, shutil, subprocess, tempfile, zipfile
from copy import deepcopy
from pathlib import Path

from lxml import etree

# ─── Constants ───────────────────────────────────────────────────────────────
WNS        = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MAX_BULLET = 215   # fallback — overridden by format_rules extracted from the user's template


# ─── Section-reset keywords ───────────────────────────────────────────────────
# These are major CV headings — when encountered they signal the start of a new
# CV section and reset the current section tracker (so bullets beneath them are
# not incorrectly attributed to the previous company / project).
# Education, skills, and all structural headings are in here so they are NEVER
# passed to the AI for bullet generation — they stay exactly as in the template.
_SECTION_RESETS = frozenset({
    # Experience headings — pure containers: individual company rows come after
    "experience", "work experience", "professional experience",
    "relevant experience", "internship experience",
    # Education headings — pure containers: institution rows come after
    "education", "academic background", "academic qualifications",
    "educational background", "academic history",
    # Project headings — pure containers: project rows come after
    "side project", "side projects", "projects", "personal projects",
    "academic projects", "selected projects",
    # Skills headings — filled server-side from skills_text, never as bullets
    "skills", "skills & additional information",
    "skills &amp; additional information",
    "core competencies", "technical skills", "key skills",
    "tools & technologies",
    # Certifications — special-cased via _CERTIFICATIONS_HEADINGS (handled before
    # _is_section_reset); kept here for robustness when no dedicated cert key path.
    "certifications", "certificates", "licenses & certifications",
    "professional certifications",
    # Profile / summary — not bullet sections
    "summary", "profile", "professional summary", "executive summary",
    "career summary", "objective", "career objective",
    # Non-bullet trailing blocks
    "interests", "hobbies", "references",
    # NOTE: "awards", "achievements", "publications", "research",
    # "presentations", "languages", "leadership", "leadership experience",
    # "volunteer", "volunteering", "community involvement", "extracurricular",
    # "extracurricular activities", "honors & awards" were intentionally REMOVED
    # from this set. Those are typically SELF-CONTAINED bullet sections (the
    # heading *is* the section) and should be captured as cur_title so their
    # bullets are counted / tailored, not discarded. They remain matchable as
    # anchors via `_build_anchors` (humanised section_key / template_anchor).
})


# Headings that specifically designate a DEDICATED Certifications bullet
# section (i.e. the template has "Certifications" as its own heading with
# bullet paragraphs under it, as opposed to certifications living inside the
# Skills paragraph). When we see one of these, we route subsequent bullets to
# a reserved synthetic section key so generate() can populate them from the
# user's certifications list.
_CERTIFICATIONS_HEADINGS = frozenset({
    "certifications", "certificates",
    "licenses & certifications", "licenses and certifications",
    "licences & certifications", "licences and certifications",
    "professional certifications",
    "certifications & licenses", "certifications and licenses",
    "certifications & licences", "certifications and licences",
})

# Reserved section key used when the template has a dedicated Certifications
# bullet section. `sections[CERTIFICATIONS_KEY]` is populated server-side from
# `master_bank["certifications"]` (JD-ranked) — the AI never writes to it.
CERTIFICATIONS_KEY = "__certifications__"


def _is_section_reset(text_lower: str) -> bool:
    """
    True if `text_lower` (already lowercased, stripped) is a recognised
    section heading that should end the current section-of-interest.

    Uses prefix matching so variants like "volunteer experience",
    "leadership experience", "extracurricular activities", and
    "community involvement" all match correctly — the original exact-set
    check missed these.
    """
    if not text_lower:
        return False
    if text_lower in _SECTION_RESETS:
        return True
    if text_lower.rstrip("s") in _SECTION_RESETS:
        return True
    if len(text_lower) > 60:
        return False   # body-of-text, not a heading
    for reset in _SECTION_RESETS:
        # Word-boundary prefix match: "volunteer experience" starts with "volunteer "
        if text_lower.startswith(reset + " "):
            return True
    return False


def _is_certifications_heading(text_lower: str) -> bool:
    """True if this heading designates a dedicated Certifications bullet section."""
    if not text_lower:
        return False
    if text_lower in _CERTIFICATIONS_HEADINGS:
        return True
    if len(text_lower) > 60:
        return False
    for h in _CERTIFICATIONS_HEADINGS:
        if text_lower == h or text_lower.startswith(h + " ") or text_lower.endswith(" " + h):
            return True
    return False


# ─── XML helpers ─────────────────────────────────────────────────────────────

def _table_text(tbl) -> str:
    return "".join(t.text or "" for t in tbl.iter(f"{{{WNS}}}t"))


def _para_text(child) -> str:
    return "".join(x.text or "" for x in child.iter(f"{{{WNS}}}t"))


def _first_significant_text(tbl) -> str:
    """
    Return the first cell text in a table that looks like a name or title
    (skips empty cells, pure-date strings, and pure numbers).
    Used to auto-detect company / institution names from any CV template.
    """
    for cell in tbl.iter(f"{{{WNS}}}tc"):
        t = "".join(x.text or "" for x in cell.iter(f"{{{WNS}}}t")).strip()
        # Must be non-trivial and not a date / number string
        if len(t) > 3 and not re.fullmatch(r"[\d\s\u2013\-\–\/\.\(\),]+", t):
            return t
    return ""


def _clone_bullet(template_para, text: str):
    """Clone a bullet paragraph with a bold SubHeading (text before ':') + regular body."""
    para = deepcopy(template_para)
    for tag in ("r", "ins"):
        for el in para.findall(f"{{{WNS}}}{tag}"):
            para.remove(el)

    subhead, body = ("", text)
    if ":" in text:
        i = text.index(":")
        subhead, body = text[: i + 1], text[i + 1 :]

    pPr      = para.find(f"{{{WNS}}}pPr")
    base_rPr = pPr.find(f"{{{WNS}}}rPr") if pPr is not None else None

    def run(txt, bold=False):
        r   = etree.SubElement(para, f"{{{WNS}}}r")
        rPr = etree.SubElement(r,    f"{{{WNS}}}rPr")
        if base_rPr is not None:
            for ch in base_rPr:
                rPr.append(deepcopy(ch))
        if bold:
            rPr.insert(0, etree.Element(f"{{{WNS}}}bCs"))
            rPr.insert(0, etree.Element(f"{{{WNS}}}b"))
        t = etree.SubElement(r, f"{{{WNS}}}t")
        t.text = txt
        if txt and (txt[0] == " " or txt[-1] == " "):
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    if subhead:
        run(subhead, bold=True)
    if body:
        run(body)
    return para


def _skill_rPr(bold: bool = False, font_name: str = "Verdana",
               font_size_half_pt: int = 16):
    """Build a run properties element for skills text with the given font settings."""
    rPr = etree.Element(f"{{{WNS}}}rPr")
    if bold:
        etree.SubElement(rPr, f"{{{WNS}}}b")
        etree.SubElement(rPr, f"{{{WNS}}}bCs")
    fonts = etree.SubElement(rPr, f"{{{WNS}}}rFonts")
    for attr in ("ascii", "eastAsia", "hAnsi", "cs"):
        fonts.set(f"{{{WNS}}}{attr}", font_name)
    sz_val = str(font_size_half_pt)
    etree.SubElement(rPr, f"{{{WNS}}}sz").set(f"{{{WNS}}}val",   sz_val)
    etree.SubElement(rPr, f"{{{WNS}}}szCs").set(f"{{{WNS}}}val", sz_val)
    return rPr


def _add_skill_run(para, text: str, bold: bool = False, add_br: bool = False,
                   font_name: str = "Verdana", font_size_half_pt: int = 16):
    r = etree.SubElement(para, f"{{{WNS}}}r")
    r.append(_skill_rPr(bold=bold, font_name=font_name, font_size_half_pt=font_size_half_pt))
    if add_br:
        etree.SubElement(r, f"{{{WNS}}}br")
    t = etree.SubElement(r, f"{{{WNS}}}t")
    t.text = text
    if text and (text[0] == " " or text[-1] == " "):
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


# ─── Template format extraction ──────────────────────────────────────────────

# Skills-section heading variants (subset of _SECTION_RESETS)
_SKILL_HEADINGS = frozenset({
    "skills", "skills & additional information",
    "core competencies", "technical skills", "key skills",
    "tools & technologies",
})


def _extract_skill_lines(para) -> list[str]:
    """
    Extract individual skill lines from a skills paragraph.
    Skills paragraphs use <w:br> (soft-return) to separate lines within a single
    paragraph block rather than newline characters.
    """
    lines: list[str] = []
    current: list[str] = []

    for r in para.findall(f"{{{WNS}}}r"):
        br = r.find(f"{{{WNS}}}br")
        if br is not None and br.get(f"{{{WNS}}}type", "") != "page":
            lines.append("".join(current))
            current = []
        for t in r.findall(f"{{{WNS}}}t"):
            if t.text:
                current.append(t.text)

    if current:
        lines.append("".join(current))

    return [ln for ln in lines if ln.strip()]


def extract_template_format_rules(template_path: Path) -> dict:
    """
    Auto-extract formatting rules from the user's uploaded .docx CV template.

    Reads the DOCX XML directly — zero assumptions about font, size, or layout.
    This function is called once when the template is uploaded; the result is
    stored in the database and injected at CV generation time so that all
    formatting (bullet length limit, skill font, etc.) matches the user's own
    template exactly.

    Returns a dict with:
        max_bullet_chars     — 90th-percentile char count of bullet paragraphs
        max_skill_lines      — number of soft-return lines in the skills block
        max_skill_line_chars — longest individual skill line (characters)
        bullet_font          — font name from the first bullet run's rPr
        bullet_font_size_pt  — font size in points (half-points ÷ 2)
        has_bold_subheading  — True when bold runs precede ':' in bullets
        bullet_format        — descriptor string used in AI prompts
    """
    with zipfile.ZipFile(template_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)
    body = tree.find(f"{{{WNS}}}body")

    bullet_lengths:   list[int] = []
    first_bullet_rPr             = None   # lxml Element or None
    has_bold_subheading          = False
    skill_lines:      list[str] = []
    in_skills                    = False

    for child in body:
        if child.tag.split("}")[-1] != "p":
            continue

        pt       = (_para_text(child) or "").strip()
        pt_lower = pt.lower()

        # ── Section boundary detection ────────────────────────────────────────
        is_reset = (pt_lower in _SECTION_RESETS
                    or pt_lower.rstrip("s") in _SECTION_RESETS)
        if is_reset:
            in_skills = (pt_lower in _SKILL_HEADINGS
                         or pt_lower.rstrip("s") in _SKILL_HEADINGS)
            continue

        # ── Skills paragraph (first content para after skills heading) ────────
        if in_skills and pt:
            skill_lines = _extract_skill_lines(child)
            in_skills   = False
            continue

        # ── Bullet paragraphs ─────────────────────────────────────────────────
        if child.find(f".//{{{WNS}}}numPr") is not None and len(pt) > 15:
            bullet_lengths.append(len(pt))

            # Capture rPr from the first run of the first substantial bullet
            if first_bullet_rPr is None and len(pt) > 40:
                runs = child.findall(f"{{{WNS}}}r")
                if runs:
                    rPr = runs[0].find(f"{{{WNS}}}rPr")
                    if rPr is not None:
                        first_bullet_rPr = rPr

            # Detect bold subheading: a bold run whose text contains ':'
            if not has_bold_subheading and ":" in pt:
                for r in child.findall(f"{{{WNS}}}r"):
                    rPr_r = r.find(f"{{{WNS}}}rPr")
                    if rPr_r is not None and rPr_r.find(f"{{{WNS}}}b") is not None:
                        t_txt = "".join(t.text or "" for t in r.findall(f"{{{WNS}}}t"))
                        if ":" in t_txt:
                            has_bold_subheading = True
                            break

    # ── max_bullet_chars: 90th-percentile of actual bullet lengths ────────────
    max_bullet_chars = 180  # safer fallback
    if bullet_lengths:
        bullet_lengths.sort()
        idx = min(int(len(bullet_lengths) * 0.90), len(bullet_lengths) - 1)
        max_bullet_chars = max(120, min(350, bullet_lengths[idx]))

    # ── Font + size from the first bullet run's rPr ───────────────────────────
    bullet_font          = "Verdana"
    bullet_font_size_pt  = 8
    if first_bullet_rPr is not None:
        fonts_el = first_bullet_rPr.find(f"{{{WNS}}}rFonts")
        if fonts_el is not None:
            font = (
                fonts_el.get(f"{{{WNS}}}ascii")
                or fonts_el.get(f"{{{WNS}}}hAnsi")
                or fonts_el.get(f"{{{WNS}}}cs")
            )
            if font:
                bullet_font = font
        sz_el = first_bullet_rPr.find(f"{{{WNS}}}sz")
        if sz_el is not None:
            try:
                half_pts = int(sz_el.get(f"{{{WNS}}}val", "16"))
                bullet_font_size_pt = half_pts // 2
            except (TypeError, ValueError):
                pass

    # ── Skill line metrics ────────────────────────────────────────────────────
    max_skill_lines      = len(skill_lines) if skill_lines else 5
    max_skill_line_chars = max((len(ln) for ln in skill_lines), default=80)

    # ── Chars-per-line estimate (line-fill discipline, widow-word avoidance) ──
    # Compute how many characters of the bullet font/size fit on one rendered
    # line of the template's page at its margins. This is the key input for
    # "no dangling last-line words" logic: the AI is asked to pack bullets so
    # that the last line is substantially full, and app.py post-processes any
    # bullet whose final wrapped line has <3 words.
    usable_twips     = _get_text_width_twips(body)
    chars_per_line   = _estimate_chars_per_line(bullet_font, bullet_font_size_pt, usable_twips)

    # Ideal 2-line max: pack the second line to ~90% full so minor font-metric
    # differences between LibreOffice and Word don't push it to a 3rd line.
    # Leave a 6-char safety margin on each full line.
    ideal_1line_max  = max(60,  chars_per_line - 6)
    ideal_2line_min  = chars_per_line + 30            # enough to clearly need 2 lines
    ideal_2line_max  = min(max_bullet_chars, int(chars_per_line * 1.88))

    return {
        "max_bullet_chars":     max_bullet_chars,
        "max_skill_lines":      max_skill_lines,
        "max_skill_line_chars": max_skill_line_chars,
        "bullet_font":          bullet_font,
        "bullet_font_size_pt":  bullet_font_size_pt,
        "has_bold_subheading":  has_bold_subheading,
        "bullet_format":        "SubHeading: [verb] [action+context], [result]",
        "chars_per_line":       chars_per_line,
        "ideal_1line_max":      ideal_1line_max,
        "ideal_2line_min":      ideal_2line_min,
        "ideal_2line_max":      ideal_2line_max,
    }


# ─── Line-width estimation + widow-word fixer ────────────────────────────────
# These are used to (a) tell the AI the line budget in chars so it can write
# bullets that fill their final line, and (b) post-process any bullet whose
# last wrapped line has <3 words (a "widow"), by trimming to end cleanly on
# the previous line. Character widths below are empirical average-char widths
# in ems for each font; they're the same values typography tools use for rough
# text-fitting estimates. Proportional fonts vary ±5%, which is well within
# the 6-char safety margin we leave on each line.

_AVG_CHAR_EM: dict[str, float] = {
    "verdana":            0.560,
    "tahoma":             0.520,
    "calibri":            0.450,
    "carlito":            0.450,   # Calibri-metric-compatible (Linux)
    "arial":              0.490,
    "helvetica":          0.490,
    "liberation sans":    0.490,
    "times":              0.445,
    "times new roman":    0.445,
    "georgia":            0.490,
    "cambria":            0.475,
    "caladea":            0.475,   # Cambria-metric-compatible (Linux)
    "garamond":           0.440,
    "book antiqua":       0.480,
    "palatino":           0.490,
    "courier":            0.600,
    "courier new":        0.600,
}
_DEFAULT_CHAR_EM = 0.500


def _estimate_chars_per_line(font_name: str, font_size_pt: int | float,
                             usable_twips: int) -> int:
    """
    Estimate how many characters of `font_name` at `font_size_pt` fit on one
    line of width `usable_twips`. Uses an average-char-em lookup; accurate
    enough (±5%) for proportional fonts used in CVs. 1 pt = 20 twips.
    """
    size_pt = float(font_size_pt) if font_size_pt else 10.0
    if size_pt <= 0:
        size_pt = 10.0
    em = _AVG_CHAR_EM.get((font_name or "").strip().lower(), _DEFAULT_CHAR_EM)
    char_w_twips = size_pt * 20.0 * em   # 1 pt = 20 twips
    if char_w_twips <= 0:
        return 100
    cpl = int(usable_twips / char_w_twips)
    # Clamp to a sane range — very small/large values indicate weird templates
    return max(60, min(180, cpl))


def fix_widow_line(text: str, chars_per_line: int,
                   min_last_line_words: int = 3,
                   min_last_line_chars: int = 18) -> str:
    """
    Prevent a bullet from ending with 1–2 words dangling on a new line.

    Greedy-wraps `text` at `chars_per_line`-char line boundaries (breaking at
    spaces). If the bullet wraps to >1 line AND the final line contains
    fewer than `min_last_line_words` words AND fewer than `min_last_line_chars`
    characters, we drop those dangling tokens and tidy the trailing punctuation.

    Why drop instead of extend? Extending would require another AI round-trip
    and risk hallucinating facts. The widow content is almost always a filler
    suffix ("across regions", "by leveraging X") — losing it costs little and
    guarantees the bullet renders without visual waste.

    Returns the original text when no widow is detected or when `chars_per_line`
    is unknown.
    """
    if not text or chars_per_line <= 0:
        return text
    words = text.split()
    if not words:
        return text

    lines: list[list[str]] = [[]]
    cur_len = 0
    for w in words:
        add = len(w) + (1 if lines[-1] else 0)
        if cur_len + add > chars_per_line and lines[-1]:
            lines.append([w])
            cur_len = len(w)
        else:
            lines[-1].append(w)
            cur_len += add

    if len(lines) <= 1:
        return text

    last = lines[-1]
    last_chars = sum(len(w) for w in last) + max(0, len(last) - 1)
    if len(last) >= min_last_line_words and last_chars >= min_last_line_chars:
        return text

    kept_words = [w for line in lines[:-1] for w in line]
    if not kept_words:
        return text
    trimmed = " ".join(kept_words).rstrip(" ,;:·–-—&/")
    # Ensure we didn't strip into a mid-sentence preposition / connector
    tail_lower = trimmed.lower().split()[-1] if trimmed else ""
    _CONNECTORS = {"and", "or", "the", "a", "an", "of", "to", "for", "in",
                   "on", "with", "by", "via", "from", "at", "across", "over",
                   "under", "into", "as", "per", "through"}
    while tail_lower in _CONNECTORS:
        trimmed = trimmed.rsplit(" ", 1)[0].rstrip(" ,;:·–-—&/")
        tail_lower = trimmed.lower().split()[-1] if trimmed else ""
    return trimmed or text


# ─── Template structure discovery ────────────────────────────────────────────

def discover_template_sections(template_path: Path) -> dict:
    """
    Walk the template DOCX and return {section_title_or_synthetic_key: bullet_count}.

    Handles three structural patterns the engine must support:
      • Standard section: a non-bullet title paragraph ("Acme Corp — Analyst")
        followed by bullet paragraphs. Title becomes the dict key.
      • Dedicated Certifications section: a "Certifications" heading followed
        by bullet paragraphs. Slots are routed under CERTIFICATIONS_KEY so
        generate() can fill them from the user's cert list (AI never writes here).
      • Missing sections (e.g. no Projects block, no Leadership): simply
        absent from the returned dict — downstream code only fills what's present.
    """
    with zipfile.ZipFile(template_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)

    slot_counts: dict[str, int] = {}
    cur_title: str | None = None

    for child in tree.iter(f"{{{WNS}}}p"):
        pt = (_para_text(child) or "").strip()
        if not pt:
            continue

        pt_lower = pt.lower()

        # Dedicated Certifications heading → route following bullets to synthetic key
        if _is_certifications_heading(pt_lower):
            cur_title = CERTIFICATIONS_KEY
            continue

        if _is_section_reset(pt_lower):
            cur_title = None
            continue

        if child.find(f".//{{{WNS}}}numPr") is not None:
            if cur_title:
                slot_counts[cur_title] = slot_counts.get(cur_title, 0) + 1
        else:
            if len(pt) > 3 and len(pt) <= 80 and not re.fullmatch(r"[\d\s\u2013\-\–\/\.\(\),\|]+", pt):
                cur_title = pt

    return slot_counts


def extract_bank_from_template(template_path: Path) -> dict:
    """
    Build a minimal master_bank by reading the template DOCX itself — used
    as a FALLBACK when the user has not uploaded any CV bullet bank text.

    Strategy:
      - Walk body paragraphs AND tables in order.
      - Each non-bullet heading that passes the title-shape filter opens a
        new section, with `template_anchor` = the exact heading text so
        `_build_anchors` re-registers it exactly as in the template.
      - Every subsequent bullet paragraph is captured as a bank bullet under
        that section.
      - Dedicated Certifications bullets are collected into top-level
        `certifications: []`.
      - Tables are treated like titles when they contain company/role-shaped
        text, so Experience rows become sections anchored on the full row
        text (works for single and multi-role templates alike).

    The returned bank is deliberately terse — it mirrors the template's
    existing content so the AI can tailor each bullet to the JD while the
    engine fills the same slot counts the template defines.
    """
    with zipfile.ZipFile(template_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)
    body = tree.find(f"{{{WNS}}}body")
    if body is None:
        return {"sections": {}, "certifications": [], "skills_text": ""}

    sections: dict[str, dict] = {}
    certifications: list[str] = []
    cur_key: str | None = None
    used_keys: set[str] = set()

    def _slug(txt: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "_", txt.lower()).strip("_")
        return (s or "section")[:40]

    def _open_section(heading: str) -> str:
        base = _slug(heading)
        key  = base
        i = 2
        while key in used_keys:
            key = f"{base}_{i}"; i += 1
        used_keys.add(key)
        sections[key] = {
            "company":        "",
            "role":           "",
            "project_name":   "",
            "date":           "",
            "template_anchor": heading,
            "bullet_slots":   0,
            "bullets":        [],
        }
        return key

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "tbl":
            full = _table_text(child).strip()
            if not full:
                continue
            tt_lower = full.lower()[:120]
            if _is_certifications_heading(tt_lower):
                cur_key = "__certifications_collect__"
                continue
            if _is_section_reset(tt_lower):
                cur_key = None
                continue
            first = (_first_significant_text(child) or full[:80]).strip()
            if first and len(first) <= 120:
                cur_key = _open_section(first)
            continue

        if tag != "p":
            continue

        pt = (_para_text(child) or "").strip()
        if not pt:
            continue
        pt_lower = pt.lower()

        is_bullet = child.find(f".//{{{WNS}}}numPr") is not None
        if not is_bullet and (pt.startswith("•") or pt.startswith("–") or pt.startswith("-") or pt.startswith("*")):
            is_bullet = True

        if is_bullet:
            if cur_key == "__certifications_collect__":
                cleaned = re.sub(r"^[•\-\*\–\s]+", "", pt).strip()
                if cleaned:
                    certifications.append(cleaned)
            elif cur_key and cur_key in sections:
                sections[cur_key]["bullets"].append({
                    "id": f"{cur_key}_{len(sections[cur_key]['bullets']) + 1}",
                    "text": re.sub(r"^[•\-\*\–\s]+", "", pt).strip(),
                    "tags": [],
                })
                sections[cur_key]["bullet_slots"] = len(sections[cur_key]["bullets"])
            continue

        # Non-bullet paragraph
        if _is_certifications_heading(pt_lower):
            cur_key = "__certifications_collect__"
            continue
        if _is_section_reset(pt_lower):
            cur_key = None
            continue

        if len(pt) > 3 and len(pt) <= 120 and not re.fullmatch(r"[\d\s\u2013\-\–\/\.\(\),\|]+", pt):
            cur_key = _open_section(pt)

    # Drop empty sections (headings that had no bullets beneath them)
    sections = {k: v for k, v in sections.items() if v["bullets"]}

    return {
        "sections":       sections,
        "certifications": certifications,
        "skills_text":    "",
        "skills_header":  "Skills",
    }


def map_template_slots_from_raw(raw_slots: dict, master_bank: dict) -> dict:
    """
    Map a raw_slots dict (title → bullet_count, as returned by
    discover_template_sections) to master-bank section keys, without
    re-opening the DOCX file.

    This is the in-memory equivalent of read_template_slots() when the raw
    slot counts were cached in format_rules at upload time.
    """
    if not master_bank:
        return raw_slots
    all_anchors = _build_anchors(master_bank)
    result: dict[str, int] = {}
    for title, count in raw_slots.items():
        # Synthetic cert key is already canonical — pass through untouched
        if title == CERTIFICATIONS_KEY:
            result[title] = result.get(title, 0) + count
            continue
        title_lower = title.lower()
        matched_key = None
        for anchor, key in all_anchors:
            if anchor.lower() in title_lower:
                matched_key = key
                break
        if matched_key:
            result[matched_key] = result.get(matched_key, 0) + count
    return result


def read_template_slots(template_path: Path,
                        master_bank: dict | None = None) -> dict:
    """"""
    if not master_bank:
        return discover_template_sections(template_path)

    all_anchors = _build_anchors(master_bank)

    slot_counts: dict[str, int] = {}
    cur_section: str | None     = None

    with zipfile.ZipFile(template_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)

    for child in tree.iter(f"{{{WNS}}}p"):
        pt = (_para_text(child) or "").strip()
        if not pt:
            continue

        if child.find(f".//{{{WNS}}}numPr") is not None:
            if cur_section:
                slot_counts[cur_section] = slot_counts.get(cur_section, 0) + 1
            continue

        pt_lower = pt.lower()
        if _is_certifications_heading(pt_lower):
            cur_section = CERTIFICATIONS_KEY
            continue
        if _is_section_reset(pt_lower):
            cur_section = None
            continue

        for anchor, key in all_anchors:
            if anchor.lower() in pt_lower:
                cur_section = key
                break

    return slot_counts


# ─── Side project date alignment helpers ─────────────────────────────────────

def _get_text_width_twips(body) -> int:
    """Return the usable text-area width in twips from the document's sectPr."""
    sectPr = body.find(f"{{{WNS}}}sectPr")
    if sectPr is None:
        return 11361  # fallback: ~7.89 in at 0.3-in margins
    pgSz  = sectPr.find(f"{{{WNS}}}pgSz")
    pgMar = sectPr.find(f"{{{WNS}}}pgMar")
    if pgSz is None or pgMar is None:
        return 11361
    try:
        w     = int(pgSz.get(f"{{{WNS}}}w",     0))
        left  = int(pgMar.get(f"{{{WNS}}}left",  0))
        right = int(pgMar.get(f"{{{WNS}}}right", 0))
        return w - left - right
    except (TypeError, ValueError):
        return 11361


def _fix_side_project_date_alignment(body) -> None:
    """
    Side project title paragraphs use different amounts of tab+space padding per
    slot. When a title is substituted with one of a different length the padding
    no longer balances and the two dates land at different x-positions.

    Fix: for every direct-child paragraph of <body> that (a) has no bullet marker
    and (b) contains a bare 4-digit year run, replace all inter-padding runs with
    a single right-align tab stop so both dates snap to the same right edge
    regardless of title length.
    """
    right_pos = _get_text_width_twips(body)

    for child in body:
        if child.tag.split("}")[-1] != "p":
            continue
        if child.find(f".//{{{WNS}}}numPr") is not None:
            continue
        pt = "".join(x.text or "" for x in child.iter(f"{{{WNS}}}t"))
        # Also skip if it starts with a bullet symbol (safety)
        if pt.startswith("•") or pt.startswith("-") or pt.startswith("*"):
            continue
        if not re.search(r'\b20\d{2}\b', pt):
            continue
        if len(pt) > 120:
            continue

        all_r = list(child.findall(f".//{{{WNS}}}r"))

        year_idx = None
        for i, r in enumerate(all_r):
            t = "".join(x.text or "" for x in r.findall(f"{{{WNS}}}t"))
            if (t or "").strip().isdigit() and len((t or "").strip()) == 4:
                year_idx = i
                break
        if year_idx is None:
            continue

        last_text_idx = 0
        for i in range(year_idx - 1, -1, -1):
            t = "".join(x.text or "" for x in all_r[i].findall(f"{{{WNS}}}t"))
            if (t or "").strip() and len((t or "").strip()) > 1:
                last_text_idx = i
                break

        if year_idx - last_text_idx <= 1:
            continue

        for pr in all_r[last_text_idx + 1 : year_idx]:
            child.remove(pr)

        tab_r = etree.Element(f"{{{WNS}}}r")
        rPr_src = all_r[0].find(f"{{{WNS}}}rPr")
        if rPr_src is not None:
            tab_r.append(deepcopy(rPr_src))
        etree.SubElement(tab_r, f"{{{WNS}}}tab")

        for i, el in enumerate(list(child)):
            t = "".join(x.text or "" for x in el.iter(f"{{{WNS}}}t"))
            if (t or "").strip().isdigit() and len((t or "").strip()) == 4:
                child.insert(i, tab_r)
                break

        pPr = child.find(f"{{{WNS}}}pPr")
        if pPr is None:
            pPr = etree.Element(f"{{{WNS}}}pPr")
            child.insert(0, pPr)
        tabs_el = pPr.find(f"{{{WNS}}}tabs")
        if tabs_el is None:
            tabs_el = etree.SubElement(pPr, f"{{{WNS}}}tabs")
        for t in tabs_el.findall(f"{{{WNS}}}tab"):
            tabs_el.remove(t)
        new_tab = etree.SubElement(tabs_el, f"{{{WNS}}}tab")
        new_tab.set(f"{{{WNS}}}val", "right")
        new_tab.set(f"{{{WNS}}}pos", str(right_pos))

        print(f"  ✏️  date alignment fixed: {pt[:55].strip()}")


# ─── Side project title / date replacement ────────────────────────────────────

def _update_side_project_title(para, old_name: str, new_name: str,
                                new_subtitle, new_date_str: str) -> None:
    """
    Replace project name, optional subtitle, and date in a side project title paragraph.

    Template run structure:
      Slot with subtitle:    [NAME][" "]["- "][SUBTITLE][tabs…]["YEAR"][" - Suffix"]
      Slot without subtitle: [NAME][tabs…]["YEAR"][" - Suffix"]
    """
    m = re.match(r'(\d{4})(.*)', (new_date_str or "").strip())
    new_year       = m.group(1) if m else new_date_str
    raw_suffix     = m.group(2).strip() if m else ""
    new_suffix_run = (f" - {raw_suffix[2:].strip()}" if raw_suffix.startswith("- ")
                      else (f" - {raw_suffix}" if raw_suffix else ""))

    all_r = list(para.findall(f".//{{{WNS}}}r"))

    # 1. Replace project name
    for r in all_r:
        for t in r.findall(f"{{{WNS}}}t"):
            if t.text and old_name in t.text:
                t.text = t.text.replace(old_name, new_name); break

    # 2. Handle "- Subtitle" runs
    sep_run_idx = None
    for i, r in enumerate(all_r):
        for t in r.findall(f"{{{WNS}}}t"):
            if t.text == "- ":
                sep_run_idx = i; break
        if sep_run_idx is not None: break

    if sep_run_idx is not None:
        if new_subtitle:
            if sep_run_idx + 1 < len(all_r):
                for t in all_r[sep_run_idx + 1].findall(f"{{{WNS}}}t"):
                    if t.text: t.text = new_subtitle
        else:
            for offset in [-1, 0, 1]:
                idx = sep_run_idx + offset
                if 0 <= idx < len(all_r):
                    for t in all_r[idx].findall(f"{{{WNS}}}t"):
                        if t.text is not None: t.text = ""

    # 3. Replace year and date suffix
    for r in all_r:
        for t in r.findall(f"{{{WNS}}}t"):
            if t.text and re.fullmatch(r'\d{4}', t.text.strip()):
                t.text = new_year
            elif t.text and re.match(r'\s*-\s*(Present|\d{4})', t.text):
                t.text = new_suffix_run


# ─── Generic section anchor builder ──────────────────────────────────────────

def _build_anchors(master_bank: dict) -> list[tuple[str, str]]:
    """
    Build the list of (anchor_text, section_key) pairs used to map template
    paragraphs/tables to bank section keys. Sorted longest-first so that more
    specific anchors (e.g. "Goldman Sachs Associate") match before shorter
    substrings ("Goldman Sachs" or "Associate" alone).

    Multi-role at the same company:
      When two+ bank sections share the same company (e.g. Analyst then
      Associate at Goldman), the bare company name is ambiguous. We instead
      register a set of composite anchors using every common text ordering a
      template might use ("Goldman Sachs Associate", "Associate, Goldman Sachs",
      "Goldman Sachs - Analyst", …). The role name alone is also registered as
      a lower-priority fallback for templates that list the role without
      repeating the company.

    Explicit `template_anchor`:
      If a section specifies `template_anchor`, it's used verbatim — overrides
      everything else. Useful when a user wants to pin a section to exact
      template text.

    Projects: anchor is `project_name`.
    """
    sections = master_bank.get("sections", {})

    # Pass 1: detect companies that appear in ≥2 sections (multi-role case)
    company_usage: dict[str, int] = {}
    for _key, sec in sections.items():
        if (sec.get("template_anchor") or "").strip():
            continue
        c = (sec.get("company") or "").strip().lower()
        if c:
            company_usage[c] = company_usage.get(c, 0) + 1

    primary: dict[str, str] = {}      # preferred anchors (company + composites)
    fallback: dict[str, str] = {}     # role-alone (lower priority)

    for key, sec in sections.items():
        explicit = (sec.get("template_anchor") or "").strip()
        if explicit:
            primary[explicit] = key
            continue

        company = (sec.get("company") or "").strip()
        project = (sec.get("project_name") or "").strip()
        role    = (sec.get("role") or "").strip()

        if company:
            is_multirole = company_usage.get(company.lower(), 0) >= 2
            if is_multirole and role:
                # Composite anchors — cover common template orderings. Longest-first
                # sort ensures these beat the bare company anchor of a different
                # section if it happens to be registered elsewhere.
                composites = [
                    f"{company} {role}",       f"{role} {company}",
                    f"{company} - {role}",     f"{role} - {company}",
                    f"{company} – {role}",     f"{role} – {company}",   # en-dash
                    f"{company}, {role}",      f"{role}, {company}",
                    f"{company} | {role}",     f"{role} | {company}",
                    f"{company}: {role}",      f"{role}: {company}",
                ]
                for form in composites:
                    primary.setdefault(form, key)
                fallback.setdefault(role, key)
            else:
                # Single-role company (or no role field) — keep the original
                # simple behaviour.
                primary.setdefault(company, key)
                if role:
                    fallback.setdefault(role, key)
        elif project:
            primary.setdefault(project, key)
        else:
            # Custom section with no company / project / role (e.g. Awards,
            # Publications, Languages, Leadership, Volunteer, Research). Use
            # the humanised section_key as an anchor so it can match the
            # template heading text. This is what makes generic custom-section
            # support work.
            humanised = key.replace("_", " ").strip()
            if humanised:
                # Also try a title-cased variant and a plural/singular variant
                fallback.setdefault(humanised, key)
                # Common plural if bank key is singular (award → awards)
                if not humanised.endswith("s"):
                    fallback.setdefault(humanised + "s", key)

    # Merge fallbacks only where they don't clash with a primary anchor
    for role, key in fallback.items():
        if role not in primary:
            primary[role] = key

    return sorted(primary.items(), key=lambda x: len(x[0]), reverse=True)


def _get_skills_header(master_bank: dict) -> str:
    """Return the text in the template that marks the start of the skills section."""
    return master_bank.get("skills_header", "skills").lower()


# ─── DOCX modification ───────────────────────────────────────────────────────

def modify_docx(
    sections:          dict,
    skills_text:       str,
    template_path:     Path,
    output_path:       Path,
    master_bank:       dict | None = None,
    project_overrides: dict | None = None,
) -> None:
    """
    Read base template, inject tailored bullets + skills, write to output_path.

    Args:
        sections:           {section_key: [bullet_str, …]}
        skills_text:        multi-line skills string (Category: skill · skill\nCategory2: …)
        template_path:      path to the user's base .docx template
        output_path:        where to write the tailored .docx
        master_bank:        optional — if provided, maps template text to bank section keys.
                            If omitted, section keys are matched directly against template text
                            (works when section keys match the text in the template).
        project_overrides:  optional {slot_key: {old_name, new_name, new_subtitle, new_date}}
                            — replaces side project titles/dates in the template
    """
    # Read format rules from master_bank (populated from template extraction at generate time)
    fmt                = (master_bank or {}).get("format_rules", {})
    max_bullet         = int(fmt.get("max_bullet_chars", MAX_BULLET))
    skill_font         = str(fmt.get("bullet_font", "Verdana"))
    skill_font_half_pt = int(fmt.get("bullet_font_size_pt", 8)) * 2   # pts → half-pts

    # Guard: warn on oversized bullets
    long_bullets = [
        (sec, b) for sec, bullets in sections.items()
        for b in bullets if len(b) > max_bullet
    ]
    if long_bullets:
        print(f"  ⚠️  Long bullets (>{max_bullet} chars, may overflow to page 2):")
        for sec, b in long_bullets:
            print(f"     [{len(b)} chars] {sec}: {b[:90]}…")

    # Parse template
    with zipfile.ZipFile(template_path) as z:
        xml_content = z.read("word/document.xml")
        all_files   = {i.filename: z.read(i.filename) for i in z.infolist()}

    tree     = etree.fromstring(xml_content)
    body     = tree.find(f"{{{WNS}}}body")
    children = list(body)

    # ── Build anchor lookups ──────────────────────────────────────────────────
    all_anchors = _build_anchors(master_bank) if master_bank else []
    skills_header = _get_skills_header(master_bank) if master_bank else "skills"

    covered_keys = {k for _, k in all_anchors}
    for key in sections:
        if key not in covered_keys:
            all_anchors.append((key, key))
    
    all_anchors.sort(key=lambda x: len(x[0]), reverse=True)

    print(f"  🔍  Starting DOCX modification for {len(sections)} sections...")

    # ── Build working lists ───────────────────────────────────────────────────
    # We only detect and replace bullets that are DIRECT children of <w:body>.
    # Table cells may contain paragraphs too, but replacing those would corrupt
    # the column layout (e.g. overwrite dates or role text).
    # Tables are still scanned for anchor text (company/role names live there).
    body_children = list(body)  # direct children: <w:p>, <w:tbl>, <w:sectPr> …
    body_paras    = [c for c in body_children if c.tag == f"{{{WNS}}}p"]
    para_pos      = {id(p): i for i, p in enumerate(body_paras)}  # element→index

    # ── Map section keys → bullet paragraph indices (in body_paras) ──────────
    bullet_map:     dict[str, list[int]] = {}
    title_para_map: dict[str, int]       = {}
    cur_section:    str | None           = None

    print(f"  📄  Direct body children: {len(body_children)} ({len(body_paras)} paragraphs)")

    for child in body_children:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "tbl":
            # Tables hold company names / role titles — use for anchor matching only.
            # For multi-role-at-same-company templates, we scan the FULL concatenated
            # table text (not just the first cell) so composite anchors like
            # "Goldman Sachs Associate" can match even when the company is in one
            # cell and the role is in the next.
            first_cell = (_first_significant_text(child) or "").strip()
            full_text  = _table_text(child).strip()
            tt = first_cell or full_text[:120]
            if not tt:
                continue
            tt_lower   = tt.lower()
            full_lower = full_text.lower()
            if _is_certifications_heading(tt_lower):
                cur_section = CERTIFICATIONS_KEY
                continue
            if _is_section_reset(tt_lower):
                cur_section = None
                continue
            if skills_header in tt_lower:
                cur_section = None
                continue
            # Match against the full table text so composite anchors work
            # across cells. Anchors are sorted longest-first, so the most
            # specific composite wins before any bare-company match.
            for anchor, key in all_anchors:
                if anchor.lower() in full_lower:
                    cur_section = key
                    break
            continue

        if tag != "p":
            continue  # sectPr, bookmarkStart, etc.

        pt = (_para_text(child) or "").strip()
        if not pt:
            continue

        pt_lower = pt.lower()

        is_bullet = child.find(f".//{{{WNS}}}numPr") is not None
        if not is_bullet:
            if pt.startswith("•") or pt.startswith("–") or pt.startswith("-") or pt.startswith("*"):
                is_bullet = True

        if is_bullet:
            if cur_section:
                bullet_map.setdefault(cur_section, []).append(para_pos[id(child)])
            continue

        if _is_certifications_heading(pt_lower):
            cur_section = CERTIFICATIONS_KEY
            continue
        if _is_section_reset(pt_lower):
            cur_section = None
            continue
        if skills_header in pt_lower:
            cur_section = None
            continue

        for anchor, key in all_anchors:
            if anchor.lower() in pt_lower:
                cur_section = key
                title_para_map[key] = para_pos[id(child)]
                break

    print(f"  🗂️  Sections mapped: {list(bullet_map.keys())}")

    # ── Replace bullets ───────────────────────────────────────────────────────
    replaced = 0
    for section_key, new_bullets in sections.items():
        if section_key not in bullet_map:
            print(f"  ⚠️  Section '{section_key}' not found in template — skipping")
            continue

        existing_idxs = bullet_map[section_key]

        # STRICT CAP: never insert more bullets than the template already has.
        # Extra AI bullets beyond the template's slot count would push content
        # onto page 2 and break the one-page constraint.
        if len(new_bullets) > len(existing_idxs):
            print(f"  ✂️  '{section_key}': AI gave {len(new_bullets)} bullets, "
                  f"template has {len(existing_idxs)} slots — trimming")
            new_bullets = new_bullets[:len(existing_idxs)]

        template_para  = body_paras[existing_idxs[0]]
        new_paras      = [_clone_bullet(template_para, b) for b in new_bullets]
        existing_paras = [body_paras[i] for i in existing_idxs]
        n_e, n_n       = len(existing_paras), len(new_paras)

        # Replace first n_n slots in-place, remove any leftover template slots.
        # All elements are direct body children so body.replace/remove is safe.
        for i, np_ in enumerate(new_paras):
            body.replace(existing_paras[i], np_)
        for ep in existing_paras[n_n:]:
            body.remove(ep)

        # Refresh body_paras after structural changes so later sections still work.
        body_paras = [c for c in body if c.tag == f"{{{WNS}}}p"]
        para_pos   = {id(p): i for i, p in enumerate(body_paras)}

        replaced += 1

    print(f"  📝  Replaced bullets in {replaced}/{len(sections)} sections.")

    # ── Replace side project titles / dates ──────────────────────────────────
    if project_overrides:
        # Rebuild body_paras once more (bullet replacement may have shifted things)
        body_paras = [c for c in body if c.tag == f"{{{WNS}}}p"]
        for slot_key, ov in project_overrides.items():
            if slot_key not in title_para_map:
                print(f"  ⚠️  Title para for '{slot_key}' not found — skipping override")
                continue
            idx = title_para_map[slot_key]
            if idx >= len(body_paras):
                continue
            _update_side_project_title(
                body_paras[idx],
                old_name     = ov["old_name"],
                new_name     = ov["new_name"],
                new_subtitle = ov.get("new_subtitle"),
                new_date_str = ov.get("new_date", "2026 - Present"),
            )
            print(f"  ✏️  {slot_key}: title → '{ov['new_name']}'")

    # ── Fix side project date alignment ──────────────────────────────────────
    _fix_side_project_date_alignment(body)

    # ── Update skills paragraph ───────────────────────────────────────────────
    if skills_text:
        lines = [ln.strip() for ln in skills_text.split("\n") if ln.strip()]
        
        # 1. Locate the skills header and all paragraphs belonging to the skills block
        found_header_idx = -1
        block_para_idxs = []
        body_children = list(body)
        
        for i, child in enumerate(body_children):
            if child.tag != f"{{{WNS}}}p": continue
            pt = (_para_text(child) or "").strip()
            if not pt: continue
            
            if skills_header in pt.lower():
                found_header_idx = i
                # Collect all following paragraphs until the next section reset
                for j in range(i + 1, len(body_children)):
                    next_child = body_children[j]
                    if next_child.tag != f"{{{WNS}}}p": 
                        # If it's a table, it might be the start of a new section (company/project)
                        if next_child.tag == f"{{{WNS}}}tbl":
                            break
                        continue 
                    next_pt = (_para_text(next_child) or "").strip()
                    if not next_pt: continue
                    
                    next_pt_lower = next_pt.lower()
                    if (next_pt_lower in _SECTION_RESETS or 
                        next_pt_lower.rstrip("s") in _SECTION_RESETS):
                        break
                    
                    block_para_idxs.append(j)
                break
        
        # 2. Replace the first slot and remove the rest
        if found_header_idx != -1 and block_para_idxs:
            first_slot_idx = block_para_idxs[0]
            first_slot_para = body_children[first_slot_idx]
            
            # Clear first slot and inject
            for r in first_slot_para.findall(f"{{{WNS}}}r"):
                first_slot_para.remove(r)
            for i, line in enumerate(lines):
                add_br = (i > 0)
                if ":" in line:
                    ci = line.index(":")
                    _add_skill_run(first_slot_para, line[: ci + 1], bold=True,  add_br=add_br,
                                   font_name=skill_font, font_size_half_pt=skill_font_half_pt)
                    _add_skill_run(first_slot_para, line[ci + 1:],  bold=False, add_br=False,
                                   font_name=skill_font, font_size_half_pt=skill_font_half_pt)
                else:
                    _add_skill_run(first_slot_para, line, add_br=add_br,
                                   font_name=skill_font, font_size_half_pt=skill_font_half_pt)
            
            # Remove all other leftover paragraphs in the skills block
            for leftover_idx in block_para_idxs[1:]:
                body.remove(body_children[leftover_idx])
            
            print(f"  ✏️  skills updated (cleared {len(block_para_idxs)} template paragraphs)")

    # ── Write DOCX ────────────────────────────────────────────────────────────
    new_xml = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for fname, data in all_files.items():
            zout.writestr(fname, new_xml if fname == "word/document.xml" else data)

    print(f"  ✅  DOCX → {output_path.name}")


# ─── PDF conversion ──────────────────────────────────────────────────────────

def convert_to_pdf(docx_path: Path) -> Path | None:
    """
    Convert a DOCX to PDF via LibreOffice.
    Returns the PDF path, or None on failure.
    Works both locally and in sandboxed/Docker environments.
    """
    print(f"  🚀  Starting PDF conversion via LibreOffice: {docx_path.name}")
    pdf_path = docx_path.with_suffix(".pdf")
    tmp_dir  = Path(tempfile.mkdtemp())
    tmp_docx = tmp_dir / "cv.docx"
    tmp_pdf  = tmp_dir / "cv.pdf"
    shutil.copy2(docx_path, tmp_docx)

    def _run(exe: str) -> "subprocess.CompletedProcess | None":
        env = os.environ.copy()
        env["SAL_USE_VCLPLUGIN"] = "svp"
        try:
            return subprocess.run(
                [
                    exe, 
                    "-env:UserInstallation=file://" + str(tmp_dir / "profile"),
                    "--headless", 
                    "--convert-to", "pdf", 
                    "--outdir", str(tmp_dir), 
                    str(tmp_docx)
                ],
                env=env, capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            print(f"      ⚠️  Exec failed for {exe}: {e}")
            return None

    result = None
    for exe in ("soffice", "libreoffice", "/usr/bin/libreoffice",
                "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if shutil.which(exe) or Path(exe).exists():
            print(f"  🔍  Trying {exe}...")
            result = _run(exe)
            if result and result.returncode == 0:
                break

    if result and result.returncode == 0 and tmp_pdf.exists():
        shutil.copy2(tmp_pdf, pdf_path)
        print(f"  ✅  PDF conversion successful: {pdf_path.name}")
        return pdf_path

    print(f"  ❌  PDF conversion failed (RC: {result.returncode if result else 'N/A'})")
    if result:
        print(f"      STDOUT: {result.stdout}")
        print(f"      STDERR: {result.stderr}")
    return None


# ─── Text extraction from uploaded files ─────────────────────────────────────

def extract_text_from_docx(docx_path: Path) -> str:
    """Extract plain readable text from a .docx file, preserving paragraph breaks."""
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)
    paragraphs = []
    for para in tree.iter(f"{{{WNS}}}p"):
        text = "".join(t.text or "" for t in para.iter(f"{{{WNS}}}t")).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract plain readable text from a PDF file using pypdf."""
    try:
        import pypdf
    except ImportError:
        raise RuntimeError("pypdf is required for PDF extraction. Run: pip install pypdf")
    with open(pdf_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def extract_text_from_txt(txt_path: Path) -> str:
    """Read plain text file, trying UTF-8 then latin-1 fallback."""
    try:
        return txt_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return txt_path.read_text(encoding="latin-1")


def extract_text(file_path: Path) -> str:
    """
    Extract text from a .docx, .pdf, or .txt file.
    Returns the plain text content ready for AI parsing.
    """
    suffix = file_path.suffix.lower()
    if suffix == ".docx":
        return extract_text_from_docx(file_path)
    elif suffix == ".pdf":
        return extract_text_from_pdf(file_path)
    elif suffix in (".txt", ".md"):
        return extract_text_from_txt(file_path)
    else:
        raise ValueError(f"Unsupported file type: {suffix}. Please upload .docx, .pdf, or .txt")


# ─── 1-page verification ─────────────────────────────────────────────────────

def check_one_page(pdf_path: Path | None) -> bool:
    """Return True if the PDF is exactly 1 page."""
    if not pdf_path or not pdf_path.exists():
        print("  ⚠️  PDF not found — cannot verify page count")
        return False

    # pdfinfo (poppler-utils)
    try:
        out = subprocess.check_output(
            ["pdfinfo", str(pdf_path)], stderr=subprocess.DEVNULL, timeout=10
        ).decode()
        for line in out.splitlines():
            if line.startswith("Pages:"):
                pages = int(line.split(":")[1].strip())
                if pages == 1:
                    print("  ✅  1-page verified")
                    return True
                print(f"\n  ❌  CV IS {pages} PAGES — trim bullets to fit on one page")
                return False
    except Exception:
        pass

    # Fallback: pypdf
    try:
        import pypdf
        with open(pdf_path, "rb") as f:
            pages = len(pypdf.PdfReader(f).pages)
        if pages == 1:
            print("  ✅  1-page verified")
            return True
        print(f"\n  ❌  CV IS {pages} PAGES")
        return False
    except ImportError:
        print("  ⚠️  Install pdfinfo or pypdf to enable page-count check")
        return True


def measure_last_page_fill_ratio(pdf_path: Path | None) -> float:
    """
    Return ratio of last-text baseline y / page height on the LAST page of the PDF.
    0.0 means empty page; 1.0 means text touches the very bottom.

    We use this to add a safety margin on top of the binary 1-page check:
    LibreOffice (server) renders slightly tighter than Word (user's desktop),
    so a PDF that *just* fits on 1 page can still overflow to page 2 when the
    same DOCX is opened in Word. Requiring the last-page fill ratio to be
    <= ~0.96 leaves ~4% of page height as a cushion for Word's looser metrics.
    """
    if not pdf_path or not pdf_path.exists():
        return 0.0
    import tempfile, re as _re
    html_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
            html_path = tmp.name
        # pdftotext -bbox emits an XHTML file with per-word bounding boxes.
        subprocess.check_output(
            ["pdftotext", "-bbox", str(pdf_path), html_path],
            stderr=subprocess.DEVNULL, timeout=15,
        )
        html = Path(html_path).read_text(encoding="utf-8", errors="ignore")
        pages = _re.findall(
            r'<page[^>]*width="([\d.]+)"[^>]*height="([\d.]+)"[^>]*>(.*?)</page>',
            html, _re.DOTALL,
        )
        if not pages:
            return 0.0
        _w, h, body = pages[-1]
        page_h = float(h) or 1.0
        ymaxes = [float(m) for m in _re.findall(r'yMax="([\d.]+)"', body)]
        if not ymaxes:
            return 0.0
        return max(ymaxes) / page_h
    except Exception:
        return 0.0
    finally:
        if html_path:
            try:
                Path(html_path).unlink()
            except Exception:
                pass
