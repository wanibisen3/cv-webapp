#!/usr/bin/env python3
"""
CV Tailor Web App  (v3)
========================
Self-contained Flask application.

Features:
  • Sign up / Sign in (Supabase Auth)
  • Build master bank from: uploaded CV file (DOCX/PDF/TXT), pasted text, or manual entry
  • Import more experience into an existing bank at any time
  • Full bank editor: add / edit / delete bullets, add new sections, edit skills & certs
  • Upload CV template (.docx)
  • AI Settings: choose provider (Anthropic / OpenAI / Gemini) + enter own API key
  • Generate tailored CV from JD → review & edit bullets → download .docx AND .pdf

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # fill in values
    python app.py

Deploy (Docker recommended — includes LibreOffice for PDF):
    docker-compose up --build
"""

import json
import os
import re
import tempfile
import uuid as _uuid
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, flash, jsonify, redirect, render_template_string,
    request, send_file, session, url_for,
)

import supabase_client as sb
from ai_providers import PROVIDERS, call_ai, decrypt_key, encrypt_key, generate_bank_summary, parse_cv_to_bank
from concurrent.futures import ThreadPoolExecutor

from cv_engine import (
    check_one_page, convert_to_pdf, discover_template_sections,
    extract_template_format_rules, extract_text, map_template_slots_from_raw,
    modify_docx, read_template_slots,
)

# ─── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24))

# In-memory session proxy (now backed by Supabase for multi-worker safety)
class CVStore:
    def __init__(self, mode="pending"):
        self.mode = mode

    def __setitem__(self, token, data):
        user_id = session.get("user_id")
        if user_id:
            # Create a stringified version for the DB
            db_data = {}
            for k, v in data.items():
                db_data[k] = str(v) if isinstance(v, Path) else v
            db_data["_store_mode"] = self.mode
            sb.save_cv_session(user_id, token, db_data)

    def get(self, token, default=None):
        client = sb.get_client()
        try:
            res = client.table("cv_sessions").select("*").eq("token", token).execute()
            if not res.data: return default
            
            row = res.data[0]
            data = row["data"]
            uid = row["user_id"]
            
            if data.get("_store_mode") != self.mode:
                return default

            # Restore Path objects
            for key in ["template_path", "docx", "pdf"]:
                if key in data and data[key]:
                    p = Path(data[key])
                    if not p.exists():
                        try:
                            if key == "template_path":
                                sb.download_cv_template(uid, p)
                            elif key in ["docx", "pdf"]:
                                sb.download_generated_cv(uid, token, p.name, p)
                        except Exception as e:
                            print(f"  ⚠️  Restore failed for {key}: {e}")
                    data[key] = p
            return data
        except Exception as e:
            print(f"  ❌ CVStore access error: {e}")
            return default

    def pop(self, token, default=None):
        val = self.get(token, default)
        sb.delete_cv_session(token)
        return val

_generated = CVStore("generated")


# ─── Skills-text normaliser (generic, template-driven) ───────────────────────
def _cap_skills_text(skills_text: str, max_lines: int, max_line_chars: int) -> str:
    """
    Enforce the template's skill-block constraints on arbitrary skills text.

    Rules:
      • Each line must fit in `max_line_chars` (truncate at word boundary).
      • Total line count must not exceed `max_lines`. If it does:
          - Always keep the "Certifications:" line (last), if present.
          - Drop the shortest non-certifications lines first (least value per line).
    Works for any template — no hardcoded categories.
    """
    if not skills_text:
        return skills_text

    lines = [ln.strip() for ln in skills_text.splitlines() if ln.strip()]

    # ── Per-line length cap ──
    capped_lines = []
    for ln in lines:
        if len(ln) <= max_line_chars:
            capped_lines.append(ln)
        else:
            # Preserve "Category:" prefix, trim the trailing values at a separator.
            head, sep, tail = ln.partition(":")
            if sep:
                room = max(0, max_line_chars - len(head) - 2)
                # Split tail by common separators and add items until we run out.
                parts = re.split(r"\s*[·•|]\s*|\s*,\s*", tail)
                acc   = ""
                for p in parts:
                    add = (" · " if acc else "") + p.strip()
                    if len(acc) + len(add) > room:
                        break
                    acc += add
                capped_lines.append(f"{head}: {acc}".strip() if acc else head + ":")
            else:
                # No colon — just hard-truncate at word boundary.
                capped_lines.append(ln[:max_line_chars].rsplit(" ", 1)[0].rstrip(",;: "))
        if len(capped_lines[-1]) != len(ln):
            print(f"  ✂️  Skill line capped ({len(ln)}→{len(capped_lines[-1])} chars)")

    # ── Line count cap ──
    if len(capped_lines) <= max_lines:
        return "\n".join(capped_lines)

    # Always keep any line starting with "Certifications" (case-insensitive).
    cert_idxs = [i for i, ln in enumerate(capped_lines) if ln.lower().startswith("certifications")]
    keep_mask = [False] * len(capped_lines)
    for i in cert_idxs:
        keep_mask[i] = True

    # Score remaining lines by length (longer = richer = keep). Drop shortest first.
    remaining_budget = max_lines - sum(keep_mask)
    scored = sorted(
        [(i, len(ln)) for i, ln in enumerate(capped_lines) if not keep_mask[i]],
        key=lambda t: -t[1],
    )
    for i, _len in scored[:remaining_budget]:
        keep_mask[i] = True

    pruned = [ln for i, ln in enumerate(capped_lines) if keep_mask[i]]
    dropped = len(capped_lines) - len(pruned)
    if dropped:
        print(f"  ✂️  Skill lines pruned ({len(capped_lines)}→{len(pruned)}; -{dropped})")
    return "\n".join(pruned)



# ─── Auth decorator ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in first.", "warning")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
#  HTML TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────

_BASE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}My INSEAD CV{% endblock %}</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800;900&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    :root {
      --deep: #060918;
      --navy: #0f172a;
      --navy-80: #1e293b;
      --indigo: #4f46e5;
      --indigo-l: #6366f1;
      --violet: #7c3aed;
      --violet-l: #a78bfa;
      --amber: #d97706;
      --amber-l: #f59e0b;
      --gold: #d97706;
      --gold-l: #f59e0b;
      --emerald: #059669;
      --surface: #ffffff;
      --bg: #f8fafc;
      --border: rgba(15,23,42,0.08);
      --text: #0f172a;
      --muted: #64748b;
      --r20: 20px; --r16: 16px; --r12: 12px; --r10: 10px;
      --shadow: 0 1px 3px rgba(15,23,42,0.04), 0 4px 16px rgba(15,23,42,0.06);
      --shadow-md: 0 4px 6px rgba(15,23,42,0.04), 0 12px 32px rgba(15,23,42,0.10);
      --shadow-lg: 0 8px 16px rgba(15,23,42,0.06), 0 24px 56px rgba(15,23,42,0.16);
    }
    *, *::before, *::after { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      background: var(--bg);
      font-family: 'Inter', system-ui, sans-serif;
      font-size: .92rem; color: var(--text);
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    /* ── Navbar ── */
    .cc-nav {
      position: sticky; top: 0; z-index: 1000; height: 60px;
      background: rgba(6,9,24,0.96);
      backdrop-filter: blur(20px) saturate(180%);
      -webkit-backdrop-filter: blur(20px) saturate(180%);
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 2rem;
    }
    .cc-nav::after {
      content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 2px;
      background: linear-gradient(90deg, var(--indigo), var(--violet), var(--amber));
      opacity: 0.55;
    }
    .cc-brand {
      display: flex; align-items: center; gap: .65rem; text-decoration: none;
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700;
      font-size: 1.05rem; color: #fff; letter-spacing: -.3px;
    }
    .cc-brand-icon {
      width: 32px; height: 32px;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      border-radius: 9px; display: flex; align-items: center; justify-content: center;
      font-size: .9rem; color: #fff; box-shadow: 0 4px 14px rgba(79,70,229,0.5);
    }
    .cc-brand-cv { color: var(--amber-l); }
    .cc-nav-links { display: flex; align-items: center; gap: .4rem; }
    .cc-nav-pill {
      padding: .35rem .9rem; border-radius: 22px; font-size: .8rem; font-weight: 500;
      color: rgba(255,255,255,0.65); text-decoration: none;
      transition: background .18s, color .18s; border: 1px solid transparent;
      display: flex; align-items: center; gap: .35rem;
    }
    .cc-nav-pill:hover { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.95); }
    .cc-nav-pill.outline { border-color: rgba(255,255,255,0.15); color: rgba(255,255,255,0.7); }
    .cc-nav-pill.outline:hover { border-color: rgba(255,255,255,0.35); color: #fff; background: rgba(255,255,255,0.05); }
    .cc-email-badge {
      font-size: .73rem; color: rgba(255,255,255,0.35);
      padding: .28rem .7rem; background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.07); border-radius: 14px;
    }

    /* ── Loading overlay ── */
    .ov-card {
      background: rgba(255,255,255,0.98); border-radius: var(--r20);
      padding: 2.75rem 3.5rem; text-align: center;
      box-shadow: var(--shadow-lg); min-width: 300px;
    }
    .cc-dual-ring {
      display: inline-block; width: 52px; height: 52px;
      position: relative; margin: 0 auto 1.25rem;
    }
    .cc-dual-ring::before, .cc-dual-ring::after {
      content: ''; position: absolute; border-radius: 50%;
      border: 3.5px solid transparent;
      animation: dualSpin 1.2s cubic-bezier(.5,0,.5,1) infinite;
    }
    .cc-dual-ring::before {
      inset: 0; border-top-color: var(--indigo); border-right-color: var(--indigo);
    }
    .cc-dual-ring::after {
      inset: 8px; border-bottom-color: var(--violet); border-left-color: var(--violet);
      animation-direction: reverse; animation-duration: .9s;
    }
    @keyframes dualSpin { to { transform: rotate(360deg); } }
    .spinner-text {
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700;
      font-size: 1.05rem; color: var(--navy);
    }
    .spinner-sub { color: var(--muted); font-size: .82rem; margin-top: .4rem; }

    /* ── Alerts ── */
    .alert {
      border-radius: var(--r12); border: none; border-left: 4px solid;
      font-size: .875rem; font-weight: 500; padding: .85rem 1.1rem;
    }
    .alert-success  { border-color: var(--emerald); background: #f0fdf4; color: #14532d; }
    .alert-danger   { border-color: #dc2626;        background: #fef2f2; color: #7f1d1d; }
    .alert-warning  { border-color: var(--amber);   background: #fffbeb; color: #78350f; }
    .alert-info     { border-color: var(--indigo);  background: #eef2ff; color: #312e81; }

    /* ── Cards ── */
    .card-base { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r20); box-shadow: var(--shadow); }
    .card-hover { transition: transform .22s ease, box-shadow .22s ease; }
    .card-hover:hover { transform: translateY(-4px); box-shadow: var(--shadow-md); }
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--r20) !important; box-shadow: var(--shadow);
    }
    .card-header {
      border-radius: var(--r20) var(--r20) 0 0 !important; font-weight: 600;
      background: var(--bg); border-bottom: 1px solid var(--border); padding: .9rem 1.35rem;
    }

    /* ── Buttons ── */
    .btn-indig {
      background: linear-gradient(135deg, var(--indigo), var(--indigo-l));
      color: #fff; border: none; border-radius: var(--r10); font-weight: 600;
      padding: .6rem 1.4rem; transition: opacity .18s, transform .14s, box-shadow .18s;
      box-shadow: 0 4px 14px rgba(79,70,229,0.35); cursor: pointer;
    }
    .btn-indig:hover { opacity: .88; transform: translateY(-1px); color: #fff; box-shadow: 0 8px 24px rgba(79,70,229,0.42); }
    .btn-indig:active { transform: translateY(0); }
    .btn-ghost {
      background: transparent; border: 1.5px solid var(--border); color: var(--text);
      border-radius: var(--r10); font-weight: 500; padding: .5rem 1.2rem;
      transition: background .18s, border-color .18s, color .18s; cursor: pointer;
    }
    .btn-ghost:hover { background: var(--bg); border-color: rgba(15,23,42,0.18); color: var(--navy); }
    .btn-success-custom {
      background: linear-gradient(135deg, #059669, #10b981); color: #fff; border: none;
      border-radius: var(--r10); font-weight: 600; padding: .6rem 1.4rem;
      transition: opacity .18s, transform .14s; box-shadow: 0 4px 14px rgba(5,150,105,0.3); cursor: pointer;
    }
    .btn-success-custom:hover { opacity: .88; transform: translateY(-1px); color: #fff; }
    .btn-gold {
      background: linear-gradient(135deg, var(--amber), var(--amber-l)); color: #fff;
      border: none; border-radius: var(--r10); font-weight: 700; padding: .6rem 1.4rem;
      transition: opacity .18s, transform .14s; box-shadow: 0 4px 14px rgba(217,119,6,0.35); cursor: pointer;
    }
    .btn-gold:hover { opacity: .88; transform: translateY(-1px); color: #fff; }
    .btn-xs { padding: .22rem .6rem; font-size: .73rem; font-weight: 500; border-radius: 7px; }

    /* ── Form controls ── */
    .fc {
      width: 100%; padding: .65rem 1rem; background: #fff;
      border: 1.5px solid var(--border); border-radius: var(--r10);
      font-family: 'Inter', sans-serif; font-size: .875rem; color: var(--text);
      transition: border-color .18s, box-shadow .18s; outline: none;
    }
    .fc:focus { border-color: var(--indigo); box-shadow: 0 0 0 3px rgba(79,70,229,0.1); }
    .fl {
      display: block; font-size: .75rem; font-weight: 700; color: var(--navy);
      margin-bottom: .4rem; text-transform: uppercase; letter-spacing: .05em;
    }
    .form-control, .form-select {
      font-family: 'Inter', sans-serif; background: #fff;
      border: 1.5px solid var(--border); border-radius: var(--r10);
      font-size: .875rem; color: var(--text); transition: border-color .18s, box-shadow .18s;
    }
    .form-control:focus, .form-select:focus {
      border-color: var(--indigo); box-shadow: 0 0 0 3px rgba(79,70,229,0.1); background: #fff;
    }
    textarea.form-control { font-family: 'Inter', sans-serif; font-size: .84rem; line-height: 1.65; }

    /* ── Status dots ── */
    .sdot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; flex-shrink: 0; }
    .sdot-ok { background: var(--emerald); box-shadow: 0 0 0 3px rgba(5,150,105,0.2); }
    .sdot-no { background: #cbd5e1; }
    .status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:5px; }
    .dot-ok { background: var(--emerald); box-shadow: 0 0 0 3px rgba(5,150,105,0.2); }
    .dot-no { background: #cbd5e1; }

    /* ── Step badge ── */
    .step-badge {
      display: inline-flex; align-items: center; justify-content: center;
      width: 28px; height: 28px; border-radius: 50%; font-size: .72rem; font-weight: 700;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      color: #fff; flex-shrink: 0; box-shadow: 0 2px 10px rgba(79,70,229,0.35);
    }

    /* ── Import zone ── */
    .import-zone {
      border: 2px dashed rgba(79,70,229,0.2); border-radius: var(--r16);
      padding: 2.75rem 2rem; text-align: center; cursor: pointer;
      transition: border-color .2s, background .2s; background: #fafbff;
    }
    .import-zone:hover, .import-zone.drag-over { border-color: var(--indigo); background: #eef2ff; }

    /* ── STAR guide ── */
    .star-guide {
      background: #eef2ff; border: 1px solid rgba(79,70,229,0.15);
      border-radius: var(--r10); padding: .8rem 1.1rem; font-size: .8rem;
      color: #312e81; line-height: 1.6;
    }
    .star-guide code {
      background: rgba(79,70,229,0.12); border-radius: 4px;
      padding: .1rem .35rem; font-size: .77rem; color: var(--indigo);
    }

    /* ── Bullet row ── */
    .bullet-row {
      border-left: 3px solid var(--border); padding-left: .85rem; margin-bottom: .45rem;
      border-radius: 0 8px 8px 0; transition: border-color .15s, background .15s;
    }
    .bullet-row:hover { border-left-color: var(--indigo); background: #f8f9ff; }

    /* ── Review bullet ── */
    .review-bullet {
      background: #fff; border: 1.5px solid var(--border); border-radius: var(--r10);
      padding: .65rem .95rem; margin-bottom: .55rem; transition: border-color .18s, box-shadow .18s;
    }
    .review-bullet:focus-within { border-color: var(--indigo); box-shadow: 0 0 0 3px rgba(79,70,229,0.08); }

    /* ── Badge pill ── */
    .badge-pill { padding: .3rem .75rem; border-radius: 20px; font-size: .73rem; font-weight: 600; letter-spacing: .02em; }
    .badge-indigo  { background: #eef2ff; color: var(--indigo); border: 1px solid rgba(79,70,229,0.2); }
    .badge-navy    { background: rgba(15,23,42,0.06); color: var(--navy); border: 1px solid var(--border); }
    .badge-gold    { background: #fffbeb; color: var(--amber); border: 1px solid rgba(217,119,6,0.2); }
    .badge-emerald { background: #f0fdf4; color: var(--emerald); border: 1px solid rgba(5,150,105,0.2); }

    /* ── Tabs ── */
    .cc-tabs { display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 0; }
    .cc-tab-btn {
      padding: .75rem 1.4rem; font-size: .84rem; font-weight: 600; color: var(--muted);
      background: transparent; border: none; border-bottom: 2px solid transparent;
      margin-bottom: -2px; cursor: pointer; transition: color .18s, border-color .18s;
      display: flex; align-items: center; gap: .4rem;
    }
    .cc-tab-btn:hover { color: var(--navy); }
    .cc-tab-btn.active { color: var(--indigo); border-bottom-color: var(--indigo); }
    .tab-pane { padding-top: 1.25rem; }

    /* ── Section card borders ── */
    .section-card { margin-bottom: 1rem; }
    .section-card.type-company { border-left: 4px solid var(--indigo) !important; }
    .section-card.type-project { border-left: 4px solid var(--emerald) !important; }

    /* ── Shimmer ── */
    @keyframes shimmer {
      0%   { background-position: -200% center; }
      100% { background-position: 200% center; }
    }

    /* ── Generate button ── */
    .btn-generate {
      background: linear-gradient(90deg, var(--indigo) 0%, var(--violet) 25%, #818cf8 50%, var(--violet) 75%, var(--indigo) 100%);
      background-size: 200% auto; color: #fff; border: none; border-radius: var(--r12);
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 800; font-size: 1.05rem;
      padding: 1rem 2.5rem; width: 100%; cursor: pointer;
      transition: transform .18s, box-shadow .2s; box-shadow: 0 6px 24px rgba(79,70,229,0.45);
    }
    .btn-generate:hover {
      animation: shimmer 1.8s linear infinite; transform: translateY(-3px);
      box-shadow: 0 14px 36px rgba(79,70,229,0.52); color: #fff;
    }
    .btn-generate:active { transform: translateY(-1px); }

    /* ── Provider cards ── */
    .provider-card {
      border: 2px solid var(--border); border-radius: var(--r12); padding: .8rem 1.1rem;
      cursor: pointer; transition: border-color .18s, background .18s, box-shadow .18s; background: var(--bg);
    }
    .provider-card:hover { border-color: var(--indigo-l); background: #eef2ff; }
    .provider-card.selected { border-color: var(--indigo); background: #eef2ff; box-shadow: 0 0 0 3px rgba(79,70,229,0.1); }

    /* ── Layout ── */
    .cc-page { max-width: 880px; margin: 0 auto; padding: 2.25rem 1.5rem 5rem; }
    .section-eyebrow { font-size: .68rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; color: var(--amber); }
  </style>
</head>
<body>

<!-- Navbar -->
<nav class="cc-nav">
  <a class="cc-brand" href="/">
    <div class="cc-brand-icon"><i class="bi bi-file-earmark-text"></i></div>
    My INSEAD&nbsp;<span class="cc-brand-cv">CV</span>
  </a>
  {% if session.user_id %}
  <div class="cc-nav-links">
    <span class="cc-email-badge d-none d-md-inline">{{ session.email }}</span>
    <a class="cc-nav-pill" href="/bank"><i class="bi bi-database"></i>Bank</a>
    <a class="cc-nav-pill" href="/settings"><i class="bi bi-gear"></i>Settings</a>
    <a class="cc-nav-pill outline" href="/signout">Sign out</a>
  </div>
  {% endif %}
</nav>

<!-- Loading overlay -->
<div id="loadingOverlay" style="display:none;position:fixed;inset:0;background:rgba(6,9,24,0.85);backdrop-filter:blur(8px);z-index:9999;justify-content:center;align-items:center;flex-direction:column;">
  <div class="ov-card">
    <div class="cc-dual-ring"></div>
    <div class="spinner-text" id="overlayTitle">Working on it…</div>
    <div class="spinner-sub" id="overlaySub">This usually takes 20–40 seconds</div>
  </div>
</div>

<div class="cc-page">
  {% for cat, msg in get_flashed_messages(with_categories=true) %}
    <div class="alert alert-{{ 'danger' if cat=='error' else cat }} alert-dismissible fade show mb-3" role="alert">
      {{ msg }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    </div>
  {% endfor %}
  {% block content %}{% endblock %}
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ── Global loading overlay helper ────────────────────────────────────────────
function showLoading(title, sub) {
  const ov = document.getElementById('loadingOverlay');
  if (!ov) return;
  if (title) document.getElementById('overlayTitle').textContent = title;
  if (sub)   document.getElementById('overlaySub').textContent   = sub;
  ov.style.display = 'flex';
}

// ── Universal data-loading forms ─────────────────────────────────────────────
// Add data-loading to any <form> to get an automatic spinner on submit.
// Optionally set data-loading-title and data-loading-sub for custom text.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form[data-loading]').forEach(form => {
    form.addEventListener('submit', function() {
      showLoading(
        this.dataset.loadingTitle || 'Saving…',
        this.dataset.loadingSub   || 'Just a moment…'
      );
      // Disable every submit button in this form to prevent double-submits
      this.querySelectorAll('[type="submit"]').forEach(b => b.disabled = true);
    });
  });

  // ── Inline quick-action buttons (bank edit/delete/add) ────────────────────
  // Buttons with data-quick-action get a subtle in-place spinner instead of
  // the full overlay (fast DB writes don't need the heavy overlay).
  document.querySelectorAll('[data-quick-action]').forEach(btn => {
    btn.addEventListener('click', function() {
      const orig = this.innerHTML;
      this.innerHTML = '<span style="display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:ccSpin .7s linear infinite;vertical-align:middle;"></span>';
      this.disabled = true;
      // Restore if the form fails (navigation will clear state on success)
      setTimeout(() => { this.innerHTML = orig; this.disabled = false; }, 8000);
    });
  });
});
</script>
{% block scripts %}{% endblock %}
</body></html>"""


# ── Landing / index ───────────────────────────────────────────────────────────

_INDEX = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>My INSEAD CV — Land interviews, every time</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800;900&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    :root {
      --deep: #060918; --navy: #0f172a; --navy-80: #1e293b;
      --indigo: #4f46e5; --indigo-l: #6366f1;
      --violet: #7c3aed; --violet-l: #a78bfa;
      --amber: #d97706; --amber-l: #f59e0b;
      --emerald: #059669;
      --surface: #ffffff; --bg: #f8fafc;
      --border: rgba(15,23,42,0.08);
      --text: #0f172a; --muted: #64748b;
      --r20: 20px; --r16: 16px; --r12: 12px; --r10: 10px;
      --shadow: 0 1px 3px rgba(15,23,42,0.04), 0 4px 16px rgba(15,23,42,0.06);
      --shadow-md: 0 4px 6px rgba(15,23,42,0.04), 0 12px 32px rgba(15,23,42,0.10);
      --shadow-lg: 0 8px 16px rgba(15,23,42,0.06), 0 24px 56px rgba(15,23,42,0.16);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; }
    html { scroll-behavior: smooth; }
    body {
      font-family: 'Inter', system-ui, sans-serif;
      font-size: .92rem; color: var(--text);
      -webkit-font-smoothing: antialiased; background: var(--bg);
    }

    /* ── Navbar ── */
    .cc-nav {
      position: sticky; top: 0; z-index: 1000; height: 60px;
      background: rgba(6,9,24,0.96);
      backdrop-filter: blur(20px) saturate(180%);
      -webkit-backdrop-filter: blur(20px) saturate(180%);
      display: flex; align-items: center; justify-content: space-between; padding: 0 2rem;
    }
    .cc-nav::after {
      content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 2px;
      background: linear-gradient(90deg, var(--indigo), var(--violet), var(--amber)); opacity: 0.55;
    }
    .cc-brand {
      display: flex; align-items: center; gap: .65rem; text-decoration: none;
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700;
      font-size: 1.05rem; color: #fff; letter-spacing: -.3px;
    }
    .cc-brand-icon {
      width: 32px; height: 32px;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      border-radius: 9px; display: flex; align-items: center; justify-content: center;
      font-size: .9rem; color: #fff; box-shadow: 0 4px 14px rgba(79,70,229,0.5);
    }
    .cc-brand-cv { color: var(--amber-l); }
    .cc-nav-links { display: flex; align-items: center; gap: .5rem; }
    .cc-nav-pill {
      padding: .35rem .9rem; border-radius: 22px; font-size: .8rem; font-weight: 500;
      color: rgba(255,255,255,0.65); text-decoration: none;
      transition: background .18s, color .18s; border: 1px solid transparent;
    }
    .cc-nav-pill:hover { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.95); }
    .cc-nav-pill.outline { border-color: rgba(255,255,255,0.2); color: rgba(255,255,255,0.75); }
    .cc-nav-pill.outline:hover { border-color: rgba(255,255,255,0.4); color: #fff; }

    /* ── Alert ── */
    .alert { border-radius: var(--r12); border: none; border-left: 4px solid; font-size: .875rem; font-weight: 500; padding: .85rem 1.1rem; }
    .alert-success  { border-color: var(--emerald); background: #f0fdf4; color: #14532d; }
    .alert-danger   { border-color: #dc2626; background: #fef2f2; color: #7f1d1d; }
    .alert-warning  { border-color: var(--amber); background: #fffbeb; color: #78350f; }

    /* ── Hero ── */
    .hero {
      background: var(--deep);
      position: relative; overflow: hidden;
      padding: 4rem 0 4.5rem;
      display: flex; align-items: center;
    }
    /* Grid texture */
    .hero::before {
      content: ''; position: absolute; inset: 0; pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px);
      background-size: 72px 72px;
    }
    /* Aurora orbs */
    .aurora-orb {
      position: absolute; border-radius: 50%; filter: blur(80px);
      opacity: 0.35; pointer-events: none;
    }
    .orb-1 {
      width: 600px; height: 600px;
      background: radial-gradient(circle, var(--indigo) 0%, transparent 70%);
      top: -15%; left: -10%;
      animation: orbFloat1 18s ease-in-out infinite;
    }
    .orb-2 {
      width: 500px; height: 500px;
      background: radial-gradient(circle, var(--violet) 0%, transparent 70%);
      bottom: -20%; right: -5%;
      animation: orbFloat2 14s ease-in-out infinite;
    }
    .orb-3 {
      width: 350px; height: 350px;
      background: radial-gradient(circle, #1e40af 0%, transparent 70%);
      top: 40%; left: 40%;
      animation: orbFloat3 20s ease-in-out infinite;
    }
    @keyframes orbFloat1 {
      0%, 100% { transform: translate(0, 0); }
      33% { transform: translate(60px, 40px); }
      66% { transform: translate(-30px, 60px); }
    }
    @keyframes orbFloat2 {
      0%, 100% { transform: translate(0, 0); }
      50% { transform: translate(-80px, -50px); }
    }
    @keyframes orbFloat3 {
      0%, 100% { transform: translate(0, 0); }
      33% { transform: translate(50px, -40px); }
      66% { transform: translate(-60px, 30px); }
    }
    .hero-inner { position: relative; z-index: 1; }
    .hero-badge {
      display: inline-flex; align-items: center; gap: .5rem;
      background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.3);
      border-radius: 24px; padding: .35rem 1rem;
      font-size: .72rem; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
      color: var(--amber-l); margin-bottom: 1.25rem;
    }
    .hero-h1 {
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900;
      font-size: clamp(2.4rem, 5vw, 3.4rem); line-height: 1.05;
      color: #fff; letter-spacing: -.05em; margin-bottom: 1rem;
    }
    .hero-h1 em { font-style: normal; color: var(--amber-l); }
    .hero-sub {
      font-size: 1rem; line-height: 1.65; color: rgba(255,255,255,0.6);
      max-width: 600px; margin-bottom: 1.5rem;
    }
    .hero-cta-row { display: flex; gap: .85rem; flex-wrap: wrap; margin-bottom: 1rem; }
    .btn-hero-primary {
      padding: .75rem 1.75rem; border-radius: var(--r12);
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      color: #fff; font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700; font-size: .95rem;
      text-decoration: none; border: none; box-shadow: 0 6px 22px rgba(79,70,229,0.5);
      transition: transform .18s, box-shadow .18s; display: inline-flex; align-items: center; gap: .5rem;
    }
    .btn-hero-primary:hover { transform: translateY(-2px); box-shadow: 0 10px 32px rgba(79,70,229,0.55); color: #fff; }
    .btn-hero-outline {
      padding: .75rem 1.75rem; border-radius: var(--r12);
      border: 1.5px solid rgba(255,255,255,0.25); color: rgba(255,255,255,0.85);
      font-weight: 600; font-size: .95rem; text-decoration: none;
      transition: border-color .18s, background .18s; background: rgba(255,255,255,0.04);
      display: inline-flex; align-items: center; gap: .5rem;
    }
    .btn-hero-outline:hover { border-color: rgba(255,255,255,0.5); background: rgba(255,255,255,0.08); color: #fff; }
    .hero-trust {
      font-size: .77rem; color: rgba(255,255,255,0.35);
      display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
      margin-bottom: 2rem;
    }
    .hero-trust i { color: rgba(255,255,255,0.25); }

    /* ── Hero feature grid (fills left column) ── */
    .hero-feat-grid {
      display: grid; grid-template-columns: 1fr 1fr; gap: .9rem;
      max-width: 640px;
    }
    @media (max-width: 560px) { .hero-feat-grid { grid-template-columns: 1fr; } }
    .hero-feat {
      display: flex; align-items: flex-start; gap: .75rem;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 14px; padding: .85rem .95rem;
      backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
      transition: background .2s, border-color .2s, transform .2s;
    }
    .hero-feat:hover {
      background: rgba(255,255,255,0.07);
      border-color: rgba(255,255,255,0.18);
      transform: translateY(-2px);
    }
    .hero-feat-ic {
      width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      color: #fff; font-size: .9rem;
      box-shadow: 0 4px 14px rgba(79,70,229,0.4);
    }
    .hero-feat-title {
      font-family: 'Plus Jakarta Sans', sans-serif;
      font-size: .82rem; font-weight: 700; color: #fff;
      letter-spacing: -.01em; margin-bottom: .15rem;
    }
    .hero-feat-desc {
      font-size: .72rem; line-height: 1.45;
      color: rgba(255,255,255,0.5);
    }

    /* ── CV mockup ── */
    .cv-mockup-wrap { display: flex; justify-content: center; align-items: center; padding: 2rem 1rem; }
    .cv-mockup {
      background: #fff; border-radius: 16px;
      box-shadow: 0 32px 80px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.1);
      width: 270px; padding: 1.75rem; position: relative;
      animation: floatCV 4.5s ease-in-out infinite;
      transform-origin: center;
    }
    @keyframes floatCV {
      0%, 100% { transform: translateY(0px) rotate(-1.8deg); box-shadow: 0 32px 80px rgba(0,0,0,0.5); }
      50% { transform: translateY(-18px) rotate(0.5deg); box-shadow: 0 48px 100px rgba(0,0,0,0.6); }
    }
    .cv-glow {
      position: absolute; inset: -2px; border-radius: 18px; z-index: -1;
      background: linear-gradient(135deg, var(--indigo), var(--violet), var(--amber));
      filter: blur(16px); opacity: 0.5;
      animation: glowPulse 4.5s ease-in-out infinite;
    }
    @keyframes glowPulse {
      0%, 100% { opacity: 0.4; filter: blur(16px); }
      50% { opacity: 0.65; filter: blur(22px); }
    }
    .cv-ai-badge {
      position: absolute; top: -.65rem; right: -.65rem;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      color: #fff; font-size: .62rem; font-weight: 700; padding: .3rem .7rem;
      border-radius: 20px; box-shadow: 0 4px 14px rgba(79,70,229,0.5);
      animation: pulseBadge 2.5s ease-in-out infinite;
    }
    @keyframes pulseBadge {
      0%, 100% { box-shadow: 0 4px 14px rgba(79,70,229,0.5); }
      50% { box-shadow: 0 4px 28px rgba(79,70,229,0.8); }
    }
    .cv-mock-name { font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 800; font-size: .9rem; color: #0f172a; margin-bottom: .12rem; }
    .cv-mock-contact { font-size: .56rem; color: #94a3b8; margin-bottom: .9rem; line-height: 1.5; }
    .cv-mock-section { font-size: .58rem; font-weight: 800; text-transform: uppercase; letter-spacing: .1em; color: var(--indigo); margin-bottom: .4rem; border-bottom: 1.5px solid #e2e8f0; padding-bottom: .25rem; }
    .cv-mock-line { height: 7px; border-radius: 4px; background: linear-gradient(90deg, #e2e8f0, #f1f5f9); margin-bottom: .3rem; }
    .cv-mock-role { font-size: .65rem; font-weight: 700; color: #334155; margin-bottom: .25rem; }
    .cv-mock-company { font-size: .58rem; color: #64748b; margin-bottom: .5rem; }
    .cv-mock-skills { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .4rem; }
    .cv-mock-skill-pill {
      font-size: .5rem; font-weight: 600; padding: .2rem .5rem; border-radius: 10px;
      background: #eef2ff; color: var(--indigo); border: 1px solid rgba(79,70,229,0.2);
    }

    /* ── Stats bar ── */
    .stats-bar {
      background: rgba(15,23,42,0.9); backdrop-filter: blur(20px);
      border-bottom: 1px solid rgba(255,255,255,0.06);
      padding: 1.1rem 0;
    }
    .stat-item { text-align: center; padding: .5rem 1.5rem; }
    .stat-item:not(:last-child) { border-right: 1px solid rgba(255,255,255,0.08); }
    .stat-number {
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900;
      font-size: 1.75rem; color: #fff; line-height: 1; letter-spacing: -.03em;
      margin-bottom: .3rem;
    }
    .stat-number span { color: var(--amber-l); }
    .stat-label { font-size: .72rem; color: rgba(255,255,255,0.45); font-weight: 500; letter-spacing: .03em; }

    /* ── How it works ── */
    .how-section { background: #0d1117; padding: 4rem 0 4.5rem; }
    .how-heading {
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900;
      font-size: clamp(1.8rem, 3.5vw, 2.6rem); color: #fff;
      letter-spacing: -.04em; margin-bottom: .75rem; line-height: 1.1;
    }
    .how-sub { font-size: .95rem; color: rgba(255,255,255,0.45); max-width: 480px; margin: 0 auto; }
    /* Connecting line between cards */
    .step-connector { position: relative; }
    .step-connector::before {
      content: '';
      position: absolute; top: 38px; left: calc(50% + 130px); right: calc(-50% + 130px);
      height: 1px; border-top: 2px dashed rgba(255,255,255,0.1); z-index: 0;
      display: none;
    }
    @media (min-width: 768px) { .step-connector::before { display: block; } }
    .step-card {
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.09);
      backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
      border-radius: var(--r20); padding: 2.25rem 1.75rem;
      cursor: default; position: relative; z-index: 1;
      opacity: 0; transform: translateY(32px);
      transition: opacity .5s ease, transform .5s ease, box-shadow .25s;
    }
    .step-card.visible { opacity: 1; transform: translateY(0); }
    .step-card:hover { box-shadow: 0 20px 48px rgba(0,0,0,0.4); }
    .step-icon-circle {
      width: 56px; height: 56px; border-radius: 16px;
      display: flex; align-items: center; justify-content: center;
      font-size: 1.35rem; color: #fff; margin-bottom: 1.2rem;
    }
    .ic-indigo { background: linear-gradient(135deg, var(--indigo), var(--indigo-l)); box-shadow: 0 6px 20px rgba(79,70,229,0.45); }
    .ic-violet { background: linear-gradient(135deg, var(--violet), var(--violet-l)); box-shadow: 0 6px 20px rgba(124,58,237,0.45); }
    .ic-emerald { background: linear-gradient(135deg, var(--emerald), #10b981); box-shadow: 0 6px 20px rgba(5,150,105,0.4); }
    .step-num {
      font-size: .65rem; font-weight: 800; letter-spacing: .1em;
      text-transform: uppercase; color: rgba(255,255,255,0.3); margin-bottom: .55rem;
    }
    .step-title {
      font-family: 'Plus Jakarta Sans', sans-serif; font-size: 1.05rem; font-weight: 800;
      color: #fff; margin-bottom: .6rem; letter-spacing: -.02em;
    }
    .step-desc { font-size: .86rem; line-height: 1.7; color: rgba(255,255,255,0.5); }

    /* ── Bank boost (optional) callout ── */
    .bank-boost-card {
      display: flex; align-items: center; gap: 1.75rem;
      background: linear-gradient(135deg, rgba(79,70,229,0.12), rgba(124,58,237,0.08));
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: var(--r20);
      padding: 1.75rem 2rem;
      backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
      box-shadow: 0 20px 48px rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.06);
    }
    .bank-boost-left { flex: 1; }
    .bank-boost-badge {
      display: inline-flex; align-items: center;
      font-size: .68rem; font-weight: 800; letter-spacing: .09em; text-transform: uppercase;
      color: var(--amber-l, #fbbf24);
      background: rgba(217,119,6,0.14);
      border: 1px solid rgba(217,119,6,0.28);
      padding: .3rem .7rem; border-radius: 999px; margin-bottom: .85rem;
    }
    .bank-boost-title {
      font-family: 'Plus Jakarta Sans', sans-serif; font-size: 1.25rem; font-weight: 800;
      color: #fff; letter-spacing: -.02em; margin-bottom: .5rem;
    }
    .bank-boost-desc { font-size: .88rem; line-height: 1.7; color: rgba(255,255,255,0.55); margin: 0; }
    .bank-boost-right { flex: 0 0 auto; }
    .bank-boost-ic {
      width: 72px; height: 72px; border-radius: 20px;
      display: flex; align-items: center; justify-content: center;
      font-size: 1.8rem; color: #fff;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      box-shadow: 0 12px 32px rgba(79,70,229,0.45);
    }
    @media (max-width: 768px) {
      .bank-boost-card { flex-direction: column-reverse; text-align: center; }
      .bank-boost-badge { margin-left: auto; margin-right: auto; }
    }

    /* ── INSEAD pledge section ── */
    .insead-pledge {
      background: linear-gradient(180deg, #0d1117 0%, #060918 100%);
      padding: 4rem 0 4.5rem; position: relative; overflow: hidden;
    }
    .insead-pledge::before {
      content: ''; position: absolute; inset: 0; pointer-events: none;
      background:
        radial-gradient(circle at 20% 30%, rgba(124,58,237,0.18), transparent 55%),
        radial-gradient(circle at 80% 70%, rgba(217,119,6,0.15), transparent 55%);
    }
    .pledge-card {
      position: relative; z-index: 1;
      max-width: 860px; margin: 0 auto;
      background: linear-gradient(135deg, rgba(79,70,229,0.15), rgba(124,58,237,0.10), rgba(217,119,6,0.10));
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 28px;
      padding: 3.25rem 2.75rem;
      text-align: center;
      backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
      box-shadow: 0 32px 80px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.08);
    }
    .pledge-card::after {
      content: ''; position: absolute; inset: -1px; border-radius: 28px;
      background: linear-gradient(135deg, var(--violet), var(--amber), var(--indigo));
      z-index: -1; filter: blur(24px); opacity: 0.22;
    }
    .pledge-badge {
      display: inline-flex; align-items: center;
      background: rgba(245,158,11,0.14); border: 1px solid rgba(245,158,11,0.35);
      color: var(--amber-l); padding: .4rem 1.1rem; border-radius: 24px;
      font-size: .72rem; font-weight: 800; letter-spacing: .08em;
      text-transform: uppercase; margin-bottom: 1.25rem;
    }
    .pledge-heading {
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900;
      font-size: clamp(2.3rem, 5vw, 3.4rem); color: #fff;
      letter-spacing: -.05em; line-height: 1.05; margin-bottom: 1rem;
      background: linear-gradient(135deg, #fff 40%, var(--amber-l) 100%);
      -webkit-background-clip: text; background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .pledge-sub {
      font-size: 1rem; line-height: 1.75; color: rgba(255,255,255,0.72);
      max-width: 620px; margin: 0 auto 2rem;
    }
    .pledge-sub strong { color: var(--amber-l); font-weight: 700; }
    .pledge-pills {
      display: flex; flex-wrap: wrap; gap: .6rem;
      justify-content: center; margin-bottom: 2.2rem;
    }
    .pledge-pill {
      display: inline-flex; align-items: center; gap: .45rem;
      font-size: .78rem; font-weight: 600; color: rgba(255,255,255,0.85);
      background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
      padding: .5rem 1rem; border-radius: 100px;
      transition: background .2s, border-color .2s, transform .2s;
    }
    .pledge-pill:hover {
      background: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.22);
      transform: translateY(-1px);
    }
    .pledge-pill i { color: var(--amber-l); }
    .pledge-cta {
      display: inline-flex; align-items: center; gap: .55rem;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      color: #fff; padding: .85rem 1.9rem; border-radius: 14px;
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700; font-size: .95rem;
      text-decoration: none; box-shadow: 0 10px 30px rgba(79,70,229,0.45);
      transition: transform .2s, box-shadow .2s;
    }
    .pledge-cta:hover {
      transform: translateY(-2px); color: #fff;
      box-shadow: 0 14px 40px rgba(79,70,229,0.6);
    }

    /* ── Auth section ── */
    .auth-section { background: var(--bg); padding: 4rem 0 4.5rem; }
    .auth-heading {
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900;
      font-size: clamp(1.8rem, 3.5vw, 2.4rem); color: var(--navy);
      letter-spacing: -.04em; margin-bottom: .65rem;
    }
    .auth-sub { font-size: .95rem; color: var(--muted); }
    .auth-card {
      background: #fff; border-radius: var(--r20);
      box-shadow: 0 4px 24px rgba(15,23,42,0.08), 0 1px 3px rgba(15,23,42,0.04);
      overflow: hidden; transition: transform .25s, box-shadow .25s;
    }
    .auth-card:hover { transform: translateY(-4px); box-shadow: 0 12px 40px rgba(15,23,42,0.12); }
    .auth-card-top {
      height: 3px;
    }
    .auth-card-top.signin, .auth-card-top.signup { background: linear-gradient(90deg, var(--indigo), var(--violet)); }
    .auth-card-body { padding: 2rem 2.25rem 2.25rem; }
    .auth-card-title {
      font-family: 'Plus Jakarta Sans', sans-serif; font-size: 1.1rem; font-weight: 800;
      color: var(--navy); margin-bottom: 1.5rem; display: flex; align-items: center; gap: .55rem;
      letter-spacing: -.02em;
    }
    .auth-icon {
      width: 34px; height: 34px; border-radius: 10px;
      display: flex; align-items: center; justify-content: center; font-size: .9rem; color: #fff;
    }
    .ic-sign-in, .ic-sign-up { background: linear-gradient(135deg, var(--indigo), var(--violet)); }
    .auth-label {
      display: block; font-size: .72rem; font-weight: 700; color: var(--navy);
      margin-bottom: .4rem; text-transform: uppercase; letter-spacing: .06em;
    }
    .auth-input {
      width: 100%; padding: .7rem 1rem; background: var(--bg);
      border: 1.5px solid var(--border); border-radius: var(--r10);
      font-family: 'Inter', sans-serif; font-size: .875rem; color: var(--text);
      transition: border-color .18s, box-shadow .18s; outline: none;
    }
    .auth-input:focus { border-color: var(--indigo); box-shadow: 0 0 0 3px rgba(79,70,229,0.1); background: #fff; }
    .auth-mb { margin-bottom: .95rem; }
    .btn-auth-signin, .btn-auth-signup {
      width: 100%; padding: .75rem 1rem;
      background: linear-gradient(135deg, var(--indigo), var(--violet));
      color: #fff; border: none; border-radius: var(--r10);
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700; font-size: .9rem; cursor: pointer;
      box-shadow: 0 6px 20px rgba(79,70,229,0.4); transition: opacity .18s, transform .15s;
    }
    .btn-auth-signin:hover, .btn-auth-signup:hover { opacity: .88; transform: translateY(-1px); }

    /* ── Footer ── */
    .site-footer {
      background: var(--deep); border-top: 1px solid rgba(255,255,255,0.05);
      padding: 2.5rem 0;
    }
    .footer-brand {
      font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 800;
      font-size: 1rem; color: #fff; margin-bottom: .4rem;
    }
    .footer-brand span { color: var(--amber-l); }
    .footer-tag { font-size: .78rem; color: rgba(255,255,255,0.3); }
    .footer-lock { font-size: .75rem; color: rgba(255,255,255,0.25); display: flex; align-items: center; gap: .4rem; }

    .section-eyebrow { font-size: .68rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; color: var(--amber); }
  </style>
</head>
<body>

<!-- Navbar -->
<nav class="cc-nav">
  <a class="cc-brand" href="/">
    <div class="cc-brand-icon"><i class="bi bi-file-earmark-text"></i></div>
    My INSEAD&nbsp;<span class="cc-brand-cv">CV</span>
  </a>
  <div class="cc-nav-links">
    <a class="cc-nav-pill outline" href="#signin">Sign in</a>
    <a class="cc-nav-pill" style="background:linear-gradient(135deg,var(--indigo),var(--violet));color:#fff;font-weight:700;box-shadow:0 4px 14px rgba(79,70,229,0.4);" href="#signup">Get started free</a>
  </div>
</nav>

{% for cat, msg in get_flashed_messages(with_categories=true) %}
<div style="padding:.75rem 2rem 0;">
  <div class="alert alert-{{ 'danger' if cat=='error' else cat }} alert-dismissible fade show mb-0" role="alert">
    {{ msg }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>
  </div>
</div>
{% endfor %}

<!-- Hero -->
<section class="hero">
  <div class="aurora-orb orb-1"></div>
  <div class="aurora-orb orb-2"></div>
  <div class="aurora-orb orb-3"></div>
  <div class="container hero-inner" style="width:100%;">
    <div class="row align-items-center g-4">
      <div class="col-lg-7">
        <div class="hero-badge"><i class="bi bi-mortarboard-fill"></i>&nbsp;Free forever for INSEADers</div>
        <h1 class="hero-h1">Land interviews,<br><em>every time.</em></h1>
        <p class="hero-sub">Upload your CV template, paste any job description, and get a tailored, ATS-optimised CV in 60 seconds. <strong style="color:#fff;">Your template, your words</strong> — zero hallucination. Want even sharper tailoring? Build your master bank once and let AI pull the most relevant experience for every JD.</p>
        <div class="hero-cta-row">
          <a href="#signup" class="btn-hero-primary"><i class="bi bi-rocket-takeoff"></i>Start for free</a>
          <a href="#signin" class="btn-hero-outline"><i class="bi bi-box-arrow-in-right"></i>Sign in</a>
        </div>
        <div class="hero-trust">
          <i class="bi bi-key-fill"></i>Bring your own API key
          <span style="color:rgba(255,255,255,0.15);">|</span>
          <i class="bi bi-shield-check"></i>Encrypted & private
          <span style="color:rgba(255,255,255,0.15);">|</span>
          <i class="bi bi-infinity"></i>Unlimited tailored CVs
        </div>

        <!-- Hero feature grid — fills the vertical space beside the mockup -->
        <div class="hero-feat-grid">
          <div class="hero-feat">
            <div class="hero-feat-ic"><i class="bi bi-lightning-charge-fill"></i></div>
            <div>
              <div class="hero-feat-title">60-second generation</div>
              <div class="hero-feat-desc">From paste to polished PDF in under a minute.</div>
            </div>
          </div>
          <div class="hero-feat">
            <div class="hero-feat-ic"><i class="bi bi-file-earmark-check-fill"></i></div>
            <div>
              <div class="hero-feat-title">Your template, preserved</div>
              <div class="hero-feat-desc">Exact fonts, spacing, and bullet counts — pixel perfect.</div>
            </div>
          </div>
          <div class="hero-feat">
            <div class="hero-feat-ic"><i class="bi bi-ban"></i></div>
            <div>
              <div class="hero-feat-title">Zero hallucination</div>
              <div class="hero-feat-desc">AI only rewrites what's in your CV and master bank — never invents.</div>
            </div>
          </div>
          <div class="hero-feat">
            <div class="hero-feat-ic"><i class="bi bi-cpu-fill"></i></div>
            <div>
              <div class="hero-feat-title">Any AI provider</div>
              <div class="hero-feat-desc">Anthropic, OpenAI, or Gemini — your choice, your cost.</div>
            </div>
          </div>
        </div>
      </div>
      <div class="col-lg-5">
        <div class="cv-mockup-wrap">
          <div style="position:relative;">
            <div class="cv-glow"></div>
            <div class="cv-mockup">
              <div class="cv-ai-badge">&#10024; AI Tailored</div>
              <div class="cv-mock-name">Wani Bisen</div>
              <div class="cv-mock-contact">wani.bisen@email.com &nbsp;&bull;&nbsp; London, UK<br>linkedin.com/in/wani-bisen &nbsp;&bull;&nbsp; +44 7700 000000</div>
              <div class="cv-mock-section">Experience</div>
              <div class="cv-mock-role">Strategy Manager</div>
              <div class="cv-mock-company">Elite Strategy Firm &nbsp;&middot;&nbsp; 2022–Present</div>
              <div class="cv-mock-line" style="width:95%"></div>
              <div class="cv-mock-line" style="width:80%"></div>
              <div class="cv-mock-line" style="width:88%"></div>
              <div style="margin-top:.6rem;"></div>
              <div class="cv-mock-role">Associate Consultant</div>
              <div class="cv-mock-company">MBB Tier Consulting &nbsp;&middot;&nbsp; 2020–2022</div>
              <div class="cv-mock-line" style="width:90%"></div>
              <div class="cv-mock-line" style="width:75%"></div>
              <div style="margin-top:.75rem;"></div>
              <div class="cv-mock-section">Education</div>
              <div class="cv-mock-role">MBA — INSEAD</div>
              <div class="cv-mock-line" style="width:70%"></div>
              <div style="margin-top:.75rem;"></div>
              <div class="cv-mock-section">Skills</div>
              <div class="cv-mock-skills">
                <span class="cv-mock-skill-pill">Strategy</span>
                <span class="cv-mock-skill-pill">P&amp;L</span>
                <span class="cv-mock-skill-pill">M&amp;A</span>
                <span class="cv-mock-skill-pill">Python</span>
                <span class="cv-mock-skill-pill">SQL</span>
                <span class="cv-mock-skill-pill">Leadership</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- Stats bar -->
<section class="stats-bar">
  <div class="container">
    <div class="row g-0 justify-content-center">
      <div class="col-auto stat-item">
        <div class="stat-number"><span>60</span> sec</div>
        <div class="stat-label">Per CV Generation</div>
      </div>
      <div class="col-auto stat-item">
        <div class="stat-number"><span>Private</span></div>
        <div class="stat-label">Encrypted &amp; Yours Only</div>
      </div>
      <div class="col-auto stat-item">
        <div class="stat-number"><span>Any</span> AI</div>
        <div class="stat-label">Your Own API Key</div>
      </div>
    </div>
  </div>
</section>

<!-- How it works -->
<section class="how-section">
  <div class="container">
    <div class="text-center mb-4">
      <div class="section-eyebrow mb-3">The workflow</div>
      <h2 class="how-heading">Three steps.<br>Sixty seconds.</h2>
      <p class="how-sub">Upload your template, paste a JD, download your tailored CV. That's it. The master bank is an optional boost — not a prerequisite.</p>
    </div>
    <div class="row g-4 position-relative">
      <div class="col-md-4 step-connector">
        <div class="step-card">
          <div class="step-icon-circle ic-indigo"><i class="bi bi-file-earmark-arrow-up-fill"></i></div>
          <div class="step-num">Step 01</div>
          <div class="step-title">Upload your CV template</div>
          <div class="step-desc">Drop in the .docx you already use. We preserve its fonts, spacing, and bullet counts exactly — every generated CV comes out looking like the one you designed.</div>
        </div>
      </div>
      <div class="col-md-4 step-connector">
        <div class="step-card">
          <div class="step-icon-circle ic-violet"><i class="bi bi-file-earmark-text-fill"></i></div>
          <div class="step-num">Step 02</div>
          <div class="step-title">Paste any job description</div>
          <div class="step-desc">Copy-paste any JD from any company. Our AI reads its language, identifies key priorities, and rewrites your bullets in STAR format using the JD's exact keywords.</div>
        </div>
      </div>
      <div class="col-md-4">
        <div class="step-card">
          <div class="step-icon-circle ic-emerald"><i class="bi bi-download"></i></div>
          <div class="step-num">Step 03</div>
          <div class="step-title">Download your tailored CV</div>
          <div class="step-desc">Get a Word + PDF instantly — ready to edit or send. Same template, perfect fit, one-page guaranteed.</div>
        </div>
      </div>
    </div>

    <!-- Optional boost callout -->
    <div class="row justify-content-center mt-5">
      <div class="col-lg-10">
        <div class="bank-boost-card">
          <div class="bank-boost-left">
            <div class="bank-boost-badge"><i class="bi bi-stars"></i>&nbsp;Optional &middot; Highly recommended</div>
            <div class="bank-boost-title">Want sharper tailoring? Build a master bank.</div>
            <div class="bank-boost-desc">
              Collate every role, project, and achievement you've ever had — including the ones that didn't make it onto your current CV.
              When you paste a JD, the AI can dip into your bank and surface the <em>most relevant</em> experience for that specific role,
              even if it wasn't on your base template. One-time effort; every future application gets smarter.
            </div>
          </div>
          <div class="bank-boost-right">
            <div class="bank-boost-ic"><i class="bi bi-database-fill"></i></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- For INSEADers — free-forever pledge -->
<section class="insead-pledge">
  <div class="container">
    <div class="pledge-card">
      <div class="pledge-inner">
        <div class="pledge-badge">
          <i class="bi bi-mortarboard-fill"></i>&nbsp;For INSEADers
        </div>
        <h2 class="pledge-heading">Free. Forever.</h2>
        <p class="pledge-sub">
          This tool will <strong>always</strong> be free for INSEADers. Bring your own API key,
          build your master bank once, and tailor unlimited CVs to land any role after your MBA.
        </p>
        <div class="pledge-pills">
          <span class="pledge-pill"><i class="bi bi-infinity"></i>Unlimited generations</span>
          <span class="pledge-pill"><i class="bi bi-key-fill"></i>Your own API key</span>
          <span class="pledge-pill"><i class="bi bi-shield-lock-fill"></i>Your data, your control</span>
          <span class="pledge-pill"><i class="bi bi-heart-fill"></i>Built by an INSEADer</span>
        </div>
        <a href="#signup" class="pledge-cta">
          <i class="bi bi-rocket-takeoff"></i>Create your free account
        </a>
      </div>
    </div>
  </div>
</section>

<!-- Auth -->
<section class="auth-section" id="signin">
  <div class="container">
    <div class="text-center mb-4">
      <div class="section-eyebrow mb-3">Get started</div>
      <h2 class="auth-heading">Start in 30 seconds</h2>
      <p class="auth-sub">No credit card. Bring your own API key. Your data stays yours.</p>
    </div>
    <div class="row g-4 justify-content-center" style="max-width:820px;margin:0 auto;">
      <div class="col-md-6">
        <div class="auth-card">
          <div class="auth-card-top signin"></div>
          <div class="auth-card-body">
            <div class="auth-card-title">
              <div class="auth-icon ic-sign-in"><i class="bi bi-box-arrow-in-right"></i></div>
              Sign in
            </div>
            <form method="post" action="/signin">
              <div class="auth-mb">
                <label class="auth-label">Email</label>
                <input name="email" type="email" class="auth-input" required autocomplete="email" placeholder="you@email.com">
              </div>
              <div class="auth-mb">
                <label class="auth-label">Password</label>
                <input name="password" type="password" class="auth-input" required autocomplete="current-password" placeholder="••••••••">
              </div>
              <button type="submit" class="btn-auth-signin">Sign in &rarr;</button>
            </form>
          </div>
        </div>
      </div>
      <div class="col-md-6" id="signup">
        <div class="auth-card">
          <div class="auth-card-top signup"></div>
          <div class="auth-card-body">
            <div class="auth-card-title">
              <div class="auth-icon ic-sign-up"><i class="bi bi-person-plus-fill"></i></div>
              Create your account
            </div>
            <form method="post" action="/signup">
              <div class="auth-mb">
                <label class="auth-label">Full name</label>
                <input name="name" class="auth-input signup-focus" required placeholder="Your full name">
              </div>
              <div class="auth-mb">
                <label class="auth-label">Email</label>
                <input name="email" type="email" class="auth-input signup-focus" required autocomplete="email" placeholder="you@email.com">
              </div>
              <div class="auth-mb">
                <label class="auth-label">Password</label>
                <input name="password" type="password" class="auth-input signup-focus" required autocomplete="new-password" minlength="8" placeholder="Min. 8 characters">
              </div>
              <button type="submit" class="btn-auth-signup">Create free account &rarr;</button>
            </form>
          </div>
        </div>
      </div>
    </div>
    <div style="text-align:center;font-size:.77rem;color:var(--muted);margin-top:2rem;display:flex;align-items:center;justify-content:center;gap:.5rem;">
      <i class="bi bi-shield-lock-fill" style="color:var(--emerald);"></i>
      Your API key is encrypted at rest and only used for CV generation. Your data is yours — protected by row-level security.
    </div>
  </div>
</section>

<!-- Footer -->
<footer class="site-footer">
  <div class="container">
    <div class="d-flex align-items-center justify-content-between flex-wrap gap-3">
      <div>
        <div class="footer-brand">My INSEAD <span>CV</span></div>
        <div class="footer-tag">AI-powered CV tailoring &mdash; free forever for INSEADers</div>
      </div>
      <div class="footer-lock">
        <i class="bi bi-shield-check"></i> Your data stays yours — always.
      </div>
    </div>
  </div>
</footer>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
// 3D tilt on step cards
document.querySelectorAll('.step-card').forEach(card => {
  card.addEventListener('mousemove', e => {
    const r = card.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const rx = ((y - r.height/2) / r.height) * -12;
    const ry = ((x - r.width/2) / r.width) * 12;
    card.style.transform = `perspective(800px) rotateX(${rx}deg) rotateY(${ry}deg) translateY(-6px)`;
  });
  card.addEventListener('mouseleave', () => {
    card.style.transform = card.classList.contains('visible') ? '' : 'translateY(0)';
  });
});

// Scroll reveal with staggered 150ms delay
const obs = new IntersectionObserver(entries => {
  entries.forEach((e, i) => {
    if (e.isIntersecting) {
      setTimeout(() => e.target.classList.add('visible'), i * 150);
      obs.unobserve(e.target);
    }
  });
}, { threshold: 0.1 });
document.querySelectorAll('.step-card').forEach(c => obs.observe(c));

// Sign-in / Sign-up: swap button text to a spinner on submit
document.querySelectorAll('form[action="/signin"], form[action="/signup"]').forEach(form => {
  form.addEventListener('submit', function() {
    const btn = this.querySelector('[type="submit"]');
    if (!btn) return;
    btn.disabled = true;
    btn.innerHTML = '<span style="display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:authSpin .7s linear infinite;vertical-align:middle;margin-right:6px;"></span>Signing in…';
  });
});
</script>
<style>
@keyframes authSpin { to { transform: rotate(360deg); } }
</style>
</body>
</html>"""


# ── Dashboard ──────────────────────────────────────────────────────────────────

_DASHBOARD = _BASE.replace("{% block content %}{% endblock %}", """
<style>
  .dash-greeting { font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900; font-size: 2rem; letter-spacing: -.04em; color: var(--navy); margin-bottom: .3rem; line-height: 1.2; }
  .dash-sub { color: var(--muted); font-size: .95rem; }
  .setup-card {
    background: var(--navy); border-radius: 20px;
    border-left: 4px solid var(--amber); padding: 2rem;
    box-shadow: 0 8px 32px rgba(15,23,42,0.18), 0 0 0 1px rgba(255,255,255,0.03);
    margin-bottom: 1.75rem;
  }
  .setup-eyebrow { font-size: .65rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; color: var(--amber-l); margin-bottom: .6rem; }
  .setup-heading { font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 800; font-size: 1.1rem; color: #fff; margin-bottom: 1.4rem; letter-spacing: -.02em; }
  .setup-step { display: flex; align-items: flex-start; gap: .85rem; margin-bottom: 1.1rem; }
  .setup-step:last-child { margin-bottom: 0; }
  .setup-step-num {
    display: inline-flex; align-items: center; justify-content: center;
    width: 28px; height: 28px; border-radius: 50%; font-size: .72rem; font-weight: 800; flex-shrink: 0; margin-top: .1rem;
    background: linear-gradient(135deg, var(--indigo), var(--violet)); color: #fff; box-shadow: 0 2px 10px rgba(79,70,229,0.4);
  }
  .setup-step-num.done { background: linear-gradient(135deg, var(--emerald), #10b981); box-shadow: 0 2px 10px rgba(5,150,105,0.4); }
  .setup-step-title { font-weight: 700; font-size: .9rem; color: #fff; }
  .setup-step-title.done { color: rgba(255,255,255,0.4); text-decoration: line-through; }
  .setup-step-desc { font-size: .78rem; color: rgba(255,255,255,0.38); margin-top: .2rem; line-height: 1.55; }

  .status-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 1.75rem; }
  @media (max-width: 640px) { .status-cards { grid-template-columns: 1fr; } }
  .status-card {
    background: #fff; border-radius: 20px; padding: 1.4rem 1.35rem;
    border: 1px solid var(--border); box-shadow: var(--shadow);
    transition: transform .22s, box-shadow .22s;
    display: flex; flex-direction: column;
  }
  .status-card:hover { transform: translateY(-4px); box-shadow: var(--shadow-md); }
  .status-card-top { display: flex; align-items: center; gap: .8rem; margin-bottom: .7rem; }
  .status-icon {
    width: 40px; height: 40px; border-radius: 12px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-size: 1rem; color: #fff;
  }
  .si-bank { background: linear-gradient(135deg, var(--indigo), var(--indigo-l)); box-shadow: 0 4px 12px rgba(79,70,229,0.35); }
  .si-tpl  { background: linear-gradient(135deg, var(--emerald), #10b981); box-shadow: 0 4px 12px rgba(5,150,105,0.3); }
  .si-key  { background: linear-gradient(135deg, var(--amber), var(--amber-l)); box-shadow: 0 4px 12px rgba(217,119,6,0.35); }
  .status-card-name { font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 800; font-size: .95rem; color: var(--navy); letter-spacing: -.02em; }
  .status-dot-row { display: flex; align-items: center; gap: .3rem; margin-top: .18rem; }
  .sdot2 { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
  .sdot2-ok { background: var(--emerald); box-shadow: 0 0 0 3px rgba(5,150,105,0.18); }
  .sdot2-no { background: #cbd5e1; }
  .status-card-desc { font-size: .8rem; color: var(--muted); line-height: 1.55; margin-bottom: .9rem; flex-grow: 1; }
  .status-card-active-bar {
    height: 3px; border-radius: 0 0 3px 3px; margin: -1.4rem -1.35rem 1rem;
    margin-bottom: .7rem;
  }

  .generate-wrap {
    border-radius: 20px; overflow: hidden;
    box-shadow: 0 4px 6px rgba(15,23,42,0.04), 0 12px 32px rgba(15,23,42,0.08);
    position: relative;
  }
  .generate-wrap::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(90deg, var(--indigo), var(--violet), var(--amber), var(--emerald));
  }
  .generate-inner { background: #fff; border: 1px solid var(--border); border-radius: 20px; padding: 2.25rem; border-top: none; border-radius: 0 0 20px 20px; }
  .generate-title {
    font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900;
    font-size: 1.3rem; color: var(--navy); margin-bottom: .4rem; letter-spacing: -.03em;
    display: flex; align-items: center; gap: .55rem;
  }
  .generate-sub { color: var(--muted); font-size: .88rem; margin-bottom: 1.4rem; line-height: 1.6; }

  /* ── How it works in Dashboard (Dark theme consistent) ── */
  .dash-how { background: var(--deep); border-radius: 20px; padding: 2.25rem; margin-top: 1.75rem; border: 1px solid rgba(255,255,255,0.05); }
  .how-heading { font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 900; font-size: 1.5rem; color: #fff; letter-spacing: -.03em; margin-bottom: 1rem; }
  .how-sub { font-size: .88rem; color: rgba(255,255,255,0.45); margin-bottom: 2rem; }
  .step-card {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.09); border-radius: var(--r20); padding: 1.75rem;
    transition: transform .22s;
  }
  .step-card:hover { transform: translateY(-4px); background: rgba(255,255,255,0.06); }
  .step-icon-circle {
    width: 48px; height: 48px; border-radius: 12px; display: flex; align-items: center; justify-content: center;
    font-size: 1.2rem; color: #fff; margin-bottom: 1rem;
  }
  .ic-indigo { background: linear-gradient(135deg, var(--indigo), var(--indigo-l)); box-shadow: 0 6px 20px rgba(79,70,229,0.45); }
  .ic-violet { background: linear-gradient(135deg, var(--violet), var(--violet-l)); box-shadow: 0 6px 20px rgba(124,58,237,0.45); }
  .ic-emerald { background: linear-gradient(135deg, var(--emerald), #10b981); box-shadow: 0 6px 20px rgba(5,150,105,0.4); }
  .step-num { font-size: .6rem; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; color: rgba(255,255,255,0.3); margin-bottom: .4rem; }
  .step-title { font-family: 'Plus Jakarta Sans', sans-serif; font-size: .95rem; font-weight: 800; color: #fff; margin-bottom: .4rem; }
  .step-desc { font-size: .82rem; line-height: 1.6; color: rgba(255,255,255,0.5); }

  /* ── Bank boost card ── */
  .bank-boost-card {
    display: flex; align-items: center; gap: 1.75rem; margin-top: 1.5rem;
    background: linear-gradient(135deg, rgba(79,70,229,0.12), rgba(124,58,237,0.08));
    border: 1px solid rgba(255,255,255,0.10); border-radius: var(--r20); padding: 1.5rem 1.75rem;
    box-shadow: 0 20px 48px rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.06);
  }
  .bank-boost-left { flex: 1; text-align: left; }
  .bank-boost-badge {
    display: inline-flex; align-items: center; font-size: .62rem; font-weight: 800; letter-spacing: .09em; text-transform: uppercase;
    color: var(--amber-l, #fbbf24); background: rgba(217,119,6,0.14); border: 1px solid rgba(217,119,6,0.28);
    padding: .25rem .65rem; border-radius: 999px; margin-bottom: .75rem;
  }
  .bank-boost-title { font-family: 'Plus Jakarta Sans', sans-serif; font-size: 1.15rem; font-weight: 800; color: #fff; margin-bottom: .5rem; }
  .bank-boost-desc { font-size: .84rem; line-height: 1.6; color: rgba(255,255,255,0.55); margin: 0; }
  .bank-boost-ic {
    width: 64px; height: 64px; border-radius: 18px; display: flex; align-items: center; justify-content: center;
    font-size: 1.6rem; color: #fff; background: linear-gradient(135deg, var(--indigo), var(--violet));
    box-shadow: 0 10px 24px rgba(79,70,229,0.4);
  }
  @media (max-width: 768px) {
    .bank-boost-card { flex-direction: column-reverse; text-align: center; }
    .bank-boost-left { text-align: center; }
    .bank-boost-badge { margin-left: auto; margin-right: auto; }
  }
</style>

<!-- Greeting -->
<div class="mb-4" style="padding-top:.75rem;">
  <h2 class="dash-greeting">Welcome, {{ name }} &#128075;</h2>
  <p class="dash-sub">Your master bank is ready — paste a JD below and land your next interview.</p>
</div>

{% if not (has_bank and has_template and has_ai) %}
<div class="setup-card">
  <div class="setup-eyebrow">Setup checklist</div>
  <div class="setup-heading">Complete setup to start generating CVs</div>

  <div class="setup-step {% if has_ai %}opacity-50{% endif %}">
    <div class="setup-step-num {% if has_ai %}done{% endif %}">
      {% if has_ai %}<i class="bi bi-check-lg" style="font-size:.75rem;"></i>{% else %}1{% endif %}
    </div>
    <div>
      <div class="setup-step-title {% if has_ai %}done{% endif %}">
        {% if has_ai %}Add your API key{% else %}<a href="/settings" style="color:var(--amber-l);text-decoration:none;">Add your Anthropic API key &rarr;</a>{% endif %}
      </div>
      <div class="setup-step-desc">
        Go to <a href="https://console.anthropic.com/settings/keys" target="_blank" style="color:var(--amber-l);opacity:.8;">console.anthropic.com</a> &rarr; copy your key &rarr; paste in Settings. ~$0.02 per CV.
      </div>
    </div>
  </div>

  <div class="setup-step {% if has_bank %}opacity-50{% endif %}">
    <div class="setup-step-num {% if has_bank %}done{% endif %}">
      {% if has_bank %}<i class="bi bi-check-lg" style="font-size:.75rem;"></i>{% else %}2{% endif %}
    </div>
    <div>
      <div class="setup-step-title {% if has_bank %}done{% endif %}">
        {% if has_bank %}Build your experience bank{% else %}<a href="/bank/create" style="color:var(--amber-l);text-decoration:none;">Build your experience bank &rarr;</a>{% endif %}
      </div>
      <div class="setup-step-desc">
        Upload your existing CV (.docx/.pdf) or paste your experience — AI extracts every role, bullet, and skill automatically.
      </div>
    </div>
  </div>

  <div class="setup-step {% if has_template %}opacity-50{% endif %}">
    <div class="setup-step-num {% if has_template %}done{% endif %}">
      {% if has_template %}<i class="bi bi-check-lg" style="font-size:.75rem;"></i>{% else %}3{% endif %}
    </div>
    <div>
      <div class="setup-step-title {% if has_template %}done{% endif %}">
        {% if has_template %}Upload your CV template{% else %}<a href="/upload-template" style="color:var(--amber-l);text-decoration:none;">Upload your CV template (.docx) &rarr;</a>{% endif %}
      </div>
      <div class="setup-step-desc">
        Your formatted base CV — the app preserves its exact layout, bullet counts, and sections.
      </div>
    </div>
  </div>
</div>
{% endif %}

<!-- Status cards -->
<div class="status-cards">
  <div class="status-card" style="{% if has_bank %}border-top:3px solid var(--indigo);{% endif %}">
    <div class="status-card-top">
      <div class="status-icon si-bank"><i class="bi bi-database-fill"></i></div>
      <div>
        <div class="status-card-name">Info Bank</div>
        <div class="status-dot-row">
          <span class="sdot2 {{ 'sdot2-ok' if has_bank else 'sdot2-no' }}"></span>
          <span style="font-size:.72rem;color:var(--muted);">{{ 'Active' if has_bank else 'Not set up' }}</span>
        </div>
      </div>
    </div>
    <p class="status-card-desc">{{ 'Your experience &amp; bullets are ready.' if has_bank else 'Upload your CV or paste your experience.' }}</p>
    {% if has_bank %}
      <div class="d-grid gap-1 mt-auto">
        <a href="/bank" class="btn btn-ghost btn-sm">View Bank</a>
        <a href="/bank/download" class="btn btn-ghost btn-sm">Download JSON</a>
      </div>
    {% else %}
      <a href="/bank/create" class="btn btn-indig btn-sm mt-auto" style="display:block;text-align:center;">Set up Bank</a>
    {% endif %}
  </div>

  <div class="status-card" style="{% if has_template %}border-top:3px solid var(--emerald);{% endif %}">
    <div class="status-card-top">
      <div class="status-icon si-tpl"><i class="bi bi-file-earmark-word-fill"></i></div>
      <div>
        <div class="status-card-name">CV Template</div>
        <div class="status-dot-row">
          <span class="sdot2 {{ 'sdot2-ok' if has_template else 'sdot2-no' }}"></span>
          <span style="font-size:.72rem;color:var(--muted);">{{ 'Uploaded' if has_template else 'Not uploaded' }}</span>
        </div>
      </div>
    </div>
    <p class="status-card-desc">{{ 'Template ready — format preserved on generation.' if has_template else 'Upload your base .docx template.' }}</p>
    <a href="/upload-template" class="btn {{ 'btn-ghost' if has_template else 'btn-success-custom' }} btn-sm mt-auto" style="display:block;text-align:center;">
      {{ 'Replace Template' if has_template else 'Upload Template' }}
    </a>
  </div>

  <div class="status-card" style="{% if has_ai %}border-top:3px solid var(--amber);{% endif %}">
    <div class="status-card-top">
      <div class="status-icon si-key"><i class="bi bi-key-fill"></i></div>
      <div>
        <div class="status-card-name">API Key</div>
        <div class="status-dot-row">
          <span class="sdot2 {{ 'sdot2-ok' if has_ai else 'sdot2-no' }}"></span>
          <span style="font-size:.72rem;color:var(--muted);">{{ ai_label + ' connected' if has_ai else 'Not configured' }}</span>
        </div>
      </div>
    </div>
    <p class="status-card-desc">{{ 'Your AI key is encrypted and ready.' if has_ai else 'Add your Anthropic / OpenAI / Gemini key.' }}</p>
    <a href="/settings" class="btn {{ 'btn-ghost' if has_ai else 'btn-gold' }} btn-sm mt-auto" style="display:block;text-align:center;">
      {{ 'Change Settings' if has_ai else 'Add API Key' }}
    </a>
  </div>
</div>

{% if has_bank and has_template and has_ai %}
<div class="generate-wrap">
  <div style="height:3px;background:linear-gradient(90deg,var(--indigo),var(--violet),var(--amber),var(--emerald));"></div>
  <div class="generate-inner">
    <div class="generate-title">
      <span style="font-size:1.3rem;">&#10024;</span>Generate your tailored CV
    </div>
    <p class="generate-sub">
      Paste the full job description below — the more detail, the better the tailoring.
    </p>
    <form method="post" action="/generate" id="genForm">
      <div class="mb-3">
        <textarea name="jd_text" class="form-control" rows="14"
          style="min-height:280px;font-family:'Inter',sans-serif;font-size:.86rem;border-radius:var(--r12);background:#fafbff;"
          placeholder="Paste the full job description here — include role title, responsibilities, requirements, and any keywords you spot…" required></textarea>
      </div>
      <button class="btn-generate" type="submit" id="genBtn">
        <span id="genBtnDefault">&#10024; Tailor my CV for this role &rarr;</span>
        <span id="genBtnLoading" style="display:none;align-items:center;justify-content:center;gap:.6rem;">
          <span style="display:inline-block;width:18px;height:18px;border:2.5px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:ccSpin .7s linear infinite;"></span>
          Building your tailored CV&hellip;
        </span>
      </button>
    </form>
  </div>
</div>
{% endif %}

<!-- How it works (Educational Footer) -->
<div class="dash-how text-center">
  <div class="section-eyebrow mb-3">Recall the workflow</div>
  <h3 class="how-heading">Building your tailored CV</h3>
  <p class="how-sub">Reference these steps if you ever get stuck or want to sharpen your tailoring.</p>

  <div class="row g-3">
    <div class="col-md-4">
      <div class="step-card">
        <div class="step-icon-circle ic-indigo"><i class="bi bi-database-fill"></i></div>
        <div class="step-num">Step 01</div>
        <div class="step-title">Master Bank</div>
        <div class="step-desc">Keep your full experience history in your Bank — unstructured and raw.</div>
      </div>
    </div>
    <div class="col-md-4">
      <div class="step-card">
        <div class="step-icon-circle ic-violet"><i class="bi bi-file-earmark-text-fill"></i></div>
        <div class="step-num">Step 02</div>
        <div class="step-title">Paste JD</div>
        <div class="step-desc">Paste the JD above. AI reads the language and key requirements.</div>
      </div>
    </div>
    <div class="col-md-4">
      <div class="step-card">
        <div class="step-icon-circle ic-emerald"><i class="bi bi-download"></i></div>
        <div class="step-num">Step 03</div>
        <div class="step-title">Download</div>
        <div class="step-desc">Get a Word + PDF with bullets rewritten in perfect STAR format.</div>
      </div>
    </div>
  </div>

  <div class="bank-boost-card">
    <div class="bank-boost-left">
      <div class="bank-boost-badge">⭐️ Optional · Highly Recommended</div>
      <div class="bank-boost-title">Want sharper tailoring? Build a master bank.</div>
      <p class="bank-boost-desc">
        Collate every role, project, and achievement you've ever had — including things that didn't fit on your original CV.
        When you paste a JD, the AI can dip into your bank and surface the <strong>most relevant</strong> experience for that specific role.
      </p>
    </div>
    <div class="bank-boost-right">
      <div class="bank-boost-ic"><i class="bi bi-layers-half"></i></div>
    </div>
  </div>
</div>
""").replace("{% block scripts %}{% endblock %}", """
<script>
(function() {
  const form = document.getElementById('genForm');
  if (!form) return;
  form.addEventListener('submit', function() {
    // In-button spinner (always visible even if overlay has issues)
    const btn       = document.getElementById('genBtn');
    const btnDef    = document.getElementById('genBtnDefault');
    const btnLoad   = document.getElementById('genBtnLoading');
    if (btn)     btn.disabled = true;
    if (btnDef)  btnDef.style.display = 'none';
    if (btnLoad) btnLoad.style.display = 'inline-flex';
    // Full-screen overlay (richer feedback)
    const ov = document.getElementById('loadingOverlay');
    const ot = document.getElementById('overlayTitle');
    const os = document.getElementById('overlaySub');
    if (ot) ot.textContent = 'Building your tailored CV\u2026';
    if (os) os.textContent = "Analysing the JD, writing bullets, and building your .docx + .pdf \u2014 ~30\u201360 seconds";
    if (ov) ov.style.display = 'flex';
  });
})();
</script>
""")


# ── Upload template ────────────────────────────────────────────────────────────

_UPLOAD_TPL = _BASE.replace("{% block content %}{% endblock %}", """
<div style="max-width:520px;margin:0 auto;">
  <div class="mb-3">
    <a href="/dashboard" style="display:inline-flex;align-items:center;gap:.4rem;font-size:.82rem;font-weight:600;color:var(--muted);text-decoration:none;padding:.35rem .75rem;border:1.5px solid var(--border);border-radius:var(--r10);background:var(--surface);transition:background .18s,border-color .18s;"
       onmouseover="this.style.background='var(--bg)';this.style.borderColor='rgba(15,23,42,.2)'"
       onmouseout="this.style.background='var(--surface)';this.style.borderColor='var(--border)'">
      <i class="bi bi-arrow-left"></i> Dashboard
    </a>
  </div>
  <div class="card p-4">
    <div class="d-flex align-items-center gap-3 mb-3">
      <div style="width:44px;height:44px;background:linear-gradient(135deg,var(--emerald),#10b981);border-radius:12px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:1.2rem;flex-shrink:0;">
        <i class="bi bi-file-earmark-word"></i>
      </div>
      <div>
        <h5 style="font-weight:800;font-size:1.1rem;color:var(--navy);margin:0;">Upload CV Template</h5>
        <p style="color:var(--muted);font-size:.8rem;margin:0;">Your base .docx — preserved on every generation</p>
      </div>
    </div>
    <p style="color:var(--muted);font-size:.85rem;line-height:1.6;margin-bottom:1rem;">
      Upload the <code style="background:#eef2ff;color:var(--indigo);border-radius:4px;padding:.1rem .35rem;">.docx</code> CV you normally use and tweak by hand.
      This becomes the format for every tailored CV we generate — layout, fonts, and structure are preserved exactly.
    </p>
    <div style="background:#eef2ff;border:1px solid rgba(79,70,229,0.2);border-radius:var(--r10);padding:.75rem 1rem;font-size:.8rem;color:#312e81;margin-bottom:1.5rem;">
      <strong>Tip:</strong> Make sure your template has bullet points under each role — the app auto-detects
      how many bullet slots to fill per section. No hardcoding required.
    </div>
    <form method="post" action="/upload-template" enctype="multipart/form-data"
          data-loading
          data-loading-title="Uploading & analysing template…"
          data-loading-sub="Extracting font, bullet slots and formatting rules — ~10 seconds">
      <div class="mb-4">
        <label class="fl">CV template (.docx)</label>
        <div class="import-zone" onclick="document.getElementById('tplFileInput').click()">
          <i class="bi bi-cloud-upload" style="font-size:2rem;color:var(--indigo);display:block;margin-bottom:.6rem;"></i>
          <div style="font-weight:600;font-size:.9rem;color:var(--navy);">Click to choose your .docx file</div>
          <div style="color:var(--muted);font-size:.78rem;margin-top:.25rem;" id="tplFileLabel">or drag and drop here</div>
        </div>
        <input id="tplFileInput" name="template_file" type="file" accept=".docx"
          class="d-none" required
          onchange="document.getElementById('tplFileLabel').textContent = this.files[0].name">
      </div>
      <button type="submit" class="btn-indig w-100" style="display:block;text-align:center;">
        <i class="bi bi-upload me-1"></i> Upload Template
      </button>
    </form>
  </div>
</div>
""")


# ── Settings ───────────────────────────────────────────────────────────────────

_SETTINGS = _BASE.replace("{% block content %}{% endblock %}", """
<div style="max-width:560px;margin:0 auto;">
  <div class="mb-3">
    <a href="/dashboard" style="display:inline-flex;align-items:center;gap:.4rem;font-size:.82rem;font-weight:600;color:var(--muted);text-decoration:none;padding:.35rem .75rem;border:1.5px solid var(--border);border-radius:var(--r10);background:var(--surface);transition:background .18s,border-color .18s;"
       onmouseover="this.style.background='var(--bg)';this.style.borderColor='rgba(15,23,42,.2)'"
       onmouseout="this.style.background='var(--surface)';this.style.borderColor='var(--border)'">
      <i class="bi bi-arrow-left"></i> Dashboard
    </a>
  </div>
  <div class="card p-4">
    <div class="d-flex align-items-center gap-3 mb-4">
      <div style="width:44px;height:44px;background:linear-gradient(135deg,var(--gold),var(--gold-l));border-radius:12px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:1.2rem;flex-shrink:0;">
        <i class="bi bi-key"></i>
      </div>
      <div>
        <h5 style="font-weight:800;font-size:1.1rem;color:var(--navy);margin:0;">AI Settings</h5>
        <p style="color:var(--muted);font-size:.8rem;margin:0;">Encrypted at rest — you pay your provider directly</p>
      </div>
    </div>

    <!-- Info box -->
    <div style="background:#fffbeb;border:1px solid rgba(217,119,6,0.25);border-radius:var(--r10);padding:.9rem 1.1rem;font-size:.8rem;color:#78350f;margin-bottom:1.5rem;">
      <strong>Recommended: Anthropic Claude Sonnet 4.6</strong> — best CV quality, ~$0.01–0.03 per tailoring.&nbsp;
      <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener" style="color:var(--gold);">Get Anthropic key &rarr;</a>
      <hr style="border-color:rgba(217,119,6,0.2);margin:.65rem 0;">
      Other providers:&nbsp;
      <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener" style="color:var(--gold);">OpenAI</a> (GPT-4o) &nbsp;&middot;&nbsp;
      <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener" style="color:var(--gold);">Google Gemini</a>
      <br><span style="opacity:.7;">Haiku / Flash models are cheapest but slightly weaker on CV rewriting.</span>
    </div>

    <form method="post" action="/settings"
          data-loading
          data-loading-title="Saving AI settings…"
          data-loading-sub="Encrypting and storing your API key securely">
      <!-- Provider selector cards -->
      <div class="mb-4">
        <label class="fl">AI Provider</label>
        <input type="hidden" name="provider" id="providerHidden" value="{{ current_provider }}">
        <div class="d-flex flex-column gap-2" id="providerCards">
          {% for pid, p in providers.items() %}
          <div class="provider-card {{ 'selected' if pid == current_provider }}"
               data-pid="{{ pid }}"
               onclick="selectProvider('{{ pid }}')">
            <div style="display:flex;align-items:center;justify-content:space-between;">
              <div>
                <div style="font-weight:700;font-size:.88rem;color:var(--navy);">{{ p.label }}</div>
                <div style="font-size:.75rem;color:var(--muted);">{{ p.key_placeholder }}</div>
              </div>
              <div class="provider-check-{{ pid }}" style="display:{% if pid == current_provider %}flex{% else %}none{% endif %};width:22px;height:22px;background:var(--indigo);border-radius:50%;align-items:center;justify-content:center;color:#fff;font-size:.7rem;">
                <i class="bi bi-check-lg"></i>
              </div>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>

      <!-- Model -->
      <div class="mb-3">
        <label class="fl">Model</label>
        <select name="model" class="form-select" id="modelSelect">
          {% for pid, p in providers.items() %}
            {% for mid, mlabel in p.models %}
              <option value="{{ mid }}" data-provider="{{ pid }}"
                {{ 'selected' if mid == current_model }}>{{ mlabel }}</option>
            {% endfor %}
          {% endfor %}
        </select>
      </div>

      <!-- API Key -->
      <div class="mb-4">
        <label class="fl">API Key</label>
        <div style="position:relative;">
          <input name="api_key" type="password" id="apiKeyInput" class="form-control"
            style="padding-right:2.75rem;"
            placeholder="{{ providers[current_provider].key_placeholder if current_provider else 'sk-ant-…' }}"
            autocomplete="off">
          <button type="button" onclick="toggleApiKey()" tabindex="-1"
            style="position:absolute;right:.6rem;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--muted);cursor:pointer;padding:.2rem;">
            <i class="bi bi-eye" id="apiKeyEyeIcon"></i>
          </button>
        </div>
        {% if has_key %}
          <div style="font-size:.75rem;color:var(--emerald);margin-top:.4rem;display:flex;align-items:center;gap:.3rem;">
            <i class="bi bi-check-circle-fill"></i> Key saved — enter a new value to replace it
          </div>
        {% endif %}
      </div>

      <button type="submit" class="btn-indig w-100" style="display:block;text-align:center;">
        <i class="bi bi-floppy me-1"></i> Save Settings
      </button>
    </form>
  </div>
</div>
""").replace("{% block scripts %}{% endblock %}", """
<script>
const providerModels = {{ provider_models_json | safe }};

function selectProvider(pid) {
  document.getElementById('providerHidden').value = pid;
  document.querySelectorAll('.provider-card').forEach(c => {
    const cpid = c.dataset.pid;
    const isSelected = cpid === pid;
    c.classList.toggle('selected', isSelected);
    const check = document.querySelector('.provider-check-' + cpid);
    if (check) check.style.display = isSelected ? 'flex' : 'none';
  });
  updateModelList(pid);
}

function updateModelList(pid) {
  if (!pid) pid = document.getElementById('providerHidden').value;
  const sel = document.getElementById('modelSelect');
  [...sel.options].forEach(o => {
    o.style.display = o.dataset.provider === pid ? '' : 'none';
    if (o.dataset.provider !== pid) o.selected = false;
  });
  const opts = [...sel.options].filter(o => o.dataset.provider === pid);
  if (opts.length) opts[0].selected = true;
}

function toggleApiKey() {
  const inp = document.getElementById('apiKeyInput');
  const ico = document.getElementById('apiKeyEyeIcon');
  if (inp.type === 'password') {
    inp.type = 'text';
    ico.className = 'bi bi-eye-slash';
  } else {
    inp.type = 'password';
    ico.className = 'bi bi-eye';
  }
}

updateModelList();
</script>
""")


# ── Bank create / import (shared template, mode differs) ─────────────────────

_BANK_CREATE = _BASE.replace("{% block content %}{% endblock %}", """
<!-- Header -->
<div class="d-flex align-items-center gap-3 mb-4">
  <a href="{{ back_url }}" style="display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border:1.5px solid var(--border);border-radius:var(--r10);color:var(--muted);text-decoration:none;background:var(--surface);transition:background .18s,border-color .18s;flex-shrink:0;"
     onmouseover="this.style.background='var(--bg)';this.style.borderColor='rgba(15,23,42,.2)'"
     onmouseout="this.style.background='var(--surface)';this.style.borderColor='var(--border)'">
    <i class="bi bi-arrow-left"></i>
  </a>
  <div>
    <h4 style="font-weight:800;font-size:1.3rem;color:var(--navy);margin:0;letter-spacing:-.03em;">{{ page_title }}</h4>
    <div style="color:var(--muted);font-size:.82rem;margin-top:.1rem;">{{ page_subtitle }}</div>
  </div>
</div>

{% if not has_ai %}
<div class="alert alert-warning mb-4">
  <i class="bi bi-exclamation-triangle me-1"></i>
  <strong>API key needed for AI parsing.</strong>
  <a href="/settings" class="alert-link">Add your API key</a> first, then come back here.
</div>
{% endif %}

<!-- Custom tab nav -->
<div class="cc-tabs" id="createTabs">
  <button class="cc-tab-btn active" data-cc-tab="tab-file">
    <i class="bi bi-file-earmark-arrow-up"></i>Upload a file
  </button>
  <button class="cc-tab-btn" data-cc-tab="tab-text">
    <i class="bi bi-textarea-t"></i>Paste text
  </button>
</div>

<div class="card" style="border-radius:0 0 var(--r16) var(--r16)!important;border-top:none;">
  <div class="card-body p-4">

    <!-- File upload tab -->
    <div class="cc-tab-pane" id="tab-file">
      <h6 style="font-weight:700;color:var(--navy);margin-bottom:.4rem;">Upload information to create your master bank</h6>
      <p style="color:var(--muted);font-size:.85rem;line-height:1.6;margin-bottom:1rem;">
        Any format works: a Word doc of notes, a PDF, or a plain text file. Please upload your whole-life professional experience, extra side projects, extra certifications, and any other details not already on your CV that might be helpful to generate and tailor a new CV to a job description (for example, you can upload your 10-page CV if you'd like!).
        AI will read it and extract every role, project, and skill automatically.
        Accepted: <strong>.docx &middot; .pdf &middot; .txt</strong>
      </p>
      <form method="post" action="{{ file_action }}" enctype="multipart/form-data" id="fileForm">
        <div class="import-zone mb-3" onclick="document.getElementById('cvFileInput').click()">
          <i class="bi bi-cloud-upload" style="font-size:2.2rem;color:var(--indigo);display:block;margin-bottom:.6rem;"></i>
          <div style="font-weight:600;font-size:.9rem;color:var(--navy);">Click to choose a file</div>
          <div style="color:var(--muted);font-size:.78rem;margin-top:.25rem;" id="fileLabel">or drag and drop here (.docx &middot; .pdf &middot; .txt)</div>
        </div>
        <input id="cvFileInput" name="cv_file" type="file" accept=".docx,.pdf,.txt"
          class="d-none" required onchange="document.getElementById('fileLabel').textContent = this.files[0].name">
        <button class="btn-indig w-100" style="display:block;text-align:center;" {{ 'disabled' if not has_ai }}
          onclick="showLoading('Parsing your file with AI\u2026', 'Extracting roles, bullets, and skills \u2014 ~20\u201330 seconds')">
          <i class="bi bi-magic me-1"></i>Parse with AI &amp; {{ action_verb }} Bank
        </button>
      </form>
    </div>

    <!-- Text paste tab -->
    <div class="cc-tab-pane" id="tab-text" style="display:none;">
      <h6 style="font-weight:700;color:var(--navy);margin-bottom:.4rem;">Paste your experience &mdash; any format works</h6>
      <p style="color:var(--muted);font-size:.85rem;line-height:1.6;margin-bottom:.75rem;">
        Paste your LinkedIn text, rough bullet points, or a brain-dump of everything you've done (e.g. you can paste your 10-page CV if you'd like!).
        Don't worry about formatting &mdash; AI structures it all automatically.
      </p>
      <div class="star-guide mb-3">
        <strong>Include as much detail as possible</strong> &mdash; the more context and numbers you give,
        the stronger the tailored bullets will be. Metrics ($, %, team sizes, rankings) are especially valuable.
        <br>Don't have polished bullets? That's fine &mdash; just describe what you did and what happened.
      </div>
      <form method="post" action="{{ text_action }}" id="textForm">
        <textarea name="cv_text" class="form-control mb-3" rows="16"
          placeholder="Paste anything here — rough notes, LinkedIn text, old CV, bullet points. Examples:

--- Option A: Jobs + projects ---
Acme Corp — Marketing Analyst (Jan 2023 – Jun 2024)
Ran paid campaigns across Google and Meta, managing $500K annual budget.
Improved conversion rate by 18% through A/B testing landing pages.

Personal project: Customer churn prediction model (2024)
Built XGBoost classifier on 50K row dataset, achieving 84% recall.

--- Option B: Finance / consulting ---
Big 4 Firm — Audit Associate (Aug 2022 – Present)
Led statutory audits for 4 mid-cap clients in healthcare and retail sectors.
Identified £1.2M in revenue recognition errors across two engagements.

--- Skills ---
Excel (advanced), Python (pandas, sklearn), SQL, PowerPoint, Tableau
Certifications: ACCA Part-Qualified, Google Analytics" {{ 'disabled' if not has_ai }} required></textarea>
        <button class="btn-indig w-100" style="display:block;text-align:center;" {{ 'disabled' if not has_ai }}
          onclick="showLoading('Parsing your experience with AI\u2026', 'Extracting roles, bullets, and skills \u2014 ~20\u201330 seconds')">
          <i class="bi bi-magic me-1"></i>Parse with AI &amp; {{ action_verb }} Bank
        </button>
      </form>
    </div>



  </div>
</div>
""").replace("{% block scripts %}{% endblock %}", """
<script>
// Custom tab switching
document.querySelectorAll('.cc-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.cc-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.cc-tab-pane').forEach(p => p.style.display = 'none');
    btn.classList.add('active');
    document.getElementById(btn.dataset.ccTab).style.display = 'block';
  });
});

// Style the active radio type label
function styleTypeLabels() {
  const checked = document.querySelector('input[name="section_type"]:checked');
  document.querySelectorAll('label[for="type-job"], label[for="type-project"]').forEach(l => {
    const isActive = l.getAttribute('for') === checked?.id;
    l.style.borderColor = isActive ? 'var(--indigo)' : 'var(--border)';
    l.style.background  = isActive ? '#eef2ff' : 'var(--bg)';
    l.style.color       = isActive ? 'var(--indigo)' : 'var(--navy)';
  });
}
document.querySelectorAll('input[name="section_type"]').forEach(r => {
  r.addEventListener('change', () => {
    if (!r.checked) return;
    styleTypeLabels();
    const job = r.value === 'job';
    document.getElementById('company-field').style.display     = job  ? '' : 'none';
    document.getElementById('role-field').style.display        = job  ? '' : 'none';
    document.getElementById('project-field').style.display     = job  ? 'none' : '';
    document.getElementById('type-hint-job').style.display     = job  ? '' : 'none';
    document.getElementById('type-hint-project').style.display = job  ? 'none' : '';
  });
});
styleTypeLabels();

function showLoading(title, sub) {
  if (!{{ 'true' if has_ai else 'false' }}) return;
  document.getElementById('overlayTitle').textContent = title;
  document.getElementById('overlaySub').textContent   = sub;
  document.getElementById('loadingOverlay').style.display = 'flex';
}

// Drag-and-drop on import zone
const zone = document.querySelector('.import-zone');
if (zone) {
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => { zone.classList.remove('drag-over'); });
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    const f = e.dataTransfer.files[0];
    if (f) {
      document.getElementById('cvFileInput').files = e.dataTransfer.files;
      document.getElementById('fileLabel').textContent = f.name;
    }
  });
}
</script>
""")


# ── Bank editor ────────────────────────────────────────────────────────────────

_BANK = _BASE.replace("{% block content %}{% endblock %}", """
<!-- Header row -->
<div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
  <div>
    <h4 style="font-weight:800;font-size:1.4rem;color:var(--navy);margin:0;letter-spacing:-.03em;">
      <i class="bi bi-database me-2" style="color:var(--indigo);"></i>Master Info Bank
    </h4>
    <p style="color:var(--muted);font-size:.82rem;margin:.2rem 0 0;">Your library of experience — the source of every tailored CV</p>
  </div>
  <div class="d-flex gap-2 flex-wrap">
    {% if bank %}
    <a href="/bank/download" class="btn-ghost" style="font-size:.8rem;padding:.4rem .9rem;border-radius:var(--r10);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem;">
      <i class="bi bi-download"></i>Download JSON
    </a>
    {% endif %}
  </div>
</div>

{% if not bank %}
<div class="card p-5 text-center">
  <i class="bi bi-database-x" style="font-size:2.5rem;color:var(--muted);display:block;margin-bottom:.75rem;"></i>
  <p style="color:var(--muted);margin-bottom:1.25rem;">No info bank yet. Upload your CV or start from scratch.</p>
  <a href="/bank/create" class="btn-indig" style="display:inline-block;padding:.6rem 1.5rem;border-radius:var(--r10);text-decoration:none;">Set up your Bank</a>
</div>
{% else %}

<!-- Replace bank — primary action, top of page -->
<div class="card mb-3" style="border-left:4px solid var(--indigo)!important;">
  <div class="card-header" style="font-weight:700;color:var(--navy);display:flex;align-items:center;gap:.4rem;">
    <i class="bi bi-arrow-repeat" style="color:var(--indigo);"></i>Replace master file
    <span style="font-size:.72rem;color:var(--muted);font-weight:400;margin-left:.35rem;">Upload a new file to overwrite everything below</span>
  </div>
  <div class="card-body p-3">
    <ul class="nav nav-pills mb-2" role="tablist" style="gap:.4rem;">
      <li class="nav-item"><button class="nav-link active" data-bs-toggle="pill" data-bs-target="#replace-file" type="button" style="font-size:.8rem;padding:.3rem .75rem;">File upload</button></li>
      <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#replace-text" type="button" style="font-size:.8rem;padding:.3rem .75rem;">Paste text</button></li>
    </ul>
    <div class="tab-content">
      <div class="tab-pane fade show active" id="replace-file">
        <form method="post" action="/bank/from-file" enctype="multipart/form-data"
              data-loading data-loading-title="Replacing your bank…" data-loading-sub="Parsing your file with AI">
          <input type="file" name="cv_file" class="form-control mb-2" style="font-size:.82rem;"
                 accept=".docx,.pdf,.txt,.json" required>
          <div style="font-size:.74rem;color:var(--muted);margin-bottom:.6rem;">Accepts .docx, .pdf, .txt, or .json (round-trip)</div>
          <button class="btn-indig" style="font-size:.82rem;padding:.4rem .9rem;border-radius:var(--r10);"
                  data-quick-action>Replace master file</button>
        </form>
      </div>
      <div class="tab-pane fade" id="replace-text">
        <form method="post" action="/bank/from-text"
              data-loading data-loading-title="Replacing your bank…" data-loading-sub="Parsing text with AI">
          <textarea name="cv_text" class="form-control mb-2" rows="6" required
            style="font-size:.82rem;" placeholder="Paste your full CV / experience text here…"></textarea>
          <button class="btn-indig" style="font-size:.82rem;padding:.4rem .9rem;border-radius:var(--r10);"
                  data-quick-action>Replace master file</button>
        </form>
      </div>
    </div>
  </div>
</div>

<!-- Bank contents — read-only scrollable view -->
<div class="card mb-3">
  <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
    <span style="font-weight:700;color:var(--navy);display:flex;align-items:center;gap:.4rem;">
      <i class="bi bi-journal-text" style="color:var(--indigo);"></i>Your master bank
    </span>
    <span style="font-size:.72rem;color:var(--muted);">
      {{ (bank.sections or {})|length }} section(s) &middot;
      {{ (bank.certifications or [])|length }} cert(s) &middot; read-only
    </span>
  </div>
  <div class="card-body p-0">
    <div style="max-height:560px;overflow-y:auto;padding:1rem 1.1rem;">

      {% if bank.ai_summary %}
      <div style="background:linear-gradient(135deg,#eef2ff,#f5f3ff);border:1px solid rgba(79,70,229,0.18);border-radius:var(--r10);padding:.8rem 1rem;margin-bottom:1.1rem;">
        <div style="font-size:.68rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--indigo);margin-bottom:.3rem;">Summary</div>
        <p style="font-size:.85rem;line-height:1.6;color:var(--text);margin:0;">{{ bank.ai_summary }}</p>
      </div>
      {% endif %}

      {% if bank.sections %}
      <div style="font-size:.68rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:.3rem 0 .6rem;">Experience &amp; projects</div>
      {% for key, sec in bank.sections.items() %}
      <div style="padding:.7rem .9rem;border:1px solid var(--border);border-radius:var(--r10);margin-bottom:.6rem;background:var(--surface);">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap;margin-bottom:.4rem;">
          <div style="font-weight:700;color:var(--navy);font-size:.9rem;">
            {% if sec.company %}
              <i class="bi bi-briefcase me-1" style="color:var(--indigo);"></i>{{ sec.company }}
              {% if sec.role %}<span style="color:var(--muted);font-weight:500;font-size:.82rem;"> &middot; {{ sec.role }}</span>{% endif %}
            {% elif sec.project_name %}
              <i class="bi bi-layers me-1" style="color:var(--emerald);"></i>{{ sec.project_name }}
            {% else %}
              <i class="bi bi-card-text me-1" style="color:var(--muted);"></i>{{ key }}
            {% endif %}
          </div>
          {% if sec.date %}<span style="color:var(--muted);font-size:.76rem;">{{ sec.date }}</span>{% endif %}
        </div>
        {% if sec.bullets %}
        <ul style="margin:0;padding-left:1.1rem;">
          {% for b in sec.bullets %}
          <li style="font-size:.82rem;line-height:1.55;color:var(--text);margin-bottom:.2rem;">{{ b.text }}</li>
          {% endfor %}
        </ul>
        {% else %}
        <div style="font-size:.78rem;color:var(--muted);font-style:italic;">No bullets</div>
        {% endif %}
      </div>
      {% endfor %}
      {% endif %}

      {% if bank.skills_text %}
      <div style="font-size:.68rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:1rem 0 .5rem;">Skills</div>
      <div style="padding:.7rem .9rem;border:1px solid var(--border);border-radius:var(--r10);background:var(--surface);">
        <pre style="white-space:pre-wrap;font-family:'Inter',sans-serif;font-size:.82rem;line-height:1.55;color:var(--text);margin:0;">{{ bank.skills_text }}</pre>
      </div>
      {% endif %}

      {% if bank.certifications %}
      <div style="font-size:.68rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:1rem 0 .5rem;">Certifications</div>
      <div style="padding:.7rem .9rem;border:1px solid var(--border);border-radius:var(--r10);background:var(--surface);">
        <ul style="margin:0;padding-left:1.1rem;">
          {% for c in bank.certifications %}
          <li style="font-size:.82rem;line-height:1.55;color:var(--text);margin-bottom:.15rem;">
            <i class="bi bi-patch-check me-1" style="color:var(--emerald);"></i>{{ c }}
          </li>
          {% endfor %}
        </ul>
      </div>
      {% endif %}

      <div style="margin-top:1rem;font-size:.72rem;color:var(--muted);text-align:center;">
        <i class="bi bi-info-circle me-1"></i>
        This view is read-only. To edit, <a href="/bank/download" style="color:var(--indigo);text-decoration:none;">download the JSON</a>, edit externally, then replace above.
      </div>
    </div>
  </div>
</div>

{% endif %}
<div class="mt-3 d-flex gap-2 flex-wrap">
  <a href="/dashboard" style="font-size:.82rem;color:var(--muted);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem;padding:.35rem .75rem;border:1.5px solid var(--border);border-radius:var(--r10);background:var(--surface);">
    <i class="bi bi-arrow-left"></i> Dashboard
  </a>
</div>
""")


# ── Result / download ─────────────────────────────────────────────────────────

_RESULT = _BASE.replace("{% block content %}{% endblock %}", """
<div style="max-width:500px;margin:0 auto;">
  <div class="card p-4 text-center">
    <!-- Animated success ring -->
    <div style="margin:0 auto 1.25rem;width:72px;height:72px;">
      <svg viewBox="0 0 72 72" style="width:72px;height:72px;">
        <circle cx="36" cy="36" r="30" fill="none" stroke="#e2e8f0" stroke-width="4"/>
        <circle cx="36" cy="36" r="30" fill="none" stroke="var(--emerald)" stroke-width="4"
          stroke-dasharray="188.5" stroke-dashoffset="188.5" stroke-linecap="round"
          style="transform:rotate(-90deg);transform-origin:center;animation:drawRing .8s ease forwards .1s;"/>
        <polyline points="22,36 32,46 50,28" fill="none" stroke="var(--emerald)" stroke-width="4"
          stroke-linecap="round" stroke-linejoin="round"
          stroke-dasharray="40" stroke-dashoffset="40"
          style="animation:drawCheck .4s ease forwards .7s;"/>
      </svg>
    </div>
    <style>
      @keyframes drawRing  { to { stroke-dashoffset: 0; } }
      @keyframes drawCheck { to { stroke-dashoffset: 0; } }
    </style>

    <h4 style="font-weight:800;font-size:1.35rem;color:var(--navy);margin-bottom:.35rem;letter-spacing:-.03em;">Your CV is ready!</h4>
    <p style="color:var(--muted);font-size:.88rem;margin-bottom:.5rem;">
      Tailored for <strong style="color:var(--navy);">{{ company }}</strong> &mdash; <em>{{ role }}</em>
    </p>

    <div style="margin-bottom:1.25rem;"></div>

    <div style="display:grid;gap:.6rem;margin-bottom:1.5rem;">
      {% if has_pdf %}
      <a href="/download/{{ token }}/pdf" class="btn-indig" style="display:block;padding:.75rem;border-radius:var(--r10);text-decoration:none;font-size:.95rem;">
        <i class="bi bi-file-earmark-pdf me-1"></i>Download PDF
      </a>
      {% else %}
      <div style="padding:.7rem;border-radius:var(--r10);font-size:.9rem;border:1.5px solid var(--border);color:var(--muted);text-align:center;">
        <i class="bi bi-file-earmark-pdf me-1"></i>PDF not available (see deployment docs)
      </div>
      {% endif %}
      <a href="/download/{{ token }}/docx" style="display:block;padding:.7rem;border-radius:var(--r10);text-decoration:none;font-size:.9rem;border:1.5px solid var(--indigo);color:var(--indigo);font-weight:600;transition:background .18s;text-align:center;"
         onmouseover="this.style.background='#eef2ff'" onmouseout="this.style.background=''">
        <i class="bi bi-file-earmark-word me-1"></i>Download Word (.docx)
      </a>
    </div>

    <hr style="border-color:var(--border);margin:0 0 1.25rem;">
    <a href="/dashboard" class="btn-success-custom" style="display:block;padding:.65rem;border-radius:var(--r10);text-decoration:none;font-size:.9rem;">
      <i class="bi bi-magic me-1"></i>Tailor for another role &rarr;
    </a>
  </div>
</div>
""")


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — Auth
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template_string(_INDEX)


@app.route("/signup", methods=["POST"])
def signup():
    email    = request.form["email"].strip()
    password = request.form["password"]
    name     = request.form.get("name", "").strip()
    try:
        user_id = sb.sign_up(email, password, name=name)
        # Since email confirmation is off, auto-login using the same logic as signin()
        profile = sb.get_profile(user_id)
        ai_cfg  = sb.load_ai_settings(user_id)
        session.update({
            "user_id":      user_id,
            "email":        email,
            "name":         profile.get("name", email.split("@")[0]),
            "has_bank":     sb.has_master_bank(user_id),
            "has_template": sb.has_cv_template(user_id),
            "has_ai":       bool(ai_cfg.get("api_key_enc")),
            "ai_provider":  ai_cfg.get("provider", ""),
            "ai_model":     ai_cfg.get("model", ""),
        })
        flash("Account created! Welcome.", "success")
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"Sign-up failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/signin", methods=["POST"])
def signin():
    email    = request.form["email"].strip()
    password = request.form["password"]
    try:
        user_id = sb.sign_in(email, password)
        profile = sb.get_profile(user_id)
        ai_cfg  = sb.load_ai_settings(user_id)
        session.update({
            "user_id":      user_id,
            "email":        email,
            "name":         profile.get("name", email.split("@")[0]),
            "has_bank":     sb.has_master_bank(user_id),
            "has_template": sb.has_cv_template(user_id),
            "has_ai":       bool(ai_cfg.get("api_key_enc")),
            "ai_provider":  ai_cfg.get("provider", ""),
            "ai_model":     ai_cfg.get("model", ""),
        })
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"Sign-in failed: {e}", "error")
        return redirect(url_for("index"))


@app.route("/signout")
def signout():
    session.clear()
    return redirect(url_for("index"))


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    has_bank     = sb.has_master_bank(user_id)
    has_template = sb.has_cv_template(user_id)
    ai_cfg       = sb.load_ai_settings(user_id)
    has_ai       = bool(ai_cfg.get("api_key_enc"))
    ai_label     = PROVIDERS.get(ai_cfg.get("provider", ""), {}).get("label", "") if has_ai else ""
    session.update({"has_bank": has_bank, "has_template": has_template, "has_ai": has_ai})

    return render_template_string(
        _DASHBOARD,
        name=session.get("name", ""),
        has_bank=has_bank, has_template=has_template,
        has_ai=has_ai, ai_label=ai_label,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — CV Template
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/upload-template", methods=["GET"])
@login_required
def upload_template_page():
    return render_template_string(_UPLOAD_TPL)


@app.route("/upload-template", methods=["POST"])
@login_required
def upload_template():
    f = request.files.get("template_file")
    if not f or not f.filename.endswith(".docx"):
        flash("Please upload a .docx file.", "error")
        return redirect(url_for("upload_template_page"))
    try:
        tmp = Path(tempfile.mktemp(suffix=".docx"))
        f.save(str(tmp))
        # Extract formatting rules from this specific template before uploading.
        # This runs on every upload (including "Replace Template") so format_rules
        # always reflect the current template — not an older version.
        fmt_rules      = {}
        fmt_desc_parts = []
        try:
            fmt_rules = extract_template_format_rules(tmp)
            if fmt_rules.get("bullet_font"):
                fmt_desc_parts.append(
                    f"{fmt_rules['bullet_font']} {fmt_rules.get('bullet_font_size_pt', '?')}pt"
                )
            if fmt_rules.get("max_bullet_chars"):
                fmt_desc_parts.append(f"{fmt_rules['max_bullet_chars']} chars/bullet")
            if fmt_rules.get("max_skill_lines"):
                fmt_desc_parts.append(f"{fmt_rules['max_skill_lines']} skill lines")
        except Exception:
            pass   # non-fatal — fallback defaults will be used at generate time

        # Cache raw section slot counts so generate() can skip re-downloading
        # the template just to count bullets. Stored inside format_rules JSONB.
        try:
            raw_slots = discover_template_sections(tmp)
            if raw_slots:
                fmt_rules["raw_slots"] = raw_slots
        except Exception:
            pass  # non-fatal

        sb.upload_cv_template(session["user_id"], tmp, format_rules=fmt_rules or None)
        session["has_template"] = True

        fmt_hint = f" Detected: {', '.join(fmt_desc_parts)}." if fmt_desc_parts else ""
        flash(f"CV template uploaded successfully.{fmt_hint}", "success")
    except Exception as e:
        flash(f"Upload failed: {e}", "error")
    return redirect(url_for("dashboard"))


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — AI Settings
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET"])
@login_required
def settings_page():
    user_id    = session["user_id"]
    ai_cfg     = sb.load_ai_settings(user_id)
    curr_prov  = ai_cfg.get("provider", "anthropic")
    curr_model = ai_cfg.get("model", PROVIDERS[curr_prov]["default_model"])
    has_key    = bool(ai_cfg.get("api_key_enc"))
    pm_json    = json.dumps({
        pid: [m[0] for m in p["models"]] for pid, p in PROVIDERS.items()
    })
    return render_template_string(
        _SETTINGS,
        providers=PROVIDERS,
        current_provider=curr_prov,
        current_model=curr_model,
        has_key=has_key,
        provider_models_json=pm_json,
    )


@app.route("/settings", methods=["POST"])
@login_required
def settings_save():
    provider = request.form.get("provider", "").strip()
    model    = request.form.get("model", "").strip()
    raw_key  = request.form.get("api_key", "").strip()
    if provider not in PROVIDERS:
        flash("Invalid provider.", "error")
        return redirect(url_for("settings_page"))
    user_id = session["user_id"]
    if not raw_key:
        ai_cfg  = sb.load_ai_settings(user_id)
        enc_key = ai_cfg.get("api_key_enc", "")
        if not enc_key:
            flash("Please enter your API key.", "error")
            return redirect(url_for("settings_page"))
    else:
        try:
            enc_key = encrypt_key(raw_key)
        except Exception as e:
            flash(f"Encryption error (check ENCRYPT_KEY): {e}", "error")
            return redirect(url_for("settings_page"))
            
    try:
        sb.save_ai_settings(user_id, provider, enc_key, model)
        ai_label = PROVIDERS.get(provider, {}).get("label", provider)
        session.update({
            "has_ai":    True,
            "ai_provider": provider,
            "ai_model":  model,
            "ai_label":  ai_label,
        })
        flash(f"✓ AI settings saved — {ai_label} ({model})", "success")
    except Exception as e:
        flash(f"Database error: {e}", "error")
        
    return redirect(url_for("dashboard"))


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — Master Bank: create & import
# ─────────────────────────────────────────────────────────────────────────────

def _bank_create_context(is_create: bool, has_ai: bool):
    """Shared context dict for the bank create/import template."""
    if is_create:
        return dict(
            page_title="Build your Master Bank",
            page_subtitle="Your library of experience — set it up once, use it forever",
            file_action="/bank/from-file",
            text_action="/bank/from-text",
            action_verb="Create",
            back_url=url_for("dashboard"),
            is_create=True,
            has_ai=has_ai,
        )
    return dict(
        page_title="Import more experience",
        page_subtitle="Add new roles, projects, or skills to your existing bank",
        file_action="/bank/from-file?mode=append",
        text_action="/bank/from-text?mode=append",
        action_verb="Update",
        back_url=url_for("bank_page"),
        is_create=False,
        has_ai=has_ai,
    )


@app.route("/bank/create")
@login_required
def bank_create_page():
    ai_cfg = sb.load_ai_settings(session["user_id"])
    has_ai = bool(ai_cfg.get("api_key_enc"))
    ctx    = _bank_create_context(is_create=True, has_ai=has_ai)
    return render_template_string(_BANK_CREATE, **ctx)


@app.route("/bank/import")
@login_required
def bank_import_page():
    ai_cfg = sb.load_ai_settings(session["user_id"])
    has_ai = bool(ai_cfg.get("api_key_enc"))
    ctx    = _bank_create_context(is_create=False, has_ai=has_ai)
    return render_template_string(_BANK_CREATE, **ctx)


def _load_ai_for_parsing(user_id: str):
    """Return (provider, raw_key, model) or raise ValueError."""
    ai_cfg = sb.load_ai_settings(user_id)
    if not ai_cfg.get("api_key_enc"):
        raise ValueError("No API key configured. Go to Settings to add one.")
    provider = ai_cfg.get("provider", "anthropic")
    raw_key  = decrypt_key(ai_cfg["api_key_enc"])
    model    = ai_cfg.get("model") or None
    return provider, raw_key, model


def _try_refresh_summary(user_id: str) -> None:
    """Best-effort: regenerate the AI summary for the user's bank. Silent on failure."""
    try:
        bank = sb.load_master_bank(user_id)
        provider, raw_key, model = _load_ai_for_parsing(user_id)
        summary = generate_bank_summary(bank, provider, raw_key, model)
        if summary:
            bank["ai_summary"] = summary.strip()
            sb.save_master_bank(user_id, bank)
    except Exception:
        # Summary is a nice-to-have; never block the import flow
        pass


def _merge_or_replace_bank(user_id: str, new_bank: dict, append: bool) -> None:
    """Save new_bank, optionally merging sections into any existing bank."""
    if not append:
        sb.save_master_bank(user_id, new_bank)
        return
    try:
        existing = sb.load_master_bank(user_id)
    except FileNotFoundError:
        sb.save_master_bank(user_id, new_bank)
        return
    # Merge: add new sections; don't overwrite existing ones
    existing_sections = existing.get("sections", {})
    for key, sec in new_bank.get("sections", {}).items():
        if key in existing_sections:
            # Append bullets, avoid duplicates by text
            existing_texts = {b["text"] for b in existing_sections[key].get("bullets", [])}
            for b in sec.get("bullets", []):
                if b["text"] not in existing_texts:
                    existing_sections[key].setdefault("bullets", []).append(b)
        else:
            existing_sections[key] = sec
    existing["sections"] = existing_sections
    # Merge skills: append new lines not already present
    if new_bank.get("skills_text"):
        old_lines = set((existing.get("skills_text") or "").splitlines())
        new_lines = [l for l in new_bank["skills_text"].splitlines() if l not in old_lines]
        if new_lines:
            existing["skills_text"] = (existing.get("skills_text") or "") + "\n" + "\n".join(new_lines)
    # Merge certs
    if new_bank.get("certifications"):
        existing_certs = set(existing.get("certifications") or [])
        for c in new_bank["certifications"]:
            existing_certs.add(c)
        existing["certifications"] = list(existing_certs)
    sb.save_master_bank(user_id, existing)


@app.route("/bank/from-file", methods=["POST"])
@login_required
def bank_from_file():
    append  = request.args.get("mode") == "append"
    user_id = session["user_id"]
    f       = request.files.get("cv_file")
    if not f or not f.filename:
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("bank_import_page" if append else "bank_create_page"))
    suffix = Path(f.filename).suffix.lower()
    if suffix not in (".docx", ".pdf", ".txt", ".json"):
        flash("Unsupported file type. Please upload .docx, .pdf, .txt or .json", "error")
        return redirect(url_for("bank_import_page" if append else "bank_create_page"))
    try:
        if suffix == ".json":
            # Direct round-trip: parse JSON as bank — no AI call, lossless
            try:
                raw = f.read().decode("utf-8")
                bank = json.loads(raw)
            except Exception as e:
                raise ValueError(f"Invalid JSON file: {e}")
            if not isinstance(bank, dict) or "sections" not in bank:
                raise ValueError("JSON doesn't look like a bank (missing 'sections').")
        else:
            provider, raw_key, model = _load_ai_for_parsing(user_id)
            tmp = Path(tempfile.mktemp(suffix=suffix))
            f.save(str(tmp))
            cv_text = extract_text(tmp)
            if not cv_text.strip():
                raise ValueError("Could not extract any text from this file. Try a .txt version.")
            bank = parse_cv_to_bank(cv_text, provider, raw_key, model)
        _merge_or_replace_bank(user_id, bank, append)
        session["has_bank"] = True
        n_sections = len(bank.get("sections", {}))
        verb = "updated with" if append else "created with"
        flash(f"Bank {verb} {n_sections} section(s) from your file.", "success")
        _try_refresh_summary(user_id)
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Parsing failed: {e}", "error")
    return redirect(url_for("bank_page"))


@app.route("/bank/from-text", methods=["POST"])
@login_required
def bank_from_text():
    append  = request.args.get("mode") == "append"
    user_id = session["user_id"]
    cv_text = request.form.get("cv_text", "").strip()
    if not cv_text:
        flash("Please paste some text first.", "error")
        return redirect(url_for("bank_import_page" if append else "bank_create_page"))
    try:
        provider, raw_key, model = _load_ai_for_parsing(user_id)
        bank = parse_cv_to_bank(cv_text, provider, raw_key, model)
        _merge_or_replace_bank(user_id, bank, append)
        session["has_bank"] = True
        n_sections = len(bank.get("sections", {}))
        verb = "updated with" if append else "created with"
        flash(f"Bank {verb} {n_sections} section(s).", "success")
        _try_refresh_summary(user_id)
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Parsing failed: {e}", "error")
    return redirect(url_for("bank_page"))


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — Master Bank: editor
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/bank")
@login_required
def bank_page():
    user_id = session["user_id"]
    try:
        bank = sb.load_master_bank(user_id)
    except FileNotFoundError:
        bank = None
    # Back-compat: generate summary once for banks that pre-date this feature
    if bank and not bank.get("ai_summary"):
        _try_refresh_summary(user_id)
        try:
            bank = sb.load_master_bank(user_id)
        except FileNotFoundError:
            pass
    return render_template_string(_BANK, bank=bank)


@app.route("/bank/regenerate-summary", methods=["POST"])
@login_required
def bank_regenerate_summary():
    user_id = session["user_id"]
    try:
        bank = sb.load_master_bank(user_id)
        provider, raw_key, model = _load_ai_for_parsing(user_id)
        summary = generate_bank_summary(bank, provider, raw_key, model)
        bank["ai_summary"] = (summary or "").strip()
        sb.save_master_bank(user_id, bank)
        flash("Summary refreshed.", "success")
    except FileNotFoundError:
        flash("No bank found.", "error")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Could not regenerate summary: {e}", "error")
    return redirect(url_for("bank_page"))


@app.route("/bank/download")
@login_required
def bank_download():
    try:
        bank = sb.load_master_bank(session["user_id"])
        tmp  = Path(tempfile.mktemp(suffix=".json"))
        tmp.write_text(json.dumps(bank, indent=2, ensure_ascii=False))
        return send_file(tmp, as_attachment=True,
                         download_name="master_info_bank.json",
                         mimetype="application/json")
    except FileNotFoundError:
        flash("No bank found.", "error")
        return redirect(url_for("bank_page"))


@app.route("/bank/skills", methods=["POST"])
@login_required
def bank_skills():
    skills_text    = request.form.get("skills_text", "").strip()
    certifications = [c.strip() for c in request.form.get("certifications", "").split(",") if c.strip()]
    skills_header  = request.form.get("skills_header", "skills").strip()
    try:
        bank = sb.load_master_bank(session["user_id"])
        bank["skills_text"]    = skills_text
        bank["certifications"] = certifications
        if skills_header:
            bank["skills_header"] = skills_header
        sb.save_master_bank(session["user_id"], bank)
        flash("Skills & certifications updated.", "success")
    except Exception as e:
        flash(f"Save failed: {e}", "error")
    return redirect(url_for("bank_page"))


@app.route("/bank/section/add", methods=["GET", "POST"])
@login_required
def bank_add_section():
    user_id = session["user_id"]
    ai_cfg  = sb.load_ai_settings(user_id)
    has_ai  = bool(ai_cfg.get("api_key_enc"))

    if request.method == "GET":
        # Show the manual tab of the create/import page
        ctx = _bank_create_context(is_create=False, has_ai=has_ai)
        return render_template_string(_BANK_CREATE, **ctx)

    # POST — add a new section
    section_type = request.form.get("section_type", "job")
    is_first     = request.form.get("is_first", "0") == "1"
    bullet_slots = max(1, min(6, int(request.form.get("bullet_slots", 4) or 4)))
    date_val     = request.form.get("date", "").strip()
    first_bullet = request.form.get("first_bullet", "").strip()

    if section_type == "job":
        company = request.form.get("company", "").strip()
        role    = request.form.get("role", "").strip()
        if not company:
            flash("Company name is required.", "error")
            return redirect(url_for("bank_add_section"))
        # Build a unique snake_case key
        key = re.sub(r"[^\w]", "_", company.lower())[:20].strip("_")
        new_sec = {
            "company":         company,
            "role":            role,
            "date":            date_val,
            "template_anchor": company,
            "bullet_slots":    bullet_slots,
            "bullets":         [],
        }
    else:
        project_name = request.form.get("project_name", "").strip()
        if not project_name:
            flash("Project name is required.", "error")
            return redirect(url_for("bank_add_section"))
        key = re.sub(r"[^\w]", "_", project_name.lower())[:20].strip("_") + "_proj"
        new_sec = {
            "project_name":    project_name,
            "date":            date_val,
            "template_anchor": project_name,
            "bullet_slots":    bullet_slots,
            "bullets":         [],
        }

    if first_bullet:
        new_sec["bullets"].append({
            "id":   f"{key}_{_uuid.uuid4().hex[:6]}",
            "text": first_bullet,
            "tags": [],
        })

    try:
        if is_first:
            # Create a brand-new empty bank
            bank = {"sections": {key: new_sec}, "certifications": [],
                    "skills_text": "", "skills_header": "Skills & Additional Information"}
        else:
            bank = sb.load_master_bank(user_id)
            # Deduplicate key if collision
            if key in bank.get("sections", {}):
                key = key + "_2"
            bank.setdefault("sections", {})[key] = new_sec

        sb.save_master_bank(user_id, bank)
        session["has_bank"] = True
        label = new_sec.get("company") or new_sec.get("project_name", key)
        flash(f"Section '{label}' added to your bank.", "success")
    except Exception as e:
        flash(f"Failed to save section: {e}", "error")

    return redirect(url_for("bank_page"))


@app.route("/bank/section/<section_key>/bullet/add", methods=["POST"])
@login_required
def bank_add_bullet(section_key):
    text = request.form.get("text", "").strip()
    if not text:
        flash("Bullet text cannot be empty.", "error")
        return redirect(url_for("bank_page"))
    try:
        sb.add_bullet(session["user_id"], section_key, text)
        flash("Bullet added.", "success")
    except Exception as e:
        flash(f"Failed: {e}", "error")
    return redirect(url_for("bank_page"))


@app.route("/bank/section/<section_key>/bullet/<bullet_id>/update", methods=["POST"])
@login_required
def bank_update_bullet(section_key, bullet_id):
    text = request.form.get("text", "").strip()
    if not text:
        flash("Bullet text cannot be empty.", "error")
        return redirect(url_for("bank_page"))
    try:
        sb.update_bullet(session["user_id"], section_key, bullet_id, text)
    except Exception as e:
        flash(f"Failed: {e}", "error")
    return redirect(url_for("bank_page"))


@app.route("/bank/section/<section_key>/bullet/<bullet_id>/delete", methods=["POST"])
@login_required
def bank_delete_bullet(section_key, bullet_id):
    try:
        sb.delete_bullet(session["user_id"], section_key, bullet_id)
        flash("Bullet deleted.", "success")
    except Exception as e:
        flash(f"Failed: {e}", "error")
    return redirect(url_for("bank_page"))


@app.route("/bank/section/<section_key>/slots", methods=["POST"])
@login_required
def bank_update_slots(section_key):
    try:
        slots = int(request.form.get("slots", 3))
        sb.update_section_slots(session["user_id"], section_key, slots)
    except Exception as e:
        flash(f"Failed: {e}", "error")
    return redirect(url_for("bank_page"))


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — Generate CV (with review step)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/generate", methods=["POST"])
@login_required
def generate():
    jd_text = request.form.get("jd_text", "").strip()
    if not jd_text:
        flash("Please paste a job description.", "error")
        return redirect(url_for("dashboard"))

    user_id = session["user_id"]
    tmp_dir = Path(tempfile.mkdtemp())

    try:
        # 1–3. Parallelise all three DB reads — they are independent of each other.
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_bank = ex.submit(sb.load_master_bank,            user_id)
            f_ai   = ex.submit(sb.load_ai_settings,            user_id)
            f_fmt  = ex.submit(sb.load_template_format_rules,  user_id)
        master_bank = f_bank.result()
        ai_cfg      = f_ai.result()
        fmt_rules   = f_fmt.result() or {}

        if not ai_cfg.get("api_key_enc"):
            flash("No AI API key configured. Go to Settings first.", "error")
            return redirect(url_for("dashboard"))
        provider = ai_cfg.get("provider", "anthropic")
        raw_key  = decrypt_key(ai_cfg["api_key_enc"])
        model    = ai_cfg.get("model") or None

        # 3. Resolve template slot counts.
        # Fast path: raw_slots was cached in format_rules at upload time → no download needed.
        # Slow path (back-compat): download template, extract, persist for next time.
        raw_slots = fmt_rules.get("raw_slots") if isinstance(fmt_rules, dict) else None
        if raw_slots:
            template_slots = map_template_slots_from_raw(raw_slots, master_bank)
            template_path  = None   # downloaded lazily just before DOCX build
        else:
            template_path = tmp_dir / "base_cv.docx"
            sb.download_cv_template(user_id, template_path)
            template_slots = read_template_slots(template_path, master_bank)
            # Lazy back-fill: persist format_rules + raw_slots so next generate is fast.
            if not fmt_rules:
                try:
                    fmt_rules = extract_template_format_rules(template_path)
                except Exception:
                    fmt_rules = {}
            try:
                new_raw = discover_template_sections(template_path)
                if new_raw:
                    fmt_rules["raw_slots"] = new_raw
                    sb.save_template_format_rules(user_id, fmt_rules)
            except Exception:
                pass

        if fmt_rules:
            master_bank["format_rules"] = {
                **master_bank.get("format_rules", {}),
                **{k: v for k, v in fmt_rules.items() if k != "raw_slots"},
            }

        # 4. Call AI
        result  = call_ai(jd_text, master_bank, provider, raw_key, model,
                          template_slots=template_slots)
        jd_info = result.get("jd_analysis", {})
        company = jd_info.get("company", "Company")
        role    = jd_info.get("role", "Role")
        safe    = re.sub(r"[^\w\s-]", "", f"{company} {role}").strip().replace(" ", "_")

        # 5. Take AI output directly — no review step.
        sections = {k: list(v) for k, v in result.get("sections", {}).items() if v}
        skills_text       = (result.get("skills_text", "") or "").strip()
        project_overrides = result.get("project_overrides") or None
        token = _uuid.uuid4().hex

        # ── Generic format-rule enforcement — works for any user's template ──
        # Use the rules auto-extracted from the user's DOCX at upload time:
        #   max_bullet_chars      — longest bullet the template font can hold on one line
        #   max_skill_lines       — number of soft-return lines the skills paragraph has
        #   max_skill_line_chars  — longest single skill line the template can hold
        # template_slots maps section_key → bullet count for that section's slot.
        eff_fmt              = master_bank.get("format_rules", {})
        max_bullet_chars     = int(eff_fmt.get("max_bullet_chars",     215))
        max_skill_lines      = int(eff_fmt.get("max_skill_lines",      5))
        max_skill_line_chars = int(eff_fmt.get("max_skill_line_chars", 120))

        # 1. Cap each bullet's length (word-boundary truncate) ────────────────
        for sec_key in sections:
            capped = []
            for b in sections[sec_key]:
                if len(b) <= max_bullet_chars:
                    capped.append(b)
                else:
                    truncated = b[:max_bullet_chars].rsplit(" ", 1)[0].rstrip(",;: ")
                    capped.append(truncated)
                    print(f"  ✂️  Bullet capped ({len(b)}→{len(truncated)} chars) in '{sec_key}'")
            sections[sec_key] = capped

        # 2. Cap bullet COUNT per section to the template's slot count ────────
        for sec_key, slot_count in template_slots.items():
            if sec_key in sections and slot_count > 0 and len(sections[sec_key]) > slot_count:
                before = len(sections[sec_key])
                sections[sec_key] = sections[sec_key][:slot_count]
                print(f"  ✂️  Bullet count capped ({before}→{slot_count}) in '{sec_key}' (template slots)")

        # 3. Cap skills text — line count AND per-line length ─────────────────
        skills_text = _cap_skills_text(skills_text, max_skill_lines, max_skill_line_chars)

        # Lazy template download: skipped above when raw_slots was cached.
        if template_path is None or not template_path.exists():
            template_path = tmp_dir / "base_cv.docx"
            sb.download_cv_template(user_id, template_path)
        else:
            tmp_dir = template_path.parent

        out_dir  = tmp_dir / safe
        out_dir.mkdir(parents=True, exist_ok=True)
        docx_path = out_dir / "Tailored_CV.docx"

        print(f"  🏁  Building CV for token: {token}")

        # ── HARD ONE-PAGE GUARANTEE LOOP (template-agnostic) ──
        MAX_ATTEMPTS = 5
        attempts = 0
        one_page = False
        pdf_path = None
        while attempts < MAX_ATTEMPTS:
            print(f"  - Attempt {attempts + 1}: Generating files...")
            modify_docx(
                sections=sections,
                skills_text=skills_text,
                template_path=template_path,
                output_path=docx_path,
                master_bank=master_bank,
                project_overrides=project_overrides,
            )
            pdf_path = convert_to_pdf(docx_path)
            one_page = check_one_page(pdf_path)

            if one_page:
                break
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                break

            print(f"  📏 CV is > 1 page. Pruning attempt {attempts}...")
            total_bullets = sum(len(v) for v in sections.values())
            skill_lines   = [ln for ln in skills_text.splitlines() if ln.strip()]
            n_skill_lines = len(skill_lines)
            trimmed = False
            if total_bullets >= n_skill_lines:
                max_key, max_len = None, -1
                for k, v in sections.items():
                    if len(v) > max_len:
                        max_len, max_key = len(v), k
                if max_key and max_len > 1:
                    print(f"     ✂️  Trimming 1 bullet from '{max_key}' ({max_len}→{max_len-1})")
                    sections[max_key].pop()
                    trimmed = True
            if not trimmed and n_skill_lines > 2:
                non_cert = [(i, ln) for i, ln in enumerate(skill_lines)
                            if not ln.lower().startswith("certifications")]
                if non_cert:
                    drop_i, drop_ln = min(non_cert, key=lambda t: len(t[1]))
                    print(f"     ✂️  Trimming skill line: '{drop_ln[:60]}…'")
                    skill_lines.pop(drop_i)
                    skills_text = "\n".join(skill_lines)
                    trimmed = True
            if not trimmed:
                print("     ⚠️  Nothing left to trim safely — breaking retry loop.")
                break

        # Back up to Supabase Storage so any worker can serve the download
        sb.upload_generated_cv(session["user_id"], token, docx_path)
        if pdf_path:
            sb.upload_generated_cv(session["user_id"], token, pdf_path)

        _generated[token] = {
            "docx":     docx_path,
            "pdf":      pdf_path,
            "company":  company,
            "role":     role,
            "safe":     safe,
            "one_page": one_page,
        }

        return render_template_string(
            _RESULT,
            token   = token,
            company = company,
            role    = role,
            has_pdf = pdf_path is not None and pdf_path.exists(),
            one_page= one_page,
        )

    except FileNotFoundError as e:
        flash(str(e), "error")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"CV generation failed: {e}", "error")

    return redirect(url_for("dashboard"))


@app.route("/download/<token>/<fmt>")
@login_required
def download(token, fmt):
    entry = _generated.get(token)
    if not entry:
        flash("Download link expired. Please generate again.", "error")
        return redirect(url_for("dashboard"))

    safe = entry["safe"]
    if fmt == "docx":
        p = entry["docx"]
        if not p or not p.exists():
            flash("DOCX file not found.", "error")
            return redirect(url_for("dashboard"))
        return send_file(
            p, as_attachment=True,
            download_name=f"CV_{safe}.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    elif fmt == "pdf":
        p = entry["pdf"]
        if not p or not p.exists():
            flash("PDF not available.", "error")
            return redirect(url_for("dashboard"))
        return send_file(p, as_attachment=True,
                         download_name=f"CV_{safe}.pdf", mimetype="application/pdf")
    else:
        flash("Invalid format.", "error")
        return redirect(url_for("dashboard"))


# ─────────────────────────────────────────────────────────────────────────────
#  Run
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)
