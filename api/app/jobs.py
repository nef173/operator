"""Execution-jobs scaffold (Phase 3) — the pipeline's heavier steps as durable jobs.

The control layer (actions.py) drives the <1s stdlib state machines synchronously. The
*heavy* steps — a Google-Shopping scan, a DFS keyword pull, an image-gen + Shopify build —
are represented here as durable **jobs** under the locked **hybrid** execution model:

  mode="auto"    deterministic + stdlib-only + needs only on-disk state → the app RUNS it
                 now, in a background thread, recording status to the jobs table.
  mode="manual"  needs API keys / AdsPower / image-gen / Shopify push → the app does NOT
                 run it. It records a `needs-operator` job carrying the EXACT command the
                 operator pastes into their own Claude Code (which holds the creds/MCP).

No SSE/DBOS this pass — the client polls GET /api/jobs/{id} every 2s. The jobs table
(runlog.py) is already schema-ready for a DBOS worker swap later (status + output columns).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import config, connections, readers, runlog


def _base_env() -> dict:
    """Subprocess env for a job: the operator's stored API keys (Connections) UNDER the real
    process env. Precedence = process/Railway env wins, stored creds fill any gaps — so on the
    hosted app the in-app keys power DataForSEO/Bright Data/etc., while a value set in the
    Railway dashboard still overrides. This is what turns 'configured a key in Settings' into
    'the research jobs can actually authenticate'."""
    return {**connections.as_env(), **os.environ}

# ------------------------------------------------------------------ concurrency
# Parallelism with LOGIC, not a thread-per-job free-for-all. Two rules encode the
# operator's mental model ("list 20 in parallel, but don't let dependent/same-resource
# work race"):
#
#   1) BOUNDED POOL — independent jobs (different products / SKUs / stores) run in
#      parallel, but capped at a small worker count. This is deliberate: the validated
#      image-gen lesson is that LOW concurrency is FASTER (max-concurrent 6 → ~50%
#      timeouts; 2 → 100% pass). An unbounded thread-per-job fan-out of 20 real builds
#      would thrash and fail. Override with OPERATOR_JOB_WORKERS.
#   2) PER-RESOURCE SERIALIZATION — the stdlib state machines (candidate_queue.py,
#      listing_queue.py) read-modify-write a shared per-store JSON. Two jobs mutating the
#      SAME (store, script) must NOT run at once or one update is lost. A per-key lock
#      serializes same-resource work while letting different stores/scripts/products run
#      truly in parallel. This is the "parallel only where logically independent" guarantee.
_MAX_WORKERS = max(1, int(os.environ.get("OPERATOR_JOB_WORKERS", "3")))
_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=_MAX_WORKERS, thread_name_prefix="job"
)

# The heavy, burst-y per-store BACKGROUND scans run in a SEPARATE pool so they can never occupy the
# main pool's slots that INTERACTIVE jobs need. Without this, the twice-daily bestseller-spy burst
# (one job per tracked store, each slow) fills all 3 main slots and a competitor keyword-scan / PLA
# capture / product-find the operator triggers queues behind it. Now those always get the main pool.
_HEAVY_SPECS = frozenset({"bestseller-spy", "discover-general-stores"})
_MAX_HEAVY = max(1, int(os.environ.get("OPERATOR_JOB_WORKERS_HEAVY", "2")))
_POOL_HEAVY = concurrent.futures.ThreadPoolExecutor(
    max_workers=_MAX_HEAVY, thread_name_prefix="job-bg"
)


def _pool_for(spec_id: str) -> "concurrent.futures.ThreadPoolExecutor":
    """Route heavy scheduled scans to the background pool; everything the operator triggers (finds,
    competitor keyword scans, PLA captures) to the main pool — so interactive work never waits."""
    return _POOL_HEAVY if spec_id in _HEAVY_SPECS else _POOL

_resource_locks: dict[str, threading.Lock] = {}
_resource_locks_guard = threading.Lock()


def _resource_lock(key: str) -> threading.Lock:
    """A lock unique to a mutated resource (e.g. 'nosura:candidate_queue.py'). Same key →
    same lock → serialized; different keys → independent → parallel."""
    with _resource_locks_guard:
        lock = _resource_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _resource_locks[key] = lock
        return lock

_SCRIPTS = config.repo_root() / "06-launch-general-store" / "scripts"
# The FREE, no-AI overlay remover (image_autoclean.py) runs in the general-store scripts venv
# (Pillow present). This is the image-QA gate's auto-clean path: strip a removable overlay for
# free locally instead of paying an AI model to regenerate the whole image.
_GENERAL_VENV = _SCRIPTS / ".venv" / "bin" / "python"
_AUTOCLEAN_SCRIPT = _SCRIPTS / "image_autoclean.py"
_UPSCALE_SCRIPT = _SCRIPTS / "image_upscale.py"

# Best-Seller Spy: snapshot (scrape today's best-selling rank board per tracked store) →
# diff (recompute movers.json from the two latest snapshots). Pure stdlib + requests, no
# keys/AdsPower — so the app can RUN it directly on a cadence to keep Movers + New Products
# fresh. Both scripts live in the general-store scripts dir and use its venv.
_SPY_SNAPSHOT = _SCRIPTS / "bestseller_snapshot.py"
_SPY_DIFF = _SCRIPTS / "bestseller_diff.py"
_SPY_STORES = _SCRIPTS / "stores.txt"

# Server-side general-store DISCOVERY (always-on, no MCP/browser): the api-venv orchestrator
# harvests seller domains off the real DFS Google-Shopping grid, then runs the general-store
# classifier gate (in the general venv, where `requests` lives) and adds the passing stores.
_DISCOVER_DFS = Path(__file__).resolve().parent / "discover_stores_dfs.py"
_HARVEST_STORES = _SCRIPTS / "harvest_general_stores.py"

# Server-side RESEARCH orchestrator (always-on, DataForSEO-only): runs Trend Radar + the
# Keyword Discovery funnel as background jobs, writing the same trends.json / candidate-queue.json
# the readers read. This is what makes those two steps run on the live worker 24/7 instead of
# handing the operator a slash command. keyword_data.py + trends_dfs.py need `requests`, so the
# orchestrator shells them out under the general-store venv (_GENERAL_VENV).
_RESEARCH_DFS = Path(__file__).resolve().parent / "research_dfs.py"
_NICHE_SCRIPTS = config.repo_root() / "01-niche-discovery" / "scripts"
# Per-deployment scratch dir for the orchestrator's intermediate season-*.json artifacts.
_RESEARCH_WORK = config.data_root() / "operator-app" / "api" / "data" / "research-work"

# China sourcing-match pipeline (reverse-image → VLM judge → 1688 enrich). These are the
# exact scripts the Sourcing Match modal's Run buttons fire. They need AdsPower (reverse
# image) / a vision key / the TMAPI key, so they're hybrid: the app records the precise
# command (needs-operator) the operator runs in their own Claude Code session.
_CHINA_DIR = config.repo_root() / "05-launch-niche-store" / "china-source-match" / "scripts"
_CHINA_VENV = _CHINA_DIR / ".venv" / "bin" / "python"
_CHINA_ENRICH = _CHINA_DIR / "enrich_match.py"
_CHINA_TMAPI = _CHINA_DIR / "tmapi_1688.py"
_CHINA_ENV = _CHINA_DIR / ".env"

# The Google-Shopping competitor scan runs entirely server-side from DataForSEO's structured
# API (`shopping_scan_dfs.py`, stdlib only) — the real geolocated US Shopping grid, prices,
# sellers, Google's category rows, and the `shop_ad_aclk` Sponsored flag (which competitors run
# Shopping ads), with no browser and no local profile. The old AdsPower real-Chrome path (step
# 01c, PLA screenshots) was removed 2026-07-08; its only unique output was literal screenshots,
# and the ad DATA is already in the DFS scan.
_SHOPPING_SCAN_OUT = config.data_root() / "operator-app" / "api" / "data" / "shopping-scans"


def _slug(s: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40] or "scan"


def _shopping_scan_health() -> dict:
    """The Google-Shopping competitor scan is server-side (DataForSEO structured API, stdlib
    only — no browser, no local profile). It needs ONLY the DataForSEO creds: real env vars or
    the ones set in Settings -> Connections -> Data."""
    creds = (
        (connections.as_env().get("DATAFORSEO_USERNAME") or os.environ.get("DATAFORSEO_USERNAME"))
        and (connections.as_env().get("DATAFORSEO_PASSWORD") or os.environ.get("DATAFORSEO_PASSWORD"))
    )
    if creds:
        return {"dep": "shopping_scan", "reachable": True,
                "detail": "DataForSEO creds present — the Google-Shopping competitor scan runs "
                          "server-side, no operator handoff."}
    return {"dep": "shopping_scan", "reachable": False,
            "detail": "No DataForSEO credentials — set DATAFORSEO_USERNAME + DATAFORSEO_PASSWORD in "
                      "Settings -> Connections -> Data and the Shopping scan runs itself."}


def _sponsored_plas_health() -> dict:
    """The Sponsored-PLA capture (paying Google-Shopping competitors) drives the Bright Data Scraping
    Browser over CDP — it needs the browser_api zone endpoint (assembled by BD provision from
    BRIGHTDATA_CUSTOMER_ID). No local browser/AdsPower."""
    from . import brightdata
    try:
        ready = bool(brightdata.browser_cdp_endpoint())
    except Exception:  # noqa: BLE001
        ready = False
    if ready:
        return {"dep": "sponsored_plas", "reachable": True,
                "detail": "BD Scraping-Browser CDP ready — Sponsored-PLA capture runs server-side."}
    return {"dep": "sponsored_plas", "reachable": False,
            "detail": "No BD Scraping-Browser CDP — set BRIGHTDATA_CUSTOMER_ID in Connections and run "
                      "the Bright Data provision button."}


def _image_cleaner_health() -> dict:
    """Probe whether the FREE local overlay-remover can run in-app: the general-store scripts
    venv must exist and import Pillow (the zero-paid-cost diffusion-inpaint engine). When it
    can, the image-QA auto-clean runs for free locally; when it can't, it hands off to the free
    manual route (Canva remove-bg / magic-eraser)."""
    if not _GENERAL_VENV.is_file() or not _AUTOCLEAN_SCRIPT.is_file():
        return {"dep": "image_cleaner", "reachable": False,
                "detail": f"cleaner not installed ({_AUTOCLEAN_SCRIPT.name} / scripts venv missing) — "
                          "auto-clean hands off to the free manual route (Canva remove-bg)."}
    try:
        proc = subprocess.run(
            [str(_GENERAL_VENV), "-c", "import PIL; print('ok')"],
            capture_output=True, text=True, timeout=8,
        )
        ok = proc.returncode == 0
    except Exception as e:  # noqa: BLE001
        return {"dep": "image_cleaner", "reachable": False,
                "detail": f"cleaner venv probe failed ({type(e).__name__})."}
    return {"dep": "image_cleaner", "reachable": ok,
            "detail": "Free local overlay-remover (Pillow diffusion-inpaint) is ready — auto-clean runs in-app at no cost."
            if ok else "Pillow not in the scripts venv — auto-clean hands off to the free manual route."}


def _spy_health() -> dict:
    """Can the Best-Seller Spy snapshot+diff run in-app? Needs the two scripts, the scripts
    venv, and the tracked-store roster (stores.txt). All on-disk + network — no keys."""
    ok = (_GENERAL_VENV.is_file() and _SPY_SNAPSHOT.is_file()
          and _SPY_DIFF.is_file() and _SPY_STORES.is_file())
    return {"dep": "spy", "reachable": ok,
            "detail": "Best-seller spy scripts + venv + roster present — snapshot + diff "
                      "runs in-app to refresh Movers / New Products."
            if ok else "spy scripts / scripts venv / stores.txt missing — hands off to "
                       "/competitor-best-seller-spy."}


def _tmapi_health() -> dict:
    """Can the 1688 enrich (variants/specs/gallery) run in-app? It only needs the TMAPI
    HTTP key — NOT AdsPower and NOT a vision key — so unlike china-verify it CAN run
    server-side. Reachable iff the enrich + tmapi scripts, the china venv, and a
    TMAPI_TOKEN (env, Settings -> Connections, or china .env) are all present."""
    token = bool(os.environ.get("TMAPI_TOKEN") or connections.as_env().get("TMAPI_TOKEN")) or (
        _CHINA_ENV.is_file() and "TMAPI_TOKEN=" in _CHINA_ENV.read_text(errors="ignore")
    )
    ok = (_CHINA_VENV.is_file() and _CHINA_ENRICH.is_file()
          and _CHINA_TMAPI.is_file() and token)
    if ok:
        detail = ("TMAPI key + enrich scripts present — 1688 variants/specs are pulled "
                  "in-app and written straight into the match, no operator handoff.")
    elif not token:
        detail = ("No TMAPI_TOKEN (env or china-source-match/scripts/.env) — enrich hands "
                  "off to the operator's Claude Code, which holds the key.")
    else:
        detail = "enrich scripts / china venv missing — enrich hands off to the operator."
    return {"dep": "tmapi", "reachable": ok, "detail": detail}


# Local-dependency preflight registry — maps a spec's `local_dep` to its health probe.
def _trendtrack_health() -> dict:
    """TrendTrack now ships a public REST API (api.trendtrack.io), so the hosted worker CAN call it
    directly once a workspace API token is set in Settings → Connections → Data. Reachable = token
    present; the discover-general-stores harvest then augments its domain pool with TrendTrack's
    scaling-US-shops feed. No token → the server-side DataForSEO discovery carries the job alone."""
    if bool((connections.runtime_get("TRENDTRACK_API_TOKEN") or "").strip()):
        return {"dep": "trendtrack", "reachable": True,
                "detail": "TrendTrack API token set — discovery can call api.trendtrack.io directly."}
    return {"dep": "trendtrack", "reachable": False,
            "detail": "No TrendTrack API token (Settings → Connections → Data). Using DataForSEO "
                      "server-side discovery; add a tt_live_ key to enable the premium source."}


def _dataforseo_health() -> dict:
    """Can the server-side research funnel (Trend Radar / Keyword Discovery) run in-app NOW?
    It needs ONLY the DataForSEO creds — either real env vars or the ones set in Settings ->
    Connections -> Data — plus the general-store venv (where `requests` lives) and the
    orchestrator script. When all present the job runs on the live worker with no operator
    handoff; when the creds are missing it records the one thing to fix."""
    creds = (
        (connections.as_env().get("DATAFORSEO_USERNAME") or os.environ.get("DATAFORSEO_USERNAME"))
        and (connections.as_env().get("DATAFORSEO_PASSWORD") or os.environ.get("DATAFORSEO_PASSWORD"))
    )
    venv_ok = _GENERAL_VENV.is_file() and _RESEARCH_DFS.is_file()
    ok = bool(creds) and venv_ok
    if ok:
        detail = ("DataForSEO creds + research scripts present — Trend Radar / Keyword Discovery "
                  "run on the worker automatically, no operator handoff.")
    elif not creds:
        detail = ("No DataForSEO credentials — set DATAFORSEO_USERNAME + DATAFORSEO_PASSWORD in "
                  "Settings -> Connections -> Data and this runs itself in the background.")
    else:
        detail = "research scripts / general-store venv missing — hands off to your Claude Code."
    return {"dep": "dataforseo", "reachable": ok, "detail": detail}


_LOCAL_DEP_HEALTH = {"shopping_scan": _shopping_scan_health, "image_cleaner": _image_cleaner_health,
                     "spy": _spy_health, "tmapi": _tmapi_health, "trendtrack": _trendtrack_health,
                     "dataforseo": _dataforseo_health, "sponsored_plas": _sponsored_plas_health}


def _bbox_args(a: dict) -> list[str]:
    """Turn the vision scan's normalized overlay bbox into the cleaner's --region-norm flags.
    Accepts a single [x,y,w,h] or a list of them; silently skips malformed entries."""
    bbox = a.get("overlay_bbox")
    if not bbox:
        return []
    rects = bbox if (isinstance(bbox, list) and bbox and isinstance(bbox[0], (list, tuple))) else [bbox]
    out: list[str] = []
    for r in rects:
        if isinstance(r, (list, tuple)) and len(r) == 4:
            out += ["--region-norm", ",".join(str(float(v)) for v in r)]
    return out


def preflight(dep: str) -> dict:
    """Public: is a local dependency (e.g. DataForSEO, TMAPI) reachable right now?"""
    probe = _LOCAL_DEP_HEALTH.get(dep)
    if probe is None:
        return {"dep": dep, "reachable": False, "detail": f"unknown dependency '{dep}'"}
    return probe()


# ------------------------------------------------------------------ registry
# Each spec describes one heavy step. `auto` specs carry the stdlib `script` + an args
# builder we run directly. `manual` specs carry a `command` builder — the precise string
# the operator runs in their own Claude Code session (which holds the keys/MCP/AdsPower).
# `auto-local` specs carry BOTH: a `local_run` the app executes when `local_dep` is
# reachable, and a `command` fallback for when it isn't.
def _py(script: str, store: str, *args: str) -> list[str]:
    return [sys.executable, str(_SCRIPTS / script), store, *args]


# Listing-build specs whose emitted command should automatically carry the 1688-first
# source-of-truth (resolved from sourcing-match) so the build grounds on the right source.
_LISTING_BUILD_SPECS = {"build-listing", "source-import", "general-import-url"}


def _sot_suffix(a: dict) -> str:
    """Append the resolved source-of-truth flags to a listing-build command.

    1688  -> `--source-of-truth 1688 --source <1688 offer url>` (build from the 1688 offer)
    researched/verify -> `--source-of-truth <x>` (the build keeps its researched origin)
    Absent -> nothing (back-compatible)."""
    sot = a.get("source_of_truth")
    if not sot:
        return ""
    suffix = f" --source-of-truth {sot}"
    if sot == "1688" and a.get("source_url"):
        suffix += f" --source {a['source_url']}"
    return suffix


# Real product-category seeds for the arg-less Trend Radar — a rotating evergreen/seasonal set.
# NEVER seed the meta "trending products": Google Trends fans that out into celebrity/news noise
# (who-owns-bowflex / new-york-times), producing a junk card with junk "title seeds". Used only
# when the store has no real candidate keyword to track yet.
_CURATED_TREND_SEEDS = [
    "cooling blanket", "portable air conditioner", "air purifier", "space heater",
    "dehumidifier", "electric blanket", "standing desk", "robot vacuum", "massage gun",
    "weighted blanket", "sunrise alarm clock", "posture corrector", "neck fan", "ice roller",
]


def _seed_pool(store: str, n: int = 12) -> list[str]:
    """A healthy MIX of real product-category seeds for an arg-less trend/keyword run, so ONE run
    fills the surface with ~10-15 diverse ideas across every path — never a single card. Draws,
    ROUND-ROBIN (for breadth), from four real sources:
      1. the world-events calendar — upcoming seasonal demand (events.upcoming keywords),
      2. the store's candidate queue — keywords already being tracked,
      3. the news watchlist — themes with rising real-world momentum,
      4. a curated evergreen fallback.
    De-duped + meta-filtered; interleaved so the pool is never all-events or all-curated."""
    buckets: list[list[str]] = []
    try:  # 1. upcoming events (soonest first) → their product keywords
        from . import events
        ev: list[str] = []
        for e in (events.upcoming(within_days=150).get("events") or []):
            ev.extend(e.get("keywords") or [])
        buckets.append(ev)
    except Exception:  # noqa: BLE001
        buckets.append([])
    try:  # 2. the store's already-tracked candidates
        q = readers.candidate_queue(store) or {}
        buckets.append([str((c or {}).get("keyword") or "").strip()
                        for c in (q.get("candidates") or {}).values() if isinstance(c, dict)])
    except Exception:  # noqa: BLE001
        buckets.append([])
    try:  # 3. the news watchlist's product keywords
        from . import news
        nk: list[str] = []
        for kws in (news.DEFAULT_THEMES or {}).values():
            nk.extend(kws or [])
        buckets.append(nk)
    except Exception:  # noqa: BLE001
        buckets.append([])
    buckets.append(list(_CURATED_TREND_SEEDS))  # 4. curated fallback (always available)

    pool: list[str] = []
    seen: set[str] = set()
    cap = n * 2  # over-collect so the AI 2nd pass can drop some and still leave ~n clean seeds
    stop = False
    for i in range(max((len(b) for b in buckets), default=0)):
        for b in buckets:
            if i < len(b):
                kw = re.sub(r"\s+", " ", str(b[i] or "")).strip()
                low = kw.lower()
                # Gate seeds through the SAME product-domain filter the surfaces use: the news
                # watchlist (bucket 3) surfaces breaking-news themes — airlines ('jet blue'), autos
                # ('toyota'), destinations ('portugal') — which otherwise became junk dossiers. This
                # stops them at the SOURCE so no junk dossier is ever created (not just hidden).
                if (kw and low not in seen and not readers.is_meta_keyword(kw)
                        and readers.is_listable_keyword(kw)):
                    seen.add(low)
                    pool.append(kw)
                    if len(pool) >= cap:
                        stop = True
                        break
        if stop:
            break
    # AI 2nd opinion: drop novel non-products the deterministic wordlist can't know (new brands,
    # airlines, car models, places). Fail-open + inclusive, so it only ever removes high-confidence
    # junk and never blocks seeding. Then take the top n.
    return _ai_filter_seeds(pool)[:n] or [_CURATED_TREND_SEEDS[0]]


def _ai_filter_seeds(pool: list[str]) -> list[str]:
    """AI product-gate 2nd pass over deterministic-passing seeds — drops terms the LLM is CONFIDENT
    are non-products. Fail-open (LLM down/unsure → keep) and never returns empty just because the
    model nuked everything (deterministic pool stays the floor)."""
    if not pool:
        return pool
    try:
        from . import ai_product_gate
        verdicts = ai_product_gate.classify(pool)
        kept = [t for t in pool if (verdicts.get(t.lower()) or {}).get("ok", True)]
        return kept or pool
    except Exception:  # noqa: BLE001 — AI is advisory; never break seed selection
        return pool


def _default_trend_seed(store: str) -> str:
    """A comma-separated POOL of 10-15 real seeds for an arg-less trend/keyword run (research_dfs
    splits on commas and processes each). Never the meaningless 'trending products' meta-search."""
    return ",".join(_seed_pool(store, 12))


JOB_SPECS: dict[str, dict] = {
    "candidate-score": {
        "id": "candidate-score",
        "title": "Re-score candidate queue",
        "mode": "auto",
        "surface": "keyword",
        "summary": "Recompute numeric scores + buckets across the candidate queue (stdlib, on-disk).",
        "script": "candidate_queue.py",
        "args": lambda store, a: ["score"],
    },
    "listing-snapshot": {
        "id": "listing-snapshot",
        "title": "Snapshot listing queue",
        "mode": "auto",
        "surface": "listing",
        "summary": "Dump the current listing-queue state machine for every category + SKU.",
        "script": "listing_queue.py",
        "args": lambda store, a: ["show"],
    },
    "keyword-discovery": {
        "id": "keyword-discovery",
        "title": "Keyword discovery funnel",
        "mode": "auto-local",
        "local_dep": "dataforseo",
        "surface": "keyword",
        "summary": "DFS keyword/SV pull → Lane-1 10k gate → ingest → score. Runs on the worker "
                   "automatically with the DataForSEO creds from Connections — no operator handoff.",
        # Server-side full funnel: seed → trends → expand → SV → season gate → candidate queue.
        # Writes general-stores/<store>/candidate-queue.json (what readers.keyword_discovery reads).
        "local_run": lambda store, a: {
            "python": sys.executable,
            "script": str(_RESEARCH_DFS),
            "args": [
                "--mode", "keyword",
                # No keyword given ("leave it blank") = seed from the mixed pool (events + candidate
                # queue + news + curated) so one run fills the shortlist with a healthy mix.
                "--keyword", (a.get("keyword") or "").strip() or _default_trend_seed(store),
                "--store", store,
                "--geo", (a.get("geo") or "US,GB"),
                "--python", str(_GENERAL_VENV),
                "--niche-scripts", str(_NICHE_SCRIPTS),
                "--general-scripts", str(_SCRIPTS),
                "--dossiers", str(config.dossiers_dir()),
                "--general-stores-dir", str(config.general_stores_dir()),
                "--repo-root", str(config.repo_root()),
                "--out", str(_RESEARCH_WORK / store),
            ],
            "cwd": str(config.repo_root()),
            "timeout": 1800,  # a blank run processes a 10-15 seed pool sequentially
        },
        "command": lambda store, a: f"/general-store-research {a.get('keyword', '<keyword>')} --store {store}",
    },
    "shopping-scan": {
        "id": "shopping-scan",
        "title": "Google Shopping competitor scan",
        "mode": "auto-local",
        "local_dep": "shopping_scan",
        "surface": "keyword",
        "summary": "Capture the live US Google Shopping landscape — competitor grid, price ladder, "
                   "Google's category rows, and which sellers run Shopping ads — server-side from "
                   "DataForSEO. No browser or local profile needed; runs automatically on the worker.",
        # Server-side DFS recon: the real geolocated US Shopping grid (prices, sellers, ratings,
        # the shop_ad_aclk Sponsored flag) + category rows + sub-keywords straight from
        # DataForSEO's structured API. Writes paid-scan.json + paid-landscape.md, stdlib only.
        "local_run": lambda store, a: {
            "python": sys.executable,
            "script": str(Path(__file__).resolve().parent / "shopping_scan_dfs.py"),
            "args": [
                "--keyword", (a.get("keyword") or "").strip() or "general store",
                "--geo", (a.get("geo") or "US").upper(), "--store", store,
                # Per-market output: US stays at .../<slug>/ (backward-compatible), any other market
                # (GB…) lands in a .../<slug>/<geo>/ subdir — the reader consumes all markets.
                "--out", str(
                    _SHOPPING_SCAN_OUT / store / _slug(a.get("keyword") or "scan")
                    / ("" if (a.get("geo") or "US").upper() in ("US", "USA") else (a.get("geo") or "").lower())
                ),
            ],
            "cwd": str(config.repo_root()),
            "timeout": 320,  # depth-100 grid (Merchant max): posts + polls up to ~180s + sub-kw/SV calls
        },
        "command": lambda store, a: f"/competitor-shopping-scan {a.get('keyword', '<keyword>')}",
    },
    "sponsored-plas": {
        "id": "sponsored-plas",
        "title": "Google Sponsored-PLA capture (paid competitors)",
        "mode": "auto-local",
        "local_dep": "sponsored_plas",
        "surface": "keyword",
        "summary": "Capture Google's 'Sponsored products' carousel — the PAYING dropship competitors + "
                   "their advertiser domains — for a keyword+country via the Bright Data Scraping "
                   "Browser (CDP). The one paid signal DFS + the Web Unlocker can't get (no ads block "
                   "/ Sponsored labels stripped). Server-side, no AdsPower/local machine.",
        # Drives the BD Scraping Browser over CDP (playwright connect_over_cdp to the REMOTE browser),
        # navigates the country's Google SERP, parses the Sponsored carousel. Reads the CDP endpoint
        # from BRIGHTDATA_BROWSER_CDP (exported via connections.as_env; assembled by BD provision).
        "local_run": lambda store, a: {
            "python": sys.executable,
            "script": str(Path(__file__).resolve().parent / "paid_shopping_scan_bd.py"),
            "args": [
                "--keyword", (a.get("keyword") or "").strip() or "general store",
                "--geo", (a.get("geo") or "US").upper(),
                "--out", str(_SHOPPING_SCAN_OUT / store / _slug(a.get("keyword") or "scan") / "plas"),
            ],
            "cwd": str(config.repo_root()),
            "timeout": 200,  # BD browser render + navigate + parse (a single SERP page)
        },
        "command": lambda store, a: f"/competitor-shopping-scan {a.get('keyword', '<keyword>')} --paid",
    },
    "discover-general-stores": {
        "id": "discover-general-stores",
        "title": "Discover general stores",
        "mode": "auto-local",
        "local_dep": "trendtrack",
        "surface": "product",
        "summary": "Find scaling US general stores → classifier gate → spy roster. Runs server-side "
                   "off the live Google Shopping grid (DataForSEO); when a TrendTrack API token is "
                   "set in Connections → Data, it ALSO pulls TrendTrack's scaling-US-shops feed and "
                   "merges it into the candidate pool (the premium source, now callable in-app).",
        # The harvest (discover_stores_dfs.py) merges TrendTrack's scaling-US shops into the DFS
        # domain pool whenever TRENDTRACK_API_TOKEN is present (injected via connections.as_env),
        # then classifies every candidate through the GENERAL gate → Best-Seller Spy roster.
        "local_run": lambda store, a: {
            "python": sys.executable,
            "script": str(_DISCOVER_DFS),
            "args": [
                "--keywords", (a.get("keyword") or "").strip(),
                "--geo", (a.get("geo") or "US"),
                "--store", store,
                "--harvest", str(_HARVEST_STORES),
                "--harvest-python", str(_GENERAL_VENV),
            ],
            "cwd": str(config.repo_root()),
            "timeout": 1200,
        },
        "fallback": lambda store, a: {
            "python": sys.executable,
            "script": str(_DISCOVER_DFS),
            "args": [
                "--keywords", (a.get("keyword") or "").strip(),
                "--geo", (a.get("geo") or "US"),
                "--store", store,
                "--harvest", str(_HARVEST_STORES),
                "--harvest-python", str(_GENERAL_VENV),
            ],
            "cwd": str(config.repo_root()),
            "timeout": 1200,
        },
        "command": lambda store, a: "/discover-general-stores",
    },
    "bestseller-spy": {
        "id": "bestseller-spy",
        "title": "Best-seller rank spy",
        "mode": "auto-local",
        "local_dep": "spy",
        "surface": "product",
        "summary": "Snapshot today's best-selling rank board for every tracked store, then diff "
                   "vs the prior snapshot → refresh Movers + New Products. Runs in-app (stdlib + "
                   "requests, no keys); set a 2-3 day cadence in Autonomy.",
        # snapshot writes snapshots/<store>/<today>.json; the chained diff recomputes movers.json.
        # Output goes to the DATA volume (config.spy_data_dir), not the scripts dir — the scripts
        # live in the container image on Railway, so relative output would be wiped every deploy
        # and the two-day diff could never see a prior snapshot. Readers check this dir first.
        "local_run": lambda store, a: {
            "python": str(_GENERAL_VENV),
            "script": str(_SPY_SNAPSHOT),
            "args": ["--stores", str(_SPY_STORES),
                     "--depth", str(int(a.get("depth") or 30)),
                     "--out", str(config.spy_data_dir() / "snapshots"),
                     *(["--proxy", str(a["proxy"])] if a.get("proxy") else [])],
            "cwd": str(_SCRIPTS),
            "timeout": 1800,  # scrape every tracked store sequentially — can be slow
            "chain": [{
                "python": str(_GENERAL_VENV),
                "script": str(_SPY_DIFF),
                "args": ["--snapshots", str(config.spy_data_dir() / "snapshots"),
                         "--out", str(config.spy_data_dir() / "movers.json")],
                "cwd": str(_SCRIPTS),
                "timeout": 300,
            }],
        },
        "command": lambda store, a: (
            f"cd 06-launch-general-store/scripts && "
            f".venv/bin/python bestseller_snapshot.py --stores stores.txt --depth 30 --out snapshots"
            + (f" --proxy {a['proxy']}" if a.get("proxy") else "")
            + " && .venv/bin/python bestseller_diff.py --snapshots snapshots --out movers.json"
        ),
    },
    "discover-niches": {
        "id": "discover-niches",
        "title": "Discover niches (keyword-first)",
        "mode": "manual",
        "surface": "niche",
        "summary": "Head terms → SV/CPC → SERP → Trends → GO/HOLD/SKIP dossier. Needs DataForSEO.",
        "command": lambda store, a: f"/discover-niches {a.get('keyword', '')}".strip(),
    },
    "discover-niches-pain-first": {
        "id": "discover-niches-pain-first",
        "title": "Discover niches (pain-first)",
        "mode": "manual",
        "surface": "niche",
        "summary": "Reddit pain → Trends → Amazon top-20 → Meta Ads → Amin verdict. Needs Bright Data.",
        "command": lambda store, a: f"/discover-niches-pain-first {a.get('keyword', '')}".strip(),
    },
    "trend-radar": {
        "id": "trend-radar",
        "title": "Trend radar",
        "mode": "auto-local",
        "local_dep": "dataforseo",
        "surface": "trend",
        "summary": "Scan breakout/seasonal trend signals via DFS Trends. Runs on the worker "
                   "automatically with the DataForSEO creds from Connections — no operator handoff.",
        # Server-side: DFS Google-Trends for the seed (+ related queries) across geos → writes
        # dossiers/<slug>/trends.json (what readers._trend_rows / trends_overview read).
        "local_run": lambda store, a: {
            "python": sys.executable,
            "script": str(_RESEARCH_DFS),
            "args": [
                "--mode", "trend",
                "--keyword", (a.get("keyword") or "").strip() or _default_trend_seed(store),
                "--store", store,
                "--geo", (a.get("geo") or "US,GB"),
                "--python", str(_GENERAL_VENV),
                "--niche-scripts", str(_NICHE_SCRIPTS),
                "--general-scripts", str(_SCRIPTS),
                "--dossiers", str(config.dossiers_dir()),
                "--general-stores-dir", str(config.general_stores_dir()),
                "--repo-root", str(config.repo_root()),
                "--out", str(_RESEARCH_WORK / store),
            ],
            "cwd": str(config.repo_root()),
            "timeout": 1800,  # a blank run processes a 10-15 seed pool sequentially
        },
        "command": lambda store, a: f"/trend-radar {a.get('keyword', '')}".strip(),
    },
    "build-listing": {
        "id": "build-listing",
        "title": "Build listing (images + drafts)",
        "mode": "manual",
        "surface": "listing",
        "summary": "Generate gallery + create Shopify drafts for a category. Needs image-gen + Shopify Admin.",
        "command": lambda store, a: f"/general-store-listing {a.get('slug', '<slug>')} --store {store}{_sot_suffix(a)}",
    },
    "source-import": {
        "id": "source-import",
        "title": "General Listing (URL → draft)",
        "mode": "manual",
        "surface": "listing",
        "summary": "ANY product URL — AliExpress / Temu / Amazon / 1688 / Shopify competitor — extracted by the "
                   "one `supplier_import.py` dispatcher → AI-enhance title/description/price/translate → generate "
                   "gallery → Shopify DRAFT. Needs scrape token(s) + image-gen + Shopify Admin.",
        "command": lambda store, a: f"/source-import {a.get('url', '<product-url>')} --store {store}{_sot_suffix(a)}",
    },
    "general-import-url": {
        "id": "general-import-url",
        "title": "Branded Listing / marketplace import (URL → draft)",
        "mode": "manual",
        "surface": "listing",
        "summary": "Drive the general import from ANY one product URL (AliExpress / Temu / Amazon / 1688 / Shopify "
                   "competitor — host-detected by `supplier_import.py`) → supplier refs → AI image gen → DRAFT + "
                   "metafield PDP. Same flow as branded general for every source. Needs scrape token(s) + image-gen + Shopify Admin.",
        "command": lambda store, a: f"/general-store-listing {a.get('url', '<product-url>')} --store {store}{_sot_suffix(a)}",
    },
    "image-autoclean": {
        "id": "image-autoclean",
        "title": "Auto-clean overlay (FREE, no AI)",
        "mode": "auto-local",
        "local_dep": "image_cleaner",
        "surface": "listing",
        # Reactive: fired against ONE specific image from the image-QA review, never on a
        # cadence — so it stays a creatable job but is hidden from the Autonomy scheduler.
        "schedulable": False,
        "summary": "Strip a removable overlay (text/logo/watermark/badge/brand on the background) for "
                   "FREE with local diffusion-inpaint — no paid AI regeneration. Runs in-app when the "
                   "scripts venv is present; else hands off to the free manual route (Canva remove-bg).",
        # Free local fix → ADOPT the result in place (back up original, swap on disk) so the next
        # Shopify push carries the cleaned pixels, then deterministic re-scan. (handled in _run_local)
        "adopt": True,
        "local_run": lambda store, a: {
            "python": str(_GENERAL_VENV),
            "script": str(_AUTOCLEAN_SCRIPT),
            "args": [
                "--image", str((config.general_stores_dir() / store / (a.get("path") or "")).resolve()),
                *_bbox_args(a),
            ],
            "cwd": str(_SCRIPTS),
            "timeout": 120,
        },
        "command": lambda store, a: (
            f"# FREE manual clean (no AI): open general-stores/{store}/{a.get('path', '<image>')} in "
            f"Canva → Magic Eraser / remove the overlay, export, replace the file, then re-run the image-QA scan."
        ),
    },
    "image-upscale": {
        "id": "image-upscale",
        "title": "Upscale low-res image (FREE, no AI)",
        "mode": "auto-local",
        "local_dep": "image_cleaner",
        "surface": "listing",
        # Reactive: fired against ONE specific image flagged below the feed floor, never on a cadence.
        "schedulable": False,
        "summary": "Lift a clean-but-small image below the Google-Shopping feed floor to 1024² for FREE "
                   "with local LANCZOS resampling — no paid AI regeneration. Runs in-app when the scripts "
                   "venv is present; else hands off to AI-upscale / supplier-ref regen.",
        # Free local fix → ADOPT the lifted image in place + deterministic re-scan (see _run_local).
        "adopt": True,
        "local_run": lambda store, a: {
            "python": str(_GENERAL_VENV),
            "script": str(_UPSCALE_SCRIPT),
            "args": [
                "--image", str((config.general_stores_dir() / store / (a.get("path") or "")).resolve()),
                "--target", "1024",
            ],
            "cwd": str(_SCRIPTS),
            "timeout": 120,
        },
        "command": lambda store, a: (
            f"# FREE manual upscale: open general-stores/{store}/{a.get('path', '<image>')} and enlarge the "
            f"short side to >=1024px (any AI-upscaler / image editor), replace the file, then re-run the image-QA scan."
        ),
    },
    "image-regen": {
        "id": "image-regen",
        "title": "Regenerate image (supplier-ref)",
        "mode": "manual",
        "surface": "listing",
        "schedulable": False,
        "summary": "Off-subject / wrong-product / still-soft-after-upscale image — regenerate from the MANDATORY "
                   "supplier photo ref (nano_banana_pro + gen_gallery.py). Use when a free strip/upscale can't fix it.",
        "command": lambda store, a: (
            f"cd 06-launch-general-store/scripts && python gen_gallery.py {a.get('slug', '<category>')} "
            f"--skus sku-{a.get('sku_slug', '<sku>')} --force  # regen from _supplier-refs (V10 rule 11)"
        ),
    },
    "image-relang": {
        "id": "image-relang",
        "title": "Rewrite gallery text language",
        "mode": "manual",
        "surface": "listing",
        "schedulable": False,
        "summary": "Gallery infographic text is in the wrong language — cheap rewrite to the store's "
                   "native language (Canva / cheap model), no full regeneration needed.",
        "command": lambda store, a: (
            f"# Cheap gallery-language rewrite: edit the text on general-stores/{store}/{a.get('path', '<image>')} "
            f"into the store's native language (Canva text edit), replace the file, re-run the image-QA scan."
        ),
    },
    "image-resource": {
        "id": "image-resource",
        "title": "Re-source product (REJECT class)",
        "mode": "manual",
        "surface": "listing",
        "schedulable": False,
        "summary": "HARD REJECT: a 3rd-party trademark is printed ON the actual product (counterfeit / "
                   "dropship-fraud) — an image edit can't fix a sourcing problem. Re-source a clean SKU.",
        "command": lambda store, a: (
            f"# RE-SOURCE — do NOT image-edit: general-stores/{store}/{a.get('path', '<image>')} shows a "
            f"3rd-party trademark on the physical product. Re-source SKU '{a.get('sku', '<sku>')}' "
            f"({a.get('slug', '')}) with a non-branded equivalent via the private agent, then rebuild."
        ),
    },
    "catalog-scan": {
        "id": "catalog-scan",
        "title": "Catalog check (already on the store?)",
        "mode": "manual",
        "surface": "listing",
        "summary": "Step 0 BEFORE 1688 sourcing: scan the store's live catalog and flag any "
                   "researched product the store already sells — judged on the product IMAGE "
                   "(background-invariant), not the title. Needs Shopify Admin + a vision key.",
        "command": lambda store, a: (
            f"cd 05-launch-niche-store/china-source-match/scripts && "
            f"python catalog_scan.py index --store {store} --out catalog-index-{store}.json && "
            f"python catalog_scan.py check --store {store} "
            f"--in {a.get('researched', 'researched.json')} "
            f"--index catalog-index-{store}.json --judge openrouter "
            f"--out catalog-match-{store}.json"
        ),
    },
    "china-verify": {
        "id": "china-verify",
        "title": "Verify + enrich 1688 match",
        "mode": "manual",
        "surface": "product",
        "schedulable": False,
        "summary": "Earn a ✓ vision-verified verdict for a sourcing-match row: reverse-image "
                   "search → tmapi enrich (pulls 1688 variants + specs) → Gemini VLM gallery "
                   "judge. Needs AdsPower + a vision key + the TMAPI key, so it runs in the "
                   "operator's own Claude Code session; matched.json verdicts then appear here.",
        "command": lambda store, a: (
            f"cd 05-launch-niche-store/china-source-match/scripts && "
            f"# verify{(' ' + a['subject']) if a.get('subject') else ''}: reverse-image → enrich → VLM judge\n"
            f"python china_image_search.py --in {a.get('researched', 'candidates.json')} "
            f"--site 1688 --out _candidates && "
            f"python tmapi_1688.py --in _candidates/candidates.json --enrich "
            f"--out _candidates/candidates.json && "
            f"python match_china.py --in _candidates/candidates.json --judge openrouter "
            f"--min-conf 0.85 --out matched.json"
        ),
    },
    "china-enrich": {
        "id": "china-enrich",
        "title": "Enrich 1688 specs + variants",
        "mode": "auto-local",
        "local_dep": "tmapi",
        "surface": "product",
        "schedulable": False,
        "summary": "Pull the full 1688 SKU properties (variants / specs / gallery / sold) for a "
                   "matched offer and write them into the match so the modal shows useful info. "
                   "Needs only the TMAPI HTTP key (no AdsPower / vision) — so it runs in-app when "
                   "the key is present, else hands off to the operator's Claude Code.",
        # enrich_match.py drives the existing tmapi_1688.enrich() over the `best` record(s)
        # in the matched.json and writes variants/specs/gallery/sold back in place — the
        # exact shape readers.sourcing_match() already surfaces. No reader change needed.
        "local_run": lambda store, a: {
            "python": str(_CHINA_VENV),
            "script": str(_CHINA_ENRICH),
            "args": [
                "--matched", str(a.get("source") or "nosura-neck-fan.matched.json"),
                *(["--offer", str(a["offer_id"])] if a.get("offer_id") else ["--all"]),
            ],
            "cwd": str(_CHINA_DIR),
            "timeout": 120,
        },
        "command": lambda store, a: (
            f"cd 05-launch-niche-store/china-source-match/scripts && "
            f"python enrich_match.py --matched {a.get('source') or 'nosura-neck-fan.matched.json'} "
            + (f"--offer {a['offer_id']}" if a.get("offer_id") else "--all")
        ),
    },
}


def specs() -> list[dict]:
    """Public, serializable view of the registry (no lambdas)."""
    return [
        {"id": s["id"], "title": s["title"], "mode": s["mode"],
         "surface": s["surface"], "summary": s["summary"],
         "schedulable": s.get("schedulable", True),
         **({"local_dep": s["local_dep"]} if s.get("local_dep") else {})}
        for s in JOB_SPECS.values()
    ]


# ------------------------------------------------------------------ runner
def _run_auto(job_id: int, spec: dict, store: str, args: dict) -> None:
    """Execute an auto spec's script in a pool worker, recording status.

    The subprocess is guarded by a per-(store, script) lock so two jobs that mutate the
    SAME store's SAME state machine never read-modify-write its JSON concurrently (which
    would lose an update). Jobs on a different store/script/product hold a different lock,
    so they still run in parallel — parallelism only where it's logically safe.
    """
    cmd = _py(spec["script"], store, *spec["args"](store, args))
    # Store-scoped Google ids (Ads customer / Merchant Center) layer over the global keys so a
    # store-scoped job targets THIS store's account. Real process env still wins (see _base_env).
    # STORE_MODE (general | fashion | both) rides along so every research/validation/listing
    # script knows which catalog path this store runs (fashion reverses the apparel exclusion;
    # both runs general + fashion together with nothing excluded).
    env = {
        **connections.google_env_for(store),
        **_base_env(),
        "GENERAL_STORES_DIR": str(config.general_stores_dir()),
        "STORE_MODE": readers.store_mode(store),
    }
    with _resource_lock(f"{store}:{spec['script']}"):
        runlog.job_update(job_id, "running")
        try:
            proc = subprocess.run(
                cmd, cwd=str(config.repo_root()), env=env,
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            runlog.job_update(job_id, "failed", detail="timed out after 120s")
            return
        except Exception as e:  # noqa: BLE001 — never let a launch error orphan the job / kill the pool thread
            runlog.job_update(job_id, "failed", detail=f"could not run: {e}")
            return
    if proc.returncode == 0:
        runlog.job_update(job_id, "done", output=(proc.stdout or "").strip())
    else:
        runlog.job_update(
            job_id, "failed",
            detail=(proc.stderr or "").strip() or f"exit {proc.returncode}",
            output=(proc.stdout or "").strip() or None,
        )


def _run_local(job_id: int, run: dict) -> None:
    """Execute an auto-local spec's external script (own venv/cwd/timeout) in a background
    thread, recording status. Same status lifecycle as _run_auto, just a different interpreter
    + working dir + timeout (e.g. the DataForSEO Shopping scan, the image cleaner, spy diff)."""
    runlog.job_update(job_id, "running")
    cmd = [run["python"], run["script"], *run["args"]]
    out_dir = next((run["args"][i + 1] for i, x in enumerate(run["args"]) if x == "--out"), None)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    env = {**_base_env(), **(run.get("env_extra") or {})}
    try:
        proc = subprocess.run(
            cmd, cwd=run["cwd"], env=env,
            capture_output=True, text=True, timeout=run.get("timeout", 600),
        )
    except FileNotFoundError:
        runlog.job_update(
            job_id, "failed",
            detail="scan interpreter/script missing — is the 01c .venv installed?",
        )
        return
    except subprocess.TimeoutExpired:
        runlog.job_update(job_id, "failed", detail=f"timed out after {run.get('timeout', 600)}s")
        return
    except Exception as e:  # noqa: BLE001 — never let a launch error orphan the job / kill the thread
        runlog.job_update(job_id, "failed", detail=f"could not run scan: {e}")
        return
    if proc.returncode != 0:
        runlog.job_update(
            job_id, "failed",
            detail=(proc.stderr or "").strip() or f"exit {proc.returncode}",
            output=(proc.stdout or "").strip() or None,
        )
        return
    outputs = [(proc.stdout or "").strip()]
    # Chained steps (e.g. spy: snapshot → diff): run sequentially, abort on first failure.
    for step in run.get("chain") or []:
        try:
            sp = subprocess.run(
                [step["python"], step["script"], *step["args"]],
                cwd=step.get("cwd", run["cwd"]), env=env,
                capture_output=True, text=True, timeout=step.get("timeout", 600),
            )
        except Exception as e:  # noqa: BLE001 — FileNotFound/Timeout/OSError all abort the chain cleanly
            runlog.job_update(job_id, "failed",
                              detail=f"chain step {os.path.basename(step['script'])} failed: {e}",
                              output="\n".join(o for o in outputs if o) or None)
            return
        outputs.append((sp.stdout or "").strip())
        if sp.returncode != 0:
            runlog.job_update(
                job_id, "failed",
                detail=(sp.stderr or "").strip()
                or f"{os.path.basename(step['script'])} exit {sp.returncode}",
                output="\n".join(o for o in outputs if o) or None)
            return
    # Free local image fix (auto-clean / upscale): the script wrote a sibling fixed file — ADOPT it
    # in place so the on-disk image (the source the Shopify push uploads) becomes the fixed one,
    # then deterministic re-scan. Closes the fix → adopt → re-scan loop for the free fixes.
    if run.get("adopt"):
        try:
            outputs.append(json.dumps(_adopt_fixed_image(run, outputs[0])))
        except Exception as e:  # noqa: BLE001 — adoption failure must surface, not silently "done"
            runlog.job_update(job_id, "failed", detail=f"adopt failed: {e}",
                              output="\n".join(o for o in outputs if o) or None)
            return
    runlog.job_update(job_id, "done", output="\n".join(o for o in outputs if o) or None)


def _adopt_fixed_image(run: dict, stdout: str) -> dict:
    """Adopt a FREE local image fix in place: the fix script (image_autoclean / image_upscale)
    wrote a sibling output file and printed a JSON line with its path. Back the original up ONCE
    (`<name>.orig<ext>`), swap the fixed file over the original on disk, then deterministic-re-scan
    the adopted image (resolution floor). Raises on any failure so the job is marked failed.

    There is no separate Shopify upload step in the app — the on-disk file under the store dir IS
    the source the later push uploads, so swapping it in place is what "re-upload the fix" means."""
    original = next((run["args"][i + 1] for i, x in enumerate(run["args"]) if x == "--image"), None)
    out_path = None
    for line in reversed([ln for ln in (stdout or "").splitlines() if ln.strip()]):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("ok") is False:
            raise RuntimeError(j.get("error") or "fix script reported failure")
        if isinstance(j, dict) and j.get("ok") and j.get("out"):
            out_path = j["out"]
        break  # the result line is the LAST JSON line the script printed
    if not original or not out_path or not os.path.isfile(out_path):
        raise RuntimeError("fix produced no adoptable output file")
    op = Path(original)
    backup = op.with_suffix("").as_posix() + ".orig" + op.suffix
    if not os.path.exists(backup):
        shutil.copy2(original, backup)
    os.replace(out_path, original)  # fixed pixels become the live on-disk image
    from . import image_qa  # lazy — image_qa imports jobs, so import here avoids a cycle
    metrics = image_qa._pixel_metrics(Path(original))
    return {"adopted": True, "original": original, "backup": backup, "rescan": metrics}


def _resolve_sot_args(spec_id: str, args: dict) -> dict:
    """For listing-build specs, auto-resolve the 1688-first source-of-truth from sourcing-match
    (keyed on the build's slug/url/subject) and merge it in — UNLESS the caller already set it.

    This is the workflow glue: the source-of-truth decided in Sourcing Match flows into the
    listing build automatically and identically for BOTH paths (slug build + URL import)."""
    if spec_id not in _LISTING_BUILD_SPECS:
        return args
    from . import readers  # lazy import — readers has no dep on jobs, so no import cycle
    # The 1688 master switch is authoritative: when OFF, no build grounds on 1688 — even if a
    # caller (Daily-Listings materialization, a future button) handed us an explicit 1688 SOT.
    if not readers.sourcing_1688_enabled():
        return {**args, "source_of_truth": "researched", "source_url": "", "sot_resolved": False}
    if args.get("source_of_truth"):
        return args
    key = args.get("slug") or args.get("url") or args.get("subject") or ""
    sot = readers.resolve_source_of_truth(key)
    return {
        **args,
        "source_of_truth": sot["source_of_truth"],
        "source_url": sot.get("source_url") or "",
        "sot_resolved": sot["resolved"],
    }


def _record_needs_operator(
    store: str, spec_id: str, title: str, command: str, detail: str | None = None
) -> dict:
    """Record a needs-operator job, deduped. The Daily-Listings plan re-materializes the same
    rows every day, so without this an identical keyword-discovery/source-import handoff would
    pile up one new row per day. If an open needs-operator job with the SAME spec+command
    already exists, bump its timestamp (and refresh detail) and return it instead of inserting
    a duplicate — the operator sees one actionable item, not a daily-growing stack."""
    existing = runlog.job_find_open(store, spec_id, command)
    if existing is not None:
        runlog.job_update(existing["id"], "needs-operator", detail=detail)
        return runlog.job_get(existing["id"])
    job_id = runlog.job_create(store, spec_id, "manual", title, "needs-operator", command)
    if detail is not None:
        runlog.job_update(job_id, "needs-operator", detail=detail)
    return runlog.job_get(job_id)


def create(spec_id: str, store: str, args: dict | None) -> dict:
    """Create a job. Auto specs run in a daemon thread; manual specs record the command."""
    spec = JOB_SPECS.get(spec_id)
    if spec is None:
        raise KeyError(spec_id)
    args = _resolve_sot_args(spec_id, args or {})

    if spec["mode"] == "manual":
        command = spec["command"](store, args)
        return _record_needs_operator(store, spec_id, spec["title"], command)

    # auto-local: run it ourselves IF the local dependency is reachable, else hand off.
    if spec["mode"] == "auto-local":
        health = preflight(spec["local_dep"])
        if health["reachable"]:
            run = spec["local_run"](store, args)
            run["env_extra"] = {"STORE_MODE": readers.store_mode(store)}
            if spec.get("adopt"):
                run["adopt"] = True  # free local image fix → adopt result in place (see _run_local)
            command = " ".join([run["python"], run["script"], *run["args"]])
            dup = runlog.job_find_active(store, spec_id, command)
            if dup is not None:
                return dup  # identical run already queued/running → don't stack a duplicate
            job_id = runlog.job_create(store, spec_id, "auto-local", spec["title"], "queued", command)
            _pool_for(spec_id).submit(_run_local, job_id, run)
            return runlog.job_get(job_id)
        # Local dep down — but if this spec has a server-side fallback, RUN IT so the work
        # still gets done (e.g. discover-general-stores → DFS harvest). The premium source is
        # the only thing we lose; the job completes with real output instead of stalling.
        if spec.get("fallback"):
            run = spec["fallback"](store, args)
            run["env_extra"] = {"STORE_MODE": readers.store_mode(store)}
            command = " ".join([run["python"], run["script"], *run["args"]])
            dup = runlog.job_find_active(store, spec_id, command)
            if dup is not None:
                return dup  # identical fallback run already active → collapse
            job_id = runlog.job_create(store, spec_id, "auto-local", spec["title"], "queued", command)
            runlog.job_update(job_id, "queued", detail="premium source not reachable — running the "
                              "server-side recon instead.")
            _pool_for(spec_id).submit(_run_local, job_id, run)
            return runlog.job_get(job_id)
        # No fallback → record the manual operator handoff with the reason.
        command = spec["command"](store, args)
        return _record_needs_operator(store, spec_id, spec["title"], command, detail=health["detail"])

    # auto
    command = " ".join(_py(spec["script"], store, *spec["args"](store, args)))
    dup = runlog.job_find_active(store, spec_id, command)
    if dup is not None:
        return dup  # identical run already queued/running → don't stack a duplicate
    job_id = runlog.job_create(
        store, spec_id, "auto", spec["title"], "queued", command,
    )
    _pool_for(spec_id).submit(_run_auto, job_id, spec, store, args)
    return runlog.job_get(job_id)


# Daily-Listings plan row `method` → (job spec, the spec's input arg key). This is the glue
# that turns a planned calendar row into the concrete NEXT pipeline step under the hybrid
# model: a keyword/trend signal → run the research funnel; a competitor/marketplace winner
# (carries a url) → source-import; an already-clustered category (carries a slug) → build it.
_PLAN_METHOD_SPEC = {
    "research-pipeline": ("keyword-discovery", "keyword"),
    "source-import": ("source-import", "url"),
    "build-listing": ("build-listing", "slug"),
}


def execute_plan(store: str, items: list[dict]) -> dict:
    """Materialize Daily-Listings plan rows into concrete jobs — one per row.

    Each row declares a `method` (its next pipeline step); we map it to the matching job
    spec and create it via `create` (auto runs in a thread; manual records the exact
    operator command). This is what turns the listing CALENDAR from a visualization into a
    real scheduler: "run today" fans the day's rows out into the jobs queue, keyed to the
    selected store. Rows with no usable input (keyword/url/slug) are skipped, not faked.
    """
    created: list[dict] = []
    skipped: list[dict] = []
    for it in items or []:
        method = (it or {}).get("method") or ""
        mapping = _PLAN_METHOD_SPEC.get(method)
        if mapping is None:
            skipped.append({"keyword": it.get("keyword"), "reason": f"no job mapping for method '{method}'"})
            continue
        spec_id, arg_key = mapping
        val = it.get(arg_key) or it.get("url") or it.get("slug") or it.get("keyword")
        if not val:
            skipped.append({"keyword": it.get("keyword"), "reason": "row has no keyword/url/slug input"})
            continue
        created.append(create(spec_id, store, {arg_key: val}))
    return {
        "created": created,
        "skipped": skipped,
        "counts": {"created": len(created), "skipped": len(skipped)},
    }


# Specs whose local_run does real work with EMPTY args — safe for the self-heal sweep to
# recreate blindly. Everything else (image fixes keyed on a sku/file, china enrich keyed on
# a slug) carries args the stale row can't reproduce, so those handoffs stay untouched.
_REQUEUE_ARGLESS = {"bestseller-spy", "trend-radar"}


def requeue_unblocked() -> dict:
    """Self-heal the needs-you backlog. A handoff is recorded when a job's local dependency
    (venv, scripts, key) is missing at create time — but if that dependency appears LATER
    (a new image ships the research venvs, the operator adds the TMAPI key), the stale row
    would sit in the inbox forever while the work never runs. This sweep re-checks every
    open handoff whose spec the app could run itself (auto-local) and whose dependency is
    reachable NOW, then: arg-less specs are recreated through `create` (real auto path);
    keyword-discovery recovers its keyword from the recorded command (a bare `<keyword>`
    placeholder row is just closed — the Daily-Listings plan re-materializes real ones).
    Runs on every scheduler loop; cheap when idle."""
    healed: list[dict] = []
    dep_cache: dict[str, bool] = {}
    created_keys: set[tuple] = set()  # dedupe within one sweep: N stale rows ≠ N new scans
    registered = set(readers.list_stores())
    for j in runlog.jobs_open_handoffs():
        spec = JOB_SPECS.get(j.get("spec") or "")
        if not spec or spec.get("mode") != "auto-local":
            continue
        # Handoffs for stores the operator has since deleted: close, never re-run.
        if j.get("store") and j["store"] not in registered:
            runlog.job_update(j["id"], "superseded", detail="store no longer registered")
            continue
        dep = spec.get("local_dep") or ""
        if dep not in dep_cache:
            dep_cache[dep] = bool(preflight(dep).get("reachable"))
        if not dep_cache[dep]:
            continue
        args: dict | None = None
        if j["spec"] in _REQUEUE_ARGLESS:
            args = {}
        elif j["spec"] == "keyword-discovery":
            m = re.search(r"/general-store-research\s+(.+?)\s+--store\b", j.get("command") or "")
            kw = (m.group(1).strip() if m else "").strip("\"'")
            if kw and kw != "<keyword>":
                args = {"keyword": kw}
            else:  # placeholder handoff — close it; the daily plan creates real keyword jobs
                runlog.job_update(
                    j["id"], "superseded",
                    detail="research now runs in-app — the daily plan queues real keyword jobs",
                )
                healed.append({"old": j["id"], "new": None, "spec": j["spec"], "store": j["store"]})
                continue
        if args is None:
            continue
        # The arg-less specs do GLOBAL work (spy scans the tracked-store roster, trend radar
        # writes the shared trends.json) — the store on the row is just a label. Ten stale
        # rows must collapse into ONE new job, not ten identical scans.
        key = (j["spec"], json.dumps(args, sort_keys=True)) if j["spec"] in _REQUEUE_ARGLESS \
            else (j["spec"], j.get("store"), json.dumps(args, sort_keys=True))
        if key in created_keys:
            runlog.job_update(j["id"], "superseded", detail="collapsed into the re-queued job")
            continue
        runlog.job_update(
            j["id"], "superseded",
            detail="dependency now available — re-queued as an auto job by the worker",
        )
        try:
            new = create(j["spec"], j["store"], args)
            created_keys.add(key)
            healed.append({"old": j["id"], "new": new.get("id"), "spec": j["spec"], "store": j["store"]})
        except Exception as exc:  # noqa: BLE001 — one bad spec must not kill the sweep
            runlog.job_update(j["id"], "needs-operator", detail=f"requeue failed: {exc}")
    return {"healed": len(healed), "jobs": healed}


def _kw_from_command(cmd: str) -> str:
    """Recover the `--keyword <value>` argument from a reaped job's recorded command line, so an
    interrupted keyword-discovery / shopping-scan can be re-created with its real keyword. The
    value runs until the next `--flag` (keywords contain spaces + commas, so stop at ` --<letter>`)."""
    m = re.search(r"--keyword\s+(.+?)(?:\s+--[a-z]|\s*$)", cmd or "")
    kw = (m.group(1).strip().strip("\"'") if m else "")
    # A blank keyword slot ("--keyword  --store …") lets the capture swallow the NEXT flag —
    # treat that as blank (keyword-discovery then falls back to the mixed pool).
    return "" if kw.startswith("--") else kw


def requeue_orphaned_auto() -> dict:
    """Self-heal jobs a restart interrupted. `reap_orphan_jobs` marks every in-flight job
    'failed / orphaned by restart' at boot — but those aren't real failures, the process just died
    under a deploy. Re-queue the auto-local ones (deduped) so the always-on pipeline RESUMES, and
    supersede the rest so a deploy never leaves a wall of stale 'failed' cards for the operator to
    stare at. Idempotent: once a row is superseded/re-queued it is no longer selected, so this can't
    loop across restarts. Best-effort; called once at startup."""
    healed: list[dict] = []
    superseded = 0
    seen: set[tuple] = set()
    registered = set(readers.list_stores())
    for j in runlog.jobs_orphaned_restart():
        spec_id = j.get("spec") or ""
        spec = JOB_SPECS.get(spec_id)
        store = j.get("store")
        # What args to re-create with. None ⇒ don't re-run, just clear the noise card.
        args: dict | None = None
        if spec and spec.get("mode") == "auto-local" and (not store or store in registered):
            if spec_id in _REQUEUE_ARGLESS:
                args = {}
            elif spec_id == "keyword-discovery":
                args = {"keyword": _kw_from_command(j.get("command") or "")}  # blank ⇒ mixed pool
            elif spec_id == "shopping-scan":
                kw = _kw_from_command(j.get("command") or "")
                args = {"keyword": kw} if kw else None
        if args is None:
            runlog.job_update(j["id"], "superseded",
                              detail="cleared: interrupted by a restart (not a real failure)")
            superseded += 1
            continue
        key = (spec_id, store, json.dumps(args, sort_keys=True))
        if key in seen:  # N identical orphans (e.g. the same pool run reaped twice) → ONE fresh job
            runlog.job_update(j["id"], "superseded", detail="collapsed into the re-queued job")
            superseded += 1
            continue
        seen.add(key)
        try:
            new = create(spec_id, store, args)
            runlog.job_update(j["id"], "superseded",
                              detail=f"re-queued after restart → job #{new.get('id')}")
            healed.append({"old": j["id"], "new": new.get("id"), "spec": spec_id, "store": store})
        except Exception as exc:  # noqa: BLE001 — one bad spec must not kill the sweep
            runlog.job_update(j["id"], "failed", detail=f"restart self-heal failed: {exc}")
    return {"healed": len(healed), "superseded": superseded, "jobs": healed}


def create_bulk(spec_id: str, store: str, links: list[str], arg_key: str) -> list[dict]:
    """Create one import job per pasted link (bulk import).

    Mirrors `create` exactly — each link becomes its own job under the same spec — so the
    bulk path reuses the single-import mechanism rather than inventing a parallel one. The
    `arg_key` is the per-spec input name ("url" for source/general URL imports, "slug" for
    a slug-driven build). Blank lines are skipped; whitespace is trimmed.
    """
    if spec_id not in JOB_SPECS:
        raise KeyError(spec_id)
    jobs: list[dict] = []
    for raw in links:
        link = (raw or "").strip()
        if not link:
            continue
        jobs.append(create(spec_id, store, {arg_key: link}))
    return jobs
