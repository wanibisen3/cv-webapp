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
    # Experience headings
    "experience", "work experience", "professional experience",
    "relevant experience", "internship experience",
    # Education headings
    "education", "academic background", "academic qualifications",
    "educational background", "academic history",
    # Project headings
    "side project", "side projects", "projects", "personal projects",
    "academic projects", "selected projects",
    # Skills headings
    "skills", "skills & additional information",
    "skills &amp; additional information",
    "core competencies", "technical skills", "key skills",
    "tools & technologies",
    # Certifications
    "certifications", "certificates", "licenses & certifications",
    "professional certifications",
    # Profile / summary
    "summary", "profile", "professional summary", "executive summary",
    "career summary", "objective", "career objective",
    # Other structural headings
    "achievements", "accomplishments", "awards", "honors & awards",
    "publications", "research", "presentations",
    "languages", "extracurricular", "extracurricular activities",
    "leadership", "leadership experience",
    "volunteer", "volunteering", "community involvement",
    "interests", "hobbies", "references",
})


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

        pt       = _para_text(child).strip()
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
    max_bullet_chars = 215  # safe fallback
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

    return {
        "max_bullet_chars":     max_bullet_chars,
        "max_skill_lines":      max_skill_lines,
        "max_skill_line_chars": max_skill_line_chars,
        "bullet_font":          bullet_font,
        "bullet_font_size_pt":  bullet_font_size_pt,
        "has_bold_subheading":  has_bold_subheading,
        "bullet_format":        "SubHeading: [verb] [action+context], [result]",
    }


# ─── Template structure discovery ────────────────────────────────────────────

def discover_template_sections(template_path: Path) -> dict:
    """"""
    with zipfile.ZipFile(template_path) as z:
        xml = z.read("word/document.xml")
    tree = etree.fromstring(xml)

    slot_counts: dict[str, int] = {}
    cur_title: str | None = None

    for child in tree.iter(f"{{{WNS}}}p"):
        pt = _para_text(child).strip()
        if not pt:
            continue

        pt_lower = pt.lower()
        if pt_lower in _SECTION_RESETS or pt_lower.rstrip("s") in _SECTION_RESETS:
            cur_title = None
            continue

        if child.find(f".//{{{WNS}}}numPr") is not None:
            if cur_title:
                slot_counts[cur_title] = slot_counts.get(cur_title, 0) + 1
        else:
            if len(pt) > 3 and len(pt) <= 80 and not re.fullmatch(r"[\d\s\u2013\-\–\/\.\(\),\|]+", pt):
                cur_title = pt

    return slot_counts


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
        pt = _para_text(child).strip()
        if not pt:
            continue

        if child.find(f".//{{{WNS}}}numPr") is not None:
            if cur_section:
                slot_counts[cur_section] = slot_counts.get(cur_section, 0) + 1
            continue

        pt_lower = pt.lower()
        if pt_lower in _SECTION_RESETS or pt_lower.rstrip("s") in _SECTION_RESETS:
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
        if not re.search(r'\b20\d{2}\b', pt):
            continue
        if len(pt) > 120:
            continue

        all_r = list(child.findall(f".//{{{WNS}}}r"))

        year_idx = None
        for i, r in enumerate(all_r):
            t = "".join(x.text or "" for x in r.findall(f"{{{WNS}}}t"))
            if t.strip().isdigit() and len(t.strip()) == 4:
                year_idx = i
                break
        if year_idx is None:
            continue

        last_text_idx = 0
        for i in range(year_idx - 1, -1, -1):
            t = "".join(x.text or "" for x in all_r[i].findall(f"{{{WNS}}}t"))
            if t.strip() and len(t.strip()) > 1:
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
            if t.strip().isdigit() and len(t.strip()) == 4:
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
    m = re.match(r'(\d{4})(.*)', new_date_str.strip())
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
    anchors_dict: dict[str, str] = {}
    for key, sec in master_bank.get("sections", {}).items():
        explicit = (sec.get("template_anchor") or "").strip()
        if explicit:
            anchors_dict[explicit] = key
            continue

        company = (sec.get("company") or "").strip()
        project = (sec.get("project_name") or "").strip()
        role    = (sec.get("role") or "").strip()

        if company:
            if company not in anchors_dict:
                anchors_dict[company] = key
            if role and role not in anchors_dict:
                anchors_dict[role] = key
        elif project:
            if project not in anchors_dict:
                anchors_dict[project] = key
                
    return sorted(anchors_dict.items(), key=lambda x: len(x[0]), reverse=True)


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
    # ── Map section keys → bullet paragraph indices ───────────────────────────
    bullet_map:     dict[str, list[int]] = {}
    title_para_map: dict[str, int]       = {}
    cur_section:    str | None           = None

    all_paras = list(body.iter(f"{{{WNS}}}p"))
    print(f"  📄  Total paragraphs in template: {len(all_paras)}")

    for idx, child in enumerate(all_paras):
        pt = (_para_text(child) or "").strip()
        if not pt:
            continue

        is_bullet = child.find(f".//{{{WNS}}}numPr") is not None

        if is_bullet:
            if cur_section:
                bullet_map.setdefault(cur_section, []).append(idx)
            continue

        pt_lower = pt.lower()
        if pt_lower in _SECTION_RESETS or pt_lower.rstrip("s") in _SECTION_RESETS:
            cur_section = None
            continue
        if skills_header in pt_lower:
            cur_section = None
            continue

        for anchor, key in all_anchors:
            if anchor.lower() in pt_lower:
                cur_section = key
                title_para_map[key] = idx
                break

    # ── Replace bullets ───────────────────────────────────────────────────────
    for section_key, new_bullets in sections.items():
        if section_key not in bullet_map:
            continue

        existing_idxs  = bullet_map[section_key]
        template_para  = all_paras[existing_idxs[0]]
        new_paras      = [_clone_bullet(template_para, b) for b in new_bullets]
        existing_paras = [all_paras[i] for i in existing_idxs]
        n_e, n_n       = len(existing_paras), len(new_paras)

        parent = existing_paras[0].getparent()
        if parent is None: continue # Safety skip

        if n_n <= n_e:
            for i, np_ in enumerate(new_paras):
                ep = existing_paras[i]
                p = ep.getparent()
                if p is not None:
                    p.replace(ep, np_)
            for ep in existing_paras[n_n:]:
                p = ep.getparent()
                if p is not None:
                    p.remove(ep)
        else:
            # Inline insertion for growing lists
            for i, ep in enumerate(existing_paras):
                p = ep.getparent()
                if p is not None:
                    p.replace(ep, new_paras[i])
            
            anchor_p = new_paras[n_e - 1]
            for xp in new_paras[n_e:]:
                p = anchor_p.getparent()
                if p is not None:
                    idx = p.index(anchor_p)
                    p.insert(idx + 1, xp)
                    anchor_p = xp

    print(f"  📝  Successfully modified {len(sections)} sections in DOCX memory.")

    # ── Replace side project titles / dates ──────────────────────────────────
    if project_overrides:
        for slot_key, ov in project_overrides.items():
            if slot_key not in title_para_map:
                print(f"  ⚠️  Title para for '{slot_key}' not found — skipping override")
                continue
            _update_side_project_title(
                all_paras[title_para_map[slot_key]],
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
        lines    = [ln.strip() for ln in skills_text.split("\n") if ln.strip()]
        in_hdr   = False
        for child in all_paras:
            pt = _para_text(child).strip()
            if skills_header in pt.lower() and pt:
                in_hdr = True
                continue
            if in_hdr and pt:
                # Remove existing runs
                for r in child.findall(f"{{{WNS}}}r"):
                    child.remove(r)
                # Re-inject skill lines with the font extracted from this user's template
                for i, line in enumerate(lines):
                    add_br = i > 0
                    if ":" in line:
                        ci = line.index(":")
                        _add_skill_run(child, line[: ci + 1], bold=True,  add_br=add_br,
                                       font_name=skill_font, font_size_half_pt=skill_font_half_pt)
                        _add_skill_run(child, line[ci + 1:],  bold=False, add_br=False,
                                       font_name=skill_font, font_size_half_pt=skill_font_half_pt)
                    else:
                        _add_skill_run(child, line, add_br=add_br,
                                       font_name=skill_font, font_size_half_pt=skill_font_half_pt)
                print("  ✏️  skills updated")
                break

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
