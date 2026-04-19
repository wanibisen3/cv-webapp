#!/usr/bin/env python3
"""
supabase_client.py — Supabase integration for CV Webapp
=========================================================
Provides helpers for:
  - Auth (sign up / sign in / get user)
  - User profile (name, email, AI settings)
  - Master bullet bank CRUD (save, load, patch)
  - CV template Storage (upload / download)

Environment variables (set in .env):
    SUPABASE_URL   — https://<project-ref>.supabase.co
    SUPABASE_KEY   — anon or service_role key
    ENCRYPT_KEY    — Fernet key for encrypting user API keys
"""

import json
import os
import tempfile
from pathlib import Path

try:
    from supabase import create_client, Client
except ImportError:
    raise ImportError("Run: pip install supabase")

# ─── Client singleton ─────────────────────────────────────────────────────────

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        if not url or not key:
            raise EnvironmentError(
                "SUPABASE_URL and SUPABASE_KEY must be set (in .env or your deployment platform's environment variables)"
            )
        _client = create_client(url, key)
    return _client


# ─── Auth ─────────────────────────────────────────────────────────────────────

def sign_up(email: str, password: str, name: str = "") -> str:
    """Register a new user. Returns user_id (UUID)."""
    client = get_client()
    resp = client.auth.sign_up({"email": email, "password": password})
    if not resp.user:
        raise ValueError("Sign-up failed — check email/password requirements.")
    user_id = resp.user.id
    if name:
        upsert_profile(user_id, name=name, email=email)
    return user_id


def sign_in(email: str, password: str) -> str:
    """Sign in with email + password. Returns user_id (UUID)."""
    client = get_client()
    resp = client.auth.sign_in_with_password({"email": email, "password": password})
    if not resp.user:
        raise ValueError("Sign-in failed — check email/password.")
    return resp.user.id


# ─── Profile ──────────────────────────────────────────────────────────────────

def upsert_profile(user_id: str, name: str = "", email: str = "") -> dict:
    """Create or update a user's profile row (name / email)."""
    client = get_client()
    data = {"id": user_id}
    if name:  data["name"]  = name
    if email: data["email"] = email
    resp = client.table("profiles").upsert(data).execute()
    return resp.data[0] if resp.data else {}


def get_profile(user_id: str) -> dict:
    """Fetch a user's profile row. Returns {} if not found."""
    client = get_client()
    try:
        resp = (
            client.table("profiles")
            .select("*")
            .eq("id", user_id)
            .single()
            .execute()
        )
        return resp.data or {}
    except Exception:
        return {}


# ─── AI Settings (provider + encrypted API key + model) ──────────────────────

def save_ai_settings(user_id: str, provider: str, enc_api_key: str, model: str = "") -> None:
    """
    Save the user's AI provider settings to their profile.
    enc_api_key should already be encrypted by ai_providers.encrypt_key().
    """
    client = get_client()
    settings = {"provider": provider, "api_key_enc": enc_api_key, "model": model}
    client.table("profiles").upsert(
        {"id": user_id, "ai_settings": json.dumps(settings)}
    ).execute()


def load_ai_settings(user_id: str) -> dict:
    """
    Load the user's AI settings.
    Returns {} if not configured.
    """
    profile = get_profile(user_id)
    raw = profile.get("ai_settings")
    if not raw:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return raw  # already a dict (Supabase JSONB returns dicts directly)


# ─── CV Bullet Bank ──────────────────────────────────────────────────────────────

def save_master_bank(user_id: str, bank_data: dict) -> dict:
    """Upsert the full master bullet bank JSON for a user."""
    client = get_client()
    resp = (
        client.table("master_banks")
        .upsert({"user_id": user_id, "bank_data": bank_data}, on_conflict="user_id")
        .execute()
    )
    return resp.data[0] if resp.data else {}


def load_master_bank(user_id: str) -> dict:
    """
    Load the user's master bullet bank.
    Raises FileNotFoundError if no bank uploaded yet.
    """
    client = get_client()
    resp = (
        client.table("master_banks")
        .select("bank_data")
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not resp.data:
        raise FileNotFoundError(
            "No master bullet bank found. Upload or create one first."
        )
    return resp.data["bank_data"]


def has_master_bank(user_id: str) -> bool:
    """Return True if the user has a CV bullet bank stored."""
    try:
        load_master_bank(user_id)
        return True
    except Exception:
        return False


# ── Bank patch helpers (for in-app editor) ────────────────────────────────────

def add_bullet(user_id: str, section_key: str, bullet_text: str, tags: list[str] | None = None) -> dict:
    """
    Append a new bullet to a section in the CV bullet bank.
    Returns the updated bank.
    """
    import uuid as _uuid
    bank = load_master_bank(user_id)
    section = bank.get("sections", {}).get(section_key)
    if section is None:
        raise KeyError(f"Section '{section_key}' not found in CV bullet bank")
    new_bullet = {
        "id":   f"{section_key}_{_uuid.uuid4().hex[:6]}",
        "text": bullet_text.strip(),
        "tags": tags or [],
    }
    section.setdefault("bullets", []).append(new_bullet)
    save_master_bank(user_id, bank)
    return bank


def update_bullet(user_id: str, section_key: str, bullet_id: str, new_text: str, tags: list[str] | None = None) -> dict:
    """Update an existing bullet's text (and optionally tags)."""
    bank = load_master_bank(user_id)
    section = bank.get("sections", {}).get(section_key)
    if section is None:
        raise KeyError(f"Section '{section_key}' not found")
    for b in section.get("bullets", []):
        if b["id"] == bullet_id:
            b["text"] = new_text.strip()
            if tags is not None:
                b["tags"] = tags
            break
    else:
        raise KeyError(f"Bullet '{bullet_id}' not found in section '{section_key}'")
    save_master_bank(user_id, bank)
    return bank


def delete_bullet(user_id: str, section_key: str, bullet_id: str) -> dict:
    """Delete a bullet from a section."""
    bank = load_master_bank(user_id)
    section = bank.get("sections", {}).get(section_key)
    if section is None:
        raise KeyError(f"Section '{section_key}' not found")
    before = len(section.get("bullets", []))
    section["bullets"] = [b for b in section.get("bullets", []) if b["id"] != bullet_id]
    if len(section["bullets"]) == before:
        raise KeyError(f"Bullet '{bullet_id}' not found")
    save_master_bank(user_id, bank)
    return bank


def update_skills(user_id: str, skills_text: str) -> dict:
    """Update the skills_text field of the CV bullet bank."""
    bank = load_master_bank(user_id)
    bank["skills_text"] = skills_text.strip()
    save_master_bank(user_id, bank)
    return bank


def update_certifications(user_id: str, certifications: list[str]) -> dict:
    """Update the certifications list of the CV bullet bank."""
    bank = load_master_bank(user_id)
    bank["certifications"] = [c.strip() for c in certifications if c.strip()]
    save_master_bank(user_id, bank)
    return bank


def update_section_slots(user_id: str, section_key: str, bullet_slots: int) -> dict:
    """Update how many bullet slots a section has."""
    bank = load_master_bank(user_id)
    section = bank.get("sections", {}).get(section_key)
    if section is None:
        raise KeyError(f"Section '{section_key}' not found")
    section["bullet_slots"] = max(1, int(bullet_slots))
    save_master_bank(user_id, bank)
    return bank


# ─── CV Template (Supabase Storage) ──────────────────────────────────────────

BUCKET = "cv-templates"


def _template_path(user_id: str, filename: str = "cv.docx") -> str:
    return f"templates/{user_id}/{filename}"


def upload_cv_template(user_id: str, docx_path: Path,
                       format_rules: dict | None = None) -> str:
    """
    Upload .docx to Supabase Storage. Returns storage path.
    Optionally saves extracted format_rules alongside the pointer row.
    """
    client = get_client()
    storage_path = _template_path(user_id, docx_path.name)

    with open(docx_path, "rb") as f:
        file_bytes = f.read()

    # Remove existing if present
    try:
        client.storage.from_(BUCKET).remove([storage_path])
    except Exception:
        pass

    client.storage.from_(BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={
            "content-type": (
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            )
        },
    )

    upsert_data: dict = {
        "user_id":           user_id,
        "storage_path":      storage_path,
        "original_filename": docx_path.name,
    }
    if format_rules is not None:
        upsert_data["format_rules"] = format_rules

    client.table("cv_templates").upsert(
        upsert_data, on_conflict="user_id"
    ).execute()

    return storage_path


def save_template_format_rules(user_id: str, format_rules: dict) -> None:
    """
    Persist extracted format rules (font, bullet length, skill lines …) for a user's
    CV template.  Safe to call independently from upload_cv_template — it only
    touches the format_rules column.
    """
    client = get_client()
    client.table("cv_templates").upsert(
        {"user_id": user_id, "format_rules": format_rules},
        on_conflict="user_id",
    ).execute()


def load_template_format_rules(user_id: str) -> dict:
    """
    Load the extracted format rules stored alongside the user's CV template.

    Returns {} when:
    - No template has been uploaded yet
    - The format_rules column is NULL (template uploaded before extraction was added)
    - Any Supabase error (e.g. column doesn't exist yet in the schema)
    """
    try:
        client = get_client()
        resp = (
            client.table("cv_templates")
            .select("format_rules")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        if resp.data:
            rules = resp.data.get("format_rules")
            if isinstance(rules, dict) and rules:
                return rules
    except Exception:
        pass
    return {}


def download_cv_template(user_id: str, dest_path: Path) -> Path:
    """Download the user's CV template from Supabase Storage."""
    client = get_client()
    resp = (
        client.table("cv_templates")
        .select("storage_path")
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not resp.data:
        raise FileNotFoundError(
            "No CV template found. Upload a .docx template first."
        )
    storage_path = resp.data["storage_path"]
    file_bytes   = client.storage.from_(BUCKET).download(storage_path)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(file_bytes)
    return dest_path


def has_cv_template(user_id: str) -> bool:
    """Return True if the user has a CV template stored."""
    try:
        client = get_client()
        resp = (
            client.table("cv_templates")
            .select("storage_path")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        return bool(resp.data)
    except Exception:
        return False
# ─── CV Sessions (for cross-worker persistence) ──────────────────────────────

def save_cv_session(user_id: str, token: str, data: dict) -> None:
    """
    Store temporary CV generation state (AI result, template path, etc.) 
    in the database so it can be retrieved by any worker process.
    """
    client = get_client()
    # Ensure data is JSON serializable (convert Paths to strings)
    serializable = json.loads(json.dumps(data, default=str))
    client.table("cv_sessions").upsert({
        "user_id": user_id,
        "token":   token,
        "data":    serializable
    }, on_conflict="token").execute()


def get_cv_session(token: str) -> dict | None:
    """Retrieve a temporary CV session by token."""
    try:
        client = get_client()
        resp = (
            client.table("cv_sessions")
            .select("data, user_id")
            .eq("token", token)
            .single()
            .execute()
        )
        if resp.data:
            data = resp.data["data"]
            data["user_id"] = resp.data["user_id"] # ensure user_id is available
            return data
    except Exception:
        pass
    return None


def delete_cv_session(token: str) -> None:
    """Delete a CV session once completed or expired."""
    try:
        client = get_client()
        client.table("cv_sessions").delete().eq("token", token).execute()
    except Exception:
        pass
def upload_generated_cv(user_id: str, token: str, file_path: Path) -> str:
    """Upload a generated .docx or .pdf to Supabase Storage."""
    client = get_client()
    # Path: generated/{user_id}/{token}/{filename}
    storage_path = f"generated/{user_id}/{token}/{file_path.name}"
    
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    # Remove existing if any
    try: client.storage.from_(BUCKET).remove([storage_path])
    except Exception: pass

    client.storage.from_(BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": "application/octet-stream"}
    )
    return storage_path


def download_generated_cv(user_id: str, token: str, filename: str, dest_path: Path) -> Path:
    """Download a generated CV result back to local disk."""
    client = get_client()
    storage_path = f"generated/{user_id}/{token}/{filename}"
    file_bytes   = client.storage.from_(BUCKET).download(storage_path)
    
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(file_bytes)
    return dest_path
