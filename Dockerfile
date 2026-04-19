# ── CV Tailor — Production Dockerfile ────────────────────────────────────────
# Includes LibreOffice for DOCX → PDF conversion.
# Build: docker build -t cv-tailor .
# Run:   docker-compose up

FROM python:3.11-slim

# ── System deps: LibreOffice + poppler (pdfinfo for 1-page check) ──────────
# Fonts are critical for DOCX ↔ PDF layout parity. Without them LibreOffice
# substitutes Calibri / Arial / Times / Cambria with fonts of DIFFERENT
# character widths, so the DOCX (rendered by Word on the user's machine)
# overflows to page 2 even when our server-rendered PDF fits on one.
#   • fonts-liberation           → Arial / Times / Courier (metric-compatible)
#   • fonts-crosextra-carlito    → Calibri (metric-compatible, same widths)
#   • fonts-crosextra-caladea    → Cambria (metric-compatible)
#   • ttf-mscorefonts-installer  → actual Arial, Times, Georgia, Verdana, …
#                                  (needs contrib repo + non-interactive EULA)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
        libreoffice-writer \
        poppler-utils \
        fonts-liberation \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fonts-dejavu \
        ca-certificates \
    && echo "deb http://deb.debian.org/debian bookworm contrib" \
        > /etc/apt/sources.list.d/contrib.list \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
        | debconf-set-selections \
    && apt-get update \
    && apt-get install -y --no-install-recommends ttf-mscorefonts-installer \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App source ─────────────────────────────────────────────────────────────
COPY . .

# LibreOffice needs a writable home for its lock files
ENV HOME=/tmp
ENV SAL_USE_VCLPLUGIN=svp

EXPOSE 8000

# 2 workers is safe on a 512 MB container; bump for more RAM
CMD ["gunicorn", "app:app", \
     "--workers", "2", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120", \
     "--access-logfile", "-"]
