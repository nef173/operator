# Operator API (FastAPI + in-process worker). Small, single-process image.
# Python 3.12 (not 3.14) so the pinned psycopg[binary] wheels resolve cleanly.
FROM python:3.12-slim

# Faster, quieter, no .pyc litter, unbuffered logs (so Railway shows them live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Code root inside the image (the step scripts are copied under /app). Pinning it means
# config.repo_root() returns immediately instead of walking parent dirs.
ENV GOOGLE_STORES_ROOT=/app

# Install deps first so this layer caches until requirements change.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code (the .dockerignore keeps .venv / data / __pycache__ out of the image).
COPY app ./app

# Research/listing step scripts, vendored from the Google Stores tree by scripts/sync-steps.sh.
# jobs.py resolves them at repo_root()/<step>/scripts (GOOGLE_STORES_ROOT=/app on Railway) and
# probes each step's .venv/bin/python — so the venvs are built here, at exactly those paths.
# Without this layer every research/listing job degrades to a needs-operator handoff.
COPY steps/01-niche-discovery/scripts ./01-niche-discovery/scripts
COPY steps/06-launch-general-store/scripts ./06-launch-general-store/scripts
COPY steps/05-launch-niche-store/china-source-match/scripts ./05-launch-niche-store/china-source-match/scripts
RUN python -m venv /app/06-launch-general-store/scripts/.venv \
 && /app/06-launch-general-store/scripts/.venv/bin/pip install --no-cache-dir \
      requests Pillow python-dotenv \
 && python -m venv /app/05-launch-niche-store/china-source-match/scripts/.venv \
 && /app/05-launch-niche-store/china-source-match/scripts/.venv/bin/pip install --no-cache-dir \
      requests python-dotenv

# Railway injects $PORT. Bind it; do NOT use --reload in production (it doubles RAM and
# the in-process worker would run in both reloader subprocesses). Single uvicorn worker —
# the always-on background worker lives inside this same process (see main.py lifespan),
# so running >1 uvicorn worker would start N duplicate schedulers. Keep it at one.
ENV PORT=8077
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8077}"]
