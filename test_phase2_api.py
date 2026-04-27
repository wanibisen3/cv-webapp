from __future__ import annotations
"""
Regression test for Phase 2 — verifies that the public shape of
extract_bank_from_template / discover_template_sections / read_template_slots /
map_template_slots_from_raw is preserved after the TemplateProfile rewrite.

These are the contracts app.py + the rest of cv_engine rely on. If any of
them break, the live /generate path breaks. Run before each Phase-2+ commit.

Run: python3 test_phase2_api.py
"""

import sys, tempfile
from pathlib import Path

# Reuse the in-memory DOCX builders from the structural test
from test_template_profile import _write_docx, _fixture_full_cv

import cv_engine


# ─── Required keys per layer ─────────────────────────────────────────────────

BANK_TOP_KEYS    = {"sections", "certifications", "skills_text", "skills_header"}
SECTION_KEYS_REQ = {
    # Legacy keys — unchanged shape
    "company", "role", "project_name", "date",
    "template_anchor", "bullet_slots", "bullets",
}
SECTION_KEYS_NEW = {"description_text", "bullet_has_subhead"}
BULLET_KEYS      = {"id", "text", "tags"}


def _fail(msg: str, errors: list[str]):
    errors.append(msg)
    print(f"  ❌ {msg}")


def _ok(msg: str):
    print(f"  ✓  {msg}")


def check_bank_shape(bank: dict, errors: list[str]):
    print("• extract_bank_from_template shape")

    # Top-level keys
    missing = BANK_TOP_KEYS - set(bank.keys())
    if missing:
        _fail(f"top-level keys missing: {missing}", errors)
    else:
        _ok(f"top-level keys present: {sorted(BANK_TOP_KEYS)}")

    # Types
    if not isinstance(bank.get("sections"), dict):
        _fail("sections is not a dict", errors)
    if not isinstance(bank.get("certifications"), list):
        _fail("certifications is not a list", errors)
    if not isinstance(bank.get("skills_text"), str):
        _fail("skills_text is not a str", errors)
    if not isinstance(bank.get("skills_header"), str):
        _fail("skills_header is not a str", errors)

    # Each section
    for key, sec in (bank.get("sections") or {}).items():
        if not isinstance(sec, dict):
            _fail(f"section {key!r} is not a dict", errors)
            continue
        miss_req = SECTION_KEYS_REQ - set(sec.keys())
        if miss_req:
            _fail(f"section {key!r} missing legacy keys: {miss_req}", errors)
        miss_new = SECTION_KEYS_NEW - set(sec.keys())
        if miss_new:
            _fail(f"section {key!r} missing new keys: {miss_new}", errors)

        if not isinstance(sec.get("bullet_slots"), int):
            _fail(f"section {key!r}: bullet_slots is not int", errors)
        if not isinstance(sec.get("bullets"), list):
            _fail(f"section {key!r}: bullets is not list", errors)
        if not isinstance(sec.get("template_anchor"), str):
            _fail(f"section {key!r}: template_anchor is not str", errors)
        if not isinstance(sec.get("description_text"), str):
            _fail(f"section {key!r}: description_text is not str", errors)

        # Per-bullet shape
        bullets = sec.get("bullets") or []
        for b in bullets:
            if set(b.keys()) - BULLET_KEYS - {"tags"} - BULLET_KEYS:
                pass
            miss = BULLET_KEYS - set(b.keys())
            if miss:
                _fail(f"bullet in {key!r} missing keys: {miss}", errors)
                break
            if not isinstance(b["text"], str):
                _fail(f"bullet in {key!r}: text is not str", errors)
                break

        # Parallel array invariant: bullet_has_subhead length == bullets length
        sub = sec.get("bullet_has_subhead") or []
        if len(sub) != len(bullets):
            _fail(
                f"section {key!r}: bullet_has_subhead len={len(sub)} != "
                f"bullets len={len(bullets)}",
                errors,
            )

    if not errors:
        _ok(f"{len(bank['sections'])} section(s), all conform to expected shape")


def check_discover_shape(slots: dict, errors: list[str]):
    print("• discover_template_sections shape")
    if not isinstance(slots, dict):
        _fail("not a dict", errors); return
    for k, v in slots.items():
        if not isinstance(k, str):
            _fail(f"non-string key: {k!r}", errors)
        if not isinstance(v, int):
            _fail(f"non-int value: {v!r}", errors)
        if isinstance(v, int) and v < 0:
            _fail(f"negative slot count: {k!r}={v}", errors)
    if errors:
        return
    _ok(f"{len(slots)} entries, all str→int (non-negative)")


def check_read_template_slots_shape(slots: dict, master_bank: dict, errors: list[str]):
    print("• read_template_slots shape")
    if not isinstance(slots, dict):
        _fail("not a dict", errors); return
    bank_section_keys = set((master_bank.get("sections") or {}).keys())
    valid_keys = bank_section_keys | {cv_engine.CERTIFICATIONS_KEY}
    for k, v in slots.items():
        if not isinstance(k, str):
            _fail(f"non-string key: {k!r}", errors)
        if not isinstance(v, int):
            _fail(f"non-int value: {v!r}", errors)
        if isinstance(k, str) and k not in valid_keys:
            _fail(
                f"slot key {k!r} is not a master_bank section_key nor "
                f"CERTIFICATIONS_KEY (orphan mapping)",
                errors,
            )
    if not errors:
        _ok(f"{len(slots)} entries, all map to master_bank section keys")


def check_map_template_slots_from_raw(template_path: Path, errors: list[str]):
    print("• map_template_slots_from_raw matches read_template_slots")
    bank = cv_engine.extract_bank_from_template(template_path)
    raw  = cv_engine.discover_template_sections(template_path)
    via_raw    = cv_engine.map_template_slots_from_raw(raw, bank)
    via_direct = cv_engine.read_template_slots(template_path, bank)
    if via_raw != via_direct:
        _fail(
            f"raw-mapped slots differ from direct: raw={via_raw} direct={via_direct}",
            errors,
        )
    else:
        _ok(f"raw and direct paths agree: {via_raw}")


def check_build_anchors_compat(bank: dict, errors: list[str]):
    """
    The downstream renderer (modify_docx) calls _build_anchors(master_bank)
    to map template paragraphs to section keys. The new bank shape leaves
    `company` / `role` / `project_name` empty and relies on `template_anchor`,
    so _build_anchors must register the anchor verbatim. Verify that.
    """
    print("• _build_anchors registers each section's template_anchor")
    anchors = cv_engine._build_anchors(bank)
    by_anchor = dict(anchors)
    for key, sec in (bank.get("sections") or {}).items():
        anchor = sec.get("template_anchor", "")
        if anchor and by_anchor.get(anchor) != key:
            _fail(
                f"section {key!r} anchor {anchor!r} not registered "
                f"(got {by_anchor.get(anchor)!r})",
                errors,
            )
    if not errors:
        _ok(f"all {len(bank['sections'])} section anchors registered correctly")


def run() -> int:
    errors: list[str] = []
    print("=== Phase 2 public API regression test ===")
    print()

    with tempfile.TemporaryDirectory() as td:
        path = _write_docx(Path(td), _fixture_full_cv())

        bank = cv_engine.extract_bank_from_template(path)
        check_bank_shape(bank, errors)

        print()
        slots = cv_engine.discover_template_sections(path)
        check_discover_shape(slots, errors)

        print()
        mapped = cv_engine.read_template_slots(path, bank)
        check_read_template_slots_shape(mapped, bank, errors)

        print()
        check_map_template_slots_from_raw(path, errors)

        print()
        check_build_anchors_compat(bank, errors)

        # Slot-count consistency: the bullet_slots in each bank section should
        # equal the count returned by read_template_slots for that key.
        print()
        print("• bullet_slots ↔ read_template_slots consistency")
        for key, sec in bank["sections"].items():
            expected = sec["bullet_slots"]
            got      = mapped.get(key, 0)
            if expected != got:
                _fail(
                    f"section {key!r}: bank.bullet_slots={expected} "
                    f"but read_template_slots returned {got}",
                    errors,
                )
        if not any("bullet_slots" in e for e in errors):
            _ok("bank slot counts match template slot counts")

        # No-bank path: read_template_slots(path, None) should match discover_template_sections
        print()
        print("• read_template_slots(path, None) → discover_template_sections")
        no_bank = cv_engine.read_template_slots(path, None)
        if no_bank != slots:
            _fail(f"diverges: {no_bank} vs {slots}", errors)
        else:
            _ok("no-bank path returns raw discovery result")

    print()
    print("=" * 40)
    if not errors:
        print("ALL PUBLIC-API CHECKS PASSED ✓")
        return 0
    print(f"{len(errors)} failure(s) ✗")
    for e in errors:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    sys.exit(run())
