from __future__ import annotations
"""
Phase 5 end-to-end regression test — runs the full pipeline against the
realistic fixture and asserts that all four bugs the user originally reported
are fixed in the rendered DOCX.

Original bug list:
  1. No skills section in the generated CV (template had one).
  2. Company description appeared below a bullet instead of between
     the META row and the first bullet.
  3. Bullets had inconsistent formats (some with bold subhead, some
     without) — didn't follow the per-slot template pattern.
  4. Side Projects bullets got merged into the previous experience
     section instead of forming their own block.

Run: python3 test_phase5_e2e.py
"""

import sys, tempfile, zipfile
from pathlib import Path

from lxml import etree

import cv_engine
from test_template_profile import _write_docx, _fixture_full_cv, WNS


def _ok(msg: str): print(f"  ✓  {msg}")
def _fail(msg: str, errs: list): errs.append(msg); print(f"  ❌ {msg}")


def _para_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{{{WNS}}}t"))


def _has_bold_run(p) -> bool:
    for r in p.findall(f"{{{WNS}}}r"):
        rPr = r.find(f"{{{WNS}}}rPr")
        if rPr is not None and rPr.find(f"{{{WNS}}}b") is not None:
            return True
    return False


def run() -> int:
    errs: list = []
    print("=== Phase 5 end-to-end regression ===")

    with tempfile.TemporaryDirectory() as td:
        path = _write_docx(Path(td), _fixture_full_cv())

        bank = cv_engine.extract_bank_from_template(path)
        fmt  = cv_engine.extract_template_format_rules(path)
        bank["format_rules"] = fmt

        # Identify section keys
        section_keys = list(bank["sections"].keys())
        fv_key = next(k for k in section_keys if k.startswith("fivetran"))
        zs_key = next(k for k in section_keys if k.startswith("zs"))
        sp_key = next(k for k in section_keys if k.startswith("cross_border"))
        aw_key = "awards"

        # Simulated AI output — uses every variation we care about
        sections = {
            fv_key: [
                "Strategic Vision: Drove 4× pipeline reliability across 1,000+ connectors.",
                "Customer Insight: Synthesised telemetry from 250 enterprises into prioritised roadmap.",
                "Re-engineered alerting to cut incident MTTR by 38%.",   # plain — slot 3 = False
            ],
            zs_key: [
                "Quality Frameworks: Architected validation harness adopted across 20+ engagements.",
                "Stakeholder Alignment: Translated client priorities into roadmap delivered on time.",
            ],
            sp_key: [
                "Platform Strategy: Built SEA corridor analytics surfacing routing inefficiencies.",
                "AI Productisation: Shipped LLM-based decision support to 5 exec users.",
            ],
            aw_key: [
                "INSEAD Dean's List, 2025",
                "Best Paper Award, NeurIPS 2023",
            ],
        }

        out = Path(td) / "out.docx"
        cv_engine.modify_docx(
            sections      = sections,
            skills_text   = "Strategy, P&L, M&A, Leadership\nPython, SQL, Tableau\nCertifications: AWS SA",
            template_path = path,
            output_path   = out,
            master_bank   = bank,
        )

        with zipfile.ZipFile(out) as z:
            xml = z.read("word/document.xml")
        tree = etree.fromstring(xml)
        body = tree.find(f"{{{WNS}}}body")
        ordered_paras = [p for p in body if p.tag == f"{{{WNS}}}p"]
        ordered_text  = [_para_text(p) for p in ordered_paras]

    # ── Bug 1: skills section rendered ───────────────────────────────────────
    print("\nBug 1: skills section is rendered")
    skills_present = any(
        "Strategy" in t and ("P&L" in t or "Python" in t)
        for t in ordered_text
    )
    if skills_present:
        _ok("skills_text content is present in rendered body")
    else:
        _fail("skills section missing or empty in rendered body", errs)

    # ── Bug 2: description position ──────────────────────────────────────────
    print("\nBug 2: description sits between META and first Fivetran bullet")
    DESC = "Industry leader in data movement"
    META = "Fivetran — Bangalore, India"
    B1   = "Strategic Vision: Drove 4× pipeline"

    def _idx(needle: str):
        return next((i for i, t in enumerate(ordered_text) if needle in t), None)

    meta_i, desc_i, b1_i = _idx(META), _idx(DESC), _idx(B1)
    if None in (meta_i, desc_i, b1_i):
        _fail(
            f"could not locate META/DESC/B1 (meta={meta_i}, desc={desc_i}, b1={b1_i})",
            errs,
        )
    elif meta_i < desc_i < b1_i:
        _ok(f"order is correct: META({meta_i}) < DESC({desc_i}) < BULLET1({b1_i})")
    else:
        _fail(
            f"order broken: META={meta_i} DESC={desc_i} BULLET1={b1_i} "
            "(expected META < DESC < BULLET1)",
            errs,
        )

    # ── Bug 3: per-slot bullet subhead pattern ───────────────────────────────
    print("\nBug 3: Fivetran bullets render [bold, bold, plain]")
    # Find each bullet by content
    fv_b1 = next(p for p in ordered_paras if "Strategic Vision" in _para_text(p))
    fv_b2 = next(p for p in ordered_paras if "Customer Insight"  in _para_text(p))
    fv_b3 = next(p for p in ordered_paras if "Re-engineered alerting" in _para_text(p))

    if _has_bold_run(fv_b1) and _has_bold_run(fv_b2) and not _has_bold_run(fv_b3):
        _ok("subhead pattern matches template ([T, T, F])")
    else:
        _fail(
            f"subhead pattern wrong: "
            f"b1={_has_bold_run(fv_b1)} b2={_has_bold_run(fv_b2)} b3={_has_bold_run(fv_b3)}",
            errs,
        )

    # ── Bug 4: Side Projects bullets in their own section ────────────────────
    print("\nBug 4: Side Projects bullets stay under SIDE PROJECTS heading")
    sp_heading_i = _idx("SIDE PROJECTS")
    sp_b1_i      = _idx("Platform Strategy")
    skills_h_i   = _idx("SKILLS")

    if None in (sp_heading_i, sp_b1_i, skills_h_i):
        _fail(
            f"missing one of (SIDE PROJECTS heading={sp_heading_i}, "
            f"first project bullet={sp_b1_i}, SKILLS heading={skills_h_i})",
            errs,
        )
    elif sp_heading_i < sp_b1_i < skills_h_i:
        _ok(
            f"Side Projects bullets sit between SIDE PROJECTS ({sp_heading_i}) "
            f"and SKILLS ({skills_h_i})"
        )
    else:
        _fail(
            f"Side Projects bullets in wrong block: "
            f"SP_HEAD={sp_heading_i} SP_B1={sp_b1_i} SKILLS={skills_h_i}",
            errs,
        )

    # Also: ZS bullets must NOT include the side-project bullets
    zs_b1_i = _idx("Quality Frameworks")
    if zs_b1_i is not None and sp_heading_i is not None:
        if zs_b1_i < sp_heading_i:
            _ok("ZS bullets sit before SIDE PROJECTS heading")
        else:
            _fail(
                f"ZS bullets at {zs_b1_i} appear after SIDE PROJECTS heading "
                f"at {sp_heading_i} — block ordering broken",
                errs,
            )

    print()
    print("=" * 40)
    if not errs:
        print("ALL FOUR BUGS RESOLVED ✓")
        return 0
    print(f"{len(errs)} regression(s) remaining ✗")
    for e in errs:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    sys.exit(run())
