# My INSEAD CV

**AI-powered CV tailoring for business school candidates.**

Paste any job description → get a tailored, ATS-optimised CV in under a minute. Your template, your format, zero hallucination.

---

## What it does

Most CV tools either rewrite your whole document (losing your formatting) or give generic advice. This app does something different:

1. **Builds a master experience bank** from your existing CV — every role, bullet point, project, and skill, structured and searchable.
2. **Reads any job description** and picks the most relevant experience from your bank.
3. **Rewrites bullets in STAR format** using the JD's exact language — without inventing facts.
4. **Outputs your own template** (.docx + PDF) with the correct number of bullets per section, your font, your layout. Nothing changes except the content.

---

## Features

- **Multi-user** — each person has their own bank, template, and API key; data is isolated with row-level security
- **Experience bank** — upload a DOCX/PDF/TXT or paste text; AI parses every role, project, and skill automatically
- **Template-aware generation** — bullet count, font, skill lines, and character limits are all extracted from *your* uploaded template, not hardcoded
- **STAR bullet format** — `SubHeading: [Strong verb] [action + context], [result with metric]`
- **Review step** — edit any bullet before generating the final files
- **Multi-provider AI** — bring your own Anthropic, OpenAI, or Gemini key; you pay your provider directly
- **DOCX + PDF download** — PDF via LibreOffice (included in Docker image)
- **Free to use** — no subscription, no markup on API costs

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, Flask 3 |
| Database & Auth | Supabase (PostgreSQL + Auth + Storage + RLS) |
| AI | Anthropic Claude / OpenAI GPT-4 / Google Gemini (BYOK) |
| DOCX engine | lxml + zipfile (no Word required) |
| PDF | LibreOffice headless |
| Deployment | Docker + Gunicorn |

---

## Quick start (local)

### Prerequisites
- Python 3.11+
- A [Supabase](https://supabase.com) project (free tier works)
- An API key from [Anthropic](https://console.anthropic.com), [OpenAI](https://platform.openai.com), or [Google AI Studio](https://aistudio.google.com)

### 1. Clone & install

```bash
git clone https://github.com/wanibisen3/cv-webapp.git
cd cv-webapp
pip install -r requirements.txt
```

### 2. Set up Supabase

In your Supabase dashboard → SQL Editor → run the full contents of `schema.sql`.

This creates:
- `profiles` table (name, email, AI settings)
- `master_banks` table (experience bank JSON)
- `cv_templates` table (template pointer + format rules)
- `cv-templates` storage bucket
- Row-level security policies for all tables

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
FLASK_SECRET=<random 32-char hex string>
SUPABASE_URL=https://<your-project-ref>.supabase.co
SUPABASE_KEY=<your-anon-key>
ENCRYPT_KEY=<fernet key>
```

Generate the values:
```bash
# FLASK_SECRET
python -c "import secrets; print(secrets.token_hex(32))"

# ENCRYPT_KEY (generate once — changing it breaks stored API keys)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 4. Run

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000)

> **PDF generation** requires LibreOffice. On macOS: `brew install --cask libreoffice`. On Linux: `apt install libreoffice`. Or just use Docker (below) which includes it.

---

## Deploy with Docker

The Docker image includes LibreOffice for PDF generation.

```bash
# Build and run
docker-compose up --build

# App runs on http://localhost:8000
```

`docker-compose.yml` reads from your `.env` file automatically.

---

## How to use

### First-time setup (3 steps)

**Step 1 — Add your API key**
Go to Settings → choose your AI provider → paste your key. It's encrypted before being stored. Cost: ~$0.02 per CV generated with Claude Sonnet.

**Step 2 — Build your experience bank**
Go to Bank → Create. Upload your current CV (`.docx`, `.pdf`, or `.txt`) or paste your experience as text. AI extracts every role, project, bullet point, and skill. You can edit, add, or delete anything afterwards.

**Step 3 — Upload your CV template**
Upload the `.docx` file you normally use as your base CV — the one with your formatting, fonts, and layout. The app reads the template to detect: font, font size, number of bullet slots per section, skill line count, and character limits. This happens automatically on upload.

### Generating a tailored CV

Once setup is complete, paste any job description on the dashboard and click **Tailor my CV**. The AI:
- Identifies the JD's key skills, priorities, and language
- Picks the most relevant experience from your bank
- Rewrites each bullet in STAR format using the JD's exact words
- Respects the exact bullet count from your template
- Rewrites the skills section to match the JD

You then see a **review page** where you can edit any bullet before generating the final `.docx` + `.pdf`.

---

## STAR bullet format

Every bullet follows this structure:

```
SubHeading: [Strong past-tense verb] [what you did + context], [result with metric]
```

**Example:**
```
Financial Controls: Led statutory audits for 4 mid-cap healthcare clients,
identifying £1.2M in revenue recognition errors and presenting findings to CFO
```

- **SubHeading** — bold, 2–4 words, taken verbatim from the JD's language
- **Verb** — Led, Drove, Built, Delivered, Spearheaded, Engineered…
- **Result** — $value, %, ×, rank, or a clear directional outcome when no metric exists
- No facts are invented — the AI only uses what's in your bank

---

## Project structure

```
cv-webapp/
├── app.py               # Flask app — all routes + inline HTML templates
├── ai_providers.py      # AI logic: tailoring + bank parsing prompts, multi-provider
├── cv_engine.py         # DOCX/PDF engine + template format extraction
├── supabase_client.py   # Auth, bank CRUD, template storage
├── schema.sql           # Full Supabase schema (run once to set up DB)
├── requirements.txt     # Python dependencies
├── Dockerfile           # Python 3.11 + LibreOffice
├── docker-compose.yml   # One-command deploy
└── .env.example         # Environment variable template
```

---

## Environment variables

| Variable | Description |
|---|---|
| `FLASK_SECRET` | Flask session secret — any random string |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key (RLS handles per-user isolation) |
| `ENCRYPT_KEY` | Fernet key for encrypting user API keys at rest — **never change after first use** |
| `PORT` | Optional — defaults to 5000 locally, 8000 in Docker |

---

## Migrating an existing deployment

If you already have the schema deployed and are updating:

```sql
-- v2 → v3: adds format_rules column to cv_templates
ALTER TABLE public.cv_templates
  ADD COLUMN IF NOT EXISTS format_rules jsonb DEFAULT null;
```

---

## Privacy & data

- Your CV content is **never stored** on our servers — it's processed in memory and discarded after download
- Your API key is **encrypted at rest** using Fernet symmetric encryption before being saved to the database
- Each user's data is isolated using **Supabase Row-Level Security** — no user can access another's bank or template
- You pay your AI provider directly — this app takes no cut and has no usage-based pricing

---

## Built for

INSEAD MBA students and business school candidates applying across consulting, finance, tech, and general management roles. Works equally well for any professional background — research, NGO, startup, Big 4, banking, or academia.

---

## License

MIT — free to use, fork, and deploy.
