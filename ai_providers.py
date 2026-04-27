from __future__ import annotations
#!/usr/bin/env python3
"""
ai_providers.py — Multi-provider AI support
=============================================
Supports Anthropic (Claude), OpenAI (GPT-4o / GPT-4), and Google Gemini.
Users supply their own API key, stored encrypted in their profile.

Public API:
    call_ai(jd_text, master_bank, provider, api_key, model=None)  → dict
    parse_cv_to_bank(cv_text, provider, api_key, model=None)       → dict
    PROVIDERS  — dict of {provider_id: {label, models, default_model}}
    encrypt_key(raw_key)  → str
    decrypt_key(enc_key)  → str
"""

import json
import os
import re

from cryptography.fernet import Fernet, InvalidToken

# ─── Supported providers ─────────────────────────────────────────────────────

PROVIDERS = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "models": [
            ("claude-opus-4-6",          "Claude Opus 4.6 (best quality)"),
            ("claude-sonnet-4-6",        "Claude Sonnet 4.6 (fast + smart)"),
            ("claude-haiku-4-5-20251001","Claude Haiku 4.5 (fastest / cheapest)"),
        ],
        "default_model": "claude-sonnet-4-6",
        "key_placeholder": "sk-ant-…",
        "docs_url": "https://console.anthropic.com/settings/keys",
    },
    "openai": {
        "label": "OpenAI (GPT-4)",
        "models": [
            ("gpt-4o",        "GPT-4o (recommended)"),
            ("gpt-4-turbo",   "GPT-4 Turbo"),
            ("gpt-4",         "GPT-4"),
            ("gpt-3.5-turbo", "GPT-3.5 Turbo (budget)"),
        ],
        "default_model": "gpt-4o",
        "key_placeholder": "sk-…",
        "docs_url": "https://platform.openai.com/api-keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "models": [
            ("gemini-1.5-pro",   "Gemini 1.5 Pro (recommended)"),
            ("gemini-1.5-flash", "Gemini 1.5 Flash (fast)"),
            ("gemini-pro",       "Gemini Pro"),
        ],
        "default_model": "gemini-1.5-pro",
        "key_placeholder": "AIza…",
        "docs_url": "https://aistudio.google.com/app/apikey",
    },
}


# ─── CV tailoring prompt ─────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior CV coach for INSEAD MBA / EMBA / MIM / MFin candidates \
applying to elite post-MBA roles (strategy consulting, private equity, \
venture capital, investment banking, corporate strategy / M&A, tech product \
management, brand & growth marketing, operations, corporate development, \
impact / ESG). Your task: rewrite a candidate's bullet bank into a 1-page, \
ATS-optimised CV precisely tailored to a specific job description (JD), \
calibrated to the role archetype and written in the JD's own language.

## Step 1 — classify the JD archetype (silently, then write for it)
Before writing, identify which archetype best fits the JD — use it to pick \
verbs, quantification style, and what context to surface:
- Consulting (MBB / T2):      hypothesis-driven, structured, C-suite exposure, cross-industry, "$X impact identified / realised"
- PE / VC / PIPE:              deal execution, investment thesis, DD, modelling, portfolio value creation, IRR / MOIC / deployed
- Investment Banking / ECM / DCM: M&A / financing execution, pitch books, sector depth, deal size, closed / priced
- Corporate Strategy / Dev:   market entry, transformation, exec alignment, P&L delta, board-level recs
- Tech Product Management:    customer / user outcomes, roadmap trade-offs, data-driven decisions, cross-functional leadership
- Brand & Growth Marketing:   category P&L, launches, CAC / LTV / ROI, audience & funnel, channel mix
- Operations / Supply Chain:  throughput, cost / unit, SLA, yield, scaled from X to Y
- Sustainability / Impact:    quantified impact, stakeholder coalitions, policy / framework shaped
If the JD spans archetypes, lean on the one emphasised by the first 3 \
responsibilities or "you will" bullets.

## Step 2 — STAR bullet format (mandatory for every bullet)
Structure: **SubHeading: [Verb] [action + scale/context], [quantified result]**

- **SubHeading** — 2–4 word theme taken VERBATIM from the JD's own language \
(title case, no trailing period). This is the ATS keyword anchor.
- **Verb** — MBA-grade past-tense action verb. Strong verbs only: Led, Drove, \
Orchestrated, Pioneered, Architected, Structured, Negotiated, Synthesised, \
Scaled, Delivered, Engineered, Launched, Secured, Closed, Mobilised, Advised, \
Spearheaded, Championed, Built, Owned, Re-engineered. Do NOT use weak verbs \
(helped, worked on, assisted, was responsible for, participated, supported).
- **Action + Context** — what was done, and at least one SCALE SIGNAL from the \
candidate's bank: team size, deal / budget / revenue base, # users or clients, \
geographies, timeline, # stakeholders, maturity (Series A, Fortune 500…). \
Scale signals distinguish MBA candidates from junior profiles.
- **Result** — quantified where possible: %, $, €, ₹, ×, #, rank, bps, or time-saved. \
If the bank has no metric, use a directional outcome with a named beneficiary \
or decision ("informing CEO's board paper on India entry", "unlocking $X \
pipeline across 3 BUs"). Never fabricate a number.

## Step 3 — line-fill discipline (NO wasted space)
This is a strict layout rule, not a stylistic preference.

- The user message gives you **CHARS_PER_LINE** (the template's chars-per-line \
budget), **IDEAL_1LINE_MAX** (max chars to stay on one line), and \
**IDEAL_2LINE_RANGE** (the sweet-spot band for 2-line bullets).
- **Every bullet must either:**
  (a) fit on ONE line — length ≤ IDEAL_1LINE_MAX, OR
  (b) fill TWO lines — length within IDEAL_2LINE_RANGE, with the second \
rendered line carrying ≥ 3 words and ≥ 40% of the line width.
- **NEVER leave 1–2 dangling words on a third partial line.** If a draft \
would wrap to 2.1 lines (i.e. 1–2 tiny words on line 3), REPHRASE — tighten \
adjectives, collapse prepositional phrases, or drop a secondary context clause \
— so it lands cleanly within the 2-line budget.
- Hard ceiling: **MAX_BULLET_CHARS** (absolute max; never exceed).
- Prefer the FULL 2-line budget when you have strong facts — empty trailing \
space on a single line is wasted real estate.

## Step 4 — keyword mirroring & ATS
- Every bullet must contain **≥1 JD noun phrase verbatim** (preferably in the SubHeading).
- Use the JD's terminology: if the JD says "commercial due diligence", do not \
write "business analysis". If the JD says "stakeholder alignment", do not \
write "relationship management".
- Avoid thesaurus-swapping the JD's own keywords.

## Step 5 — quantification hierarchy (pick the strongest available from the bank)
1. Magnitude + metric  ($12M revenue, 18% margin expansion, 4× throughput)
2. Scale + scope        (team of 9, 5 SEA markets, 40 stakeholders, Top-3 ranked)
3. Directional + named beneficiary ("accelerating partner's investment committee decision")
Never invent metrics. Never repeat the same metric across multiple bullets in \
one section — variety signals depth.

## Worked STAR examples
  JD (Consulting): "commercial due diligence · TAM sizing · C-suite communication"
  Bank: "worked on DD for PE fund, built market model for fintech target"
  → "Commercial Due Diligence: Led TAM sizing and competitive teardown for \
$120M fintech target, structuring investment case presented to PE MD and resulting in IC approval"

  JD (PM): "0→1 product launch · B2B SaaS · pricing strategy"
  Bank: "launched subscription tier, ran pricing analysis"
  → "0→1 Product Launch: Architected B2B SaaS subscription tier across 3 personas, \
designed value-based pricing ladder, driving 22% ARR uplift in first quarter post-launch"

  JD (Brand): "category P&L · launch · ROI-positive"
  Bank: "led skincare launch in SEA, 8% share"
  → "Category P&L Ownership: Led INR 120Cr skincare launch across 5 SEA markets, \
orchestrating 360° campaign and hitting 8% category share in 6 months — 30% above plan"

## Rules (non-negotiable)
1. Mirror JD language exactly — SubHeadings and key nouns use the JD's own words verbatim.
2. Fill EXACTLY the count in TEMPLATE_SLOTS per section_key — wrong counts break the DOCX.
3. Generate bullets ONLY for section_keys that appear in TEMPLATE_SLOTS — nothing else.
   Templates vary widely: some have Experience + Projects + Leadership; some have
   only Experience + Education + Skills; some have a dedicated Certifications
   block; some have multiple roles at the same employer. Trust TEMPLATE_SLOTS as
   ground truth — if a section key is not there, do NOT invent it. If a section
   key IS there, it MUST be filled (even when the bank has thin material for it —
   in that case, lean on the closest-matching bank facts and directional results).
4. Rewrite every bullet from the bank facts; do not copy verbatim; do not add any fact not in the bank.
5. First bullet of each role = the most JD-relevant / most quantified — it anchors the section.
6. Within a section, vary the SubHeadings — no two bullets share the same SubHeading.
7. **Multiple roles at the same employer**: when TEMPLATE_SLOTS contains two or
   more section_keys tied to the same company (e.g. `goldman_analyst` AND
   `goldman_associate`), treat them as DISTINCT roles. Each role's bullets
   must (a) reflect that role's scope and seniority (the Associate bullets
   should be more strategic / senior than the Analyst bullets), (b) NOT repeat
   or paraphrase bullets from the other role at the same company, and (c)
   surface the promotion arc (increasing scope / team / $ / complexity).
8. **Per-section format hints**: a bank section may carry two extra fields
   that encode template-specific structure — respect them or the rendered
   CV becomes visually inconsistent.
   - `s` (bullet subhead pattern):
       missing/omitted → DEFAULT: every output bullet uses **SubHeading: …** (Step 2).
       `"none"` → the template uses PLAIN bullets in that section. Output
         every bullet as plain prose; no leading "Theme: " and no bold
         subheading. Verb-first sentence form is fine.
       `"110…"` → MIXED pattern, matched position-by-position: the i-th
         output bullet's format mirrors the i-th digit (1 = bold SubHeading,
         0 = plain). If TEMPLATE_SLOTS for this section exceeds the digit
         count, use the MAJORITY value for the trailing slots (ties → SubHeading).
   - `d` (description): a fixed company tagline / italic blurb the template
     prints between the META row and the bullets ("Industry leader in data
     movement and real-time analytics …"). The renderer preserves it
     verbatim. DO NOT consume a bullet slot for it, echo it inside any
     bullet, or mention it in your output. It is informational.
9. Projects — read PROJECT_SLOT_COUNT from the user message:
   - 0 project slots → set project_overrides to null; no project bullets.
   - 1+ project slots → for each slot, pick the single most JD-relevant project from BANK \
sections tagged `p:1`; score by domain match, skills overlap, JD focus areas; vary per JD.
   - If a slot currently shows a different project name than your pick, add it to project_overrides.
10. Skills: order JD-relevant categories first; use JD's exact skill names; always end with "Certifications:" \
   (if none, write "Certifications: None listed"). STRICT: total lines ≤ MAX_SKILL_LINES (one line per \
category, separated by \\n); each line ≤ MAX_SKILL_LINE_CHARS. Collapse/drop least-relevant categories if needed.
11. If TEMPLATE_SLOTS contains the reserved key `__certifications__`, DO NOT \
    write bullets for it — the engine fills that block server-side from the \
    candidate's certifications list. Just omit the key from your `sections` output.
12. Any TEMPLATE_SLOTS key is valid — experience, research, leadership, volunteer, awards, \
publications, languages, presentations, patents, memberships, media, speaking, extracurriculars, \
community involvement, or any custom heading. Fill every slot. For non-experience custom \
sections where the bank's facts are inherently terse (a language line, a publication \
citation, an award + issuer + year), a concise factual format is acceptable in place of \
full STAR. If direct bank material is thin, lean on the closest adjacent content (e.g. \
club presidency facts buried in an education section for a "Leadership" slot).
13. Sections NOT in TEMPLATE_SLOTS (education, contact, other fixed content) must not be touched.
14. NEVER invent facts, numbers, employers, or experiences absent from the candidate's bank.
15. Return ONLY valid JSON — no markdown fences, no prose.

## Character-count discipline
If a draft exceeds MAX_BULLET_CHARS, shorten the Result first, then the Body — \
never sacrifice the SubHeading or Verb. Then apply Step 3: compress to ≤ \
IDEAL_1LINE_MAX, or extend into IDEAL_2LINE_RANGE. A bullet wrapping to 1.x \
lines (widow on line 2) is a defect.

## Output JSON (exact schema)
{"jd_analysis":{"company":"","role":""},"sections":{"<key>":["STAR bullet 1","STAR bullet 2"]},"skills_text":"Category: skill · skill\nCertifications: cert","project_overrides":{"<key>":{"old_name":"","new_name":"","new_subtitle":null,"new_date":"YYYY - Present"}} }
Set project_overrides to null when no title swaps are needed or PROJECT_SLOT_COUNT is 0."""


# ─── CV parsing prompt (bank creation) ───────────────────────────────────────

PARSE_BANK_PROMPT = """\
Parse the CV / experience text into a structured bullet-bank JSON. Extract \
every job, internship, project, leadership role, custom section, and skill — \
include everything; never invent.

## STAR bullet format
"SubHeading: [Strong past-tense verb] [action + context], [result/impact]"
- SubHeading: 2–4 word bold theme
- If no metric in source: use a directional outcome ("enabling X", "driving Y")
- Length: 150–220 chars typical; match the density of the source

Example — Source: "helped with market analysis for consulting client" → \
"Market Analysis: Led market sizing and competitive benchmarking for FMCG client, \
informing go-to-market strategy across 5 SEA markets"

## Output JSON — strict, no markdown, no comments, no extra keys
{
  "sections": {
    "<snake_key>": {
      "company":"","role":"","project_name":"",
      "date":"Mon YYYY \u2013 Mon YYYY",
      "template_anchor":"Company or project_name or exact heading text",
      "bullet_slots":4,
      "bullets":[{"id":"key_1","text":"SubHeading: …","tags":[]}]
    }
  },
  "education":[{"institution":"","degree":"","date":"","gpa":"","notes":""}],
  "certifications":[],
  "skills_text":"Category: skill \u00b7 skill\nCertifications: cert \u00b7 cert",
  "skills_header":"Skills & Additional Information",
  "format_rules":{"max_bullet_chars":215,"bullet_format":"SubHeading: [verb] [action+context], [result]"}
}

## Section classification (applies to any CV)
- company+role: held a position at an org — jobs, internships, research posts, \
  teaching, volunteer roles at an NGO, leadership positions (e.g. club president), \
  part-time, freelance.
- project_name: standalone work — personal/side projects, case comps, academic \
  projects, publications, open-source, independent research, hackathons, tools.
- If unclear: organisation+title → company+role; otherwise → project_name.
- section_key: snake_case (e.g. "hbs_research", "ngo_volunteer", "nlp_proj").
- bullet_slots = number of bullets you produced (1–5; 3–4 typical).
- Extract everything; never pad sparse text with invented content.

## Multiple roles at the SAME company — SEPARATE sections per role
If the candidate progressed through multiple roles at one employer \
(e.g. Goldman: Analyst → Associate → VP), emit one section per role:
  - Same `company`; distinct `role`; distinct `section_key` (e.g. "goldman_analyst", \
    "goldman_associate", "goldman_vp").
  - `template_anchor` = "<Company> <Role>" or "<Company> - <Role>" using the \
    ordering the CV actually displays (so the DOCX engine can match without collision).
  - Bullets specific to each role; no duplicates across roles.

## Custom / non-standard sections — cater to every CV structure
CVs may contain Awards, Honors, Publications, Research, Presentations, Patents, \
Languages, Leadership, Volunteer Work, Community Involvement, Extracurriculars, \
Memberships, Affiliations, Media, Speaking, Teaching, Mentorship, or any other \
heading with its own bullet list. For EACH such block:
  - Emit a section with snake_case key matching the heading (e.g. "awards", \
    "publications", "languages", "leadership", "volunteer", "patents").
  - `template_anchor` = the EXACT heading text as in the CV ("Awards", \
    "Honors & Awards", "Selected Publications", "Leadership Experience", …).
  - Leave company/role/project_name empty — the anchor alone routes bullets.
  - STAR-form where reasonable; never fabricate metrics.

## Dedicated Certifications block
If a "Certifications" heading has bullet items (e.g. "AWS Solutions Architect — 2023"), \
put each into the top-level `certifications: []` (include issuer/year when present). \
Do NOT create a section for certifications. Also append them to the last line of \
`skills_text` ("Certifications: …") for templates that inline certs in Skills.

Return only valid JSON"""


def _build_user_message(jd_text: str, master_bank: dict,
                        template_slots: dict | None = None) -> str:
    """
    Build a compact, template-aware user message for the tailoring call.

    Token-efficiency principles:
    - Compact JSON (no indent) — ~20% smaller than pretty-print
    - Only send experience sections that are actually on this user's template
      (jobs not on the template are dropped; irrelevant for this generation)
    - ALL project sections always sent so AI can pick the best ones for each slot
    - Bullet texts capped at 200 chars — full STAR facts fit; saves tokens on long bullets
    - MAX_BULLET_CHARS injected from format_rules so the AI uses the correct limit for
      this user's specific template (font, size → chars that fit on one line)
    - PROJECT_SLOT_COUNT tells the AI exactly how many project slots to fill (0 = none)
    """
    sections = master_bank.get("sections", {})
    template_keys = set(template_slots.keys()) if template_slots else None

    # Template format rules (extracted from user's DOCX at upload time)
    fmt_rules            = master_bank.get("format_rules", {})
    max_bullet_chars     = int(fmt_rules.get("max_bullet_chars",     215))
    max_skill_lines      = int(fmt_rules.get("max_skill_lines",      5))
    max_skill_line_chars = int(fmt_rules.get("max_skill_line_chars", 120))

    # Line-fill budget (for widow-word avoidance; see SYSTEM_PROMPT Step 3)
    chars_per_line       = int(fmt_rules.get("chars_per_line",       110))
    ideal_1line_max      = int(fmt_rules.get("ideal_1line_max",      max(60, chars_per_line - 6)))
    ideal_2line_min      = int(fmt_rules.get("ideal_2line_min",      chars_per_line + 30))
    ideal_2line_max      = int(fmt_rules.get("ideal_2line_max",      min(max_bullet_chars, int(chars_per_line * 1.88))))

    # Filter out the synthetic certifications key before sending to the AI —
    # app.py populates that block server-side from the candidate's cert list.
    # Keeping it out of the AI's view prevents STAR bullets being generated
    # for what should be verbatim cert entries.
    CERTS_KEY = "__certifications__"
    if template_slots and CERTS_KEY in template_slots:
        template_slots = {k: v for k, v in template_slots.items() if k != CERTS_KEY}

    # Classify template slots into experience vs project
    # A slot is a "project slot" when its key maps to a bank section with project_name
    project_slot_keys: set[str] = set()
    if template_slots:
        for k in template_slots:
            if k in sections and sections[k].get("project_name"):
                project_slot_keys.add(k)

    project_slot_count = len(project_slot_keys)

    # Build filtered section summary:
    #   - All project sections (AI must score ALL to pick best per slot)
    #   - Only experience sections whose key is in the template (others irrelevant)
    # Token-efficiency: drop empty fields; only emit `role` when non-empty;
    # only tag projects (omit the flag for experience — saves ~15 tokens/section).
    def _compress_subhead_pattern(pattern: list) -> str | None:
        """
        Encode bullet_has_subhead [bool] into a compact string the AI can read:
          - None / empty → None (omit field)
          - all True     → None (matches the prompt's default; saves tokens)
          - all False    → "none"
          - mixed        → binary digit string (e.g. "110" for [T, T, F])
        """
        if not pattern:
            return None
        if all(pattern):
            return None
        if not any(pattern):
            return "none"
        return "".join("1" if x else "0" for x in pattern)

    filtered: dict = {}
    for key, sec in sections.items():
        is_project = bool(sec.get("project_name"))
        if is_project or template_keys is None or key in template_keys:
            entry = {
                "n": sec.get("company") or sec.get("project_name") or sec.get("template_anchor") or key,
                "b": [b["text"][:180] for b in sec.get("bullets", [])],
            }
            role = sec.get("role", "")
            if role:
                entry["r"] = role
            if is_project:
                entry["p"] = 1
            # Per-section format hints (Phase 3): only sent when they deviate
            # from the default — keeps payload lean for typical templates.
            sub = _compress_subhead_pattern(sec.get("bullet_has_subhead") or [])
            if sub is not None:
                entry["s"] = sub
            desc = (sec.get("description_text") or "").strip()
            if desc:
                entry["d"] = desc[:240]
            filtered[key] = entry

    bank_payload: dict = {"sections": filtered}
    certs = master_bank.get("certifications", [])
    if certs:
        bank_payload["certs"] = certs[:12]   # skills line + cert slot; 12 is ample

    # ── Template slot info ────────────────────────────────────────────────────
    slots_note = ""
    proj_names_note = ""
    if template_slots:
        # Separate experience and project slots so the AI understands the template structure
        exp_slots  = {k: v for k, v in template_slots.items() if k not in project_slot_keys}
        proj_slots = {k: v for k, v in template_slots.items() if k in project_slot_keys}

        slots_note = (
            f"\nPROJECT_SLOT_COUNT:{project_slot_count}"
            f"\nEXP_SLOTS (fill exactly):{json.dumps(exp_slots)}"
        )
        if proj_slots:
            slots_note += f"\nPROJ_SLOTS (fill exactly; pick most JD-relevant project per slot):{json.dumps(proj_slots)}"
        else:
            slots_note += "\nNo project slots — project_overrides must be null"

        # Current project name in each project slot (for project_overrides.old_name)
        current_projs = {
            k: sections[k]["project_name"]
            for k in project_slot_keys
            if k in sections
        }
        if current_projs:
            proj_names_note = (
                f"\nCUR_PROJ_NAMES (use as old_name in project_overrides):"
                f"{json.dumps(current_projs)}"
            )

    return (
        f"JD:\n{jd_text}\n\n"
        f"BANK (schema: n=name, r=role, b=bullets, p=project-flag, "
        f"s=subhead-pattern (omitted=all bullets get SubHeading; \"none\"=plain prose only; "
        f"\"110\"=binary mask, 1=SubHeading, 0=plain), "
        f"d=description (verbatim company tagline preserved by renderer; do NOT consume a slot for it), "
        f"certs=credentials):"
        f"{json.dumps(bank_payload)}"
        f"{slots_note}"
        f"{proj_names_note}"
        f"\nMAX_BULLET_CHARS:{max_bullet_chars}  (absolute hard cap — never exceed)"
        f"\nCHARS_PER_LINE:{chars_per_line}  (template's chars-per-line budget at its bullet font + size + margins)"
        f"\nIDEAL_1LINE_MAX:{ideal_1line_max}  (a 1-line bullet must be ≤ this many chars)"
        f"\nIDEAL_2LINE_RANGE:[{ideal_2line_min},{ideal_2line_max}]  (a 2-line bullet must fall inside this band; "
        f"line 2 must carry ≥3 words and ≥40% width — NO widow words on a partial 3rd line)"
        f"\nMAX_SKILL_LINES:{max_skill_lines}  (total lines in skills_text, incl. Certifications)"
        f"\nMAX_SKILL_LINE_CHARS:{max_skill_line_chars}  (each skills line must fit)\n"
        "Produce the tailored CV JSON."
    )


def _parse(text: str) -> dict:
    """
    Robust JSON parse for AI responses.

    Handles:
      - Surrounding markdown code fences (```json … ```)
      - Leading/trailing prose ("Here is the JSON:" / "Let me know…")
      - Extra content after the JSON object (JSONDecodeError: Extra data)
      - Stray whitespace / BOM / invisible chars
    """
    text = (text or "").strip()
    # Strip a leading BOM if present
    if text.startswith("\ufeff"):
        text = text[1:]
    # Strip ``` or ```json fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # Fast path: plain json.loads
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Locate the first "{" and use raw_decode to parse one value,
    # discarding any prose before or after the JSON.
    first_brace = text.find("{")
    if first_brace == -1:
        raise ValueError("AI response contains no JSON object")
    candidate = text[first_brace:]
    decoder   = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(candidate)
        return obj
    except json.JSONDecodeError as e:
        # Last resort: surface a clear error with a snippet of the bad payload
        snippet = candidate[:300].replace("\n", " ")
        raise ValueError(
            f"AI returned invalid JSON ({e.msg} at pos {e.pos}): {snippet}…"
        ) from e


# ─── Provider implementations ─────────────────────────────────────────────────

def _call_anthropic(system: str, user_msg: str, api_key: str, model: str,
                    max_tokens: int = 2048) -> dict:
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return _parse(msg.content[0].text)


def _call_openai(system: str, user_msg: str, api_key: str, model: str,
                 max_tokens: int = 2048) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user_msg},
        ],
        response_format={"type": "json_object"},
    )
    return _parse(resp.choices[0].message.content)


def _call_gemini(system: str, user_msg: str, api_key: str, model: str,
                 max_tokens: int = 2048) -> dict:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    m = genai.GenerativeModel(
        model,
        generation_config={
            "response_mime_type": "application/json",
            "max_output_tokens": max_tokens,
        },
    )
    resp = m.generate_content(system + "\n\n" + user_msg)
    return _parse(resp.text)


def _detect_provider(api_key: str) -> str | None:
    """Auto-detect provider from API key prefix."""
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("sk-"):
        return "openai"
    if api_key.startswith("AIza"):
        return "gemini"
    return None


def _resolve_provider_model(provider: str, api_key: str, model: str | None):
    """Resolve provider (auto-correcting from key prefix) and effective model."""
    detected = _detect_provider(api_key)
    if detected and detected != provider:
        provider = detected
    if provider not in PROVIDERS:
        provider = "anthropic"
    return provider, model or PROVIDERS[provider]["default_model"]


# ─── Public: CV tailoring ─────────────────────────────────────────────────────

def call_ai(
    jd_text:        str,
    master_bank:    dict,
    provider:       str,
    api_key:        str,
    model:          str | None  = None,
    template_slots: dict | None = None,
) -> dict:
    """
    Call the AI provider to tailor the CV for a given job description.

    Returns parsed JSON:
    {
        "jd_analysis":       {...},
        "sections":          {section_key: [bullet, ...]},
        "skills_text":       "...",
        "project_overrides": {...} or null,
    }
    """
    if not api_key:
        raise ValueError("API key is empty. Please add your API key in Settings.")

    provider, effective_model = _resolve_provider_model(provider, api_key, model)
    user_msg = _build_user_message(jd_text, master_bank, template_slots)

    # Tailoring response is always < 1 500 tokens; 2048 is safe and cheaper
    if provider == "anthropic":
        return _call_anthropic(SYSTEM_PROMPT, user_msg, api_key, effective_model, max_tokens=2048)
    elif provider == "openai":
        return _call_openai(SYSTEM_PROMPT, user_msg, api_key, effective_model, max_tokens=2048)
    elif provider == "gemini":
        return _call_gemini(SYSTEM_PROMPT, user_msg, api_key, effective_model, max_tokens=2048)
    else:
        raise ValueError(f"Provider '{provider}' not implemented")


# ─── Public: CV parsing → CV bullet bank ───────────────────────────────────────

def parse_cv_to_bank(
    cv_text:  str,
    provider: str,
    api_key:  str,
    model:    str | None = None,
) -> dict:
    """
    Parse raw CV / experience text (any format) into a structured CV bullet bank dict.

    The input can be a plain-text CV, LinkedIn export, a dump of bullet points,
    personal notes, or any unstructured description of someone's background.

    Returns a bank dict ready to be saved via supabase_client.save_master_bank().
    """
    if not api_key:
        raise ValueError("API key is empty. Please add your API key in Settings.")

    provider, effective_model = _resolve_provider_model(provider, api_key, model)
    user_msg = (
        "CV / EXPERIENCE TEXT TO PARSE:\n\n"
        f"{cv_text}\n\n"
        "Extract the full CV bullet bank JSON now."
    )

    # Parsing produces the full bank — can be large; allow 4096
    if provider == "anthropic":
        return _call_anthropic(PARSE_BANK_PROMPT, user_msg, api_key, effective_model, max_tokens=4096)
    elif provider == "openai":
        return _call_openai(PARSE_BANK_PROMPT, user_msg, api_key, effective_model, max_tokens=4096)
    elif provider == "gemini":
        return _call_gemini(PARSE_BANK_PROMPT, user_msg, api_key, effective_model, max_tokens=4096)
    else:
        raise ValueError(f"Provider '{provider}' not implemented")


# ─── High-level bank summary (for Bank page) ─────────────────────────────────

_SUMMARY_SYSTEM = (
    "You are a concise career writer. Given a structured master CV bank (JSON of "
    "experiences, projects, skills, certifications), write a 3-5 sentence third-person "
    "professional summary of the individual. Capture: domain(s), total years/seniority "
    "signal, headline achievements, and technical/functional strengths. No bullet points, "
    "no lists, no headings — prose only. Do NOT invent facts. Return ONLY the summary text "
    "— no JSON, no quotes, no markdown."
)


def generate_bank_summary(
    bank:     dict,
    provider: str,
    api_key:  str,
    model:    str | None = None,
) -> str:
    """Produce a short narrative summary of the individual from their CV bullet bank."""
    if not api_key:
        raise ValueError("API key is empty. Please add your API key in Settings.")

    provider, effective_model = _resolve_provider_model(provider, api_key, model)

    # Compact bank payload — drop empty fields, cap length.
    def _sec(s: dict) -> dict:
        out: dict = {}
        for src, dst in (("company", "c"), ("role", "r"),
                         ("project_name", "p"), ("date", "d")):
            v = s.get(src, "")
            if v:
                out[dst] = v
        out["b"] = [b.get("text", "")[:160] for b in s.get("bullets", [])[:4]]
        return out

    compact: dict = {"sections": [_sec(s) for s in bank.get("sections", {}).values()]}
    certs = bank.get("certifications", [])
    if certs:
        compact["certifications"] = certs
    skills = (bank.get("skills_text", "") or "")[:1000]
    if skills:
        compact["skills_text"] = skills
    user_msg = "CV BULLET BANK JSON:\n" + json.dumps(compact, ensure_ascii=False)

    def _plain(client_call_result: str) -> str:
        # Providers sometimes still wrap in quotes / fences even though we ask for prose.
        t = (client_call_result or "").strip()
        t = re.sub(r"^```(?:text|markdown)?\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
        if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
            t = t[1:-1].strip()
        return t

    # Use a small max_tokens budget — summary is always short
    if provider == "anthropic":
        from anthropic import Anthropic
        resp = Anthropic(api_key=api_key).messages.create(
            model=effective_model, max_tokens=400,
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return _plain(resp.content[0].text)
    elif provider == "openai":
        from openai import OpenAI
        resp = OpenAI(api_key=api_key).chat.completions.create(
            model=effective_model, max_tokens=400,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
        )
        return _plain(resp.choices[0].message.content)
    elif provider == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(
            effective_model,
            generation_config={"max_output_tokens": 400},
        )
        resp = m.generate_content(_SUMMARY_SYSTEM + "\n\n" + user_msg)
        return _plain(resp.text)
    else:
        raise ValueError(f"Provider '{provider}' not implemented")


# ─── API key encryption ──────────────────────────────────────────────────────

def _fernet() -> Fernet:
    """
    Returns a Fernet cipher keyed from ENCRYPT_KEY env var.
    ENCRYPT_KEY must be a 32-byte URL-safe base64 string (generate with Fernet.generate_key()).
    """
    raw = os.environ.get("ENCRYPT_KEY", "").strip().strip('"').strip("'")
    if not raw:
        raise EnvironmentError(
            "ENCRYPT_KEY is not set. Run: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\" and add to .env"
        )
    return Fernet(raw.encode() if isinstance(raw, str) else raw)


def encrypt_key(raw_key: str) -> str:
    """Encrypt an API key for storage in the database."""
    return _fernet().encrypt(raw_key.encode()).decode()


def decrypt_key(enc_key: str) -> str:
    """Decrypt a stored API key."""
    try:
        return _fernet().decrypt(enc_key.encode()).decode()
    except (InvalidToken, Exception) as e:
        raise ValueError(f"Could not decrypt API key: {e}")
