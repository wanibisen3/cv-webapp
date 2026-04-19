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
You are a specialist CV writer for business school and professional candidates. \
Your task: rewrite a candidate's bullet bank into a 1-page ATS-optimised CV \
precisely tailored to a specific job description.

## STAR Bullet Format — mandatory for every bullet
Structure: SubHeading: [Strong past-tense verb] [what you did + scale/context], [result]

Rules:
- SubHeading = 2–4 word bold keyword taken verbatim from the JD's language
- Verb = powerful past-tense action word (Led, Drove, Spearheaded, Delivered, Engineered, Orchestrated…)
- Body = the action + relevant context (team size, methodology, geography — if mentioned in the JD)
- Result = concrete outcome: $value, %, ×, rank, or a directional outcome when no metric exists in the bank
- NEVER invent facts, numbers, or experiences absent from the candidate's bank
- Max MAX_BULLET_CHARS characters per bullet — the exact limit is given in the user message as MAX_BULLET_CHARS; stay STRICTLY under it. Count characters before writing. If a draft exceeds the limit, shorten the Result clause first, then the Body — never sacrifice the SubHeading or Verb.

STAR worked examples (the candidate's background does not matter — apply the same logic to any field):
  JD: "data-driven marketing · campaign optimisation · A/B testing"
  Bank: "ran Google Ads campaigns, improved conversion rate"
  → "Campaign Optimisation: Managed $500K Google Ads budget and ran A/B tests across 12 landing page variants, lifting conversion rate by 18%"

  JD: "statutory audit · financial controls · stakeholder reporting"
  Bank: "audited 4 healthcare clients, found revenue recognition errors"
  → "Financial Controls: Led statutory audits for 4 mid-cap healthcare clients, identifying £1.2M in revenue recognition errors and presenting findings to CFO"

## Rules (non-negotiable)
1. Mirror JD language exactly — SubHeadings and key nouns must use the JD's own words verbatim
2. Fill EXACTLY the count in TEMPLATE_SLOTS per section_key — wrong counts break the DOCX; this is mandatory
3. Generate bullets ONLY for section_keys that appear in TEMPLATE_SLOTS — nothing else
4. Rewrite every bullet from the bank facts; do not copy verbatim; do not add any fact not in the bank
5. Projects — read PROJECT_SLOT_COUNT from the user message:
   - 0 project slots → set project_overrides to null; do not generate any project bullets
   - 1+ project slots → for each project slot, pick the single most JD-relevant project from all \
is_project=true sections; score by domain match, skills overlap, JD focus areas; vary per JD
   - If a slot currently shows a different project name than your pick, add it to project_overrides
6. Skills: put JD-relevant categories first; use JD's exact skill names; always end with "Certifications:" \
   (if none, write "Certifications: None listed"). STRICT: total lines ≤ MAX_SKILL_LINES \
   (one line per category, separated by \\n); each line ≤ MAX_SKILL_LINE_CHARS. Collapse/drop \
   least-relevant categories if needed — do not exceed these limits.
7. Every section type is valid — experience, research, leadership, volunteer, consulting, anything; \
   treat them all equally; pick the most JD-relevant bullets regardless of section type
8. Sections NOT in TEMPLATE_SLOTS (education, contact, other fixed content) must not be touched
9. Return ONLY valid JSON — no markdown fences, no prose

## Output JSON
{"jd_analysis":{"company":"","role":""},"sections":{"<key>":["STAR bullet 1","STAR bullet 2"]},"skills_text":"Category: skill · skill\nCertifications: cert","project_overrides":{"<key>":{"old_name":"","new_name":"","new_subtitle":null,"new_date":"YYYY - Present"}} }
Set project_overrides to null when no title swaps needed or PROJECT_SLOT_COUNT is 0."""


# ─── CV parsing prompt (bank creation) ───────────────────────────────────────

PARSE_BANK_PROMPT = """\
Parse the provided CV / experience text into a structured master bank JSON. \
Extract every job, internship, project, leadership role, and skill — include everything.

## STAR bullet format — apply when structuring bullets
SubHeading: [Strong past-tense verb] [what you did + context], [result/impact]
- SubHeading: 2–4 word bold theme
- Verb: powerful past-tense action word
- NEVER invent metrics or facts not present in the source text
- If no metric: use directional outcome ("enabling X", "driving Y adoption", "reducing Z")
- Keep bullets concise: 150–220 characters is typical; match the density of the original text

Restructure example:
  Source: "helped with market analysis for consulting client"
  → "Market Analysis: Led market sizing and competitive benchmarking for FMCG client, \
informing go-to-market strategy across 5 SEA markets"

## Strict JSON output — no markdown, no comments, no extra keys
{
  "sections": {
    "<snake_key>": {
      "company":          "Name",
      "role":             "Title",
      "project_name":     "Name",
      "date":             "Mon YYYY \u2013 Mon YYYY",
      "template_anchor":  "same as company or project_name",
      "bullet_slots":     4,
      "bullets": [{"id":"key_1","text":"SubHeading: verb + context, result","tags":[]}]
    }
  },
  "education": [
    {"institution":"","degree":"","date":"","gpa":"","notes":""}
  ],
  "certifications": [],
  "skills_text": "Category: skill \u00b7 skill\nCertifications: cert1 \u00b7 cert2",
  "skills_header": "Skills & Additional Information",
  "format_rules": {
    "max_bullet_chars": 215,
    "bullet_format": "SubHeading: [verb] [action+context], [result]"
  }
}

Section classification — use these rules for ANY CV background:
- Use "company" + "role" for anything where the person held a position at an organisation:
    jobs, internships, research positions, teaching/tutoring roles, volunteer roles at an NGO,
    leadership positions (e.g. club president at a university), part-time work, freelance engagements
- Use "project_name" for standalone work not tied to one organisation:
    personal/side projects, case competitions, academic projects, publications, open-source contributions,
    independent research, hackathons, self-built tools, extracurricular activities without a formal role
- When in doubt: if there's an organisation and a title → use company+role; otherwise → use project_name
- section_key: short snake_case, e.g. "hbs_research", "ngo_volunteer", "nlp_proj", "hult_case_comp"
- bullet_slots = number of bullets produced for that section (1–5; 3–4 is typical for most sections)
- Extract EVERYTHING in the text — do not drop anything
- If text is very sparse or vague, still extract what is there; do not pad with invented content
- Return only valid JSON"""


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
    filtered: dict = {}
    for key, sec in sections.items():
        is_project = bool(sec.get("project_name"))
        if is_project or template_keys is None or key in template_keys:
            filtered[key] = {
                "name":       sec.get("company") or sec.get("project_name", key),
                "role":       sec.get("role", ""),
                "is_project": is_project,
                "bullets":    [b["text"][:200] for b in sec.get("bullets", [])],
            }

    bank_payload = {
        "certifications": master_bank.get("certifications", []),
        "sections": filtered,
    }

    # ── Template slot info ────────────────────────────────────────────────────
    slots_note = ""
    proj_names_note = ""
    if template_slots:
        # Separate experience and project slots so the AI understands the template structure
        exp_slots  = {k: v for k, v in template_slots.items() if k not in project_slot_keys}
        proj_slots = {k: v for k, v in template_slots.items() if k in project_slot_keys}

        slots_note = (
            f"\nPROJECT_SLOT_COUNT:{project_slot_count}"
            f"\nTEMPLATE_SLOTS_EXPERIENCE (fill exactly):{json.dumps(exp_slots)}"
        )
        if proj_slots:
            slots_note += f"\nTEMPLATE_SLOTS_PROJECTS (fill exactly; pick most JD-relevant project per slot):{json.dumps(proj_slots)}"
        else:
            slots_note += "\nNo project slots in this template — set project_overrides to null"

        # Current project name in each project slot (needed for project_overrides.old_name)
        current_projs = {
            k: sections[k]["project_name"]
            for k in project_slot_keys
            if k in sections
        }
        if current_projs:
            proj_names_note = (
                f"\nCURRENT_PROJECT_SLOT_NAMES (use as old_name in project_overrides):"
                f"{json.dumps(current_projs)}"
            )

    return (
        f"JD:\n{jd_text}\n\n"
        f"BANK:{json.dumps(bank_payload)}"
        f"{slots_note}"
        f"{proj_names_note}"
        f"\nMAX_BULLET_CHARS:{max_bullet_chars}"
        f"\nMAX_SKILL_LINES:{max_skill_lines}  (total lines in skills_text, incl. Certifications)"
        f"\nMAX_SKILL_LINE_CHARS:{max_skill_line_chars}  (each line must fit)\n"
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


# ─── Public: CV parsing → master bank ───────────────────────────────────────

def parse_cv_to_bank(
    cv_text:  str,
    provider: str,
    api_key:  str,
    model:    str | None = None,
) -> dict:
    """
    Parse raw CV / experience text (any format) into a structured master bank dict.

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
        "Extract the full master bank JSON now."
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
    """Produce a short narrative summary of the individual from their master bank."""
    if not api_key:
        raise ValueError("API key is empty. Please add your API key in Settings.")

    provider, effective_model = _resolve_provider_model(provider, api_key, model)

    # Compact bank payload — only the fields that carry signal
    compact = {
        "certifications": bank.get("certifications", []),
        "skills_text":    bank.get("skills_text", "")[:1200],
        "sections": [
            {
                "company":      s.get("company", ""),
                "role":         s.get("role", ""),
                "project_name": s.get("project_name", ""),
                "date":         s.get("date", ""),
                "bullets":      [b.get("text", "")[:180] for b in s.get("bullets", [])[:4]],
            }
            for s in bank.get("sections", {}).values()
        ],
    }
    user_msg = "MASTER BANK JSON:\n" + json.dumps(compact, ensure_ascii=False)

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
