from __future__ import annotations
"""
Smoke test for template_profile.py — builds a minimal in-memory DOCX that
covers the edge cases our classifier must handle, then verifies the produced
TemplateProfile gets the shape right.

Run: python3 test_template_profile.py
"""

import io, sys, tempfile, zipfile
from pathlib import Path

from template_profile import (
    build_profile, summarise_profile,
    ElementKind, GroupKind, SkillLayout,
)


WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ─── XML builders ─────────────────────────────────────────────────────────────

def _p(text: str, *, bold: bool = False, italic: bool = False,
       size_pt: float | None = None, font: str | None = None,
       caps: bool = False, style_id: str | None = None,
       is_bullet: bool = False, runs: list[tuple[str, bool]] | None = None) -> str:
    """
    Build a single <w:p> XML string. If `runs` is provided, emit multi-run;
    otherwise emit a single run using top-level flags.
    """
    pPr = ""
    if style_id or is_bullet:
        parts = []
        if style_id:
            parts.append(f'<w:pStyle w:val="{style_id}"/>')
        if is_bullet:
            parts.append('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        pPr = f"<w:pPr>{''.join(parts)}</w:pPr>"

    def _run(t: str, b: bool, i: bool = False) -> str:
        rPr_parts = []
        if b:      rPr_parts.append("<w:b/><w:bCs/>")
        if i:      rPr_parts.append("<w:i/><w:iCs/>")
        if caps:   rPr_parts.append('<w:caps/>')
        if size_pt:
            rPr_parts.append(f'<w:sz w:val="{int(size_pt*2)}"/><w:szCs w:val="{int(size_pt*2)}"/>')
        if font:
            rPr_parts.append(
                f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>'
            )
        rPr = f"<w:rPr>{''.join(rPr_parts)}</w:rPr>" if rPr_parts else ""
        # XML-escape minimal
        t_esc = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<w:r>{rPr}<w:t xml:space=\"preserve\">{t_esc}</w:t></w:r>"

    if runs is not None:
        rs = "".join(_run(txt, b) for txt, b in runs)
    else:
        rs = _run(text, bold, italic)
    return f"<w:p>{pPr}{rs}</w:p>"


def _build_docx_xml(body_inner: str) -> bytes:
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{WNS}">'
          f'<w:body>{body_inner}'
            '<w:sectPr>'
              '<w:pgSz w:w="12240" w:h="15840"/>'
              '<w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/>'
            '</w:sectPr>'
          '</w:body>'
        '</w:document>'
    )
    return doc.encode("utf-8")


def _write_docx(tmpdir: Path, body_xml: str) -> Path:
    path = tmpdir / "t.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Override PartName="/word/document.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", _build_docx_xml(body_xml))
    return path


# ─── Test fixtures ────────────────────────────────────────────────────────────

def _fixture_full_cv() -> str:
    """
    A realistic CV with: contact block, Experience (2 companies, one with
    description, one without), Side Projects (the reported bug case), Skills
    with line-per-item layout, Awards (simple list), Summary (prose).
    """
    body = []

    # Contact block (pre-first-heading)
    body.append(_p("John Doe",                bold=True, size_pt=16))
    body.append(_p("john@example.com | +1 415 555 0100 | linkedin.com/in/jdoe", size_pt=9))
    body.append(_p(""))  # spacer

    # Experience heading (heading-sized, bold, all caps)
    body.append(_p("EXPERIENCE", bold=True, size_pt=12, caps=True))

    # Entity 1: Fivetran — Senior SDE — 2022-2024 — with description
    body.append(_p("Fivetran — Bangalore, India", bold=True, size_pt=10))
    body.append(_p("Senior SDE | 2022 – 2024",    size_pt=9))
    body.append(_p("Industry leader in data movement and real-time analytics for enterprises.",
                   italic=True, size_pt=9))
    body.append(_p("Product Ownership: Reduced production defects 25%.",
                   is_bullet=True,
                   runs=[("Product Ownership:", True), (" Reduced production defects 25%.", False)]))
    body.append(_p("Data Governance: Improved pipeline reliability across 1,000+ connectors.",
                   is_bullet=True,
                   runs=[("Data Governance:", True), (" Improved pipeline reliability across 1,000+ connectors.", False)]))
    body.append(_p("Analyzed customer telemetry to identify failure patterns.",
                   is_bullet=True))  # plain bullet, NO bold subheading

    # Entity 2: ZS Associates — Sr SDET — 2018-2022 — without description
    body.append(_p("ZS Associates — Pune, India", bold=True, size_pt=10))
    body.append(_p("Sr SDET Team Lead | 2018 – 2022", size_pt=9))
    body.append(_p("Process & Quality Controls: Designed validation frameworks.",
                   is_bullet=True,
                   runs=[("Process & Quality Controls:", True), (" Designed validation frameworks.", False)]))
    body.append(_p("Cross-Functional Alignment: Translated client requirements into platform enhancements.",
                   is_bullet=True,
                   runs=[("Cross-Functional Alignment:", True), (" Translated client requirements into platform enhancements.", False)]))

    # Side Projects heading — the bug case
    body.append(_p("SIDE PROJECTS", bold=True, size_pt=12, caps=True))
    body.append(_p("Cross-border payments intelligence | 2026 – Present", bold=True, size_pt=10))
    body.append(_p("Platform Strategy: Built SEA corridor analytics surfacing routing inefficiencies.",
                   is_bullet=True,
                   runs=[("Platform Strategy:", True), (" Built SEA corridor analytics surfacing routing inefficiencies.", False)]))
    body.append(_p("AI Product Development: Built LLM-based executive decision support system.",
                   is_bullet=True,
                   runs=[("AI Product Development:", True), (" Built LLM-based executive decision support system.", False)]))

    # Skills heading + line-per-item layout
    body.append(_p("SKILLS", bold=True, size_pt=12, caps=True))
    body.append(_p("Strategy, P&L management, M&A, Market Entry"))
    body.append(_p("Python, SQL, Tableau, dbt, Airflow"))
    body.append(_p("Leadership, Cross-functional delivery"))

    # Awards (simple list: heading + bullets only)
    body.append(_p("AWARDS", bold=True, size_pt=12, caps=True))
    body.append(_p("INSEAD Dean's List, 2025", is_bullet=True))
    body.append(_p("Best Paper Award, NeurIPS 2023", is_bullet=True))

    # Summary / Prose
    body.append(_p("SUMMARY", bold=True, size_pt=12, caps=True))
    body.append(_p(
        "INSEAD MBA candidate with 8 years of engineering leadership "
        "at consulting and data-platform firms, pivoting to product "
        "management in fintech."
    ))

    return "".join(body)


# ─── Assertions ───────────────────────────────────────────────────────────────

def _fail(msg: str):
    print(f"  ❌ {msg}")
    return False


def _ok(msg: str):
    print(f"  ✓  {msg}")
    return True


def run() -> int:
    print("=== template_profile smoke test ===")
    with tempfile.TemporaryDirectory() as td:
        path = _write_docx(Path(td), _fixture_full_cv())
        profile = build_profile(path)

    print()
    print(summarise_profile(profile))
    print()

    failures = 0
    # Build a lookup by label
    groups = {g.label: g for g in profile.groups}

    # 1. Contact group exists and contains the email line
    print("1) Contact block")
    contact = next((g for g in profile.groups if g.kind is GroupKind.CONTACT), None)
    if not contact:
        failures += 1; _fail("no CONTACT group built")
    else:
        emails = [
            profile.elements[i].text for i in contact.contact_indices
            if "@" in profile.elements[i].text
        ]
        (_ok if emails else _fail)(f"contact contains email line: {emails}")
        if not emails: failures += 1

    # 2. EXPERIENCE is ENTITY_LIST with 2 items
    print("\n2) Experience: ENTITY_LIST with 2 items")
    exp = groups.get("EXPERIENCE")
    if not exp:
        failures += 1; _fail("no EXPERIENCE group")
    else:
        (_ok if exp.kind is GroupKind.ENTITY_LIST else _fail)(
            f"kind={exp.kind.value}"
        )
        (_ok if len(exp.items) == 2 else _fail)(
            f"items count = {len(exp.items)} (expected 2)"
        )
        if exp.kind is not GroupKind.ENTITY_LIST or len(exp.items) != 2:
            failures += 1

    # 3. First item (Fivetran) has a description AND bullets, with mixed subhead flags
    print("\n3) Fivetran item: description + mixed subhead bullets")
    if exp and exp.items:
        fv = exp.items[0]
        (_ok if len(fv.description_indices) >= 1 else _fail)(
            f"description paragraphs = {len(fv.description_indices)} (expected ≥1)"
        )
        if len(fv.description_indices) < 1: failures += 1

        (_ok if len(fv.bullet_indices) == 3 else _fail)(
            f"bullets = {len(fv.bullet_indices)} (expected 3)"
        )
        if len(fv.bullet_indices) != 3: failures += 1

        # First two have bold subhead, last does not
        expected_subheads = [True, True, False]
        (_ok if fv.bullet_has_subhead == expected_subheads else _fail)(
            f"per-bullet subheads = {fv.bullet_has_subhead} "
            f"(expected {expected_subheads})"
        )
        if fv.bullet_has_subhead != expected_subheads: failures += 1

    # 4. Second item (ZS) has no description, 2 bullets, both subheaded
    print("\n4) ZS item: no description + all bullets subheaded")
    if exp and len(exp.items) >= 2:
        zs = exp.items[1]
        (_ok if len(zs.description_indices) == 0 else _fail)(
            f"description paragraphs = {len(zs.description_indices)} (expected 0)"
        )
        if len(zs.description_indices) != 0: failures += 1

        (_ok if zs.bullet_has_subhead == [True, True] else _fail)(
            f"per-bullet subheads = {zs.bullet_has_subhead}"
        )
        if zs.bullet_has_subhead != [True, True]: failures += 1

    # 5. SIDE PROJECTS is ENTITY_LIST (not orphaned, not SIMPLE_LIST) — the reported bug
    print("\n5) SIDE PROJECTS: ENTITY_LIST with 1 item + 2 bullets (the bug case)")
    sp = groups.get("SIDE PROJECTS")
    if not sp:
        failures += 1; _fail("SIDE PROJECTS missing")
    else:
        (_ok if sp.kind is GroupKind.ENTITY_LIST else _fail)(
            f"kind={sp.kind.value} (expected ENTITY_LIST)"
        )
        if sp.kind is not GroupKind.ENTITY_LIST: failures += 1

        (_ok if len(sp.items) == 1 else _fail)(
            f"items = {len(sp.items)} (expected 1)"
        )
        if len(sp.items) != 1: failures += 1

        if sp.items:
            item = sp.items[0]
            (_ok if len(item.bullet_indices) == 2 else _fail)(
                f"project bullets = {len(item.bullet_indices)} (expected 2)"
            )
            if len(item.bullet_indices) != 2: failures += 1

    # 6. SKILLS detected with LINE_PER_ITEM layout
    print("\n6) SKILLS: detected as SKILLS group with line-per-item layout")
    skills = groups.get("SKILLS")
    if not skills:
        failures += 1; _fail("SKILLS missing")
    else:
        (_ok if skills.kind is GroupKind.SKILLS else _fail)(
            f"kind={skills.kind.value}"
        )
        if skills.kind is not GroupKind.SKILLS: failures += 1

        (_ok if skills.skill_layout is SkillLayout.LINE_PER_ITEM else _fail)(
            f"layout={skills.skill_layout.value}"
        )
        if skills.skill_layout is not SkillLayout.LINE_PER_ITEM: failures += 1

    # 7. AWARDS is SIMPLE_LIST with 2 bullets
    print("\n7) AWARDS: SIMPLE_LIST with 2 bullets")
    awards = groups.get("AWARDS")
    if not awards:
        failures += 1; _fail("AWARDS missing")
    else:
        (_ok if awards.kind is GroupKind.SIMPLE_LIST else _fail)(
            f"kind={awards.kind.value}"
        )
        if awards.kind is not GroupKind.SIMPLE_LIST: failures += 1

        (_ok if len(awards.bullet_indices) == 2 else _fail)(
            f"bullets = {len(awards.bullet_indices)}"
        )
        if len(awards.bullet_indices) != 2: failures += 1

    # 8. SUMMARY is PROSE with 1 content paragraph
    print("\n8) SUMMARY: PROSE group")
    summ = groups.get("SUMMARY")
    if not summ:
        failures += 1; _fail("SUMMARY missing")
    else:
        (_ok if summ.kind is GroupKind.PROSE else _fail)(
            f"kind={summ.kind.value}"
        )
        if summ.kind is not GroupKind.PROSE: failures += 1

        (_ok if len(summ.content_indices) >= 1 else _fail)(
            f"content paras = {len(summ.content_indices)}"
        )
        if len(summ.content_indices) < 1: failures += 1

    # ── Fixture 2: minimalist CV with odd custom sections ───────────────────
    #   • No SKILLS section at all
    #   • No descriptions anywhere
    #   • Custom section "BOARD POSITIONS" (never heard of by classic parsers)
    #   • Custom section "SELECTED MEDIA" (bullets only)
    #   • PATENTS heading (simple list)
    print()
    print("─" * 40)
    print("Fixture 2: unusual sections, no skills, no descriptions")
    print("─" * 40)

    body = []
    body.append(_p("Jane Smith", bold=True, size_pt=16))
    body.append(_p("jane@x.com | linkedin.com/in/j-smith", size_pt=9))
    body.append(_p("EXPERIENCE", bold=True, size_pt=12, caps=True))
    body.append(_p("McKinsey — London",             bold=True, size_pt=10))
    body.append(_p("Associate | 2021 – 2024",       size_pt=9))
    body.append(_p("Led digital transformation engagement for FTSE 100 client.", is_bullet=True))
    body.append(_p("Built 3-year growth strategy adopted by exec committee.",    is_bullet=True))
    body.append(_p("BOARD POSITIONS", bold=True, size_pt=12, caps=True))
    body.append(_p("Oxfam GB — Trustee",             bold=True, size_pt=10))
    body.append(_p("2022 – Present",                 size_pt=9))
    body.append(_p("Serve on the audit and risk committee.", is_bullet=True))
    body.append(_p("SELECTED MEDIA", bold=True, size_pt=12, caps=True))
    body.append(_p("Featured in Financial Times, 2023", is_bullet=True))
    body.append(_p("Guest on Acquired podcast, 2024",   is_bullet=True))
    body.append(_p("PATENTS", bold=True, size_pt=12, caps=True))
    body.append(_p("US 11,123,456 — Method for distributed consensus (2022)", is_bullet=True))

    with tempfile.TemporaryDirectory() as td:
        path = _write_docx(Path(td), "".join(body))
        p2 = build_profile(path)

    print(summarise_profile(p2))
    print()

    g2 = {g.label: g for g in p2.groups}

    # No SKILLS group should exist
    if any(g.kind is GroupKind.SKILLS for g in p2.groups):
        failures += 1; _fail("unexpected SKILLS group in a template that has none")
    else:
        _ok("no SKILLS group — correct (template doesn't have one)")

    # Custom sections classify correctly
    for lbl, expected_kind in [
        ("BOARD POSITIONS", GroupKind.ENTITY_LIST),
        ("SELECTED MEDIA",  GroupKind.SIMPLE_LIST),
        ("PATENTS",         GroupKind.SIMPLE_LIST),
    ]:
        g = g2.get(lbl)
        if not g:
            failures += 1; _fail(f"{lbl} group missing")
        elif g.kind is not expected_kind:
            failures += 1; _fail(f"{lbl}: kind={g.kind.value} (expected {expected_kind.value})")
        else:
            _ok(f"{lbl}: {g.kind.value}")

    # Contact block present and clean
    c2 = next((g for g in p2.groups if g.kind is GroupKind.CONTACT), None)
    if not c2 or not any("@" in p2.elements[i].text for i in c2.contact_indices):
        failures += 1; _fail("contact block missing or broken")
    else:
        _ok("contact block correct")

    # ── Fixture 3: comma-single skills layout ───────────────────────────────
    print()
    print("─" * 40)
    print("Fixture 3: comma-separated single-line SKILLS")
    print("─" * 40)

    body = []
    body.append(_p("Jane Smith", bold=True, size_pt=16))
    body.append(_p("jane@x.com", size_pt=9))
    body.append(_p("EXPERIENCE", bold=True, size_pt=12, caps=True))
    body.append(_p("Acme — London",       bold=True, size_pt=10))
    body.append(_p("Manager | 2020–2024", size_pt=9))
    body.append(_p("Did things.", is_bullet=True))
    body.append(_p("CORE COMPETENCIES", bold=True, size_pt=12, caps=True))
    body.append(_p("Strategy, P&L, M&A, Leadership, Python, SQL, Tableau, Financial modelling"))

    with tempfile.TemporaryDirectory() as td:
        path = _write_docx(Path(td), "".join(body))
        p3 = build_profile(path)

    print(summarise_profile(p3))
    skills3 = next((g for g in p3.groups if g.kind is GroupKind.SKILLS), None)
    if not skills3:
        failures += 1; _fail("SKILLS group missing")
    elif skills3.skill_layout is not SkillLayout.COMMA_SINGLE:
        failures += 1; _fail(
            f"skills layout = {skills3.skill_layout.value} (expected comma_single)"
        )
    else:
        _ok(f"comma-single skills layout detected: {skills3.skill_layout.value}")

    # ── Fixture 4: experience block wrapped in a single <w:tbl> ─────────────
    #   Some templates put the entire entity (company row + bullets) inside
    #   one table for layout purposes. The classifier must flatten the inner
    #   paragraphs so bullets remain BULLETs rather than being collapsed into
    #   a single META.
    print()
    print("─" * 40)
    print("Fixture 4: experience block inside <w:tbl>")
    print("─" * 40)

    def _tbl(inner_paragraphs_xml: str) -> str:
        # Single-cell single-row table containing the inner paragraphs.
        return (
            "<w:tbl>"
              "<w:tblPr/><w:tblGrid><w:gridCol w:w=\"9000\"/></w:tblGrid>"
              "<w:tr><w:tc>"
                "<w:tcPr><w:tcW w:w=\"9000\" w:type=\"dxa\"/></w:tcPr>"
                f"{inner_paragraphs_xml}"
              "</w:tc></w:tr>"
            "</w:tbl>"
        )

    body = []
    body.append(_p("Jane Smith",         bold=True, size_pt=16))
    body.append(_p("jane@x.com",         size_pt=9))
    body.append(_p("EXPERIENCE",         bold=True, size_pt=12, caps=True))
    # Whole Acme block lives inside one table.
    inner = (
        _p("Acme Corp — London", bold=True, size_pt=10)
        + _p("Director | 2020 – 2024", size_pt=9)
        + _p("Strategic Vision: Drove 30% revenue uplift across EMEA.",
             is_bullet=True,
             runs=[("Strategic Vision:", True),
                   (" Drove 30% revenue uplift across EMEA.", False)])
        + _p("Team Leadership: Managed cross-functional team of 12.",
             is_bullet=True,
             runs=[("Team Leadership:", True),
                   (" Managed cross-functional team of 12.", False)])
    )
    body.append(_tbl(inner))
    # Plus a non-table entity to confirm sequencing still works.
    body.append(_p("Beta Inc — Paris", bold=True, size_pt=10))
    body.append(_p("Lead | 2018 – 2020", size_pt=9))
    body.append(_p("Shipped flagship product to 5 markets.", is_bullet=True))

    with tempfile.TemporaryDirectory() as td:
        path = _write_docx(Path(td), "".join(body))
        p4 = build_profile(path)

    print(summarise_profile(p4))
    exp4 = next(
        (g for g in p4.groups
         if g.kind is GroupKind.ENTITY_LIST and g.label == "EXPERIENCE"),
        None,
    )
    if not exp4:
        failures += 1; _fail("EXPERIENCE group missing in fixture 4")
    else:
        (_ok if len(exp4.items) == 2 else _fail)(
            f"items in table-based experience = {len(exp4.items)} (expected 2)"
        )
        if len(exp4.items) != 2:
            failures += 1
        else:
            acme = exp4.items[0]
            (_ok if len(acme.bullet_indices) == 2 else _fail)(
                f"Acme bullets = {len(acme.bullet_indices)} (expected 2)"
            )
            if len(acme.bullet_indices) != 2: failures += 1
            (_ok if acme.bullet_has_subhead == [True, True] else _fail)(
                f"Acme subheads = {acme.bullet_has_subhead}"
            )
            if acme.bullet_has_subhead != [True, True]: failures += 1

            beta = exp4.items[1]
            (_ok if len(beta.bullet_indices) == 1 else _fail)(
                f"Beta (non-table) bullets = {len(beta.bullet_indices)}"
            )
            if len(beta.bullet_indices) != 1: failures += 1

    print()
    print("=" * 40)
    if failures == 0:
        print("ALL CHECKS PASSED ✓")
        return 0
    print(f"{failures} failure(s) ✗")
    return 1


if __name__ == "__main__":
    sys.exit(run())
