"""Configuration for the operator app backend.

The backend WRAPS the existing Google Stores pipeline — it reads the pipeline's
on-disk outputs (listing queues, candidate queues, dossiers, niche launches).
It does NOT reimplement any of that intelligence.

The repo root resolves to the Google Stores project directory. By default that is
three levels up from this file (operator-app/api/app/config.py -> project root),
but it can be overridden with the GOOGLE_STORES_ROOT env var (e.g. when the
backend is deployed separately from the pipeline checkout).

CODE vs DATA — the two roots (this is what makes one repo serve many businesses):

  * repo_root()  — where the CODE lives (the pipeline scripts run as subprocesses, the
    canonical news_radar.py / candidate_queue.py, etc.). Ships WITH the deploy; the same
    for every business because they all run the same code.
  * data_root()  — where this deployment's LIVE per-business DATA lives (store queues,
    dossiers, news snapshots, the run-log DB). Overridden per deployment with the
    GOOGLE_STORES_DATA env var so each business writes to its OWN persistent volume and
    never sees another's data. Falls back to repo_root() when unset, so a single-business
    laptop run is byte-for-byte unchanged (code and data coexist as before).

So three businesses = ONE git repo (change code once, push, all redeploy) × three
deployments, each with its own GOOGLE_STORES_DATA volume + DATABASE_URL + Connections keys.
The TENANT env var is a human label for which business a deployment serves (shown in /api
/settings) — it changes nothing behaviourally, it just makes "which business is this?" visible.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _load_env_file() -> None:
    """Load operator-app/api/.env into os.environ at import time so per-deployment
    settings (GOOGLE_STORES_DATA, TENANT, …) take effect without an external process
    manager. A real environment variable always wins — we only fill what's unset —
    so Railway/host env vars are never overridden. No dependency (stdlib parse)."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    try:
        text = env_path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file()


@lru_cache(maxsize=1)
def repo_root() -> Path:
    env = os.environ.get("GOOGLE_STORES_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # Locally the layout is operator-app/api/app/config.py (parents[3] == root); in the
    # Railway container it is /app/app/config.py (shallower), so clamp to the deepest
    # available parent instead of IndexError-ing and crashing the app at startup.
    parents = Path(__file__).resolve().parents
    return parents[3] if len(parents) > 3 else parents[1]


@lru_cache(maxsize=1)
def data_root() -> Path:
    """Per-deployment LIVE DATA root. Override with GOOGLE_STORES_DATA so each business
    writes to its own persistent volume; falls back to repo_root() (single-business / laptop)."""
    env = os.environ.get("GOOGLE_STORES_DATA")
    if env:
        return Path(env).expanduser().resolve()
    return repo_root()


# app_settings key holding the operator-renamed business label (so the name can be edited from
# Settings without a redeploy, and survives one). Falls back to the TENANT env var, then 'default'.
TENANT_KEY = "tenant_name"


def tenant() -> str:
    """Human label for which business this deployment serves. The operator can rename it from
    Settings (persisted in app_settings); otherwise it's the TENANT env var, then 'default'.
    Display-only — the real isolation comes from data_root() + DATABASE_URL, not this label."""
    try:
        from . import runlog  # lazy: runlog imports db, avoid an import cycle at module load
        stored = runlog.setting_get(TENANT_KEY)
        if stored and isinstance(stored.get("name"), str) and stored["name"].strip():
            return stored["name"].strip()
    except Exception:
        pass
    return (os.environ.get("TENANT") or "default").strip() or "default"


def set_tenant(name: str) -> str:
    """Persist the operator-set business name (Settings → System). An empty name clears the
    override, falling back to the TENANT env var / 'default'. Returns the effective name."""
    from . import runlog
    clean = (name or "").strip()[:80]
    if clean:
        runlog.setting_set(TENANT_KEY, {"name": clean})
    else:
        try:
            runlog.setting_delete(TENANT_KEY)
        except Exception:
            pass
    return tenant()


def general_stores_dir() -> Path:
    return data_root() / "general-stores"


def dossiers_dir() -> Path:
    return data_root() / "dossiers"


def news_dir() -> Path:
    """News-radar leading-signal snapshot + theme watchlist (GDELT news-velocity sensor)."""
    return data_root() / "news-radar"


def events_dir() -> Path:
    """World-events calendar watchlist — the PREDICTABLE, dated demand signal (holidays,
    seasonal, world events per country). Distinct from news-radar (unpredictable breaking
    events): this is the only signal you can plan a build-ahead batch around with certainty."""
    return data_root() / "events-calendar"


def dossiers_pain_first_dir() -> Path:
    """The parallel pain-first niche-discovery pipeline (01b — Amin 14-step)."""
    return data_root() / "dossiers-pain-first"


def niche_launches_dir() -> Path:
    return repo_root() / "05-launch-niche-store" / "niche-launches"


def general_store_scripts_dir() -> Path:
    """Where the competitor-spy roster data lives (store_traffic / store_class / stores.txt)."""
    return repo_root() / "06-launch-general-store" / "scripts"


def spy_data_dir() -> Path:
    """RUNTIME output of the Best-Seller Spy (snapshots/ + movers.json). Kept on the DATA
    volume — not next to the scripts — because on Railway the scripts live in the container
    image, whose filesystem is wiped on every deploy; movers.json is a diff between two
    DIFFERENT days' snapshots, so rank history has to survive redeploys to ever produce one.
    Readers fall back to the legacy scripts-dir location for pre-existing local history."""
    return data_root() / "operator-app" / "api" / "data" / "spy"


def china_source_match_dir() -> Path:
    """The 1688 / Alibaba sourcing-match toolkit (alibaba_bulk + match_china)."""
    return repo_root() / "05-launch-niche-store" / "china-source-match" / "scripts"


# CORS origins for the Next.js frontend (comma-separated env override).
def cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "http://localhost:3000")
    return [o.strip() for o in raw.split(",") if o.strip()]
