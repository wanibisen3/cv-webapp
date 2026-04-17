# ── CV Tailor — Production Dockerfile ────────────────────────────────────────
# Includes LibreOffice for DOCX → PDF conversion.
# Build: docker build -t cv-tailor .
# Run:   docker-compose up

FROM python:3.11-slim

# ── System deps: LibreOffice + poppler (pdfinfo for 1-page check) ──────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
        libreoffice-writer \
        poppler-utils \
        fonts-liberation \
        fonts-dejavu \
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
