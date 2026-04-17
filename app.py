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
from ai_providers import PROVIDERS, call_ai, decrypt_key, encrypt_key, parse_cv_to_bank
from cv_engine import (
    check_one_page, convert_to_pdf, extract_template_format_rules,
    extract_text, modify_docx, read_template_slots,
)

# ─── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24))

# In-memory stores (keyed by UUID token)
_pending:   dict[str, dict] = {}   # token → AI result awaiting user review
_generated: dict[str, dict] = {}   # token → {docx, pdf, ...} ready for download


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
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    :root {
      --navy: #0f172a;
      --navy-80: #1e293b;
      --indigo: #4f46e5;
      --indigo-l: #6366f1;
      --gold: #d97706;
      --gold-l: #f59e0b;
      --emerald: #059669;
      --surface: #ffffff;
      --bg: #f8fafc;
      --border: rgba(15,23,42,0.09);
      --text: #0f172a;
      --muted: #64748b;
      --r16: 16px;
      --r10: 10px;
      --shadow: 0 2px 16px rgba(15,23,42,0.07);
      --shadow-md: 0 8px 32px rgba(15,23,42,0.12);
      --shadow-lg: 0 20px 56px rgba(15,23,42,0.18);
    }
    *, *::before, *::after { box-sizing: border-box; }
    body {
      background: var(--bg);
      font-family: 'Inter', system-ui, sans-serif;
      font-size: .92rem;
      color: var(--text);
      -webkit-font-smoothing: antialiased;
    }

    /* ── Navbar ── */
    .cc-nav {
      position: sticky;
      top: 0;
      z-index: 1000;
      background: rgba(15,23,42,0.97);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid rgba(255,255,255,0.06);
      padding: .75rem 2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .cc-brand {
      display: flex;
      align-items: center;
      gap: .6rem;
      text-decoration: none;
      font-weight: 700;
      font-size: 1.05rem;
      color: #fff;
      letter-spacing: -.3px;
    }
    .cc-brand-icon {
      width: 30px; height: 30px;
      background: linear-gradient(135deg, var(--indigo), var(--indigo-l));
      border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      font-size: .9rem; color: #fff;
    }
    .cc-brand-cv { color: var(--gold-l); }
    .cc-nav-links { display: flex; align-items: center; gap: .5rem; }
    .cc-nav-pill {
      padding: .35rem .85rem;
      border-radius: 20px;
      font-size: .82rem;
      font-weight: 500;
      color: rgba(255,255,255,0.75);
      text-decoration: none;
      transition: background .18s, color .18s;
      border: 1px solid transparent;
    }
    .cc-nav-pill:hover {
      background: rgba(255,255,255,0.09);
      color: #fff;
    }
    .cc-nav-pill.outline {
      border-color: rgba(255,255,255,0.2);
      color: rgba(255,255,255,0.8);
    }
    .cc-nav-pill.outline:hover {
      border-color: rgba(255,255,255,0.45);
      color: #fff;
    }
    .cc-email-badge {
      font-size: .75rem;
      color: rgba(255,255,255,0.4);
      padding: .3rem .7rem;
      background: rgba(255,255,255,0.05);
      border-radius: 12px;
    }

    /* ── Loading overlay ── */
    #loadingOverlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15,23,42,0.82);
      z-index: 9999;
      justify-content: center;
      align-items: center;
      flex-direction: column;
    }
    .ov-card {
      background: #fff;
      border-radius: var(--r16);
      padding: 2.5rem 3rem;
      text-align: center;
      box-shadow: var(--shadow-lg);
      min-width: 280px;
    }
    .cc-ring {
      width: 48px; height: 48px;
      border: 4px solid rgba(79,70,229,0.15);
      border-top-color: var(--indigo);
      border-radius: 50%;
      animation: ccSpin .8s linear infinite;
      margin: 0 auto;
    }
    @keyframes ccSpin { to { transform: rotate(360deg); } }
    .spinner-text {
      margin-top: 1.1rem;
      font-weight: 700;
      font-size: 1rem;
      color: var(--navy);
    }
    .spinner-sub {
      color: var(--muted);
      font-size: .82rem;
      margin-top: .3rem;
    }

    /* ── Alert overrides ── */
    .alert {
      border-radius: var(--r10);
      border: none;
      border-left: 4px solid;
      font-size: .875rem;
      font-weight: 500;
    }
    .alert-success  { border-color: var(--emerald); background: #f0fdf4; color: #14532d; }
    .alert-danger   { border-color: #dc2626;        background: #fef2f2; color: #7f1d1d; }
    .alert-warning  { border-color: var(--gold);    background: #fffbeb; color: #78350f; }
    .alert-info     { border-color: var(--indigo);  background: #eef2ff; color: #312e81; }

    /* ── Card base ── */
    .card-base {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--r16);
      box-shadow: var(--shadow);
    }
    .card-hover {
      transition: transform .22s ease, box-shadow .22s ease;
    }
    .card-hover:hover {
      transform: translateY(-4px);
      box-shadow: var(--shadow-md);
    }
    /* Bootstrap card overrides */
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--r16) !important;
      box-shadow: var(--shadow);
    }
    .card-header {
      border-radius: var(--r16) var(--r16) 0 0 !important;
      font-weight: 600;
      background: var(--bg);
      border-bottom: 1px solid var(--border);
      padding: .85rem 1.25rem;
    }

    /* ── Buttons ── */
    .btn-indig {
      background: linear-gradient(135deg, var(--indigo), var(--indigo-l));
      color: #fff;
      border: none;
      border-radius: var(--r10);
      font-weight: 600;
      padding: .55rem 1.4rem;
      transition: opacity .18s, transform .14s;
      box-shadow: 0 4px 14px rgba(79,70,229,0.35);
    }
    .btn-indig:hover { opacity: .9; transform: translateY(-1px); color: #fff; }
    .btn-indig:active { transform: translateY(0); }
    .btn-ghost {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: var(--r10);
      font-weight: 500;
      padding: .5rem 1.2rem;
      transition: background .18s, border-color .18s;
    }
    .btn-ghost:hover { background: var(--bg); border-color: rgba(15,23,42,0.18); }
    .btn-success-custom {
      background: linear-gradient(135deg, #059669, #10b981);
      color: #fff;
      border: none;
      border-radius: var(--r10);
      font-weight: 600;
      padding: .55rem 1.4rem;
      transition: opacity .18s, transform .14s;
      box-shadow: 0 4px 14px rgba(5,150,105,0.3);
    }
    .btn-success-custom:hover { opacity: .9; transform: translateY(-1px); color: #fff; }
    .btn-gold {
      background: linear-gradient(135deg, var(--gold), var(--gold-l));
      color: #fff;
      border: none;
      border-radius: var(--r10);
      font-weight: 700;
      padding: .55rem 1.4rem;
      transition: opacity .18s, transform .14s;
      box-shadow: 0 4px 14px rgba(217,119,6,0.35);
    }
    .btn-gold:hover { opacity: .9; transform: translateY(-1px); color: #fff; }
    .btn-xs {
      padding: .2rem .55rem;
      font-size: .73rem;
      font-weight: 500;
      border-radius: 6px;
    }

    /* ── Form controls ── */
    .fc {
      width: 100%;
      padding: .6rem .9rem;
      background: var(--bg);
      border: 1.5px solid var(--border);
      border-radius: var(--r10);
      font-family: 'Inter', sans-serif;
      font-size: .875rem;
      color: var(--text);
      transition: border-color .18s, box-shadow .18s;
      outline: none;
    }
    .fc:focus {
      border-color: var(--indigo);
      box-shadow: 0 0 0 3px rgba(79,70,229,0.1);
      background: #fff;
    }
    .fl {
      display: block;
      font-size: .8rem;
      font-weight: 600;
      color: var(--navy-80);
      margin-bottom: .35rem;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    /* Override bootstrap form-control */
    .form-control, .form-select {
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      border: 1.5px solid var(--border);
      border-radius: var(--r10);
      font-size: .875rem;
      color: var(--text);
      transition: border-color .18s, box-shadow .18s;
    }
    .form-control:focus, .form-select:focus {
      border-color: var(--indigo);
      box-shadow: 0 0 0 3px rgba(79,70,229,0.1);
      background: #fff;
    }
    textarea.form-control { font-family: 'Inter', sans-serif; font-size: .84rem; }

    /* ── Status dots ── */
    .sdot {
      width: 9px; height: 9px;
      border-radius: 50%;
      display: inline-block;
      margin-right: 6px;
      flex-shrink: 0;
    }
    .sdot-ok { background: var(--emerald); box-shadow: 0 0 0 3px rgba(5,150,105,0.18); }
    .sdot-no { background: #cbd5e1; }
    /* Legacy compat */
    .status-dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; }
    .dot-ok { background: var(--emerald); box-shadow: 0 0 0 3px rgba(5,150,105,0.18); }
    .dot-no { background: #cbd5e1; }

    /* ── Step badge ── */
    .step-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 26px; height: 26px;
      border-radius: 50%;
      font-size: .72rem;
      font-weight: 700;
      background: linear-gradient(135deg, var(--indigo), var(--indigo-l));
      color: #fff;
      flex-shrink: 0;
      box-shadow: 0 2px 8px rgba(79,70,229,0.3);
    }

    /* ── Import zone ── */
    .import-zone {
      border: 2px dashed var(--border);
      border-radius: var(--r16);
      padding: 2.5rem 2rem;
      text-align: center;
      cursor: pointer;
      transition: border-color .2s, background .2s;
      background: #f8fafc;
    }
    .import-zone:hover, .import-zone.drag-over {
      border-color: var(--indigo);
      background: #eef2ff;
    }

    /* ── STAR guide ── */
    .star-guide {
      background: #eef2ff;
      border: 1px solid rgba(79,70,229,0.2);
      border-radius: var(--r10);
      padding: .75rem 1.1rem;
      font-size: .8rem;
      color: #312e81;
    }
    .star-guide code {
      background: rgba(79,70,229,0.12);
      border-radius: 4px;
      padding: .1rem .35rem;
      font-size: .77rem;
      color: var(--indigo);
    }

    /* ── Bullet row ── */
    .bullet-row {
      border-left: 3px solid var(--border);
      padding-left: .85rem;
      margin-bottom: .45rem;
      border-radius: 0 8px 8px 0;
      transition: border-color .15s, background .15s;
    }
    .bullet-row:hover {
      border-left-color: var(--indigo);
      background: #f8f9ff;
    }

    /* ── Review bullet ── */
    .review-bullet {
      background: #fff;
      border: 1.5px solid var(--border);
      border-radius: var(--r10);
      padding: .6rem .9rem;
      margin-bottom: .55rem;
      transition: border-color .18s;
    }
    .review-bullet:focus-within {
      border-color: var(--indigo);
      box-shadow: 0 0 0 3px rgba(79,70,229,0.08);
    }

    /* ── Badge pill ── */
    .badge-pill {
      padding: .3rem .75rem;
      border-radius: 20px;
      font-size: .75rem;
      font-weight: 600;
      letter-spacing: .02em;
    }
    .badge-indigo { background: #eef2ff; color: var(--indigo); border: 1px solid rgba(79,70,229,0.2); }
    .badge-navy   { background: rgba(15,23,42,0.08); color: var(--navy); border: 1px solid var(--border); }
    .badge-gold   { background: #fffbeb; color: var(--gold); border: 1px solid rgba(217,119,6,0.2); }
    .badge-emerald { background: #f0fdf4; color: var(--emerald); border: 1px solid rgba(5,150,105,0.2); }

    /* ── Tab navigation ── */
    .cc-tabs {
      display: flex;
      gap: 0;
      border-bottom: 2px solid var(--border);
      margin-bottom: 0;
    }
    .cc-tab-btn {
      padding: .7rem 1.3rem;
      font-size: .84rem;
      font-weight: 600;
      color: var(--muted);
      background: transparent;
      border: none;
      border-bottom: 2px solid transparent;
      margin-bottom: -2px;
      cursor: pointer;
      transition: color .18s, border-color .18s;
      display: flex;
      align-items: center;
      gap: .4rem;
    }
    .cc-tab-btn:hover { color: var(--navy); }
    .cc-tab-btn.active { color: var(--indigo); border-bottom-color: var(--indigo); }
    .tab-pane { padding-top: 1.25rem; }

    /* ── Section card borders by type ── */
    .section-card { margin-bottom: 1rem; }
    .section-card.type-company { border-left: 4px solid var(--indigo) !important; }
    .section-card.type-project { border-left: 4px solid var(--emerald) !important; }

    /* ── Generate shimmer button ── */
    @keyframes shimmer {
      0%   { background-position: -200% center; }
      100% { background-position: 200% center; }
    }
    .btn-generate {
      background: linear-gradient(90deg, var(--indigo) 0%, var(--indigo-l) 30%, #818cf8 50%, var(--indigo-l) 70%, var(--indigo) 100%);
      background-size: 200% auto;
      color: #fff;
      border: none;
      border-radius: var(--r10);
      font-weight: 700;
      font-size: 1rem;
      padding: .85rem 2rem;
      width: 100%;
      cursor: pointer;
      transition: transform .14s, box-shadow .18s;
      box-shadow: 0 6px 20px rgba(79,70,229,0.4);
    }
    .btn-generate:hover {
      animation: shimmer 1.6s linear infinite;
      transform: translateY(-2px);
      box-shadow: 0 10px 28px rgba(79,70,229,0.45);
      color: #fff;
    }

    /* Provider card selectors */
    .provider-card {
      border: 2px solid var(--border);
      border-radius: var(--r10);
      padding: .75rem 1rem;
      cursor: pointer;
      transition: border-color .18s, background .18s;
      background: var(--bg);
    }
    .provider-card:hover { border-color: var(--indigo-l); background: #eef2ff; }
    .provider-card.selected { border-color: var(--indigo); background: #eef2ff; box-shadow: 0 0 0 3px rgba(79,70,229,0.1); }

    /* ── Misc layout ── */
    .cc-page { max-width: 860px; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
    .section-eyebrow {
      font-size: .7rem;
      font-weight: 700;
      letter-spacing: .1em;
      text-transform: uppercase;
      color: var(--gold);
    }
  </style>
</head>
<body>

<!-- Navbar -->
<nav class="cc-nav">
  <a class="cc-brand" href="/">
    <div class="cc-brand-icon"><i class="bi bi-file-earmark-text"></i></div>
    My INSEAD <span class="cc-brand-cv">CV</span>
  </a>
  {% if session.user_id %}
  <div class="cc-nav-links">
    <span class="cc-email-badge d-none d-md-inline">{{ session.email }}</span>
    <a class="cc-nav-pill" href="/bank"><i class="bi bi-database me-1"></i>Bank</a>
    <a class="cc-nav-pill" href="/settings"><i class="bi bi-gear me-1"></i>Settings</a>
    <a class="cc-nav-pill outline" href="/signout">Sign out</a>
  </div>
  {% endif %}
</nav>

<!-- Loading overlay -->
<div id="loadingOverlay">
  <div class="ov-card">
    <div class="cc-ring"></div>
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
{% block scripts %}{% endblock %}
</body>
</html>"""


# ── Landing / index ───────────────────────────────────────────────────────────

_INDEX = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>My INSEAD CV — Get shortlisted, every time</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    :root {
      --navy: #0f172a; --navy-80: #1e293b;
      --indigo: #4f46e5; --indigo-l: #6366f1;
      --gold: #d97706; --gold-l: #f59e0b;
      --emerald: #059669;
      --surface: #ffffff; --bg: #f8fafc;
      --border: rgba(15,23,42,0.09);
      --text: #0f172a; --muted: #64748b;
      --r16: 16px; --r10: 10px;
      --shadow: 0 2px 16px rgba(15,23,42,0.07);
      --shadow-md: 0 8px 32px rgba(15,23,42,0.12);
      --shadow-lg: 0 20px 56px rgba(15,23,42,0.18);
    }
    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0; padding: 0;
      font-family: 'Inter', system-ui, sans-serif;
      font-size: .92rem;
      color: var(--text);
      -webkit-font-smoothing: antialiased;
      background: var(--bg);
    }

    /* Navbar */
    .cc-nav {
      position: sticky; top: 0; z-index: 1000;
      background: rgba(15,23,42,0.97);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid rgba(255,255,255,0.06);
      padding: .75rem 2rem;
      display: flex; align-items: center; justify-content: space-between;
    }
    .cc-brand {
      display: flex; align-items: center; gap: .6rem;
      text-decoration: none; font-weight: 700; font-size: 1.05rem;
      color: #fff; letter-spacing: -.3px;
    }
    .cc-brand-icon {
      width: 30px; height: 30px;
      background: linear-gradient(135deg, var(--indigo), var(--indigo-l));
      border-radius: 8px; display: flex; align-items: center;
      justify-content: center; font-size: .9rem; color: #fff;
    }
    .cc-brand-cv { color: var(--gold-l); }
    .cc-nav-links { display: flex; align-items: center; gap: .5rem; }
    .cc-nav-pill {
      padding: .35rem .85rem; border-radius: 20px; font-size: .82rem; font-weight: 500;
      color: rgba(255,255,255,0.75); text-decoration: none;
      transition: background .18s, color .18s; border: 1px solid transparent;
    }
    .cc-nav-pill:hover { background: rgba(255,255,255,0.09); color: #fff; }
    .cc-nav-pill.outline {
      border-color: rgba(255,255,255,0.2); color: rgba(255,255,255,0.8);
    }
    .cc-nav-pill.outline:hover { border-color: rgba(255,255,255,0.45); color: #fff; }

    /* Hero */
    .hero {
      background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 55%, #0c1128 100%);
      position: relative;
      overflow: hidden;
      padding: 6rem 0 5rem;
    }
    .hero::before {
      content: '';
      position: absolute; inset: 0;
      background-image:
        linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
      background-size: 64px 64px;
      pointer-events: none;
    }
    .hero::after {
      content: '';
      position: absolute; inset: 0;
      background: radial-gradient(ellipse at 30% 50%, rgba(79,70,229,0.15) 0%, transparent 60%);
      pointer-events: none;
    }
    .hero-inner { position: relative; z-index: 1; }
    .hero-badge {
      display: inline-flex; align-items: center; gap: .4rem;
      background: rgba(217,119,6,0.15);
      border: 1px solid rgba(217,119,6,0.35);
      border-radius: 20px;
      padding: .3rem .85rem;
      font-size: .73rem; font-weight: 700;
      letter-spacing: .06em; text-transform: uppercase;
      color: var(--gold-l); margin-bottom: 1.5rem;
    }
    .hero-h1 {
      font-weight: 900; font-size: clamp(2.2rem, 5vw, 3.2rem);
      line-height: 1.08; color: #fff;
      letter-spacing: -.04em; margin-bottom: 1.2rem;
    }
    .hero-sub {
      font-size: 1.05rem; line-height: 1.7;
      color: rgba(255,255,255,0.65);
      max-width: 480px; margin-bottom: 2rem;
    }
    .hero-cta-row { display: flex; gap: .75rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
    .btn-hero-outline {
      padding: .65rem 1.5rem; border-radius: var(--r10);
      border: 1.5px solid rgba(255,255,255,0.3); color: rgba(255,255,255,0.88);
      font-weight: 600; font-size: .9rem; text-decoration: none;
      transition: border-color .18s, background .18s;
      background: transparent; display: inline-block;
    }
    .btn-hero-outline:hover { border-color: rgba(255,255,255,0.6); background: rgba(255,255,255,0.06); color: #fff; }
    .btn-hero-gold {
      padding: .65rem 1.5rem; border-radius: var(--r10);
      background: linear-gradient(135deg, var(--gold), var(--gold-l));
      color: #fff; font-weight: 700; font-size: .9rem;
      text-decoration: none; border: none;
      box-shadow: 0 4px 18px rgba(217,119,6,0.4);
      transition: opacity .18s, transform .14s; display: inline-block;
    }
    .btn-hero-gold:hover { opacity: .9; transform: translateY(-1px); color: #fff; }
    .hero-trust {
      font-size: .78rem; color: rgba(255,255,255,0.4);
      display: flex; align-items: center; gap: .4rem;
    }

    /* Floating CV card */
    .cv-mockup-wrap { display: flex; justify-content: center; align-items: center; padding: 2rem 1rem; }
    .cv-mockup {
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.4);
      width: 260px;
      padding: 1.5rem;
      position: relative;
      animation: floatCV 3.8s ease-in-out infinite;
    }
    @keyframes floatCV {
      0%, 100% { transform: translateY(0) rotate(-1.5deg); box-shadow: 0 20px 60px rgba(0,0,0,0.4); }
      50% { transform: translateY(-14px) rotate(0.5deg); box-shadow: 0 34px 80px rgba(0,0,0,0.5); }
    }
    .cv-mock-name { font-weight: 800; font-size: .95rem; color: #0f172a; margin-bottom: .15rem; }
    .cv-mock-contact { font-size: .6rem; color: #94a3b8; margin-bottom: .85rem; }
    .cv-mock-section { font-size: .62rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: #4f46e5; margin-bottom: .4rem; border-bottom: 1px solid #e2e8f0; padding-bottom: .25rem; }
    .cv-mock-line {
      height: 7px; border-radius: 4px;
      background: linear-gradient(90deg, #e2e8f0, #f1f5f9);
      margin-bottom: .35rem;
    }
    .cv-mock-skills { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .4rem; }
    .cv-mock-skill { height: 7px; border-radius: 4px; background: linear-gradient(90deg, #e2e8f0, #f1f5f9); }
    .cv-ai-badge {
      position: absolute; top: -.6rem; right: -.6rem;
      background: linear-gradient(135deg, var(--indigo), var(--indigo-l));
      color: #fff; font-size: .65rem; font-weight: 700;
      padding: .25rem .6rem; border-radius: 20px;
      box-shadow: 0 4px 12px rgba(79,70,229,0.4);
      animation: pulseBadge 2s ease-in-out infinite;
    }
    @keyframes pulseBadge {
      0%, 100% { box-shadow: 0 4px 12px rgba(79,70,229,0.4); }
      50% { box-shadow: 0 4px 24px rgba(79,70,229,0.7); }
    }

    /* How it works */
    .how-section { background: #fff; padding: 5rem 0; }
    .step-card {
      background: #fff;
      border: 1px solid var(--border);
      border-radius: var(--r16);
      padding: 2rem 1.5rem;
      box-shadow: var(--shadow);
      cursor: default;
      transition: transform .22s ease, box-shadow .22s ease;
      opacity: 0;
      transform: translateY(28px);
    }
    .step-card.visible { opacity: 1; transform: translateY(0); transition: opacity .45s ease, transform .45s ease; }
    .step-icon-circle {
      width: 52px; height: 52px; border-radius: 14px;
      display: flex; align-items: center; justify-content: center;
      font-size: 1.3rem; color: #fff;
      margin-bottom: 1.1rem;
    }
    .ic-indigo { background: linear-gradient(135deg, var(--indigo), var(--indigo-l)); box-shadow: 0 4px 16px rgba(79,70,229,0.35); }
    .ic-purple { background: linear-gradient(135deg, #7c3aed, #a78bfa); box-shadow: 0 4px 16px rgba(124,58,237,0.35); }
    .ic-emerald { background: linear-gradient(135deg, var(--emerald), #10b981); box-shadow: 0 4px 16px rgba(5,150,105,0.3); }
    .step-num {
      font-size: .7rem; font-weight: 800; letter-spacing: .06em;
      text-transform: uppercase; color: var(--muted); margin-bottom: .5rem;
    }
    .step-title { font-size: 1.1rem; font-weight: 700; color: var(--navy); margin-bottom: .55rem; }
    .step-desc { font-size: .85rem; line-height: 1.65; color: var(--muted); }

    /* Auth section */
    .auth-section { background: var(--bg); padding: 5rem 0; }
    .auth-card {
      background: #fff;
      border: 1px solid var(--border);
      border-radius: var(--r16);
      padding: 2.25rem;
      box-shadow: var(--shadow);
      transition: transform .22s, box-shadow .22s;
    }
    .auth-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-md); }
    .auth-card-title { font-size: 1.1rem; font-weight: 700; color: var(--navy); margin-bottom: 1.4rem; display: flex; align-items: center; gap: .5rem; }
    .auth-icon { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: .9rem; color: #fff; }
    .ic-sign-in { background: linear-gradient(135deg, var(--indigo), var(--indigo-l)); }
    .ic-sign-up { background: linear-gradient(135deg, var(--emerald), #10b981); }
    .auth-label { display: block; font-size: .75rem; font-weight: 700; color: var(--navy-80); margin-bottom: .35rem; text-transform: uppercase; letter-spacing: .04em; }
    .auth-input {
      width: 100%; padding: .6rem .9rem;
      background: var(--bg); border: 1.5px solid var(--border);
      border-radius: var(--r10); font-family: 'Inter', sans-serif;
      font-size: .875rem; color: var(--text); transition: border-color .18s, box-shadow .18s;
      outline: none;
    }
    .auth-input:focus { border-color: var(--indigo); box-shadow: 0 0 0 3px rgba(79,70,229,0.1); background: #fff; }
    .auth-mb { margin-bottom: .85rem; }
    .btn-auth-indigo {
      width: 100%; padding: .65rem 1rem;
      background: linear-gradient(135deg, var(--indigo), var(--indigo-l));
      color: #fff; border: none; border-radius: var(--r10);
      font-weight: 700; font-size: .9rem; cursor: pointer;
      box-shadow: 0 4px 14px rgba(79,70,229,0.35);
      transition: opacity .18s, transform .14s;
    }
    .btn-auth-indigo:hover { opacity: .9; transform: translateY(-1px); }
    .btn-auth-gold {
      width: 100%; padding: .65rem 1rem;
      background: linear-gradient(135deg, var(--gold), var(--gold-l));
      color: #fff; border: none; border-radius: var(--r10);
      font-weight: 700; font-size: .9rem; cursor: pointer;
      box-shadow: 0 4px 14px rgba(217,119,6,0.35);
      transition: opacity .18s, transform .14s;
    }
    .btn-auth-gold:hover { opacity: .9; transform: translateY(-1px); }
    .security-note { text-align: center; font-size: .77rem; color: var(--muted); margin-top: 1.5rem; display: flex; align-items: center; justify-content: center; gap: .4rem; }

    /* Alerts */
    .alert { border-radius: var(--r10); border: none; border-left: 4px solid; font-size: .875rem; font-weight: 500; }
    .alert-success  { border-color: var(--emerald); background: #f0fdf4; color: #14532d; }
    .alert-danger   { border-color: #dc2626;        background: #fef2f2; color: #7f1d1d; }
    .alert-warning  { border-color: var(--gold);    background: #fffbeb; color: #78350f; }
    .section-eyebrow { font-size: .7rem; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--gold); margin-bottom: .6rem; }
  </style>
</head>
<body>

<!-- Navbar -->
<nav class="cc-nav">
  <a class="cc-brand" href="/">
    <div class="cc-brand-icon"><i class="bi bi-file-earmark-text"></i></div>
    My INSEAD <span class="cc-brand-cv">CV</span>
  </a>
  <div class="cc-nav-links">
    <a class="cc-nav-pill outline" href="#signin">Sign in</a>
    <a class="cc-nav-pill" style="background:linear-gradient(135deg,var(--gold),var(--gold-l));color:#fff;font-weight:700;" href="#signup">Get started free</a>
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
  <div class="container hero-inner">
    <div class="row align-items-center g-5">
      <!-- Left column -->
      <div class="col-lg-6">
        <div class="hero-badge"><i class="bi bi-stars"></i>AI-Powered CV Tailoring</div>
        <h1 class="hero-h1">Get shortlisted,<br>every time.</h1>
        <p class="hero-sub">Paste any job description. Get a tailored, ATS-optimised CV in 60 seconds. Your template, your format, zero hallucination.</p>
        <div class="hero-cta-row">
          <a href="#signin" class="btn-hero-outline"><i class="bi bi-box-arrow-in-right me-1"></i>Sign in</a>
          <a href="#signup" class="btn-hero-gold">Create free account &rarr;</a>
        </div>
        <div class="hero-trust">
          <i class="bi bi-lock-fill"></i>
          Free to use &nbsp;&middot;&nbsp; Bring your own API key &nbsp;&middot;&nbsp; Your data stays yours
        </div>
      </div>
      <!-- Right column: floating CV card -->
      <div class="col-lg-6">
        <div class="cv-mockup-wrap">
          <div class="cv-mockup">
            <div class="cv-ai-badge">&#10024; AI Tailored</div>
            <div class="cv-mock-name">Alexandra Chen</div>
            <div class="cv-mock-contact">alexandra@email.com &nbsp;&bull;&nbsp; linkedin.com/in/achen &nbsp;&bull;&nbsp; London, UK</div>
            <div class="cv-mock-section">Experience</div>
            <div class="cv-mock-line" style="width:90%"></div>
            <div class="cv-mock-line" style="width:80%"></div>
            <div class="cv-mock-line" style="width:95%"></div>
            <div class="cv-mock-line" style="width:70%"></div>
            <div class="cv-mock-line" style="width:85%"></div>
            <br>
            <div class="cv-mock-section">Education</div>
            <div class="cv-mock-line" style="width:75%"></div>
            <div class="cv-mock-line" style="width:60%"></div>
            <br>
            <div class="cv-mock-section">Skills</div>
            <div class="cv-mock-skills">
              <div class="cv-mock-skill" style="width:55px"></div>
              <div class="cv-mock-skill" style="width:40px"></div>
              <div class="cv-mock-skill" style="width:65px"></div>
              <div class="cv-mock-skill" style="width:48px"></div>
              <div class="cv-mock-skill" style="width:72px"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- How it works -->
<section class="how-section">
  <div class="container">
    <div class="text-center mb-5">
      <div class="section-eyebrow">The workflow</div>
      <h2 style="font-size:2rem;font-weight:800;color:var(--navy);letter-spacing:-.03em;margin-bottom:.6rem;">Three steps to your next interview</h2>
      <p style="color:var(--muted);font-size:.95rem;">Used by MBA candidates at top business schools worldwide</p>
    </div>
    <div class="row g-4">
      <div class="col-md-4">
        <div class="step-card">
          <div class="step-icon-circle ic-indigo"><i class="bi bi-database"></i></div>
          <div class="step-num">01</div>
          <div class="step-title">Build your experience bank</div>
          <div class="step-desc">Upload your CV or paste your experience. AI extracts every role, bullet and skill — structured and ready to use.</div>
        </div>
      </div>
      <div class="col-md-4">
        <div class="step-card">
          <div class="step-icon-circle ic-purple"><i class="bi bi-file-earmark-text"></i></div>
          <div class="step-num">02</div>
          <div class="step-title">Paste any job description</div>
          <div class="step-desc">Copy-paste any JD from any company. Our AI reads the language, identifies key priorities and matches your strongest experience.</div>
        </div>
      </div>
      <div class="col-md-4">
        <div class="step-card">
          <div class="step-icon-circle ic-emerald"><i class="bi bi-download"></i></div>
          <div class="step-num">03</div>
          <div class="step-title">Download your tailored CV</div>
          <div class="step-desc">Get a Word + PDF file with bullets rewritten in STAR format using the JD's exact language. Same template, perfect fit.</div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- Auth -->
<section class="auth-section" id="signin">
  <div class="container">
    <div class="row g-4 justify-content-center" style="max-width:860px;margin:0 auto;">
      <!-- Sign in -->
      <div class="col-md-6">
        <div class="auth-card">
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
            <button type="submit" class="btn-auth-indigo">Sign in &rarr;</button>
          </form>
        </div>
      </div>
      <!-- Create account -->
      <div class="col-md-6" id="signup">
        <div class="auth-card">
          <div class="auth-card-title">
            <div class="auth-icon ic-sign-up"><i class="bi bi-person-plus"></i></div>
            Create your account
          </div>
          <form method="post" action="/signup">
            <div class="auth-mb">
              <label class="auth-label">Full name</label>
              <input name="name" class="auth-input" required placeholder="Your full name">
            </div>
            <div class="auth-mb">
              <label class="auth-label">Email</label>
              <input name="email" type="email" class="auth-input" required autocomplete="email" placeholder="you@email.com">
            </div>
            <div class="auth-mb">
              <label class="auth-label">Password</label>
              <input name="password" type="password" class="auth-input" required autocomplete="new-password" minlength="8" placeholder="Min. 8 characters">
            </div>
            <button type="submit" class="btn-auth-gold">Create free account &rarr;</button>
          </form>
        </div>
      </div>
    </div>
    <div class="security-note">
      <i class="bi bi-shield-lock"></i>
      Your API key is encrypted at rest and only used for CV generation. We never store your CV content.
    </div>
  </div>
</section>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
// 3D tilt on step cards
document.querySelectorAll('.step-card').forEach(card => {
  card.addEventListener('mousemove', e => {
    const r = card.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const rx = ((y - r.height/2) / r.height) * -10;
    const ry = ((x - r.width/2) / r.width) * 10;
    card.style.transform = `perspective(700px) rotateX(${rx}deg) rotateY(${ry}deg) translateY(-8px)`;
    card.style.boxShadow = '0 24px 56px rgba(15,23,42,0.2)';
  });
  card.addEventListener('mouseleave', () => {
    card.style.transform = '';
    card.style.boxShadow = '';
  });
});

// Scroll slide-in
const observer = new IntersectionObserver(entries => {
  entries.forEach((e, i) => {
    if (e.isIntersecting) {
      setTimeout(() => e.target.classList.add('visible'), i * 120);
    }
  });
}, { threshold: 0.15 });
document.querySelectorAll('.step-card').forEach(c => observer.observe(c));
</script>
</body>
</html>"""


# ── Dashboard ──────────────────────────────────────────────────────────────────

_DASHBOARD = _BASE.replace("{% block content %}{% endblock %}", """
<!-- Greeting -->
<div class="mb-4" style="padding-top:.5rem;">
  <h2 style="font-weight:800;font-size:1.85rem;letter-spacing:-.04em;color:var(--navy);margin-bottom:.25rem;">
    Good to see you, {{ name }} &#128075;
  </h2>
  <p style="color:var(--muted);font-size:.95rem;margin:0;">Ready to apply? Let's build something great.</p>
</div>

{% if not (has_bank and has_template and has_ai) %}
<div class="mb-4" style="background:var(--navy-80);border-radius:var(--r16);border-left:4px solid var(--gold);padding:1.75rem 1.75rem 1.5rem;box-shadow:var(--shadow-md);">
  <div style="font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--gold-l);margin-bottom:.65rem;">Setup checklist</div>
  <div style="font-weight:700;font-size:1.05rem;color:#fff;margin-bottom:1.25rem;">Complete setup to start generating CVs</div>
  <!-- Step 1 -->
  <div class="d-flex align-items-start gap-3 mb-3 {% if has_ai %}opacity-50{% endif %}">
    <span class="step-badge mt-1" style="flex-shrink:0;">{% if has_ai %}<i class="bi bi-check-lg" style="font-size:.75rem;"></i>{% else %}1{% endif %}</span>
    <div>
      <div style="font-weight:600;font-size:.9rem;color:{% if has_ai %}rgba(255,255,255,0.5){% else %}#fff{% endif %};">
        {% if has_ai %}<s style="opacity:.6;">Add your API key</s>
        {% else %}<a href="/settings" style="color:var(--gold-l);text-decoration:none;">Add your Anthropic API key</a>{% endif %}
      </div>
      <div style="font-size:.78rem;color:rgba(255,255,255,0.4);margin-top:.2rem;">
        Go to <a href="https://console.anthropic.com/settings/keys" target="_blank" style="color:var(--gold-l);opacity:.8;">console.anthropic.com</a> &rarr; copy your key &rarr; paste in Settings. ~$0.02 per CV.
      </div>
    </div>
  </div>
  <!-- Step 2 -->
  <div class="d-flex align-items-start gap-3 mb-3 {% if has_bank %}opacity-50{% endif %}">
    <span class="step-badge mt-1" style="flex-shrink:0;">{% if has_bank %}<i class="bi bi-check-lg" style="font-size:.75rem;"></i>{% else %}2{% endif %}</span>
    <div>
      <div style="font-weight:600;font-size:.9rem;color:{% if has_bank %}rgba(255,255,255,0.5){% else %}#fff{% endif %};">
        {% if has_bank %}<s style="opacity:.6;">Build your experience bank</s>
        {% else %}<a href="/bank/create" style="color:var(--gold-l);text-decoration:none;">Build your experience bank</a>{% endif %}
      </div>
      <div style="font-size:.78rem;color:rgba(255,255,255,0.4);margin-top:.2rem;">
        Upload your existing CV (.docx/.pdf) or paste your experience — AI extracts every role, bullet, and skill automatically.
      </div>
    </div>
  </div>
  <!-- Step 3 -->
  <div class="d-flex align-items-start gap-3 {% if has_template %}opacity-50{% endif %}">
    <span class="step-badge mt-1" style="flex-shrink:0;">{% if has_template %}<i class="bi bi-check-lg" style="font-size:.75rem;"></i>{% else %}3{% endif %}</span>
    <div>
      <div style="font-weight:600;font-size:.9rem;color:{% if has_template %}rgba(255,255,255,0.5){% else %}#fff{% endif %};">
        {% if has_template %}<s style="opacity:.6;">Upload your CV template</s>
        {% else %}<a href="/upload-template" style="color:var(--gold-l);text-decoration:none;">Upload your CV template (.docx)</a>{% endif %}
      </div>
      <div style="font-size:.78rem;color:rgba(255,255,255,0.4);margin-top:.2rem;">
        Your formatted base CV — the app preserves its exact layout, bullet counts, and sections.
      </div>
    </div>
  </div>
</div>
{% endif %}

<!-- Status cards -->
<div class="row g-3 mb-4">
  <!-- Bank -->
  <div class="col-sm-4">
    <div class="card card-hover h-100" style="border-left:4px solid var(--indigo)!important;">
      <div class="card-body p-3 d-flex flex-column">
        <div class="d-flex align-items-center gap-2 mb-2">
          <div style="width:36px;height:36px;background:linear-gradient(135deg,var(--indigo),var(--indigo-l));border-radius:10px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:.95rem;flex-shrink:0;">
            <i class="bi bi-database"></i>
          </div>
          <div>
            <div style="font-weight:700;font-size:.9rem;color:var(--navy);">Info Bank</div>
            <div style="display:flex;align-items:center;gap:.3rem;">
              <span class="sdot {{ 'sdot-ok' if has_bank else 'sdot-no' }}"></span>
              <span style="font-size:.72rem;color:var(--muted);">{{ 'Active' if has_bank else 'Not set up' }}</span>
            </div>
          </div>
        </div>
        <p class="text-muted small mb-3" style="font-size:.8rem;">{{ 'Your experience & bullets are ready.' if has_bank else 'Upload your CV or paste your experience.' }}</p>
        {% if has_bank %}
          <div class="d-grid gap-1 mt-auto">
            <a href="/bank" class="btn btn-ghost btn-sm">Edit Bank</a>
            <a href="/bank/import" class="btn btn-ghost btn-sm">+ Import more</a>
          </div>
        {% else %}
          <a href="/bank/create" class="btn btn-indig btn-sm mt-auto">Set up Bank</a>
        {% endif %}
      </div>
    </div>
  </div>
  <!-- Template -->
  <div class="col-sm-4">
    <div class="card card-hover h-100" style="border-left:4px solid var(--emerald)!important;">
      <div class="card-body p-3 d-flex flex-column">
        <div class="d-flex align-items-center gap-2 mb-2">
          <div style="width:36px;height:36px;background:linear-gradient(135deg,var(--emerald),#10b981);border-radius:10px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:.95rem;flex-shrink:0;">
            <i class="bi bi-file-earmark-word"></i>
          </div>
          <div>
            <div style="font-weight:700;font-size:.9rem;color:var(--navy);">CV Template</div>
            <div style="display:flex;align-items:center;gap:.3rem;">
              <span class="sdot {{ 'sdot-ok' if has_template else 'sdot-no' }}"></span>
              <span style="font-size:.72rem;color:var(--muted);">{{ 'Uploaded' if has_template else 'Not uploaded' }}</span>
            </div>
          </div>
        </div>
        <p class="text-muted small mb-3" style="font-size:.8rem;">{{ 'Template ready — format preserved on generation.' if has_template else 'Upload your base .docx template.' }}</p>
        <a href="/upload-template" class="btn {{ 'btn-ghost' if has_template else 'btn-success-custom' }} btn-sm mt-auto">
          {{ 'Replace Template' if has_template else 'Upload Template' }}
        </a>
      </div>
    </div>
  </div>
  <!-- API Key -->
  <div class="col-sm-4">
    <div class="card card-hover h-100" style="border-left:4px solid var(--gold)!important;">
      <div class="card-body p-3 d-flex flex-column">
        <div class="d-flex align-items-center gap-2 mb-2">
          <div style="width:36px;height:36px;background:linear-gradient(135deg,var(--gold),var(--gold-l));border-radius:10px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:.95rem;flex-shrink:0;">
            <i class="bi bi-key"></i>
          </div>
          <div>
            <div style="font-weight:700;font-size:.9rem;color:var(--navy);">API Key</div>
            <div style="display:flex;align-items:center;gap:.3rem;">
              <span class="sdot {{ 'sdot-ok' if has_ai else 'sdot-no' }}"></span>
              <span style="font-size:.72rem;color:var(--muted);">{{ ai_label + ' connected' if has_ai else 'Not configured' }}</span>
            </div>
          </div>
        </div>
        <p class="text-muted small mb-3" style="font-size:.8rem;">{{ 'Your AI key is encrypted and ready.' if has_ai else 'Add your Anthropic / OpenAI / Gemini key.' }}</p>
        <a href="/settings" class="btn {{ 'btn-ghost' if has_ai else 'btn-gold' }} btn-sm mt-auto">
          {{ 'Change Settings' if has_ai else 'Add API Key' }}
        </a>
      </div>
    </div>
  </div>
</div>

{% if has_bank and has_template and has_ai %}
<div class="card" style="border-top:3px solid transparent;border-image:linear-gradient(90deg,var(--indigo),var(--gold-l)) 1;">
  <div class="card-body p-4">
    <h5 style="font-weight:800;font-size:1.15rem;color:var(--navy);margin-bottom:.35rem;">
      <i class="bi bi-magic me-1" style="color:var(--indigo);"></i>Generate your tailored CV
    </h5>
    <p style="color:var(--muted);font-size:.85rem;margin-bottom:1.25rem;">
      Paste the full job description below &mdash; the more detail, the better the tailoring.
    </p>
    <form method="post" action="/generate" id="genForm">
      <div class="mb-3">
        <textarea name="jd_text" class="form-control" rows="14" style="min-height:300px;font-family:'Inter',sans-serif;font-size:.85rem;"
          placeholder="Paste the full job description here — include role title, responsibilities, requirements, and any keywords you spot…" required></textarea>
      </div>
      <button class="btn-generate" type="submit" id="genBtn">
        &#10024; Tailor my CV for this role &rarr;
      </button>
    </form>
  </div>
</div>
{% endif %}
""").replace("{% block scripts %}{% endblock %}", """
<script>
document.getElementById('genForm')?.addEventListener('submit', function() {
  document.getElementById('overlayTitle').textContent = 'Analysing JD & tailoring your CV…';
  document.getElementById('overlaySub').textContent   = 'AI is rewriting your bullets in the JD\'s language — ~20–40 seconds';
  document.getElementById('loadingOverlay').style.display = 'flex';
  document.getElementById('genBtn').disabled = true;
});
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
    <form method="post" action="/upload-template" enctype="multipart/form-data">
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

    <form method="post" action="/settings">
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
  You can also add sections manually without an API key.
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
  <button class="cc-tab-btn" data-cc-tab="tab-manual">
    <i class="bi bi-plus-circle"></i>Add manually
  </button>
</div>

<div class="card" style="border-radius:0 0 var(--r16) var(--r16)!important;border-top:none;">
  <div class="card-body p-4">

    <!-- File upload tab -->
    <div class="cc-tab-pane" id="tab-file">
      <h6 style="font-weight:700;color:var(--navy);margin-bottom:.4rem;">Upload your CV or experience notes</h6>
      <p style="color:var(--muted);font-size:.85rem;line-height:1.6;margin-bottom:1rem;">
        Any format works: your current CV, a Word doc of notes, a PDF, or a plain text file.
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
        Paste your CV, LinkedIn text, rough bullet points, or a brain-dump of everything you've done.
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

    <!-- Manual tab -->
    <div class="cc-tab-pane" id="tab-manual" style="display:none;">
      <h6 style="font-weight:700;color:var(--navy);margin-bottom:.4rem;">Add a section manually</h6>
      <p style="color:var(--muted);font-size:.85rem;line-height:1.6;margin-bottom:1rem;">
        Add one role or project at a time. You can always add bullets to any section from the bank editor.
      </p>
      <form method="post" action="/bank/section/add">
        <input type="hidden" name="is_first" value="{{ '1' if is_create else '0' }}">
        <!-- Type toggle -->
        <div style="display:flex;gap:.5rem;margin-bottom:.5rem;">
          <input type="radio" class="btn-check" name="section_type" id="type-job" value="job" checked>
          <label for="type-job" style="flex:1;padding:.6rem .9rem;border:2px solid var(--border);border-radius:var(--r10);cursor:pointer;text-align:center;font-weight:600;font-size:.84rem;color:var(--navy);background:var(--bg);transition:border-color .18s,background .18s;">
            <i class="bi bi-briefcase me-1"></i>Role at an organisation
          </label>
          <input type="radio" class="btn-check" name="section_type" id="type-project" value="project">
          <label for="type-project" style="flex:1;padding:.6rem .9rem;border:2px solid var(--border);border-radius:var(--r10);cursor:pointer;text-align:center;font-weight:600;font-size:.84rem;color:var(--navy);background:var(--bg);transition:border-color .18s,background .18s;">
            <i class="bi bi-layers me-1"></i>Project / Activity
          </label>
        </div>
        <div style="font-size:.78rem;color:var(--muted);margin-bottom:1rem;">
          <span id="type-hint-job">Job, internship, research position, volunteer role, leadership title — anything where you had a role at an organisation</span>
          <span id="type-hint-project" style="display:none">Personal project, case competition, independent research, open-source, publication, hackathon, club activity</span>
        </div>
        <div class="row g-2">
          <div class="col-md-6" id="company-field">
            <label class="fl">Organisation</label>
            <input name="company" class="form-control"
              placeholder="e.g. Goldman Sachs, WHO, Harvard Lab">
          </div>
          <div class="col-md-6" id="role-field">
            <label class="fl">Your title / role</label>
            <input name="role" class="form-control"
              placeholder="e.g. Summer Analyst, Research Assistant">
          </div>
          <div class="col-12" id="project-field" style="display:none">
            <label class="fl">Project / activity name</label>
            <input name="project_name" class="form-control"
              placeholder="e.g. NLP Sentiment Analyser, HBS Case Competition">
          </div>
          <div class="col-md-6">
            <label class="fl">Date range</label>
            <input name="date" class="form-control"
              placeholder="e.g. Jun 2024 – Aug 2024">
          </div>
          <div class="col-md-6">
            <label class="fl">Bullet slots</label>
            <input name="bullet_slots" type="number" min="1" max="6" value="4" class="form-control">
            <div style="font-size:.73rem;color:var(--muted);margin-top:.3rem;">How many bullets on your CV</div>
          </div>
          <div class="col-12">
            <label class="fl">First bullet (optional)</label>
            <textarea name="first_bullet" class="form-control" rows="2"
              placeholder="SubHeading: Achievement · metric · impact"></textarea>
          </div>
        </div>
        <button type="submit" class="btn-success-custom w-100 mt-3" style="display:block;text-align:center;border-radius:var(--r10);padding:.65rem;">
          <i class="bi bi-plus-circle me-1"></i>{{ 'Create Bank &amp; Add Section' if is_create else 'Add Section to Bank' }}
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
    <a href="/bank/import" class="btn-success-custom" style="font-size:.8rem;padding:.4rem .9rem;border-radius:var(--r10);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem;">
      <i class="bi bi-plus-circle"></i>Import more
    </a>
    <a href="/bank/section/add" class="btn-indig" style="font-size:.8rem;padding:.4rem .9rem;border-radius:var(--r10);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem;">
      <i class="bi bi-briefcase"></i>Add section
    </a>
    <a href="/bank/download" class="btn-ghost" style="font-size:.8rem;padding:.4rem .9rem;border-radius:var(--r10);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem;">
      <i class="bi bi-download"></i>Export JSON
    </a>
  </div>
</div>

{% if not bank %}
<div class="card p-5 text-center">
  <i class="bi bi-database-x" style="font-size:2.5rem;color:var(--muted);display:block;margin-bottom:.75rem;"></i>
  <p style="color:var(--muted);margin-bottom:1.25rem;">No info bank yet. Upload your CV or start from scratch.</p>
  <a href="/bank/create" class="btn-indig" style="display:inline-block;padding:.6rem 1.5rem;border-radius:var(--r10);text-decoration:none;">Set up your Bank</a>
</div>
{% else %}

<!-- STAR format guide (collapsible) -->
<div class="star-guide mb-3" style="cursor:pointer;">
  <div class="d-flex justify-content-between align-items-center"
       data-bs-toggle="collapse" data-bs-target="#starGuide">
    <span><strong>STAR bullet format</strong> &mdash; how bullets are written for every JD</span>
    <i class="bi bi-chevron-down" style="color:var(--indigo);font-size:.8rem;"></i>
  </div>
  <div class="collapse" id="starGuide">
    <hr style="border-color:rgba(79,70,229,0.2);margin:.6rem 0;">
    <div class="mb-1"><strong>Format:</strong>
      <code>SubHeading: [Strong verb] [what you did + context], [result with metric]</code>
    </div>
    <div class="mb-2" style="font-size:.78rem;color:#4c1d95;">
      SubHeading = bold JD-matched keyword &nbsp;&middot;&nbsp;
      Verb = Led / Drove / Built / Delivered / Spearheaded &nbsp;&middot;&nbsp;
      Result = $, %, &times;, rank, or directional outcome
    </div>
    <div class="mb-1"><span style="color:#dc2626;font-size:.76rem;font-weight:600;">&cross; Weak:</span>
      <code>Helped with audit work for financial services client</code>
    </div>
    <div><span style="color:var(--emerald);font-size:.76rem;font-weight:600;">&check; STAR:</span>
      <code>Financial Controls: Led statutory audit for mid-cap healthcare client, identifying &pound;1.2M in revenue recognition errors across 3 business units</code>
    </div>
    <div class="mt-2" style="font-size:.77rem;color:#4c1d95;">
      <i class="bi bi-info-circle me-1"></i>
      <strong>You don't need perfect bullets in your bank</strong> &mdash; just accurate facts and any metrics you have.
      The AI rewrites everything in STAR format using the specific JD's language at generation time.
    </div>
  </div>
</div>

<!-- Skills & Certifications -->
<div class="card mb-3" style="border-left:4px solid var(--gold)!important;">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span style="font-weight:700;color:var(--navy);display:flex;align-items:center;gap:.4rem;">
      <i class="bi bi-stars" style="color:var(--gold);"></i>Skills &amp; Certifications
    </span>
    <button class="btn btn-xs btn-ghost" onclick="toggleEdit('skills-edit')" style="font-size:.76rem;padding:.25rem .65rem;">Edit</button>
  </div>
  <div class="card-body p-3">
    <div id="skills-view">
      <pre style="white-space:pre-wrap;font-size:.82rem;margin:0;font-family:'Inter',sans-serif;color:var(--text);">{{ bank.skills_text or '(none yet)' }}</pre>
      {% if bank.certifications %}
        <hr style="border-color:var(--border);margin:.6rem 0;">
        <div style="font-size:.8rem;color:var(--muted);">
          <i class="bi bi-patch-check me-1" style="color:var(--emerald);"></i>
          {{ ', '.join(bank.certifications) }}
        </div>
      {% endif %}
    </div>
    <div id="skills-edit" style="display:none">
      <form method="post" action="/bank/skills">
        <label class="fl mt-1">Skills (one category per line)</label>
        <div style="font-size:.75rem;color:var(--muted);margin-bottom:.4rem;">Format: <code style="background:#eef2ff;color:var(--indigo);border-radius:4px;padding:.1rem .3rem;">Category: skill &middot; skill &middot; skill</code></div>
        <textarea name="skills_text" class="form-control mb-2" rows="6"
          style="font-size:.82rem;">{{ bank.skills_text or '' }}</textarea>
        <label class="fl">Certifications (comma-separated)</label>
        <input name="certifications" class="form-control mb-2" style="font-size:.82rem;"
          value="{{ ', '.join(bank.certifications or []) }}">
        <label class="fl">Skills section header in your template</label>
        <input name="skills_header" class="form-control mb-3" style="font-size:.82rem;"
          value="{{ bank.skills_header or 'skills' }}"
          placeholder="e.g. Skills & Additional Information">
        <div class="d-flex gap-2">
          <button class="btn-indig" style="font-size:.82rem;padding:.4rem .9rem;border-radius:var(--r10);">Save</button>
          <button type="button" class="btn-ghost" style="font-size:.82rem;padding:.4rem .9rem;border-radius:var(--r10);"
            onclick="toggleEdit('skills-edit')">Cancel</button>
        </div>
      </form>
    </div>
  </div>
</div>

<!-- Experience & project sections -->
{% for key, sec in bank.sections.items() %}
<div class="card section-card {{ 'type-company' if sec.company else 'type-project' }}">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span style="display:flex;align-items:center;gap:.4rem;font-weight:600;color:var(--navy);">
      {% if sec.company %}
        <i class="bi bi-briefcase" style="color:var(--indigo);"></i>
        {{ sec.company }}
        {% if sec.role %}<span style="color:var(--muted);font-weight:400;font-size:.83rem;">&middot; {{ sec.role }}</span>{% endif %}
      {% elif sec.project_name %}
        <i class="bi bi-layers" style="color:var(--emerald);"></i>
        {{ sec.project_name }}
      {% else %}
        <i class="bi bi-card-text" style="color:var(--muted);"></i>
        {{ key }}
      {% endif %}
      {% if sec.date %}<span style="color:var(--muted);font-weight:400;font-size:.78rem;margin-left:.25rem;">{{ sec.date }}</span>{% endif %}
    </span>
    <span class="badge-pill badge-navy" style="font-size:.72rem;">{{ (sec.bullets or [])|length }} bullets &middot; {{ sec.bullet_slots or 3 }} slots</span>
  </div>
  <div class="card-body p-3 pb-2">
    {% for b in (sec.bullets or []) %}
    <div class="bullet-row py-1" id="bullet-{{ b.id }}">
      <div id="view-{{ b.id }}" class="d-flex justify-content-between align-items-start gap-2">
        <span style="font-size:.84rem;color:var(--text);line-height:1.5;">{{ b.text }}</span>
        <div class="d-flex gap-1 flex-shrink-0">
          <button class="btn btn-xs btn-ghost"
            onclick="showEditBullet('{{ key }}','{{ b.id }}')">Edit</button>
          <form method="post" action="/bank/section/{{ key }}/bullet/{{ b.id }}/delete"
            onsubmit="return confirm('Delete this bullet?')" style="display:inline">
            <button class="btn btn-xs" style="padding:.2rem .5rem;font-size:.73rem;border-radius:6px;background:transparent;border:1px solid rgba(220,38,38,0.3);color:#dc2626;cursor:pointer;">&times;</button>
          </form>
        </div>
      </div>
      <div id="edit-{{ b.id }}" style="display:none;margin-top:.5rem;">
        <form method="post" action="/bank/section/{{ key }}/bullet/{{ b.id }}/update">
          <textarea name="text" class="form-control mb-2" rows="2"
            style="font-size:.82rem;">{{ b.text }}</textarea>
          <div class="d-flex gap-2">
            <button class="btn-indig" style="font-size:.78rem;padding:.3rem .75rem;border-radius:8px;">Save</button>
            <button type="button" class="btn-ghost" style="font-size:.78rem;padding:.3rem .75rem;border-radius:8px;"
              onclick="document.getElementById('edit-{{ b.id }}').style.display='none';
                       document.getElementById('view-{{ b.id }}').style.display='flex'">Cancel</button>
          </div>
        </form>
      </div>
    </div>
    {% endfor %}

    <!-- Add bullet -->
    <div class="mt-2" id="add-area-{{ key }}" style="display:none">
      <form method="post" action="/bank/section/{{ key }}/bullet/add">
        <textarea name="text" class="form-control mb-2" rows="2"
          placeholder="SubHeading: Led [what you did + context], [result with $/%/metric]"
          style="font-size:.82rem;" required></textarea>
        <div class="d-flex gap-2">
          <button class="btn-success-custom" style="font-size:.78rem;padding:.3rem .75rem;border-radius:8px;">+ Add bullet</button>
          <button type="button" class="btn-ghost" style="font-size:.78rem;padding:.3rem .75rem;border-radius:8px;"
            onclick="document.getElementById('add-area-{{ key }}').style.display='none'">Cancel</button>
        </div>
      </form>
    </div>
    <button class="btn btn-xs" style="margin-top:.5rem;font-size:.76rem;padding:.25rem .65rem;border-radius:6px;background:transparent;border:1.5px solid var(--emerald);color:var(--emerald);cursor:pointer;"
      onclick="document.getElementById('add-area-{{ key }}').style.display='block';this.style.display='none'">
      + Add bullet
    </button>

    <!-- Slots -->
    <form method="post" action="/bank/section/{{ key }}/slots"
      class="d-flex align-items-center gap-2 mt-3" style="font-size:.78rem;">
      <label style="color:var(--muted);margin:0;white-space:nowrap;">Bullet slots for AI:</label>
      <input name="slots" type="number" min="1" max="8" value="{{ sec.bullet_slots or 3 }}"
        class="form-control form-control-sm" style="width:60px;">
      <button class="btn-ghost" style="font-size:.76rem;padding:.25rem .65rem;border-radius:8px;">Update</button>
    </form>
  </div>
</div>
{% endfor %}

{% endif %}
<div class="mt-3 d-flex gap-2 flex-wrap">
  <a href="/dashboard" style="font-size:.82rem;color:var(--muted);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem;padding:.35rem .75rem;border:1.5px solid var(--border);border-radius:var(--r10);background:var(--surface);">
    <i class="bi bi-arrow-left"></i> Dashboard
  </a>
  {% if bank %}
  <a href="/bank/import" style="font-size:.82rem;color:var(--emerald);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem;padding:.35rem .75rem;border:1.5px solid rgba(5,150,105,0.3);border-radius:var(--r10);background:var(--surface);">
    + Import more experience
  </a>
  {% endif %}
</div>
""").replace("{% block scripts %}{% endblock %}", """
<script>
function toggleEdit(id) {
  const el = document.getElementById(id);
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
function showEditBullet(secKey, bulletId) {
  document.getElementById('view-' + bulletId).style.display = 'none';
  document.getElementById('edit-' + bulletId).style.display = 'block';
}
</script>
""")


# ── Review (edit AI-tailored bullets before generating DOCX) ──────────────────

_REVIEW = _BASE.replace("{% block content %}{% endblock %}", """
<!-- Header -->
<div class="d-flex align-items-center flex-wrap gap-2 mb-1">
  <h4 style="font-weight:800;font-size:1.35rem;color:var(--navy);letter-spacing:-.03em;margin:0;">Review your tailored CV</h4>
  <span class="badge-pill badge-indigo">{{ company }}</span>
  <span class="badge-pill badge-navy">{{ role }}</span>
</div>
<p style="color:var(--muted);font-size:.85rem;margin:.5rem 0 1rem;">
  Every bullet has been rewritten in STAR format using this JD's exact language.
  Read through, fix anything that doesn't sound right, then generate.
</p>

<!-- STAR tip gradient box -->
<div style="background:linear-gradient(135deg,#eef2ff,#f5f3ff);border:1px solid rgba(79,70,229,0.2);border-radius:var(--r10);padding:.85rem 1.1rem;font-size:.8rem;color:#312e81;margin-bottom:1.5rem;">
  <strong>STAR check:</strong> each bullet should read as
  <code style="background:rgba(79,70,229,0.12);border-radius:4px;padding:.1rem .3rem;color:var(--indigo);">SubHeading: [Verb] [what + context], [result]</code>
  &nbsp;&middot;&nbsp; SubHeading uses a keyword from the JD
  &nbsp;&middot;&nbsp; Result is a number, %, $ or clear outcome
  &nbsp;&middot;&nbsp; Max 215 chars
</div>

<form method="post" action="/review/{{ token }}/confirm" id="reviewForm">

  {% for sec_key, bullets in sections.items() %}
  <div class="card mb-3">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span style="font-weight:700;color:var(--navy);display:flex;align-items:center;gap:.4rem;">
        {% if sec_key in labels %}
          {% set lbl = labels[sec_key] %}
          {% if lbl.get('company') %}
            <i class="bi bi-briefcase" style="color:var(--indigo);"></i>{{ lbl.company }}
            {% if lbl.get('role') %}<span style="color:var(--muted);font-weight:400;font-size:.82rem;"> &middot; {{ lbl.role }}</span>{% endif %}
          {% elif lbl.get('project_name') %}
            <i class="bi bi-layers" style="color:var(--emerald);"></i>{{ lbl.project_name }}
          {% else %}
            <i class="bi bi-card-text" style="color:var(--muted);"></i>{{ sec_key }}
          {% endif %}
        {% else %}
          {{ sec_key }}
        {% endif %}
      </span>
      <span class="badge-pill badge-navy" style="font-size:.72rem;">{{ bullets|length }} bullet{{ 's' if bullets|length != 1 }}</span>
    </div>
    <div class="card-body p-3">
      {% for bullet in bullets %}
      <div class="review-bullet">
        <textarea name="bullet_{{ sec_key }}_{{ loop.index0 }}" class="form-control border-0 p-0"
          rows="2" style="resize:vertical;font-size:.84rem;background:transparent;font-family:'Inter',sans-serif;">{{ bullet }}</textarea>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endfor %}

  <!-- Skills -->
  <div class="card mb-4" style="border-left:4px solid var(--gold)!important;">
    <div class="card-header" style="font-weight:700;color:var(--navy);">
      <i class="bi bi-stars me-1" style="color:var(--gold);"></i>Skills Section
    </div>
    <div class="card-body p-3">
      <div style="font-size:.75rem;color:var(--muted);margin-bottom:.5rem;">
        Format: <code style="background:#eef2ff;color:var(--indigo);border-radius:4px;padding:.1rem .3rem;">Category: skill &middot; skill</code> &mdash; one category per line
      </div>
      <textarea name="skills_text" class="form-control" rows="6"
        style="font-size:.83rem;font-family:'Inter',sans-serif;">{{ skills_text }}</textarea>
    </div>
  </div>

  <div class="d-grid gap-2 mb-2">
    <button type="submit" class="btn-generate" id="confirmBtn" onclick="showGenLoading()">
      <i class="bi bi-file-earmark-arrow-down me-1"></i>Generate CV files (.docx + .pdf)
    </button>
    <a href="/dashboard" class="btn-ghost" style="display:block;text-align:center;padding:.6rem;border-radius:var(--r10);text-decoration:none;color:var(--muted);">&larr; Start over</a>
  </div>
</form>
""").replace("{% block scripts %}{% endblock %}", """
<script>
function showGenLoading() {
  document.getElementById('overlayTitle').textContent = 'Building your CV files\u2026';
  document.getElementById('overlaySub').textContent   = 'Injecting bullets into your template and converting to PDF';
  document.getElementById('loadingOverlay').style.display = 'flex';
  document.getElementById('confirmBtn').disabled = true;
}
</script>
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

    {% if one_page %}
      <div style="display:inline-flex;align-items:center;gap:.35rem;background:#f0fdf4;border:1px solid rgba(5,150,105,0.2);border-radius:20px;padding:.3rem .85rem;font-size:.78rem;font-weight:600;color:var(--emerald);margin-bottom:1.5rem;">
        <i class="bi bi-check-circle-fill"></i>1-page verified
      </div>
    {% else %}
      <div style="display:inline-flex;align-items:center;gap:.35rem;background:#fffbeb;border:1px solid rgba(217,119,6,0.2);border-radius:20px;padding:.3rem .85rem;font-size:.78rem;font-weight:600;color:var(--gold);margin-bottom:1.5rem;">
        <i class="bi bi-exclamation-triangle-fill"></i>May exceed 1 page — shorten a few bullets
      </div>
    {% endif %}

    <div style="display:grid;gap:.6rem;margin-bottom:1.5rem;">
      <a href="/download/{{ token }}/docx" class="btn-indig" style="display:block;padding:.75rem;border-radius:var(--r10);text-decoration:none;font-size:.95rem;">
        <i class="bi bi-file-earmark-word me-1"></i>Download Word (.docx)
      </a>
      {% if has_pdf %}
      <a href="/download/{{ token }}/pdf" style="display:block;padding:.7rem;border-radius:var(--r10);text-decoration:none;font-size:.9rem;border:1.5px solid var(--indigo);color:var(--indigo);font-weight:600;transition:background .18s;"
         onmouseover="this.style.background='#eef2ff'" onmouseout="this.style.background=''">
        <i class="bi bi-file-earmark-pdf me-1"></i>Download PDF
      </a>
      {% else %}
      <div style="padding:.7rem;border-radius:var(--r10);font-size:.9rem;border:1.5px solid var(--border);color:var(--muted);text-align:center;">
        <i class="bi bi-file-earmark-pdf me-1"></i>PDF not available (see deployment docs)
      </div>
      {% endif %}
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
        except EnvironmentError as e:
            flash(str(e), "error")
            return redirect(url_for("settings_page"))
    sb.save_ai_settings(user_id, provider, enc_key, model)
    session.update({"has_ai": True, "ai_provider": provider, "ai_model": model})
    flash("AI settings saved.", "success")
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
    if suffix not in (".docx", ".pdf", ".txt"):
        flash("Unsupported file type. Please upload .docx, .pdf, or .txt", "error")
        return redirect(url_for("bank_import_page" if append else "bank_create_page"))
    try:
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
        flash(f"Bank {verb} {n_sections} section(s) from your file. Review and edit below.", "success")
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
        flash(f"Bank {verb} {n_sections} section(s). Review and edit below.", "success")
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
    return render_template_string(_BANK, bank=bank)


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
        # 1. Load master bank
        master_bank = sb.load_master_bank(user_id)

        # 2. Load AI settings + decrypt key
        ai_cfg = sb.load_ai_settings(user_id)
        if not ai_cfg.get("api_key_enc"):
            flash("No AI API key configured. Go to Settings first.", "error")
            return redirect(url_for("dashboard"))
        provider = ai_cfg.get("provider", "anthropic")
        raw_key  = decrypt_key(ai_cfg["api_key_enc"])
        model    = ai_cfg.get("model") or None

        # 3. Download CV template + read slot counts + merge format rules
        template_path = tmp_dir / "base_cv.docx"
        sb.download_cv_template(user_id, template_path)
        template_slots = read_template_slots(template_path, master_bank)

        # Inject template format rules (font, bullet length…) into master_bank so
        # both call_ai() and modify_docx() use this user's exact formatting constraints.
        #
        # Lazy back-fill: if format_rules is NULL (template was uploaded before this
        # feature existed), extract from the already-downloaded file and persist so
        # the next generate is instant.
        fmt_rules = sb.load_template_format_rules(user_id)
        if not fmt_rules:
            try:
                fmt_rules = extract_template_format_rules(template_path)
                sb.save_template_format_rules(user_id, fmt_rules)
            except Exception:
                fmt_rules = {}

        if fmt_rules:
            master_bank["format_rules"] = {
                **master_bank.get("format_rules", {}),
                **fmt_rules,   # template extraction always overrides bank defaults
            }

        # 4. Call AI
        result  = call_ai(jd_text, master_bank, provider, raw_key, model,
                          template_slots=template_slots)
        jd_info = result.get("jd_analysis", {})
        company = jd_info.get("company", "Company")
        role    = jd_info.get("role", "Role")
        safe    = re.sub(r"[^\w\s-]", "", f"{company} {role}").strip().replace(" ", "_")

        # 5. Build section labels for the review page
        sections_data = master_bank.get("sections", {})
        labels = {
            key: {
                "company":      sec.get("company", ""),
                "role":         sec.get("role", ""),
                "project_name": sec.get("project_name", ""),
            }
            for key, sec in sections_data.items()
        }

        # 6. Store pending state and redirect to review
        token = _uuid.uuid4().hex
        _pending[token] = {
            "ai_result":     result,
            "master_bank":   master_bank,
            "template_path": template_path,
            "company":       company,
            "role":          role,
            "safe":          safe,
            "labels":        labels,
        }

        return redirect(url_for("review_page", token=token))

    except FileNotFoundError as e:
        flash(str(e), "error")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Generation failed: {e}", "error")

    return redirect(url_for("dashboard"))


@app.route("/review/<token>")
@login_required
def review_page(token):
    pending = _pending.get(token)
    if not pending:
        flash("Session expired or invalid. Please generate again.", "error")
        return redirect(url_for("dashboard"))

    result = pending["ai_result"]
    return render_template_string(
        _REVIEW,
        token      = token,
        sections   = result.get("sections", {}),
        skills_text= result.get("skills_text", ""),
        company    = pending["company"],
        role       = pending["role"],
        labels     = pending["labels"],
    )


@app.route("/review/<token>/confirm", methods=["POST"])
@login_required
def review_confirm(token):
    pending = _pending.get(token)
    if not pending:
        flash("Session expired. Please generate again.", "error")
        return redirect(url_for("dashboard"))

    result      = pending["ai_result"]
    master_bank = pending["master_bank"]
    template_path = pending["template_path"]

    # Rebuild sections from the review form (user may have edited bullets)
    sections = {}
    for sec_key, orig_bullets in result.get("sections", {}).items():
        bullets = []
        for i in range(len(orig_bullets) + 3):   # +3 for safety
            b = request.form.get(f"bullet_{sec_key}_{i}")
            if b is None:
                break
            b = b.strip()
            if b:
                bullets.append(b)
        if bullets:
            sections[sec_key] = bullets

    skills_text       = request.form.get("skills_text", "").strip()
    project_overrides = result.get("project_overrides") or None
    company, role, safe = pending["company"], pending["role"], pending["safe"]

    tmp_dir  = template_path.parent
    out_dir  = tmp_dir / safe
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_path = out_dir / "Tailored_CV.docx"

    try:
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

        _generated[token] = {
            "docx":     docx_path,
            "pdf":      pdf_path,
            "company":  company,
            "role":     role,
            "safe":     safe,
            "one_page": one_page,
        }
        # Clean up pending entry
        _pending.pop(token, None)

        return render_template_string(
            _RESULT,
            token   = token,
            company = company,
            role    = role,
            has_pdf = pdf_path is not None and pdf_path.exists(),
            one_page= one_page,
        )

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
