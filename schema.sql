-- =============================================================================
-- CV Tailor Web App — Supabase Schema  (v2)
-- Run in Supabase dashboard: SQL Editor → New Query → Run All
-- =============================================================================

-- ── UUID extension (already enabled in Supabase by default) ─────────────────
create extension if not exists "uuid-ossp";

-- ── Shared updated_at trigger function ──────────────────────────────────────
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. PROFILES
--    One row per user; links to Supabase Auth (auth.users).
--    ai_settings (JSONB) stores: { provider, api_key_enc, model }
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.profiles (
    id           uuid primary key references auth.users(id) on delete cascade,
    name         text,
    email        text,
    -- AI provider settings (encrypted API key stored here)
    ai_settings  jsonb default null,
    created_at   timestamptz default now(),
    updated_at   timestamptz default now()
);

-- Auto-create a profile row on sign-up
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer as $$
begin
    insert into public.profiles (id, email)
    values (new.id, new.email)
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

drop trigger if exists profiles_updated_at on public.profiles;
create trigger profiles_updated_at
    before update on public.profiles
    for each row execute function public.set_updated_at();


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. MASTER BANKS
--    Stores each user's bullet bank as JSONB.
--    Structure: { candidate, sections, certifications, skills_text, skills_header }
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.master_banks (
    id         uuid primary key default uuid_generate_v4(),
    user_id    uuid not null references public.profiles(id) on delete cascade,
    bank_data  jsonb not null,
    updated_at timestamptz default now(),
    unique (user_id)
);

drop trigger if exists master_banks_updated_at on public.master_banks;
create trigger master_banks_updated_at
    before update on public.master_banks
    for each row execute function public.set_updated_at();


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. CV TEMPLATES
--    Pointer to the user's .docx template file in Supabase Storage.
--    format_rules (JSONB) stores rules auto-extracted when the template is uploaded:
--      { max_bullet_chars, max_skill_lines, max_skill_line_chars,
--        bullet_font, bullet_font_size_pt, has_bold_subheading, bullet_format }
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.cv_templates (
    id                uuid primary key default uuid_generate_v4(),
    user_id           uuid not null references public.profiles(id) on delete cascade,
    storage_path      text not null,
    original_filename text,
    format_rules      jsonb default null,
    updated_at        timestamptz default now(),
    unique (user_id)
);

drop trigger if exists cv_templates_updated_at on public.cv_templates;
create trigger cv_templates_updated_at
    before update on public.cv_templates
    for each row execute function public.set_updated_at();


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. ROW LEVEL SECURITY (RLS)
--    Users can only read/write their own rows.
-- ─────────────────────────────────────────────────────────────────────────────
alter table public.profiles     enable row level security;
alter table public.master_banks enable row level security;
alter table public.cv_templates enable row level security;

-- profiles
drop policy if exists "profiles: own row" on public.profiles;
create policy "profiles: own row" on public.profiles
    using (auth.uid() = id)
    with check (auth.uid() = id);

-- master_banks
drop policy if exists "master_banks: own row" on public.master_banks;
create policy "master_banks: own row" on public.master_banks
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- cv_templates
drop policy if exists "cv_templates: own row" on public.cv_templates;
create policy "cv_templates: own row" on public.cv_templates
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. STORAGE BUCKET — cv-templates (private, per-user RLS)
-- ─────────────────────────────────────────────────────────────────────────────
insert into storage.buckets (id, name, public)
values ('cv-templates', 'cv-templates', false)
on conflict (id) do nothing;

-- Upload
drop policy if exists "cv-templates: own upload" on storage.objects;
create policy "cv-templates: own upload" on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'cv-templates'
        and (storage.foldername(name))[1] = 'templates'
        and (storage.foldername(name))[2] = auth.uid()::text
    );

-- Read
drop policy if exists "cv-templates: own read" on storage.objects;
create policy "cv-templates: own read" on storage.objects
    for select to authenticated
    using (
        bucket_id = 'cv-templates'
        and (storage.foldername(name))[1] = 'templates'
        and (storage.foldername(name))[2] = auth.uid()::text
    );

-- Update
drop policy if exists "cv-templates: own update" on storage.objects;
create policy "cv-templates: own update" on storage.objects
    for update to authenticated
    using (
        bucket_id = 'cv-templates'
        and (storage.foldername(name))[1] = 'templates'
        and (storage.foldername(name))[2] = auth.uid()::text
    );

-- Delete
drop policy if exists "cv-templates: own delete" on storage.objects;
create policy "cv-templates: own delete" on storage.objects
    for delete to authenticated
    using (
        bucket_id = 'cv-templates'
        and (storage.foldername(name))[1] = 'templates'
        and (storage.foldername(name))[2] = auth.uid()::text
    );


-- =============================================================================
-- MIGRATION NOTES (existing deployments — run only what you haven't applied yet):
--
-- v1 → v2: adds ai_settings column to profiles
--   alter table public.profiles add column if not exists ai_settings jsonb default null;
--
-- v2 → v3: adds format_rules column to cv_templates
--   alter table public.cv_templates add column if not exists format_rules jsonb default null;
-- =============================================================================
