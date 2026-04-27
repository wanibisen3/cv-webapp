from __future__ import annotations
"""
Phase 4 renderer tests — verify that _clone_bullet respects the per-slot
`force_subhead` flag, and that modify_docx threads the bank's
`bullet_has_subhead` array through to the right slot.

Bug being guarded: a section whose template has mixed bullet formats
([True, True, False]) was previously rendered with the AI's heuristic
output, so a stray "Theme: blah" string from the AI would render bold
on the plain slot and break visual consistency.

Run: python3 test_phase4_renderer.py
"""

import sys, tempfile, zipfile
from copy import deepcopy
from pathlib import Path

from lxml import etree

import cv_engine
from test_template_profile import (
    _write_docx, _fixture_full_cv, WNS,
)


def _has_bold_run(para) -> bool:
    """True if any <w:r> in para has a <w:b/> in its rPr."""
    for r in para.findall(f"{{{WNS}}}r"):
        rPr = r.find(f"{{{WNS}}}rPr")
        if rPr is not None and rPr.find(f"{{{WNS}}}b") is not None:
            return True
    return False


def _para_text(para) -> str:
    return "".join(t.text or "" for t in para.iter(f"{{{WNS}}}t"))


def _make_template_para() -> object:
    """Build a minimal <w:p> usable as a template for _clone_bullet."""
    xml = (
        f'<w:p xmlns:w="{WNS}">'
          '<w:pPr>'
            '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
            '<w:rPr><w:rFonts w:ascii="Verdana" w:hAnsi="Verdana"/><w:sz w:val="16"/></w:rPr>'
          '</w:pPr>'
          '<w:r><w:t>placeholder</w:t></w:r>'
        '</w:p>'
    )
    return etree.fromstring(xml)


def _ok(msg: str): print(f"  ✓  {msg}")
def _fail(msg: str, errs: list): errs.append(msg); print(f"  ❌ {msg}")


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_clone_bullet_force_flags(errs: list):
    print("• _clone_bullet(force_subhead=…)")
    tpl = _make_template_para()

    # force_subhead=True with colon → bold subhead present
    p = cv_engine._clone_bullet(tpl, "Strategic Vision: Drove revenue.", force_subhead=True)
    if not _has_bold_run(p):
        _fail("force=True with colon: no bold run emitted", errs)
    else:
        _ok("force=True: bold run present")

    # force_subhead=False with colon → no bold; prefix stripped
    p = cv_engine._clone_bullet(tpl, "Strategic Vision: Drove revenue.", force_subhead=False)
    if _has_bold_run(p):
        _fail("force=False: bold run leaked through", errs)
    else:
        _ok("force=False with colon: no bold run")
    text = _para_text(p)
    if text.startswith("Strategic Vision"):
        _fail(f"force=False: 'Theme:' prefix not stripped (got {text!r})", errs)
    elif text.strip().startswith("Drove revenue"):
        _ok("force=False: 'Theme:' prefix stripped, body kept")
    else:
        _fail(f"force=False: unexpected body {text!r}", errs)

    # force_subhead=False without colon → plain text passthrough
    p = cv_engine._clone_bullet(tpl, "Did things in the field.", force_subhead=False)
    if _has_bold_run(p):
        _fail("force=False, no colon: bold leaked", errs)
    else:
        _ok("force=False, no colon: rendered plain")

    # force_subhead=False with body-colon (e.g. "ratio: 4:1") → prefix kept
    p = cv_engine._clone_bullet(
        tpl,
        "Built a system at scale handling traffic with a steady-state utilisation ratio of 4:1 across regions.",
        force_subhead=False,
    )
    text = _para_text(p)
    if not text.startswith("Built a system"):
        _fail(f"force=False, body colon: prose corrupted (got {text!r})", errs)
    else:
        _ok("force=False, mid-sentence colon: prose preserved")

    # force_subhead=None (legacy) → bold-on-colon heuristic
    p = cv_engine._clone_bullet(tpl, "Theme: blah blah", force_subhead=None)
    if not _has_bold_run(p):
        _fail("legacy heuristic with colon: no bold run", errs)
    else:
        _ok("force=None with colon: legacy heuristic preserved")

    # force_subhead=True without colon → no bold (do not invent a heading)
    p = cv_engine._clone_bullet(tpl, "Did things plainly", force_subhead=True)
    if _has_bold_run(p):
        _fail("force=True without colon: bold invented from nothing", errs)
    else:
        _ok("force=True, no colon: no bold invented")


def test_modify_docx_threads_pattern(errs: list):
    """
    End-to-end: build the realistic fixture, hand modify_docx an AI-style
    response where the third Fivetran bullet is "Plain Foo: bar" (intentional
    colon), and assert the rendered third bullet has NO bold run because
    bullet_has_subhead[2] = False for that section.
    """
    print("• modify_docx threads bullet_has_subhead per slot")
    with tempfile.TemporaryDirectory() as td:
        path = _write_docx(Path(td), _fixture_full_cv())
        bank = cv_engine.extract_bank_from_template(path)

        # Pick the Fivetran section (mixed pattern [T, T, F]).
        fv_key = next(
            k for k, sec in bank["sections"].items()
            if sec["template_anchor"].startswith("Fivetran")
        )
        if bank["sections"][fv_key]["bullet_has_subhead"] != [True, True, False]:
            _fail(
                f"bank's pattern unexpected: "
                f"{bank['sections'][fv_key]['bullet_has_subhead']}",
                errs,
            )

        sections = {
            fv_key: [
                "Strategic Vision: Bold one.",
                "Operational Excellence: Bold two.",
                "Spurious Theme: Plain three should not be bold.",
            ],
        }
        out = Path(td) / "out.docx"
        cv_engine.modify_docx(
            sections      = sections,
            skills_text   = "",
            template_path = path,
            output_path   = out,
            master_bank   = bank,
        )

        # Read back the rendered DOCX and inspect the 3 Fivetran bullets.
        with zipfile.ZipFile(out) as z:
            xml = z.read("word/document.xml")
        tree = etree.fromstring(xml)
        bullet_paras = [
            p for p in tree.iter(f"{{{WNS}}}p")
            if p.find(f".//{{{WNS}}}numPr") is not None
        ]
        # Find the contiguous run of bullets immediately following the
        # "Fivetran — Bangalore, India" meta paragraph.
        found = []
        for p in bullet_paras:
            txt = _para_text(p)
            if txt.startswith("Strategic Vision") or txt.startswith("Operational Excellence") or "Plain three" in txt:
                found.append(p)

        if len(found) < 3:
            _fail(f"could not locate 3 Fivetran bullets (got {len(found)})", errs)
            return

        b1, b2, b3 = found[0], found[1], found[2]
        if not _has_bold_run(b1):
            _fail("bullet 1 (slot True): missing bold subhead", errs)
        else:
            _ok("bullet 1 (slot True): bold subhead present")
        if not _has_bold_run(b2):
            _fail("bullet 2 (slot True): missing bold subhead", errs)
        else:
            _ok("bullet 2 (slot True): bold subhead present")
        if _has_bold_run(b3):
            _fail("bullet 3 (slot False): bold subhead present (should be plain)", errs)
        else:
            _ok("bullet 3 (slot False): plain rendering, no bold")

        # And the AI's "Spurious Theme:" prefix should have been stripped:
        b3_text = _para_text(b3)
        if "Spurious Theme" in b3_text:
            _fail(f"bullet 3: 'Spurious Theme' prefix not stripped: {b3_text!r}", errs)
        else:
            _ok("bullet 3: 'Spurious Theme' prefix stripped to plain prose")


def _count_paragraphs_with_text(tree, text_substr: str) -> int:
    n = 0
    for p in tree.iter(f"{{{WNS}}}p"):
        txt = "".join(t.text or "" for t in p.iter(f"{{{WNS}}}t"))
        if text_substr in txt:
            n += 1
    return n


def test_skills_layout_branches(errs: list):
    """
    Build three minimal templates whose SKILLS layout differs, run them
    through extract_template_format_rules + modify_docx, and assert the
    rendered DOCX uses the right layout shape.
    """
    print("• modify_docx branches on detected skills_layout")
    from test_template_profile import _p, _build_docx_xml, WNS as _W

    def _build_template(skills_paragraphs_xml: str) -> str:
        body = []
        body.append(_p("Jane Smith",  bold=True, size_pt=16))
        body.append(_p("jane@x.com",  size_pt=9))
        body.append(_p("EXPERIENCE",  bold=True, size_pt=12, caps=True))
        body.append(_p("Acme — London",       bold=True, size_pt=10))
        body.append(_p("Manager | 2020–2024", size_pt=9))
        body.append(_p("Bullet placeholder.", is_bullet=True))
        body.append(_p("SKILLS",      bold=True, size_pt=12, caps=True))
        body.append(skills_paragraphs_xml)
        return "".join(body)

    cases = [
        # (label, template skills XML, AI-supplied skills_text, expected layout,
        #  predicate(tree) -> bool)
        (
            "comma_single",
            _p("Strategy, P&L, M&A, Leadership, Python"),
            "Strategy\nP&L Management\nM&A\nLeadership\nPython",
            "comma_single",
            lambda tree: _count_paragraphs_with_text(tree, "Strategy, P&L Management, M&A") == 1,
        ),
        (
            "line_per_item",
            _p("Strategy, P&L management, M&A") + _p("Python, SQL"),
            "Strategy, P&L management\nPython, SQL\nLeadership",
            "line_per_item",
            # 3 lines collapsed into 1 paragraph via soft-breaks
            lambda tree: _count_paragraphs_with_text(tree, "Strategy") == 1
                         and _count_paragraphs_with_text(tree, "Leadership") == 1,
        ),
        (
            "bulleted",
            _p("Strategy", is_bullet=True) + _p("P&L", is_bullet=True),
            "Strategy\nP&L\nLeadership",
            "bulleted",
            # one paragraph per skill (so 3 distinct paragraphs)
            lambda tree: _count_paragraphs_with_text(tree, "Strategy") == 1
                         and _count_paragraphs_with_text(tree, "P&L") == 1
                         and _count_paragraphs_with_text(tree, "Leadership") == 1,
        ),
    ]

    for label, skills_xml, ai_text, expected_layout, predicate in cases:
        with tempfile.TemporaryDirectory() as td:
            path = _write_docx(Path(td), _build_template(skills_xml))

            fmt = cv_engine.extract_template_format_rules(path)
            if fmt.get("skills_layout") != expected_layout:
                _fail(
                    f"[{label}] format_rules.skills_layout="
                    f"{fmt.get('skills_layout')!r} (expected {expected_layout!r})",
                    errs,
                )
                continue
            else:
                _ok(f"[{label}] detected skills_layout={expected_layout}")

            bank = cv_engine.extract_bank_from_template(path)
            bank["format_rules"] = fmt   # modify_docx reads layout from here

            out = Path(td) / "out.docx"
            cv_engine.modify_docx(
                sections      = {},     # only updating skills
                skills_text   = ai_text,
                template_path = path,
                output_path   = out,
                master_bank   = bank,
            )

            with zipfile.ZipFile(out) as z:
                xml = z.read("word/document.xml")
            tree = etree.fromstring(xml)
            if predicate(tree):
                _ok(f"[{label}] rendered output matches expected layout")
            else:
                _fail(f"[{label}] rendered output does NOT match {expected_layout}", errs)


def run() -> int:
    errs: list = []
    print("=== Phase 4 renderer test ===")
    test_clone_bullet_force_flags(errs)
    print()
    test_modify_docx_threads_pattern(errs)
    print()
    test_skills_layout_branches(errs)
    print()
    print("=" * 40)
    if not errs:
        print("ALL CHECKS PASSED ✓")
        return 0
    print(f"{len(errs)} failure(s) ✗")
    for e in errs:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    sys.exit(run())
