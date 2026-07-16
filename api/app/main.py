"""Operator app backend (Phase 1) — read-only API over the Google Stores pipeline.

Phase 1 is intentionally read-only: it surfaces the current state of the pipeline
(listing queues, candidate queues, dossiers, niche launches) so operators get a
live overview. Write paths (trigger discovery, advance a SKU, go-live) land in
later phases on top of DBOS Transact.
"""
from __future__ import annotations

import re

import json
import os
import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import actions, alerts, assistant, auth, automation, brightdata, config, connections, costs, db, events, feed, google_oauth, image_qa, jobs, news, optimization, photo_dedup, pricing, readers, runlog, shopify, shopify_webhooks, sku_plan, store_sync, trendtrack, users, worker


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Any job left 'running'/'queued' belongs to a PRIOR process that is now dead — its
    # thread did not survive the restart, so the row is a zombie. Fail them once at boot so
    # the WorkerStrip "running" count reflects truth instead of orphaned state.
    reaped = runlog.reap_orphan_jobs()
    if reaped:
        print(f"[startup] reaped {reaped} orphaned job(s) from a prior process")
    # A restart (deploy) is not a real failure — resume the auto-local work it interrupted and
    # clear the rest, so the operator never sees a wall of 'orphaned by restart' cards. Runs
    # unconditionally so it also sweeps up orphans left by EARLIER restarts. Best-effort.
    try:
        healed = jobs.requeue_orphaned_auto()
        if healed.get("healed") or healed.get("superseded"):
            print(f"[startup] restart self-heal: re-queued {healed['healed']}, cleared {healed['superseded']}")
    except Exception as exc:  # noqa: BLE001 — self-heal must never block boot
        print(f"[startup] restart self-heal skipped: {exc}")
    # Restore the competitor-spy ROSTER from the persistent volume (or seed the volume from the
    # vendored baseline the first time). The roster's stores.txt + enrichment JSONs are read/written
    # under the EPHEMERAL image dir, so without this, weekly `discover-general-stores` finds +
    # operator admit/remove edits reset to the baseline on every deploy. Best-effort; never blocks boot.
    try:
        from . import spy_roster_persist
        _rst = spy_roster_persist.hydrate()
        if _rst.get("restored") or _rst.get("seeded"):
            print(f"[startup] spy-roster hydrate: restored {_rst.get('restored')}, seeded {_rst.get('seeded')}")
    except Exception as exc:  # noqa: BLE001 — never block boot
        print(f"[startup] spy-roster hydrate skipped: {exc}")
    # Provision the real owner login from OWNER_NAME/OWNER_PASSWORD env vars (if set), so a
    # live deploy gets the operator's own credentials without a password living in the repo.
    provisioned = users.bootstrap_owner_from_env()
    if provisioned:
        print(f"[startup] owner login provisioned from env: {provisioned}")
    # Bound DB growth (keeps newest history, never touches live work) so Neon storage and
    # query times stay flat over months — part of keeping the Railway/Neon bill predictable.
    pruned = runlog.prune()
    if any(pruned.values()):
        print(f"[startup] pruned old history: {pruned}")
    # Quarantine news-driven junk dossiers ('jet-blue', 'toyota', 'portugal'...) the trend feed
    # auto-created — the read filter hides their cards, but this REMOVES the source (reversible
    # move to _junk-quarantine/) so they stop producing filtered cards + expansion stragglers.
    try:
        swept = readers.purge_junk_dossiers()
        if swept.get("quarantined"):
            print(f"[startup] quarantined {len(swept['quarantined'])} junk dossier(s): {swept['quarantined']}")
    except Exception as exc:  # noqa: BLE001 — never block boot
        print(f"[startup] junk-dossier sweep skipped: {exc}")
    # AI-sweep non-dropshippable keywords (banks/brands/motels/services the deterministic blocklist
    # can't know) off the trend + keyword surfaces. Runs in a DAEMON THREAD — it makes batched LLM
    # calls (~30-60s) and must never block boot; it persists a hide-set the reads use.
    def _kw_sweep():
        try:
            r = readers.sweep_nonproduct_keywords()
            if r.get("newly_flagged"):
                print(f"[startup] hid {len(r['newly_flagged'])} non-product keyword(s): {r['newly_flagged'][:20]}")
        except Exception as exc:  # noqa: BLE001
            print(f"[startup] non-product keyword sweep skipped: {exc}")
    threading.Thread(target=_kw_sweep, daemon=True, name="nonproduct-sweep").start()
    # Start the always-on worker daemon. It idles cheaply until the operator arms the worker
    # (worker_state.enabled), then runs cadence-gated ticks per store. Safe to start always.
    worker.start_scheduler()
    yield
    worker.stop_scheduler()


app = FastAPI(title="Google Stores — Operator API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),
    # Any *.up.railway.app origin is also allowed, so renaming the web service's public
    # domain (e.g. web-production-xxxx → operation-primecore) can never break the frontend
    # with a stale CORS_ORIGINS env ("Can't reach the backend" while the api is healthy).
    # Safe here: auth is a Bearer token from localStorage (not cookies), so a foreign
    # railway-hosted page still can't make authenticated calls.
    allow_origin_regex=r"https://[a-z0-9-]+\.up\.railway\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Server-side P&L guard — the "reps never see P&L" rule enforced at the API, not just
# hidden in the UI. Only the finance surfaces are gated (everything else keeps the open
# posture the shell login already fronts). The app always ships with a working owner login
# (seeded admin/admin, renamed from inside Access), so there's no setup-mode no-op to carve
# out. OPTIONS/preflight is always let through so CORS keeps working.
_FINANCE_PREFIXES = ("/api/finance", "/api/company-pl")

# Machine-to-machine finance endpoints with their OWN auth — the Google Ads Apps Script
# POSTs daily spend authed by the per-store generated script key (validated inside
# finance.ad_spend_ingest), never by an operator Bearer session. Without this exemption
# the script always got 401 and ad spend could never flow in.
_FINANCE_GUARD_EXEMPT = ("/api/finance/ad-spend/ingest", "/api/finance/ad-spend/checkpoint")


@app.middleware("http")
async def _finance_guard(request: Request, call_next):
    path = request.url.path
    if (
        request.method != "OPTIONS"
        and path not in _FINANCE_GUARD_EXEMPT
        and any(path == p or path.startswith(p + "/") for p in _FINANCE_PREFIXES)
    ):
        user = auth.resolve(auth.token_from_header(request.headers.get("authorization")))
        if not user:
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        if user.get("access", {}).get("finance") not in ("owner", "admin", "rep"):
            return JSONResponse({"detail": "finance access required"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict:
    # `commit` = the deployed git SHA (Railway injects RAILWAY_GIT_COMMIT_SHA) — lets a caller
    # confirm which build is live before triggering a mode-dispatched action.
    return {"ok": True, "tenant": config.tenant(), "repo_root": str(config.repo_root()),
            "commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7]}


@app.get("/api/overview")
def overview() -> dict:
    return readers.overview()


@app.get("/api/stores")
def stores() -> dict:
    return {"stores": [readers.store_summary(s) for s in readers.list_stores()]}


@app.post("/api/stores")
def stores_add(body: dict = Body(...)) -> dict:
    """Register a new Shopify store key (scaffolds its listing queue), so it becomes available
    in Connections for per-store Shopify creds + per-store Google Ads/Merchant assignment.
    Optional `mode`: general (default) | fashion | both — which catalog path the store runs."""
    key = (body or {}).get("key", "")
    mode = (body or {}).get("mode") or "general"
    try:
        slug = readers.add_store(key, mode=mode)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    runlog.record(None, "stores", "add", "done", detail=f"{slug} ({mode})")
    return {"ok": True, "store": slug, "mode": mode, "stores": readers.list_stores()}


@app.put("/api/stores/{store}/mode")
def stores_set_mode(store: str, body: dict = Body(...)) -> dict:
    """Set a store's catalog path (general | fashion | both). The flag cascades to the
    research funnel, competitor finding and listing (jobs pass STORE_MODE to every script)."""
    mode = (body or {}).get("mode", "")
    try:
        readers.set_store_mode(store, mode)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    runlog.record(store, "stores", "set-mode", "done", detail=mode)
    return {"ok": True, "store": store, "mode": mode}


@app.delete("/api/stores/{store}")
def stores_delete(store: str) -> dict:
    """Unregister a store: remove its general-stores/<store>/ directory AND clear its per-store
    credentials from Connections. Idempotent from the UI's view — 404 only if truly unknown."""
    try:
        slug = readers.remove_store(store)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        connections.forget_store(slug)
    except Exception:
        pass
    runlog.record(None, "stores", "delete", "done", detail=slug)
    return {"ok": True, "store": slug, "stores": readers.list_stores()}


@app.get("/api/shell")
def shell() -> dict:
    """The Operation-System launcher: every store + its real catalog rollup, plus the
    registry of apps each store's Frontend exposes. Counts are read from disk (honest),
    never stubbed."""
    stores = [readers.store_summary(s) for s in readers.list_stores()]
    return {
        "system": "Google Stores · Operation System",
        "stores": stores,
        "totals": {
            "stores": len(stores),
            "categories": sum(s["categories"] for s in stores),
            "skus": sum(s["skus_total"] for s in stores),
        },
    }


@app.get("/api/feed/{store}")
def feed_report(store: str) -> dict:
    """Product Feed & Optimization — per-SKU GMC feed-readiness derived from the on-disk
    listing pipeline. The live GMC half is surfaced as 'not connected' (honest), never faked."""
    report = feed.feed_report(store)
    if report is None:
        raise HTTPException(status_code=404, detail=f"store '{store}' not found")
    return report


@app.get("/api/stores/{store}")
def store_detail(store: str) -> dict:
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"store '{store}' not found")
    return {
        "summary": readers.store_summary(store),
        "categories": readers.store_categories(store),
    }


@app.get("/api/stores/{store}/categories/{slug}")
def store_category(store: str, slug: str) -> dict:
    """Per-category drill-down: recon + spec SKUs + dedup + per-SKU galleries."""
    detail = readers.category_detail(store, slug)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"category '{slug}' not found")
    return detail


@app.get("/api/stores/{store}/file")
def store_file(store: str, path: str) -> PlainTextResponse:
    """Serve a text doc (recon markdown) from inside a store's category folder."""
    target = readers.resolve_store_file(store, path)
    if target is None:
        raise HTTPException(status_code=404, detail="file not found")
    try:
        return PlainTextResponse(target.read_text())
    except (OSError, UnicodeDecodeError):
        raise HTTPException(status_code=415, detail="file is not text")


@app.get("/api/stores/{store}/image")
def store_image(store: str, path: str) -> FileResponse:
    """Serve a generated/supplier product image from inside a store's category folder."""
    target = readers.resolve_store_file(store, path)
    if target is None or target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(target)


@app.get("/api/dossiers")
def dossiers() -> dict:
    return {"dossiers": readers.list_dossiers()}


@app.get("/api/dossiers/{slug}")
def dossier_detail(slug: str) -> dict:
    detail = readers.dossier_detail(slug)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"dossier '{slug}' not found")
    return detail


@app.get("/api/dossiers/{slug}/file")
def dossier_file(slug: str, path: str) -> PlainTextResponse:
    """Serve a text doc (markdown) from inside a dossier."""
    target = readers.resolve_dossier_file(slug, path)
    if target is None:
        raise HTTPException(status_code=404, detail="file not found")
    try:
        return PlainTextResponse(target.read_text())
    except (OSError, UnicodeDecodeError):
        raise HTTPException(status_code=415, detail="file is not text")


@app.get("/api/dossiers/{slug}/image")
def dossier_image(slug: str, path: str) -> FileResponse:
    """Serve a chart image (png/jpg) from inside a dossier."""
    target = readers.resolve_dossier_file(slug, path)
    if target is None or target.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(target)


@app.get("/api/pain-first")
def pain_first() -> dict:
    """Pain-first niche-discovery pipeline (01b) — GO/HOLD/SKIP roster."""
    return {"niches": readers.list_pain_first()}


@app.get("/api/pain-first/{slug}")
def pain_first_detail(slug: str) -> dict:
    detail = readers.pain_first_detail(slug)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"pain-first niche '{slug}' not found")
    return detail


@app.get("/api/pain-first/{slug}/file")
def pain_first_file(slug: str, path: str) -> PlainTextResponse:
    target = readers.resolve_pain_first_file(slug, path)
    if target is None:
        raise HTTPException(status_code=404, detail="file not found")
    try:
        return PlainTextResponse(target.read_text())
    except (OSError, UnicodeDecodeError):
        raise HTTPException(status_code=415, detail="file is not text")


@app.get("/api/niche-launches")
def niche_launches() -> dict:
    return {"launches": readers.list_niche_launches()}


@app.get("/api/keyword-discovery")
def keyword_discovery() -> dict:
    """General-store keyword discovery funnel (Keyword Research tab)."""
    return readers.keyword_discovery()


def _sku_plan_overrides(
    anchor_pct: float | None,
    coverage_pct: float | None,
    products_per_build: int | None,
    dedup_cap: int | None,
    google: float | None,
    marketplace: float | None,
    amazon: float | None,
    meta: float | None,
) -> dict:
    """Collect the SKU-plan tuning query params into the engine's override shape."""
    o: dict = {}
    if anchor_pct is not None:
        o["anchor_pct"] = anchor_pct
    if coverage_pct is not None:
        o["coverage_pct"] = coverage_pct
    if products_per_build is not None:
        o["products_per_build"] = products_per_build
    if dedup_cap is not None:
        o["dedup_cap"] = dedup_cap
    weights = {
        k: v
        for k, v in (("google", google), ("marketplace", marketplace), ("amazon", amazon), ("meta", meta))
        if v is not None
    }
    if weights:
        o["source_weights"] = weights
    return o


@app.get("/api/sku-plan")
def sku_plan_route(
    anchor_pct: float | None = None,
    coverage_pct: float | None = None,
    products_per_build: int | None = None,
    dedup_cap: int | None = None,
    google: float | None = None,
    marketplace: float | None = None,
    amazon: float | None = None,
    meta: float | None = None,
) -> dict:
    """The SKU Plan tab — drives research. Each gate-cleared head keyword fans into a ranked,
    role-classified, budget-allocated build plan (anchor cluster + Tier-2 builds + combine /
    supporting roles + per-keyword Google-led source quota + dedup rules). Tuning params are
    optional overrides on the operator-default selection/quota knobs."""
    overrides = _sku_plan_overrides(
        anchor_pct, coverage_pct, products_per_build, dedup_cap,
        google, marketplace, amazon, meta,
    )
    return sku_plan.build(overrides)


@app.post("/api/sku-plan/research")
def sku_plan_research(body: dict = Body(...)) -> dict:
    """Fire the research handoff for ONE head keyword's plan: resolve its build terms (the
    anchor + selected Tier-2 standalones) and create a Google-Shopping competitor scan job per
    term via the existing jobs system. This is what makes the SKU plan the DRIVER of research."""
    store = (body.get("store") or "").strip()
    keyword = (body.get("keyword") or "").strip()
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword is required")
    _require_store(store)
    terms = sku_plan.research_targets(store, keyword)
    if not terms:
        raise HTTPException(
            status_code=404,
            detail=f"no SKU-plan build terms for keyword '{keyword}' in store '{store}'",
        )
    # Scan BOTH markets in the project's geo scope (US + UK) per build term — dropshipper presence
    # is situational: a keyword that's brand-dominated in one market may show real dropshippers in
    # the other. found_products reads every market's scan. (Body can pass geos=[...] to override.)
    geos = [str(g).upper() for g in (body.get("geos") or ["US", "GB"]) if str(g).strip()]
    created = [jobs.create("shopping-scan", store, {"keyword": t, "geo": g})
               for t in terms for g in geos]
    runlog.record(store, "sku-plan-research", keyword, "done",
                  detail=f"{len(created)} scan jobs ({len(terms)} terms × {'/'.join(geos)})")
    return {"store": store, "keyword": keyword, "terms": terms, "geos": geos,
            "jobs": created, "count": len(created)}


@app.post("/api/sku-plan/segment/dismiss")
def sku_plan_segment_dismiss(body: dict = Body(...)) -> dict:
    """Remove ONE sub-keyword (the X on a sub-keyword row) from a head keyword's plan. Persisted as
    a per-(store, head, term) hide-list that keyword_discovery filters — so it drops from BOTH the
    SKU plan and the keyword page, and stays gone across re-scans."""
    store = (body.get("store") or "").strip()
    keyword = (body.get("keyword") or "").strip()
    term = (body.get("term") or "").strip()
    if not store or not keyword or not term:
        raise HTTPException(status_code=400, detail="store, keyword, term are required")
    _require_store(store)
    return readers.dismiss_segment(store, keyword, term)


@app.post("/api/sku-plan/found/dismiss")
def sku_plan_found_dismiss(body: dict = Body(...)) -> dict:
    """Remove ONE found product (the X on a product card) for a head keyword. `ident` = its title or
    url. Persisted so found_products_for_head keeps it hidden across re-scans."""
    store = (body.get("store") or "").strip()
    keyword = (body.get("keyword") or "").strip()
    ident = (body.get("ident") or body.get("title") or body.get("url") or "").strip()
    if not store or not keyword or not ident:
        raise HTTPException(status_code=400, detail="store, keyword, and ident (title or url) are required")
    _require_store(store)
    return readers.dismiss_found(store, keyword, ident)


@app.get("/api/sku-plan/found")
def sku_plan_found(store: str, keyword: str) -> dict:
    """The products the discovery lanes have found for ONE head keyword — a drill-in over the
    SKU-plan row so the table stays clean and the found products live behind a click-in. Empty
    until 'Fire research' lanes collect products; each is stamped with the lane it came from."""
    store = (store or "").strip()
    keyword = (keyword or "").strip()
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword is required")
    _require_store(store)
    # The plan fires a scan per build term (anchor + selected Tier-2), each landing as its own
    # candidate — so aggregate found products across ALL of them, not just the head keyword.
    terms = sku_plan.research_targets(store, keyword) or [keyword]
    out = readers.found_products_for_head(store, keyword, terms)
    products = out.get("products") or []
    # Merge the last photo-duplicate (Gemini vision) run, if any — stamps photo_dup /
    # dup_group / photo_dup_drop onto each product so the drill-in shows the groups.
    try:
        out["photo_dedup"] = photo_dedup.apply_flags(store, keyword, products)
    except Exception:
        out["photo_dedup"] = None
    # AUTO-RUN the Gemini-vision duplicate check ON FIND (background) — the first time a keyword's
    # products are viewed, or when new products have been found since the last check. Groups the SAME
    # physical product across lanes/competitors (a belroshop AC == a marketplace AC). Cost-bounded
    # (≤60 images/run) + deduped per keyword. The flags land on the next poll of this endpoint.
    try:
        n_img = sum(1 for p in products if p.get("image"))
        dedup_cap = int(sku_plan.saved_settings().get("dedup_cap") or 3)
        out["photo_dedup_checking"] = photo_dedup.maybe_fire_on_find(store, keyword, terms, dedup_cap, n_img)
    except Exception:  # noqa: BLE001
        out["photo_dedup_checking"] = False
    return out


@app.post("/api/sku-plan/photo-dedup")
def sku_plan_photo_dedup(body: dict = Body(...)) -> dict:
    """Run the Gemini-vision photo-duplicate check over the products found for one head
    keyword: groups photos that show the SAME physical product across different listings and
    flags members beyond the dedup cap. Synchronous (a handful of cheap Flash calls); the
    result persists and is merged into /api/sku-plan/found from then on."""
    store = (body.get("store") or "").strip()
    keyword = (body.get("keyword") or "").strip()
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword is required")
    _require_store(store)
    if not photo_dedup.configured():
        raise HTTPException(
            status_code=400,
            detail="No vision key configured — add the LLM/vision key in Settings → Connections first.",
        )
    terms = sku_plan.research_targets(store, keyword) or [keyword]
    dedup_cap = int(sku_plan.saved_settings().get("dedup_cap") or 3)
    try:
        result = photo_dedup.run(store, keyword, terms, dedup_cap)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    runlog.record(
        store, "photo-dedup", keyword, "done",
        detail=f"{result['images_checked']} images → {result['duplicate_groups']} dup groups",
    )
    return result


@app.post("/api/sku-plan/found-validated")
def sku_plan_found_validated(body: dict = Body(...)) -> dict:
    """Gate #3 → auto-fire the two downstream checks. Once the operator has eyeballed the products
    product-find turned up for a head keyword and confirmed the sources look right, this chains the
    store-check (catalog dedup) + 1688-check (sourcing verify) so they don't have to be fired one
    candidate at a time. Results surface in the existing Sourcing Match tab."""
    store = (body.get("store") or "").strip()
    keyword = (body.get("keyword") or "").strip()
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword is required")
    _require_store(store)
    return {"store": store, "keyword": keyword, **worker.chain_after_found_validated(store, keyword)}


@app.get("/api/settings/sku-plan")
def sku_plan_settings_get() -> dict:
    """The persisted SKU-plan weight split (DEFAULTS ← saved operator override). This is the
    effective base every plan starts from; the Settings editor reads/writes it."""
    return {"settings": sku_plan.saved_settings(), "defaults": sku_plan._fresh_defaults()}


@app.put("/api/settings/sku-plan")
def sku_plan_settings_put(body: dict = Body(...)) -> dict:
    """Persist a new SKU-plan weight split as the default (clamped server-side). Pass
    {"reset": true} to drop the override and restore hard-coded defaults."""
    if body.get("reset"):
        return {"settings": sku_plan.reset_settings(), "defaults": sku_plan._fresh_defaults()}
    saved = sku_plan.save_settings(body)
    runlog.record(None, "sku-plan-settings", "weights", "done", detail="updated weight split")
    return {"settings": saved, "defaults": sku_plan._fresh_defaults()}


# ── Users & Access — per-person, per-app RBAC (ported from NN Master Settings) ──────
# Each person has a name / position / photo / password + a map of which apps they can
# open and their role inside each (Owner / Admin / Representative). The "logged-in"
# user is simulated by an active_user_id (real auth deferred); the shell + FinanceGuard
# read the ACTIVE user's access map via /api/access. `users.py` owns the state.


# ── Auth — name/password sign-in on top of the RBAC above ──────────────────────────
# Bearer token (not a cookie: web + api are separate origins). The app ships with a seeded
# owner login (admin/admin), so there's no setup screen — `status` just says who's signed
# in; owners create/rename everyone else from inside Settings → Access.


@app.get("/api/auth/status")
def auth_status(authorization: str | None = Header(default=None)) -> dict:
    """Web gate reads this first. `authenticated` + `user` = a valid bearer token was presented."""
    user = auth.resolve(auth.token_from_header(authorization))
    return {
        "authenticated": bool(user),
        "user": user,
    }


@app.post("/api/auth/login")
def auth_login(body: dict = Body(...)) -> dict:
    """Name + password → {token, user}. Generic 401 on any mismatch (no user enumeration).
    Accepts `name` (the NN identity); `username` kept as an alias for any in-flight client."""
    result = auth.login((body.get("name") or body.get("username") or "").strip(), body.get("password") or "")
    if not result:
        raise HTTPException(status_code=401, detail="invalid name or password")
    runlog.record(None, "auth", result["user"]["id"], "done", detail="signed in")
    return result


@app.post("/api/auth/sso")
def auth_sso(body: dict = Body(default={})) -> dict:
    """One-login pass-through from the NN shell. NN signs a short-lived token with the shared
    OPERATOR_SSO_SECRET and hands it to the embedded frame; we verify it and mint an owner
    bearer, so the operator never sees a second sign-in. Disabled (404) when no secret is set,
    so a standalone deploy keeps its normal password login."""
    if not auth.sso_enabled():
        raise HTTPException(status_code=404, detail="pass-through sign-in not enabled")
    token = (body.get("token") or "").strip()
    if not auth.verify_sso(token):
        raise HTTPException(status_code=401, detail="invalid or expired sign-in token")
    uid = users.default_owner_id()
    result = uid and auth.mint(uid)
    if not result:
        raise HTTPException(status_code=500, detail="no owner account to sign in")
    runlog.record(None, "auth", uid, "done", detail="signed in via NN pass-through")
    return result


@app.post("/api/auth/logout")
def auth_logout(authorization: str | None = Header(default=None)) -> dict:
    auth.logout(auth.token_from_header(authorization))
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(authorization: str | None = Header(default=None)) -> dict:
    user = auth.resolve(auth.token_from_header(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    return {"user": user}


@app.get("/api/access")
def access_get() -> dict:
    """Effective view-mode for the active user: {role, restricted, user, apps, roles…}.
    `restricted` = app ids the active user can't open. Backward compatible with the shell
    + FinanceGuard, plus the full RBAC surface for Settings → Users & Access."""
    return users.access_view()


@app.get("/api/users")
def users_list() -> dict:
    """Everyone with access + who is currently active + the role/app vocabulary the editor needs."""
    return {
        "people": users.list_people(),
        "active_user_id": (users.active_user() or {}).get("id"),
        "roles": list(users.ROLES),
        "role_labels": users.ROLE_LABELS,
        "app_ids": list(users.APP_IDS),
    }


def _require_owner(authorization: str | None) -> dict:
    """Gate for owner-only endpoints (user admin + stored credentials). The web sends the
    session bearer via authHeaders(); NN SSO / login mints it. Without this these endpoints
    were reachable UNAUTHENTICATED on the public API host — anyone could create an owner or
    read/overwrite stored API credentials."""
    requester = auth.resolve(auth.token_from_header(authorization))
    if not requester:
        raise HTTPException(status_code=401, detail="not signed in")
    if not users.is_owner(requester["id"]):
        raise HTTPException(status_code=403, detail="owners only")
    return requester


@app.post("/api/users")
def users_create(body: dict = Body(...), authorization: str | None = Header(default=None)) -> dict:
    """Invite a person — name (required) + password + per-app access map (role per app)."""
    _require_owner(authorization)
    try:
        person = users.create_person(
            name=body.get("name") or "",
            password=body.get("password") or "",
            access=body.get("access"),
            position=body.get("position") or "",
            photo=body.get("photo"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    runlog.record(None, "users", person["id"], "done", detail=f"invited {person['name']}")
    return person


@app.put("/api/users/{pid}")
def users_update(pid: str, body: dict = Body(...), authorization: str | None = Header(default=None)) -> dict:
    """Edit a person. Password blank = keep existing. `photo` only changes when the key is present."""
    _require_owner(authorization)
    try:
        person = users.update_person(
            pid,
            name=body.get("name"),
            password=body.get("password"),
            access=body.get("access"),
            position=body.get("position"),
            photo=body.get("photo"),
            photo_set="photo" in body,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="person not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    runlog.record(None, "users", pid, "done", detail=f"updated {person['name']}")
    return person


@app.get("/api/users/{pid}/password")
def users_reveal_password(pid: str, authorization: str | None = Header(default=None)) -> dict:
    """OWNER-ONLY: decrypt the recoverable copy of a person's password so an owner can view
    and re-share it. `password: null` = no recoverable copy on file (reset it to make one)."""
    requester = auth.resolve(auth.token_from_header(authorization))
    if not requester:
        raise HTTPException(status_code=401, detail="not signed in")
    if not users.is_owner(requester["id"]):
        raise HTTPException(status_code=403, detail="owners only")
    try:
        pw = users.reveal_password(pid)
    except KeyError:
        raise HTTPException(status_code=404, detail="person not found")
    # Audit the reveal WITHOUT logging the secret itself.
    runlog.record(None, "users", pid, "done", detail=f"password revealed by {requester['name']}")
    return {"password": pw}


@app.delete("/api/users/{pid}")
def users_delete(pid: str, authorization: str | None = Header(default=None)) -> dict:
    """Remove a person's access."""
    _require_owner(authorization)
    if not users.delete_person(pid):
        raise HTTPException(status_code=404, detail="person not found")
    runlog.record(None, "users", pid, "done", detail="removed access")
    return {"ok": True}


@app.post("/api/users/active")
def users_set_active(body: dict = Body(...)) -> dict:
    """Switch the simulated logged-in user (until real auth lands). Drives the whole shell."""
    pid = (body.get("id") or "").strip()
    try:
        users.set_active(pid)
    except KeyError:
        raise HTTPException(status_code=404, detail="person not found")
    runlog.record(None, "users", pid, "done", detail="switched active user")
    return users.access_view()


@app.get("/api/sku-plan/source-supply")
def sku_plan_source_supply_get() -> dict:
    """Per-source supply state (live/dry) that drives adaptive find-budget redistribution."""
    return {"supply": sku_plan.source_supply()}


@app.post("/api/sku-plan/source-supply")
def sku_plan_source_supply_post(body: dict = Body(...)) -> dict:
    """Flag a discovery source live or dry. A dry source's find-budget is redistributed onto the
    still-live sources so the daily listing count is still met; flip back to live the moment new
    products appear and its weight returns."""
    source = (body.get("source") or "").strip()
    state = (body.get("state") or "").strip()
    try:
        supply = sku_plan.set_source_supply(source, state, body.get("found_new"))
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown source '{source}'")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    runlog.record(None, "sku-plan-supply", source, "done", detail=f"{source} → {state}")
    return {"supply": supply}


@app.get("/api/product-research/spy")
def product_research_spy() -> dict:
    """Google-competitor spy roster (Product Research tab)."""
    return readers.spy_roster()


@app.post("/api/product-research/spy/remove")
def product_research_spy_remove(body: dict = Body(...)) -> dict:
    """Remove a tracked competitor from the roster (drop from stores.txt + prune caches)."""
    domain = (body.get("domain") or "").strip()
    if not domain:
        raise HTTPException(status_code=400, detail="domain is required")
    result = readers.spy_remove_store(domain)
    runlog.record("_spy", "spy-remove-store", result.get("domain", domain),
                  "done" if result.get("removed") else "noop")
    return result


@app.get("/api/product-research/spy/candidates")
def product_research_spy_candidates() -> dict:
    """Discovered candidate stores awaiting roster admission (the 'research new stores' review)."""
    return readers.spy_candidates()


@app.post("/api/product-research/spy/admit")
def product_research_spy_admit(body: dict = Body(...)) -> dict:
    """Admit approved candidate domains into the tracked roster, gated on the Google Ads
    Transparency Center: only domains showing live Google Shopping ads are appended to
    stores.txt; the rest come back in `skipped` with a reason. Free, no creds."""
    domains = body.get("domains")
    if not isinstance(domains, list) or not domains:
        raise HTTPException(status_code=400, detail="domains (non-empty list) is required")
    result = readers.spy_admit_stores([str(d) for d in domains])
    runlog.record("_spy", "spy-admit-stores",
                  f"{result['n_admitted']} admitted, {result['n_skipped']} skipped", "done")
    return result


@app.get("/api/product-research/movers")
def product_research_movers() -> dict:
    """Competitor best-seller rank-movers (Product Research tab, spy lane 1)."""
    return readers.bestseller_movers()


@app.get("/api/product-research/new-products")
def product_research_new_products(only_new: bool = True) -> dict:
    """Store duplicate-products scanner: competitor movers minus what we already list."""
    return readers.new_products(only_new=only_new)


@app.get("/api/product-research/bestsellers")
def product_research_bestsellers(store: str | None = None) -> dict:
    """Per-store current best-seller boards (live top-ranked products, not rank-diffs)."""
    return readers.bestseller_snapshots(store=store)


@app.get("/api/product-research/amazon-movers")
def product_research_amazon_movers() -> dict:
    """Amazon Movers & Shakers feed (Product Research tab, spy lane 4)."""
    return readers.amazon_movers()


@app.get("/api/product-research/temu-datasets")
def product_research_temu_datasets() -> dict:
    """The account's BD Temu-matching datasets (id + name) — pick a keyword-discover one for Temu."""
    return readers.temu_datasets()


@app.get("/api/product-research/gs-debug")
def product_research_gs_debug(store: str, keyword: str) -> dict:
    """Google-Shopping lane chain diagnostic (scan exists? PLA products? sellers? lane output?)."""
    return readers.gs_debug(store, keyword)


@app.get("/api/product-research/bestseller-order-debug")
def product_research_bestseller_order_debug(domain: str, geo: str = "US") -> dict:
    """Diagnostic: does the PLAIN `/collections/all?sort_by=best-selling` fetch work from the server IP?
    Returns the depth captured + first 10 handles (empty ⇒ the store bot-blocks the datacenter IP →
    competitor-scan falls back to the merchandising signal)."""
    domain = re.sub(r"^https?://|/.*$|^www\.", "", str(domain or "").strip().lower())
    order = readers._bestseller_order(domain, depth=300, geo=(geo or "US").upper())
    return {"domain": domain, "geo": (geo or "US").upper(), "depth_captured": len(order),
            "first_10": order[:10], "plain_fetch_works": bool(order)}


@app.get("/api/product-research/collections-debug")
def product_research_collections_debug(domain: str, keyword: str = "") -> dict:
    """Diagnostic for the competitor-scan merchandising signal: list the store's collections (which are
    best-seller-named, which hold the keyword products) so a '0 validated' is traceable to real data."""
    domain = re.sub(r"^https?://|/.*$|^www\.", "", str(domain or "").strip().lower())
    cols = readers._store_collections(domain)
    out = {"domain": domain, "n_collections": len(cols),
           "collections": [{"handle": c["handle"], "title": c["title"], "count": c["count"]}
                           for c in cols if c["count"] > 0][:60]}
    if keyword.strip():
        terms = [keyword.strip()]
        try:
            from . import ai_find_assist
            terms += ai_find_assist.expand_terms(keyword.strip())
        except Exception:  # noqa: BLE001
            pass
        kwp = readers._search_keyword_products(domain, keyword.strip())
        kwh = {p["handle"] for p in kwp if p.get("handle")}
        maps = readers._merchandising_maps(domain, terms, kwh)
        out.update({"keyword": keyword.strip(), "expanded_terms": terms,
                    "keyword_matches": len(kwp),
                    "bestseller_collections": maps["bestseller_collections"],
                    "category_collections": maps["category_collections"],
                    "n_bestseller_members": len(maps["bestseller"]),
                    "n_category_members": len(maps["category"])})
    return out


@app.get("/api/product-research/competitor-scan")
def product_research_competitor_scan(domain: str, keyword: str, pages: int = 8,
                                     geo: str = "US") -> dict:
    """VALIDATED-SALES competitor keyword scan (the 'Google competitor catalog keyword' check).
    Starts from the competitor's OWN keyword set (its `/search?q=<kw>` — catches deep-catalog products),
    then annotates each with its best-seller rank (`/collections/all?sort_by=best-selling`, scanned
    deep). `validated` = keyword products that ARE proven best-sellers (per-keyword tier list, ranked);
    the rest are returned as unranked so the operator always sees the store's keyword products. `geo`
    (US/UK) routes the best-seller browser through that Shopify Market. Results cached 7d per store."""
    geo = (geo or "US").upper()
    if geo not in ("US", "GB", "UK"):
        geo = "US"
    result = readers.competitor_keyword_scan(
        domain, keyword, pages=max(1, min(int(pages or 8), 12)), geo=geo)
    if result.get("bestsellers_seen") == 0:
        from . import brightdata
        result["has_browser_cdp"] = bool(brightdata.browser_cdp_endpoint())
    return result


@app.get("/api/product-research/sponsored-plas")
def product_research_sponsored_plas(keyword: str, geo: str = "US", store: str = "") -> dict:
    """Capture the Google 'Sponsored products' PLA carousel (the paying dropship competitors + their
    advertiser domains) for a keyword+country via the Bright Data Scraping Browser (CDP). This is the
    one thing DFS/Web-Unlocker can't get. Runs paid_shopping_scan_bd.py as a subprocess. When `store`
    is given, the captured advertisers are recorded as discovered dropshippers so the competitor-
    catalog finding lane scans their catalogs."""
    import subprocess
    import sys as _sys
    import tempfile
    from pathlib import Path as _Path
    from . import brightdata
    cdp = brightdata.browser_cdp_endpoint()
    if not cdp:
        return {"ok": False, "error": "No BD Scraping-Browser CDP endpoint. Set BRIGHTDATA_CUSTOMER_ID "
                "in Connections + run BD provision to create the browser zone."}
    out = tempfile.mkdtemp()
    script = str(_Path(__file__).resolve().parent / "paid_shopping_scan_bd.py")
    env = {**os.environ, **connections.as_env(), "BRIGHTDATA_BROWSER_CDP": cdp}
    try:
        p = subprocess.run([_sys.executable, script, "--keyword", keyword, "--geo", geo, "--out", out],
                           capture_output=True, text=True, timeout=160, env=env)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        data = json.loads((_Path(out) / "sponsored-plas.json").read_text())
    except (OSError, ValueError):
        data = None
    recorded = None
    if data and store and data.get("advertisers"):
        try:
            recorded = readers.record_sponsored_advertisers(store, data["advertisers"]).get("recorded")
        except Exception:  # noqa: BLE001 — recording is additive; never fail the capture
            recorded = None
    return {"ok": p.returncode == 0, "summary": (p.stdout or "")[-1500:],
            "stderr": (p.stderr or "")[-600:], "capture": data, "recorded_dropshippers": recorded}


@app.get("/api/product-research/temu-probe")
def product_research_temu_probe(keyword: str) -> dict:
    """Evidence probe: does the BD Web Unlocker return extractable Temu product data for a keyword?"""
    return readers.temu_web_probe(keyword)


@app.get("/api/product-research/lane-health")
def product_research_lane_health(keyword: str) -> dict:
    """Per-source finding-lane health for a keyword — which sources (AliExpress / 1688-TMAPI / Temu /
    Amazon) are live vs dry vs erroring, with the TMAPI client-load + Temu dataset-resolution state.
    LIVE fetch; used to diagnose why a Find returns only some sources."""
    return readers.finding_lane_health(keyword)


@app.get("/api/product-research/marketplace-movers")
def product_research_marketplace_movers() -> dict:
    """Marketplace-movers feed (Product Research tab, spy lane 5: AliExpress / Temu / 1688)."""
    return readers.marketplace_movers()


@app.get("/api/product-research/meta-dropship")
def product_research_meta_dropship() -> dict:
    """Meta dropship-winners feed (Product Research tab, spy lane 6)."""
    return readers.meta_dropship()


@app.get("/api/marketplace-browse")
def marketplace_browse() -> dict:
    """Launcher spec for the marketplaces' native best-seller views (Temu / AliExpress / 1688)."""
    return readers.marketplace_browse()


@app.get("/api/amazon-browse")
def amazon_browse() -> dict:
    """Launcher spec for Amazon's native demand boards (Best Sellers + New Releases)."""
    return readers.amazon_browse()


@app.get("/api/find-products")
def find_products(keyword: str, sv: int | None = None, source: str = "keyword") -> dict:
    """Fan a validated keyword/trend out to the product-discovery engines (marketplace
    weighted primary + competitor-catalog scan), with dedup + the pending vision check."""
    kw = (keyword or "").strip()
    if not kw:
        raise HTTPException(status_code=400, detail="keyword is required")
    return readers.find_products_for_keyword(kw, sv=sv, source=source)


@app.get("/api/pricing-rules")
def pricing_rules() -> dict:
    """The listing price rules (undercut competitor + marketplace markup floor + .99 charm)."""
    return pricing.rules()


@app.get("/api/settings/pricing")
def pricing_settings_get() -> dict:
    """The persisted pricing knobs (defaults ← saved override). Settings → Listing reads this."""
    return {"settings": pricing.saved_pricing(), "defaults": pricing._fresh_pricing()}


@app.put("/api/settings/pricing")
def pricing_settings_put(body: dict = Body(...)) -> dict:
    """Persist editable pricing knobs as the new default (clamped server-side). Pass
    {"reset": true} to drop the override back to the hard-coded defaults."""
    if body.get("reset"):
        return {"settings": pricing.reset_pricing(), "defaults": pricing._fresh_pricing()}
    saved = pricing.save_pricing(body)
    runlog.record(None, "pricing-settings", "rules", "done", detail="updated pricing rules")
    return {"settings": saved, "defaults": pricing._fresh_pricing()}


@app.get("/api/price-suggest")
def price_suggest(
    cogs: float | None = None,
    competitor: float | None = None,
    undercut: bool = True,
    compare_at_pct: int | None = None,
) -> dict:
    """Transparent per-variant price suggestion from a COGS basis and/or the lowest
    imported competitor price. `undercut` toggles the competitor-undercut setting;
    `compare_at_pct` (30/40/50) adds the struck-through compare-at for that product."""
    return pricing.price_suggest(
        cogs=cogs,
        competitor_low=competitor,
        undercut_enabled=undercut,
        compare_at_pct=compare_at_pct,
    )


@app.get("/api/compare-at-tier")
def compare_at_tier() -> dict:
    """Draw one compare-at discount tier (30/40/50, weighted toward 30/40) for a product.
    The caller applies the returned tier to every variant of that product uniformly."""
    pct = pricing.pick_discount_tier()
    return {"discount_pct": pct, "tiers": pricing.saved_pricing()["compare_at_tiers"]}


@app.get("/api/meta")
def meta() -> dict:
    """Static reference data the frontend needs to render states consistently."""
    return {
        "sku_states": readers.SKU_STATES,
        "capture_buckets": readers.CAPTURE_BUCKETS,
        "listing_methods": readers.LISTING_METHODS,
    }


@app.get("/api/listing-methods")
def listing_methods() -> dict:
    return {"listing_methods": readers.LISTING_METHODS}


@app.get("/api/research-methods/{surface}")
def research_methods(surface: str) -> dict:
    """Ways to start a research run for a surface (keyword / niche / product)."""
    methods = readers.RESEARCH_METHODS.get(surface)
    if methods is None:
        raise HTTPException(status_code=404, detail=f"research surface '{surface}' not found")
    return {"surface": surface, "research_methods": methods}


@app.get("/api/trends")
def trends() -> dict:
    """Cross-pipeline trend signals (keyword-first + pain-first dossiers)."""
    return readers.trends_overview()


@app.post("/api/trends/dismiss")
def trends_dismiss(body: dict = Body(...)) -> dict:
    """Hide one trend keyword card from the Trend Research surface (operator-curated,
    per-deployment, survives reload). Body: {slug, keyword, geo}. Returns fresh truth."""
    result = readers.dismiss_trend(body.get("slug"), body.get("keyword"), body.get("geo"))
    return {**result, "overview": readers.trends_overview()}


@app.post("/api/trends/restore")
def trends_restore() -> dict:
    """Un-hide every dismissed trend card (clears the hide list)."""
    result = readers.restore_trends()
    return {**result, "overview": readers.trends_overview()}


@app.get("/api/news")
def news_overview() -> dict:
    """News-velocity leading signals (GDELT) — the earliest layer in the breakout chain.
    Fast snapshot read of news-radar/news.json; never triggers a fetch."""
    return readers.news_signals()


@app.post("/api/news/sync")
def news_sync(geo: str = "GB", timespan: str = "28d") -> dict:
    """Refresh the news-radar snapshot now: run news_radar.py over the theme watchlist,
    persist news.json, then return the fresh read view. In-process (GDELT, free, no creds)."""
    runlog.record("_news", "news-sync", geo, "running")
    result = news.sync(geo=geo, timespan=timespan)
    if not result.get("ok"):
        runlog.record("_news", "news-sync", geo, "failed", result.get("error"))
        return {"ok": False, "error": result.get("error"), "snapshot": readers.news_signals()}
    runlog.record("_news", "news-sync", geo, "done",
                  f"{result.get('signals')} signals · {result.get('breakout')} breakout")
    return {"ok": True, "synced_at": result.get("synced_at"), "snapshot": readers.news_signals()}


@app.get("/api/events")
def events_overview(country: str = "ALL", within_days: int = 150) -> dict:
    """World-events calendar — the PREDICTABLE, dated demand signal (holidays / seasonal /
    world events per country). Pure date math over the curated calendar; no fetch, no creds.
    `country` = ALL or an ISO-2 code (that market + GLOBAL). Feeds the build-ahead bucket."""
    return events.upcoming(country=country, within_days=within_days)


@app.get("/api/listing-plan")
def listing_plan(window: str = "week", per_day: int = 50,
                 store: str | None = None, weights: str | None = None,
                 trend_bias: int = 50) -> dict:
    """The daily listing calendar — products pooled from every research category across a window.

    `store` filters the per-store candidate-queue lanes; `weights` is an optional JSON map
    {category_id: percent} that re-weights the source mix (defaults applied per-category);
    `trend_bias` (0–100) is the momentum dial for how hard rising trend keywords are
    prioritized inside the keyword pool.
    """
    parsed: dict | None = None
    if weights:
        try:
            obj = json.loads(weights)
            if isinstance(obj, dict):
                parsed = obj
        except (json.JSONDecodeError, TypeError):
            parsed = None
    return readers.listing_plan(window=window, per_day=per_day, store=store,
                                weights=parsed, trend_bias=trend_bias)


@app.post("/api/listing-plan/execute")
def execute_listing_plan(body: dict = Body(...)) -> dict:
    """Materialize a day of the Daily-Listings calendar into real jobs (hybrid model).

    Body: { store, window?, per_day?, weights?, day? }. Recomputes the plan for the store,
    takes the requested day's scheduled rows (default day 1 = today), and creates one job
    per row via the jobs system — auto specs run in a thread, manual specs record the exact
    operator command. This is the bridge that makes the plan a SCHEDULER, not just a view:
    "run today" turns the day's planned products into queued work, keyed to the store.
    """
    store = body.get("store")
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    _require_store(store)
    window = body.get("window") or "week"
    try:
        per_day = max(1, min(50, int(body.get("per_day") or 2)))
        day = max(1, int(body.get("day") or 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="per_day and day must be integers")
    weights = body.get("weights") if isinstance(body.get("weights"), dict) else None
    try:
        trend_bias = max(0, min(100, int(body.get("trend_bias", 50))))
    except (TypeError, ValueError):
        trend_bias = 50
    plan = readers.listing_plan(window=window, per_day=per_day, store=store,
                                weights=weights, trend_bias=trend_bias)
    items = [s for s in plan["schedule"] if s.get("day") == day]
    result = jobs.execute_plan(store, items)
    plan_date = next((d["date"] for d in plan["days"] if d["day"] == day), None)
    runlog.record(
        store, "plan-execute",
        f"day {day} → {result['counts']['created']} jobs", "done",
    )
    return {"ok": True, "day": day, "date": plan_date, "store": store, **result}


@app.get("/api/sourcing-match")
def sourcing_match(store: str | None = None) -> dict:
    """1688/Alibaba sourcing-match gate — matched.json verdicts + manual ingest commands.
    `store` scopes the rows to the active business so two stores never mix."""
    return readers.sourcing_match(store)


@app.post("/api/sourcing-match/feedback")
def sourcing_match_feedback(body: dict = Body(...)) -> dict:
    """Learning loop — record whether the AI's 1688 find was a good match. Writes to the
    match-feedback.json store and returns the updated accuracy summary."""
    b = body or {}
    try:
        learning = readers.record_match_feedback(
            key=b.get("key", ""),
            verdict=b.get("verdict", ""),
            correct_offer_id=b.get("correct_offer_id"),
            note=b.get("note"),
            ai_verdict=b.get("ai_verdict"),
            ai_offer_id=b.get("ai_offer_id"),
            subject=b.get("subject"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    runlog.record(None, "sourcing-match", "feedback", "done", detail=f"{b.get('key')}={b.get('verdict')}")
    return {"ok": True, "learning": learning}


@app.get("/api/sourcing-match/enabled")
def sourcing_1688_enabled_get() -> dict:
    """Is the 1688 sourcing workflow ON? (Default ON.)"""
    return {"enabled": readers.sourcing_1688_enabled()}


@app.post("/api/sourcing-match/enabled")
def sourcing_1688_enabled_set(body: dict = Body(...)) -> dict:
    """Turn the whole 1688 sourcing workflow ON/OFF. When OFF, listing builds never ground on a
    matched 1688 factory — they build straight from their researched source — so no source-of-truth
    is injected downstream. Reversible at any time."""
    enabled = bool((body or {}).get("enabled", True))
    readers.set_sourcing_1688_enabled(enabled)
    runlog.record(None, "sourcing-match", "toggle", "done",
                  detail=f"1688 workflow {'on' if enabled else 'off'}")
    return {"enabled": enabled}


@app.get("/api/source-of-truth")
def source_of_truth(key: str) -> dict:
    """Resolve a product's 1688-first listing source-of-truth (the same resolution the
    listing-build jobs inject automatically). `key` = the product subject/title, slug, or URL."""
    return readers.resolve_source_of_truth(key)


@app.get("/api/catalog-scan")
def catalog_scan(store: str | None = None) -> dict:
    """Catalog dedup gate (Step 0, BEFORE sourcing) — is the researched product already on the store?
    `store` scopes the rows to the active business."""
    return readers.catalog_scan(store)


# ------------------------------------------------------------ Gameplans (Daily Listings)
# Per-store saved settings bundles for the Daily Listings calendar: source-mix weights,
# plan window, listings-per-day cadence, and the listing method. CRUD-backed by SQLite so
# they persist as real backend state (not browser localStorage) and port to Neon.









# ============================================================ Phase 2 — write/control
# Each route DRIVES a canonical stdlib state-machine script (actions.py), wraps it in
# the durable run-log (runlog.record), and returns the fresh truth so the UI can refetch.

def _require_store(store: str) -> None:
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"store '{store}' not found")


@app.delete("/api/stores/{store}/candidates/{keyword}")
def remove_candidate(store: str, keyword: str) -> dict:
    """Dismiss a keyword from a store's discovery backlog (the X on the Keyword Research table)."""
    _require_store(store)
    result = readers.remove_candidate(store, keyword)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "keyword not found"))
    runlog.record(store, "candidate-remove", keyword, "done",
                  detail=f"removed {result.get('removed', 0)}")
    return result


@app.post("/api/stores/{store}/candidates/{keyword}/promote")
def promote_candidate(store: str, keyword: str) -> dict:
    """Promote a gate-cleared candidate keyword into the listing queue as a category.

    The candidate_queue.py script REFUSES (non-zero exit) unless the candidate's gate
    starts with PASS — we surface that as 422 so the UI shows why the gate blocked it.
    """
    _require_store(store)
    run_id = runlog.record(store, "promote", keyword, "running")
    result = actions.promote_candidate(store, keyword)
    if result["ok"]:
        runlog.record(store, "promote", keyword, "done", output=result["stdout"])
        # Chain: approving the category auto-starts the product-find research (one scan per
        # SKU-plan build term) — the operator doesn't click "research" again. Downstream
        # store-check/1688-check stay in Sourcing Match (they need the found products first).
        chain = worker.chain_after_promote(store, keyword)
        return {"ok": True, "keyword": keyword, "output": result["stdout"],
                "store": readers.store_summary(store), "chain": chain}
    runlog.record(store, "promote", keyword, "failed", detail=result["stderr"])
    raise HTTPException(status_code=422, detail=result["stderr"] or "promote failed")


@app.post("/api/trends/promote")
def promote_trend(body: dict = Body(...)) -> dict:
    """Promote a Trend card's keyword into the pipeline — the SAME meaning as Promote on the
    Keyword Research table: it goes down the project pipeline from here (SKU plan → product-find).
    A trend keyword isn't a gated candidate yet, so actions.promote_keyword ingests it (local
    season gate over the SV the trend run already pulled) then promotes; the promote fires the
    SKU-plan + product-find chain. Body: {store, keyword}."""
    store = str(body.get("store") or "").strip()
    keyword = str(body.get("keyword") or "").strip()
    if not store or not keyword:
        raise HTTPException(status_code=422, detail="store and keyword are required")
    _require_store(store)
    runlog.record(store, "promote", keyword, "running", detail="from Trend Research")
    res = actions.promote_keyword(store, keyword)
    if res.get("ok"):
        runlog.record(store, "promote", keyword, "done", output=res.get("output"))
        chain = worker.chain_after_promote(store, keyword)
        return {"ok": True, "keyword": keyword, "path": res.get("path"),
                "chain": chain, "store": readers.store_summary(store)}
    runlog.record(store, "promote", keyword, "failed", detail=res.get("reason"))
    raise HTTPException(status_code=422, detail=res.get("reason") or "promote failed")


@app.post("/api/stores/{store}/categories/{slug}/skus/{sku}/state")
def set_sku_state(store: str, slug: str, sku: str, body: dict = Body(...)) -> dict:
    """Advance a SKU through the listing state machine (candidate→…→winner|killed)."""
    _require_store(store)
    state = body.get("state")
    note = body.get("note")
    if state not in readers.SKU_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"state must be one of {readers.SKU_STATES}",
        )
    target = f"{slug}/{sku}"
    runlog.record(store, "set-state", target, "running", detail=state)
    result = actions.set_sku_state(store, slug, sku, state, note)
    if result["ok"]:
        runlog.record(store, "set-state", target, "done", detail=state, output=result["stdout"])
        detail = readers.category_detail(store, slug)
        return {"ok": True, "slug": slug, "sku": sku, "state": state, "category": detail}
    runlog.record(store, "set-state", target, "failed", detail=result["stderr"])
    raise HTTPException(status_code=422, detail=result["stderr"] or "set-state failed")


@app.post("/api/stores/{store}/categories")
def add_category(store: str, body: dict = Body(...)) -> dict:
    """Add a category (keyword cluster) to a store's listing queue."""
    _require_store(store)
    slug = body.get("slug")
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    keyword = body.get("keyword")
    sv = body.get("sv")
    capture = body.get("capture")
    if capture is not None and capture not in readers.CAPTURE_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail=f"capture must be one of {readers.CAPTURE_BUCKETS}",
        )
    runlog.record(store, "add-category", slug, "running")
    result = actions.add_category(store, slug, keyword, sv, capture)
    if result["ok"]:
        runlog.record(store, "add-category", slug, "done", output=result["stdout"])
        return {"ok": True, "slug": slug, "store": readers.store_summary(store)}
    runlog.record(store, "add-category", slug, "failed", detail=result["stderr"])
    raise HTTPException(status_code=422, detail=result["stderr"] or "add-category failed")


@app.post("/api/stores/{store}/found-products")
def add_found_products(store: str, body: dict = Body(...)) -> dict:
    """Handoff: picked found-products (AliExpress / Temu / 1688 / Amazon) → the listing queue.

    Body: {keyword: str, products: [{title, price?, url?, image?, source?, sold_count?, ...}]}.
    Adds the keyword as a category (idempotent) and each product as a `candidate` SKU carrying
    its research ref. This is the bridge FROM the Find-Products modal TO the daily listing queue."""
    _require_store(store)
    keyword = (body.get("keyword") or "").strip()
    products = body.get("products") or []
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword is required")
    if not isinstance(products, list) or not products:
        raise HTTPException(status_code=400, detail="products must be a non-empty list")
    runlog.record(store, "found-products", keyword, "running", detail=f"{len(products)} picked")
    result = actions.add_found_products(store, keyword, products)
    if result["ok"]:
        runlog.record(store, "found-products", keyword, "done",
                      detail=f"{result['count']} → {result['slug']}")
        return {
            "ok": True,
            "slug": result["slug"],
            "count": result["count"],
            "added": result["added"],
            "errors": result["errors"],
            "store": readers.store_summary(store),
        }
    runlog.record(store, "found-products", keyword, "failed", detail=result.get("reason"))
    raise HTTPException(status_code=422, detail=result.get("reason") or "add-found-products failed")


@app.delete("/api/stores/{store}/categories/{slug}")
def delete_category(store: str, slug: str) -> dict:
    """Remove a keyword-category (and its SKUs) from the listing queue — undoes a mis-added
    candidate. Queue-entry only; any on-disk build directory is left untouched."""
    _require_store(store)
    runlog.record(store, "category-remove", slug, "running")
    result = actions.remove_category(store, slug)
    if result["ok"]:
        runlog.record(store, "category-remove", slug, "done", output=result["stdout"])
        return {"ok": True, "slug": slug, "store": readers.store_summary(store)}
    runlog.record(store, "category-remove", slug, "failed", detail=result["stderr"])
    raise HTTPException(status_code=404, detail=result["stderr"] or "category not found")


@app.delete("/api/stores/{store}/categories/{slug}/skus/{sku}")
def delete_sku(store: str, slug: str, sku: str) -> dict:
    """Remove a single SKU from a category — undoes a mis-added found product."""
    _require_store(store)
    target = f"{slug}/{sku}"
    runlog.record(store, "sku-remove", target, "running")
    result = actions.remove_sku(store, slug, sku)
    if result["ok"]:
        runlog.record(store, "sku-remove", target, "done", output=result["stdout"])
        return {"ok": True, "slug": slug, "sku": sku, "store": readers.store_summary(store)}
    runlog.record(store, "sku-remove", target, "failed", detail=result["stderr"])
    raise HTTPException(status_code=404, detail=result["stderr"] or "sku not found")


# ---- VisionScan image-QA gate (post-import, pre-go-live) — OPERATOR-REVIEW-FIRST -----------
# Scan PROPOSES per-image verdicts (cheap Gemini via LiteLLM, else operator handoff); the
# operator reviews/changes them on the category page, then APPLIES. Nothing commits until apply.
@app.get("/api/stores/{store}/categories/{slug}/image-qa")
def image_qa_state(store: str, slug: str) -> dict:
    """The gate's read view: the policy + the last APPLIED report (the go-live gate of record)."""
    _require_store(store)
    return {"policy": image_qa.policy(store=store), "report": image_qa.stored_report(store, slug)}


@app.post("/api/stores/{store}/categories/{slug}/image-qa/scan")
def image_qa_scan(store: str, slug: str) -> dict:
    """Run the vision scan → PROPOSE per-image verdicts (commits nothing). Also drops a
    decision into the operator's inbox so the review shows in the 'needs you' list; approving
    that decision applies the AI's proposals as-is, while the category page lets the operator
    change individual verdicts before applying."""
    _require_store(store)
    runlog.record(store, "image-qa-scan", slug, "running")
    report = image_qa.scan_category(store, slug)
    if report is None:
        runlog.record(store, "image-qa-scan", slug, "failed", detail="category not found")
        raise HTTPException(status_code=404, detail=f"category '{slug}' not found")
    s = report["summary"]
    gate = report["go_live"]["verdict"]
    proposals = [img for sku in report["skus"] for img in sku["images"]]
    decision_id = runlog.decision_create(
        store, kind="image-qa",
        title=f"Image-QA review — {slug}",
        summary=f"{s['total']} images: {s.get('PASS', 0)} pass · {s.get('FIX', 0)} fix · "
                f"{s.get('REJECT', 0)} reject · go-live {gate}. Review/change, then apply.",
        payload={"action": "image-qa-apply", "store": store, "slug": slug, "verdicts": proposals},
        source="image-qa-scan",
    )
    runlog.record(store, "image-qa-scan", slug, "done",
                  detail=f"go-live {gate}", output=json.dumps(s))
    report["decision_id"] = decision_id
    return report


@app.post("/api/stores/{store}/categories/{slug}/image-qa/apply")
def image_qa_apply(store: str, slug: str, body: dict = Body(...)) -> dict:
    """Commit the operator-reviewed verdicts: persist the go-live gate report + return the FIX/
    re-source handoff queues. Body: { verdicts: [...], decision_id? }. The operator's per-image
    `operator_override` wins over the AI proposal. Resolves the linked inbox decision if given."""
    _require_store(store)
    verdicts = body.get("verdicts")
    if not isinstance(verdicts, list):
        raise HTTPException(status_code=400, detail="verdicts (list) is required")
    runlog.record(store, "image-qa-apply", slug, "running")
    report = image_qa.apply(store, slug, verdicts)
    decision_id = body.get("decision_id")
    if decision_id is not None:
        try:
            runlog.decision_resolve(int(decision_id), "approved")
        except (TypeError, ValueError):
            pass
    runlog.record(store, "image-qa-apply", slug, "done",
                  detail=f"go-live {report['go_live']['verdict']}",
                  output=json.dumps(report["summary"]))
    return report


@app.get("/api/alerts")
def alerts_digest() -> dict:
    """Proactive performance alerts across EVERY store — products that are bleeding money, wasting
    ad spend, spiking refunds, running thin, or winning (scale-up). Computed live from the always-
    fresh snapshots; ranked by € impact. Powers the sidebar badge + the Alerts view."""
    return alerts.scan_all()


@app.get("/api/optimization/{store}")
def optimization_snapshot(store: str) -> dict:
    """Read the product-optimization snapshot (Shopify revenue per product × 7/14/30-day windows).
    Fast — reads the last-synced general-stores/<store>/optimization.json; never triggers a pull."""
    _require_store(store)
    return readers.optimization_view(store)


@app.get("/api/optimization/{store}/alerts")
def optimization_alerts(store: str) -> dict:
    """This store's 'fix these first' work view — products bleeding money / wasting ad spend / refund
    spikes / thin margins, plus winners to scale, ranked by € impact. Powers the Alerts tab."""
    _require_store(store)
    return alerts.scan_one(store)


@app.post("/api/optimization/{store}/products/status")
def optimization_set_status(store: str, body: dict = Body(...)) -> dict:
    """Bulk product-status change from the Product Performance table (Set to Active / Draft /
    Archived on the selected rows) — a real Shopify write via productUpdate."""
    _require_store(store)
    ids = (body or {}).get("product_ids") or []
    status = (body or {}).get("status") or ""
    prev = (body or {}).get("prev") or {}          # {numeric_pid: old_status} → revertable history
    titles = (body or {}).get("titles") or {}
    result = optimization.set_products_status(store, ids, status, prev=prev, titles=titles)
    runlog.record(store, "optimization", f"set-status-{status.lower()}",
                  "done" if result.get("ok") else "failed",
                  detail=f"{result.get('updated', 0)} updated, {len(result.get('failed') or [])} failed")
    return result


# ── Automation rules (analysis-first: reports what WOULD trigger, never auto-executes) ──────────
@app.get("/api/optimization/{store}/automation/rules")
def automation_list_rules(store: str) -> dict:
    _require_store(store)
    return automation.list_rules(store)


@app.post("/api/optimization/{store}/automation/rules")
def automation_save_rule(store: str, body: dict = Body(...)) -> dict:
    """Create (no id) or update (id present) an automation rule."""
    _require_store(store)
    return automation.save_rule(store, body or {})


@app.delete("/api/optimization/{store}/automation/rules/{rule_id}")
def automation_delete_rule(store: str, rule_id: int) -> dict:
    _require_store(store)
    return automation.delete_rule(store, rule_id)


@app.post("/api/optimization/{store}/automation/enabled")
def automation_set_enabled(store: str, body: dict = Body(...)) -> dict:
    """Global on/off master switch for the store's automation. Body: { enabled: bool }."""
    _require_store(store)
    return automation.set_enabled(store, bool((body or {}).get("enabled")))


@app.get("/api/optimization/{store}/automation/evaluate")
def automation_evaluate(store: str) -> dict:
    """Run enabled rules against the current snapshot → which products would trigger which action."""
    _require_store(store)
    return automation.evaluate(store)


@app.post("/api/optimization/{store}/automation/apply")
def automation_apply(store: str, body: dict = Body(...)) -> dict:
    """Apply ONE automation action for a product (draft/exclude execute; content actions flag).
    Body: { product_id, action, rule_name?, product_title?, market?, vals? }. Everything is logged."""
    _require_store(store)
    b = body or {}
    if not b.get("product_id") or not b.get("action"):
        raise HTTPException(status_code=400, detail="product_id and action are required")
    return automation.apply_action(
        store, b["product_id"], b["action"], rule_name=b.get("rule_name"),
        product_title=b.get("product_title"), market=b.get("market"), vals=b.get("vals"))


@app.get("/api/optimization/{store}/automation/log")
def automation_log(store: str, limit: int = 100) -> dict:
    """The automation activity log — what it drafted / flagged, for which product/market, and why."""
    _require_store(store)
    return automation.get_log(store, limit=limit)


@app.post("/api/optimization/{store}/flag")
def optimization_set_flag(store: str, body: dict = Body(...)) -> dict:
    """Set a product's Exclude (hide) / Note flag — server-persisted so it syncs across devices and
    the Automation engine can read it. Body: { product_id, hidden?, note? } (only the fields sent
    change)."""
    _require_store(store)
    b = body or {}
    pid = b.get("product_id") or ""
    if not pid:
        raise HTTPException(status_code=400, detail="product_id is required")
    return optimization.set_flag(store, pid, hidden=b.get("hidden"), note=b.get("note"),
                                 title=b.get("product_title"))


@app.post("/api/optimization/{store}/tags")
def optimization_set_tags(store: str, body: dict = Body(...)) -> dict:
    """Set a product's app-side tag list (Pythago per-row Add/Remove tag). Body: { product_id,
    tags: [...], product_title? }. Merged with the product's Shopify tags in the view."""
    _require_store(store)
    b = body or {}
    if not b.get("product_id"):
        raise HTTPException(status_code=400, detail="product_id is required")
    return optimization.set_tags(store, b["product_id"], b.get("tags") or [],
                                 title=b.get("product_title"))


@app.get("/api/optimization/{store}/history")
def optimization_history(store: str, product_id: str, limit: int = 40) -> dict:
    """Mutation history for one product (status / exclude / note / tag changes), newest first."""
    _require_store(store)
    return {"ok": True, "entries": optimization.get_history(store, product_id, limit=limit)}


@app.post("/api/optimization/{store}/history/revert")
def optimization_history_revert(store: str, body: dict = Body(...)) -> dict:
    """Undo one mutation-history entry (re-applies its previous value). Body: { id }."""
    _require_store(store)
    eid = (body or {}).get("id")
    if eid is None:
        raise HTTPException(status_code=400, detail="id is required")
    return optimization.revert_history(store, int(eid))


@app.get("/api/optimization/{store}/saved-filters")
def optimization_saved_filters(store: str) -> dict:
    """Server-side saved filter presets for the Product Performance page."""
    _require_store(store)
    return {"ok": True, "filters": optimization.list_saved_filters(store)}


@app.post("/api/optimization/{store}/saved-filters")
def optimization_save_filter(store: str, body: dict = Body(...)) -> dict:
    """Save (upsert) a filter preset. Body: { name, state }."""
    _require_store(store)
    b = body or {}
    return optimization.save_filter(store, b.get("name") or "", b.get("state") or {})


@app.delete("/api/optimization/{store}/saved-filters/{name}")
def optimization_delete_filter(store: str, name: str) -> dict:
    _require_store(store)
    return optimization.delete_filter(store, name)


_opt_sync_inflight: set[str] = set()
_opt_sync_lock = threading.Lock()


@app.post("/api/optimization/{store}/sync")
def optimization_sync(store: str) -> dict:
    """Rebuild the optimization snapshot in a BACKGROUND thread. The full-window Shopify pull +
    aggregation exceeds the edge timeout for large catalogs (decorsdeluxe/lumoira 502'd when run
    synchronously, so the snapshot never got written). This returns immediately with the current
    snapshot + running=true; the client polls GET /api/optimization/{store} until synced_at bumps.
    A per-store in-flight guard collapses concurrent triggers so one store never syncs twice at once."""
    _require_store(store)
    with _opt_sync_lock:
        already = store in _opt_sync_inflight
        if not already:
            _opt_sync_inflight.add(store)

    def _job() -> None:
        try:
            runlog.record(store, "optimization-sync", store, "running")
            result = optimization.sync(store)
            if not result.get("ok"):
                runlog.record(store, "optimization-sync", store, "failed",
                              detail=result.get("error") or "sync failed")
            else:
                runlog.record(store, "optimization-sync", store, "done",
                              detail=f"{result.get('products', 0)} products",
                              output=json.dumps({"synced_at": result.get("synced_at")}))
        except Exception as e:  # noqa: BLE001 — the outcome must land in the log either way
            runlog.record(store, "optimization-sync", store, "failed", detail=f"{type(e).__name__}: {e}")
        finally:
            with _opt_sync_lock:
                _opt_sync_inflight.discard(store)

    if not already:
        threading.Thread(target=_job, daemon=True, name=f"opt-sync-{store}").start()
    return {"ok": True, "started": not already, "running": True,
            "snapshot": readers.optimization_view(store)}


# ============================================================ Multimarket (setup + localization)
# One app driving many markets off ONE Shopify store: market/language/policy/shipping SETUP plus
# native per-market product LOCALIZATION (keyword-first, not literal, measurements converted). All
# handlers are best-effort — the module returns {ok, error?} rather than raising — so a missing
# Shopify scope or one bad market surfaces cleanly instead of 500-ing the whole call.




































@app.get("/api/runs")
def runs(limit: int = 50) -> dict:
    """Durable run-log — most recent control-layer actions across all stores."""
    return {"runs": runlog.recent(limit=limit), "counts": runlog.counts()}


@app.get("/api/stores/{store}/runs")
def store_runs(store: str, limit: int = 50) -> dict:
    _require_store(store)
    return {"runs": runlog.recent(limit=limit, store=store)}


# ============================================================ Phase 3 — execution jobs
# Heavy pipeline steps as durable jobs (hybrid: auto runs locally, manual → needs-operator).

@app.get("/api/job-specs")
def job_specs() -> dict:
    return {"job_specs": jobs.specs()}


@app.post("/api/jobs")
def create_job(body: dict = Body(...)) -> dict:
    """Create a job from a registered spec. Auto → background thread; manual → command."""
    spec = body.get("spec")
    store = body.get("store")
    args = body.get("args") or {}
    if not spec:
        raise HTTPException(status_code=400, detail="spec is required")
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    _require_store(store)
    try:
        return jobs.create(spec, store, args)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"job spec '{spec}' not found")


@app.post("/api/admin/self-heal-orphans")
def admin_self_heal_orphans() -> dict:
    """Manually run + diagnose the restart self-heal (which also runs at every startup). Splits the
    selector stage from the requeue stage and returns any traceback, so a 'no effect' can be told
    apart from a silent throw."""
    import traceback
    out: dict = {}
    try:
        found = runlog.jobs_orphaned_restart()
        out["found"] = len(found)
        out["found_specs"] = sorted({str(j.get("spec")) for j in found})
        out["sample"] = found[0] if found else None
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "select", "error": str(exc), "trace": traceback.format_exc()[-1200:]}
    try:
        out["result"] = jobs.requeue_orphaned_auto()
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "stage": "requeue", "error": str(exc),
                    "trace": traceback.format_exc()[-1200:]})
    return out


@app.get("/api/admin/trendtrack-probe")
def admin_trendtrack_probe(keyword: str = Query("portable air conditioner")) -> dict:
    """Inspect the raw TrendTrack /v1/ads shape for a keyword + what competitors_for_keyword
    extracts — so the Find-Products wiring can be verified against real data before it goes live."""
    r = trendtrack.search_ads(keyword, limit=40, sort_by="relevance")
    ads = trendtrack._ads_list(r.get("data"))
    us = [a for a in ads if trendtrack._is_us_targeted(a)]
    comp = trendtrack.competitors_for_keyword(keyword, limit=12)
    return {
        "ok": r.get("ok"), "status": r.get("status"), "error": str(r.get("error"))[:200],
        "credits_remaining": r.get("credits_remaining"),
        "ad_count": len(ads), "us_ad_count": len(us),
        "us_competitors_extracted": len(comp.get("products") or []),
        "sample": (comp.get("products") or [])[:8],
    }


@app.post("/api/admin/ai-gate-eval")
def admin_ai_gate_eval() -> dict:
    """Run the AI product-domain gate over its labeled test set and score it (accuracy +
    false_rejects + missed_junk). This is the 'prove it works before trusting it' handle — hit it
    on live (where the LLM key is configured) to confirm the classifier keeps real products and
    catches junk, BEFORE it's wired into the seed/ingest gates."""
    from . import ai_product_gate
    return ai_product_gate.evaluate(use_cache=False)


@app.post("/api/admin/sweep-nonproduct-keywords")
def admin_sweep_nonproduct_keywords(refresh: bool = Query(False)) -> dict:
    """Clean the app of non-dropshippable keywords: AI-classify every keyword currently surfaced
    (trends + candidates + sub-keywords) and hide the confident non-products (banks/brands/motels/
    services/perishables/oversized the deterministic blocklist can't know — capitalone, super 8,
    8sleep, blacks, dog food, swimming pool…). Also runs at startup; this is the on-demand handle.
    refresh=true re-classifies even cached terms (use after a prompt change). Persisted → fast reads."""
    return readers.sweep_nonproduct_keywords(refresh=refresh)


@app.post("/api/admin/purge-junk-dossiers")
def admin_purge_junk_dossiers(dry_run: bool = Query(False)) -> dict:
    """Quarantine dossiers whose seed is non-product junk ('jet-blue', 'toyota', 'portugal'...) —
    the news-driven dossiers the trend feed auto-created. Also runs at every startup; this is the
    on-demand + preview handle. dry_run=true returns the hit list WITHOUT moving anything; run it
    first to confirm the blast radius, then dry_run=false to quarantine (reversible move to
    dossiers/_junk-quarantine/)."""
    return readers.purge_junk_dossiers(dry_run=dry_run)


@app.post("/api/jobs/bulk")
def create_jobs_bulk(body: dict = Body(...)) -> dict:
    """Bulk import — create one job per pasted link under the same spec + store.

    Body: { spec, store, links: [str, ...], arg_key? }. Each link becomes its own job
    (mirrors POST /api/jobs); returns the created jobs + the count.
    """
    spec = body.get("spec")
    store = body.get("store")
    links = body.get("links")
    arg_key = body.get("arg_key") or "url"
    if not spec:
        raise HTTPException(status_code=400, detail="spec is required")
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    if not isinstance(links, list) or not any((str(x).strip()) for x in links):
        raise HTTPException(status_code=400, detail="links (non-empty list) is required")
    _require_store(store)
    try:
        created = jobs.create_bulk(spec, store, [str(x) for x in links], str(arg_key))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"job spec '{spec}' not found")
    return {"count": len(created), "jobs": created}


@app.get("/api/jobs")
def list_jobs(limit: int = 50, store: str | None = None) -> dict:
    return {"jobs": runlog.jobs_recent(limit=limit, store=store)}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int) -> dict:
    job = runlog.job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


@app.get("/api/preflight/{dep}")
def preflight_dep(dep: str) -> dict:
    """Reachability of a local dependency an auto-local job needs (e.g. 'shopping_scan',
    'dataforseo'). Tells the UI whether the job runs automatically or hands off to the operator."""
    if dep not in jobs._LOCAL_DEP_HEALTH:
        raise HTTPException(status_code=404, detail=f"unknown local dependency {dep}")
    return jobs.preflight(dep)


# ============================================================ Phase B — worker / hybrid control
# The operator stays the decision node. The worker PREPARES work (suggest → decision inbox)
# and runs only what's configured auto; nothing gated advances without an approval row.

@app.get("/api/worker")
def worker_status() -> dict:
    """Live worker state for the always-visible heartbeat strip + the Decisions cockpit:
    what's running, queued, waiting on the operator, and what's scheduled next."""
    return worker.status()


@app.post("/api/worker/enable")
def worker_enable(body: dict = Body(...)) -> dict:
    """Arm/disarm automatic ticking (OFF by default — hybrid-first)."""
    return {"ok": True, "worker": worker.set_enabled(bool(body.get("enabled")))}


@app.post("/api/worker/tick")
def worker_tick(body: dict = Body(...)) -> dict:
    """Run one prepare-and-wait pass now (the operator's "Run scheduled steps").

    Body: { store, steps? }. Auto steps run; suggest steps drop decisions into the inbox.
    """
    store = body.get("store")
    if not store:
        raise HTTPException(status_code=400, detail="store is required")
    _require_store(store)
    steps = body.get("steps") if isinstance(body.get("steps"), list) else None
    # force (or an explicit `steps` list) bypasses the cadence wait — the operator's
    # "run this now". A bare scheduled pass (no steps, no force) respects each step's cadence.
    force = bool(body.get("force"))
    return worker.tick(store, steps=steps, force=force)


@app.get("/api/autonomy")
def autonomy() -> dict:
    """The what/how/when control panel — every schedulable step with its current autonomy."""
    return {"steps": worker.config()}


@app.put("/api/autonomy/{step}")
def set_autonomy(step: str, body: dict = Body(...)) -> dict:
    """Set a step's autonomy mode (manual|suggest|auto) and/or cadence (on-demand|daily|weekly)."""
    try:
        row = worker.set_step(step, mode=body.get("mode"), cadence=body.get("cadence"))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"step '{step}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "step": row}


@app.get("/api/decisions")
def decisions(status: str = "pending", store: str | None = None, limit: int = 100) -> dict:
    """The operator's "needs you" inbox. Default = pending; pass status= for history.

    Pending decisions are ENRICHED with `related_learnings` — past rejections of the same
    kind/signal — so the operator sees "you turned this kind down before because X" right on
    the card, and can reject-with-the-same-reason in one click. This is the smart-learning
    loop made visible at the point of decision."""
    status_filter = None if status in ("all", "") else status
    rows = runlog.decisions_list(status=status_filter, store=store, limit=limit)
    if status_filter == "pending":
        for d in rows:
            # Enrichment is best-effort: a failure here must degrade to "no related
            # learnings", never 500 the operator's entire inbox.
            try:
                payload = d.get("payload") or {}
                signal = None
                if isinstance(payload, dict):
                    for key in ("spec", "keyword", "slug", "term", "domain"):
                        if payload.get(key):
                            signal = str(payload[key])
                            break
                d["related_learnings"] = runlog.learnings_for(
                    d["kind"], store=d.get("store"), signal=signal, limit=4
                )
            except Exception:
                d["related_learnings"] = []
    return {"decisions": rows}


@app.post("/api/decisions/{decision_id}/approve")
def approve_decision(decision_id: int) -> dict:
    """Approve a pending decision — it goes into the proper pipeline (creates the job / runs
    the listing plan); the response carries the job/result it spawned."""
    try:
        return worker.decide(decision_id, approve=True)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")


@app.post("/api/decisions/{decision_id}/reject")
def reject_decision(decision_id: int, body: dict = Body(default={})) -> dict:
    """Reject a pending decision — nothing runs. Body (optional): {reason, action}. A `reason`
    is captured as a durable LEARNING (why it was turned down + what to change) so future
    proposals of the same kind/signal surface it."""
    reason = (body or {}).get("reason")
    action = (body or {}).get("action")
    try:
        return worker.decide(decision_id, approve=False, reason=reason, action=action)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")


@app.get("/api/learnings")
def learnings(limit: int = 50, kind: str | None = None, store: str | None = None) -> dict:
    """The smart-learning memory — every rejection reason the operator has recorded, newest
    first. This is what makes the pipeline learn: the same surface that proposes work reads
    these to stop re-suggesting what was turned down."""
    return {
        "learnings": runlog.learnings_recent(limit=limit, kind=kind, store=store),
        "count": runlog.learnings_count(),
    }


# ============================================================ Assistant (read-only copilot)
# A cheap-model copilot that reads pipeline state and PROPOSES actions. It NEVER executes a
# state-changing action: it returns proposed_actions that the UI renders as confirm-buttons,
# each routing through the SAME existing, logged, confirmed control endpoints a manual button
# uses (POST /api/jobs, PUT /api/autonomy/<step>, decision approve/reject, worker tick/enable).

@app.post("/api/assistant/chat")
def assistant_chat(body: dict = Body(...)) -> dict:
    """Run one assistant turn. Body: {messages: [{role, content}, ...], store?: str}.
    Returns {reply: str, proposed_actions: [{type, label, ...}]}. The backend does NOT
    run any proposed action — the UI confirms + routes each through an existing endpoint."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages (list) is required")
    store = body.get("store")
    if store is not None:
        _require_store(store)
    return assistant.chat(messages, store=store)


@app.post("/api/assistant/feedback")
def assistant_feedback(body: dict = Body(...)) -> dict:
    """Record the operator's verdict on an output the assistant produced (after it ran a job).
    Body: {verdict: "approve"|"reject"|"refine", note?: str, spec?: str, store?: str, job_id?: int}.
    Persisted as a durable LEARNING the assistant reads back every turn — the context+learning
    loop. 400 on a bad verdict or unknown store."""
    store = body.get("store")
    if store is not None:
        _require_store(store)
    job_id = body.get("job_id")
    try:
        return assistant.record_feedback(
            verdict=str(body.get("verdict") or ""),
            note=body.get("note"),
            spec=body.get("spec"),
            store=store,
            job_id=int(job_id) if job_id is not None else None,
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================ Settings (backend view)
@app.get("/api/costs")
def get_costs(
    store: str | None = None,
    listings_per_month: int = 30,
    stores: int = 1,
    months_retained: int = 12,
) -> dict:
    """Operation-cost overview — editable unit costs, fixed monthly infra, per-spec +
    per-listing estimates, real spend-to-date (job counts × per-run cost) + scale-aware
    monthly projection (stores × listings, data-storage line, scenario curve)."""
    if store:
        _require_store(store)
    try:
        lpm = max(0, min(50000, int(listings_per_month)))
        n_stores = max(1, min(100, int(stores)))
        retained = max(1, min(60, int(months_retained)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="listings_per_month/stores/months_retained must be integers")
    return costs.overview(
        store=store, listings_per_month=lpm, stores=n_stores, months_retained=retained
    )


@app.put("/api/costs/assumptions")
def put_cost_assumptions(body: dict = Body(...)) -> dict:
    """Persist operator overrides. Body: {unit_costs?: {key: value}, agent_model?: <id>}.
    Switching `agent_model` re-prices every step's LLM line (use a cheap tier for this
    repetitive, clearly-stepped workflow)."""
    values = body.get("unit_costs") if isinstance(body.get("unit_costs"), dict) else None
    model_id = body.get("agent_model")
    updated = costs.save_overrides(values, model_id)
    return {"ok": True, "unit_costs": updated, "agent_model": costs.agent_model()}


def _connections_view() -> dict:
    """public_view + per-store operational state stamped on: catalog mode (general | fashion | both)
    and the last automatic data-sync verdict — so the Connections UI shows the path a store
    runs AND whether its Shopify sync is healthy, without a second fetch."""
    view = connections.public_view(readers.list_stores())
    for s in view.get("shopify", []):
        key = s.get("store", "")
        s["mode"] = readers.store_mode(key)
        s["last_sync"] = store_sync.status(key)
    return view


@app.get("/api/connections")
def connections_get(authorization: str | None = Header(default=None)) -> dict:
    """Masked view of every credential the app holds — per-store Shopify + global API keys.
    Secrets are NEVER returned raw; each field reports `configured` + a `••••last4` preview."""
    _require_owner(authorization)
    return _connections_view()


@app.put("/api/connections")
def connections_put(body: dict = Body(...), authorization: str | None = Header(default=None)) -> dict:
    """Set credentials. Only the fields supplied are written (others keep their value);
    sending an empty string for a field clears it. Returns the fresh masked view."""
    _require_owner(authorization)
    connections.update(body)  # fires the always-auto-sync for any store whose creds changed
    runlog.record(None, "connections", "update", "done", detail="updated credentials")
    return _connections_view()


@app.get("/api/connections/health")
def connections_health() -> dict:
    """Live health of the data-connector APIs (DataForSEO balance, Bright Data, TMAPI, TrendTrack) so
    an out-of-credit / auth-failed / no-creds failure is VISIBLE + fixable instead of a silent 402."""
    from . import connector_health
    return connector_health.health()




@app.post("/api/stores/sync-all")
def stores_sync_all() -> dict:
    """Sync EVERY registered store NOW — profile · finance · orders · issues · Product Performance
    snapshot (with ad-spend reconciliation) — in the background, one store at a time. This is the
    global 'sync everything, all stores' control; it collapses if a pass is already running."""
    started = store_sync.run_all_async()
    return {"ok": True, "started": started, "running": True, "stores": readers.list_stores()}


@app.get("/api/stores/sync-all/status")
def stores_sync_all_status() -> dict:
    """Live status for the global sync panel: whether a full pass is running + each store's last
    store-data-sync verdict (done/failed/skipped + when)."""
    return {
        "ok": True,
        "running": store_sync.is_running_all(),
        "stores": [
            {"store": s, **(store_sync.status(s) or {"status": "never", "at": None, "detail": None})}
            for s in readers.list_stores()
        ],
    }


@app.post("/api/stores/{store}/sync")
def stores_sync(store: str) -> dict:
    """Run the full store data sync NOW (profile + finance + product orders + issues + Product
    Performance snapshot). Same engine the 24/7 worker runs daily; this is 'refresh now'."""
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"unknown store: {store}")
    return store_sync.run(store)


# ── Shopify webhooks — real-time order ingestion (push, not polling) ─────────────────────────────
@app.post("/api/webhooks/shopify")
async def shopify_webhook_inbound(request: Request):
    """PUBLIC endpoint Shopify POSTs order/refund events to — no operator auth (Shopify can't send
    our Bearer token); each event is verified by its HMAC signature under the store's Client Secret.
    Cheap: it just verifies + schedules a debounced per-store refresh, then returns fast."""
    raw = await request.body()
    topic = request.headers.get("x-shopify-topic", "")
    shop = request.headers.get("x-shopify-shop-domain", "")
    sig = request.headers.get("x-shopify-hmac-sha256", "")
    try:
        res = shopify_webhooks.handle(topic, shop, raw, sig)
    except Exception as e:  # noqa: BLE001 — never 500 back to Shopify
        res = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    if not res.get("ok"):
        # Bad HMAC / unknown shop → 401 so Shopify (and we) know it was rejected.
        return JSONResponse(res, status_code=401)
    return res


@app.post("/api/stores/webhooks/register-all")
def stores_webhooks_register_all() -> dict:
    """Register the order/refund webhooks on EVERY connected store (idempotent)."""
    return shopify_webhooks.register_all()


@app.get("/api/stores/{store}/webhooks")
def stores_webhooks_list(store: str) -> dict:
    """Current Shopify webhook subscriptions for a store (topic + callback + whether it's ours)."""
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"unknown store: {store}")
    return shopify_webhooks.list_subs(store)


@app.post("/api/stores/{store}/webhooks/register")
def stores_webhooks_register(store: str) -> dict:
    """Register (idempotently) the order/refund webhooks for one store."""
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"unknown store: {store}")
    return shopify_webhooks.register(store)


@app.delete("/api/stores/{store}/webhooks")
def stores_webhooks_unregister(store: str) -> dict:
    """Delete the webhook subscriptions that point at our callback for one store."""
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"unknown store: {store}")
    return shopify_webhooks.unregister(store)


# ── Bright Data auto-setup — one token provisions every Bright Data resource ──────
# The operator pastes ONE Bright Data token; this creates/adopts the SERP + CN zones inside
# their account, reads the passwords, and writes the resolved creds back into Connections so
# they never hand-build a zone. Creating a zone is billable, so it runs only on explicit click.
@app.get("/api/integrations/brightdata")
def brightdata_status() -> dict:
    return brightdata.status()


@app.post("/api/integrations/brightdata/provision")
def brightdata_provision() -> dict:
    try:
        out = brightdata.provision()
    except Exception as e:  # noqa: BLE001 — surface the failure to the operator, don't 500
        runlog.record(None, "brightdata", "provision", "failed", detail=str(e))
        return {"ok": False, "error": str(e)}
    runlog.record(None, "brightdata", "provision", "done" if out.get("ok") else "failed",
                  detail=", ".join(out.get("created") or []) or "no new zones")
    return out


# Deterministic MCP client: our backend drives Markifact's MCP over JSON-RPC with no LLM
# in the loop (no token cost). The operator connects once via OAuth 2.1 + PKCE; thereafter
# tools/list + tools/call run from code. Writes stay gated (Markifact human-in-the-loop +
# our ads-execution-deferred rule); these routes provide the plumbing + safe reads.















# ── Google (Ads + Merchant Center) — OAuth 2.0 connection ────────────────────────
# The proper way to connect Google: a Merchant Center / Ads Customer ID is just an account
# address, not a connection. The operator authorizes once via OAuth (scopes adwords + content);
# the refresh token is bridged into GOOGLE_ADS_REFRESH_TOKEN so every downstream Ads/GMC job is
# authorized. The per-store ids then only pick WHICH account to act on (see /accounts).

def _google_redirect_uri(request: Request) -> str:
    """OAuth callback URL, derived from the live request so it works on localhost AND on the
    Railway public host. OPERATOR_API_BASE overrides if set. NOTE: this exact URI must be
    registered as an Authorized redirect URI on the Google Cloud OAuth client."""
    base = os.environ.get("OPERATOR_API_BASE")
    if base:
        return base.rstrip("/") + "/api/integrations/google/oauth/callback"
    return str(request.base_url).rstrip("/") + "/api/integrations/google/oauth/callback"


@app.get("/api/integrations/google")
def google_status() -> dict:
    """Live, render-safe Google connection status (no tokens)."""
    return google_oauth.public_status()


@app.post("/api/integrations/google/connect")
def google_connect(request: Request) -> dict:
    """Begin the OAuth handshake — requires the Google Ads OAuth client id/secret to be set,
    returns the Google consent URL for the operator to open in their browser."""
    try:
        return google_oauth.start_oauth(_google_redirect_uri(request))
    except Exception as e:
        runlog.record(None, "google", "connect", "failed", detail=str(e))
        raise HTTPException(status_code=400, detail=f"Google connect failed: {e}")


@app.get("/api/integrations/google/oauth/callback")
def google_callback(code: str | None = None, state: str | None = None,
                    error: str | None = None) -> HTMLResponse:
    """OAuth redirect target. Exchanges the code for tokens, then renders a tiny self-closing
    page (this opens in a popup/new tab from the Settings UI)."""
    if error:
        return HTMLResponse(f"<p>Google authorization failed: {error}. You can close this tab.</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<p>Missing code/state. You can close this tab.</p>", status_code=400)
    try:
        google_oauth.finish_oauth(code, state)
        runlog.record(None, "google", "connect", "done", detail="oauth complete")
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Google connected</title>"
            "<body style='font:14px system-ui;padding:2rem'>"
            "<p><b>Google connected.</b> You can close this tab and return to the app.</p>"
            "<script>try{window.opener&&window.opener.postMessage('google-connected','*')}catch(e){}"
            "setTimeout(()=>window.close(),800)</script></body>"
        )
    except Exception as e:
        runlog.record(None, "google", "connect", "failed", detail=str(e))
        return HTMLResponse(f"<p>Token exchange failed: {e}. You can close this tab.</p>", status_code=400)


@app.post("/api/integrations/google/disconnect")
def google_disconnect() -> dict:
    """Revoke + clear all stored Google OAuth credentials (and the bridged refresh token)."""
    out = google_oauth.disconnect()
    runlog.record(None, "google", "disconnect", "done")
    return out


@app.get("/api/integrations/google/accounts")
def google_accounts() -> dict:
    """Enumerate the Ads customers + Merchant Center accounts this login can reach — powers the
    per-store account picker. Best-effort: each side reports its own error without failing both."""
    try:
        return google_oauth.list_accounts()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Merchant Center accounts (multi-account GMC connection registry).
#
# Distinct from the app-wide Google OAuth above: a real multi-brand operator owns several
# Merchant Center accounts under DIFFERENT Google logins, so each is connected via its OWN
# OAuth client (client id/secret) + refresh token, keyed by its numeric Merchant ID. This is
# the native port of NN Operations' Settings · Merchant Center tab (gmc.js account model).
# ─────────────────────────────────────────────────────────────────────────────

def _gmc_account_redirect_uri(request: Request) -> str:
    """OAuth callback for a Merchant Center account connect. Must be added, EXACTLY, as an
    Authorized redirect URI on that account's Google Cloud OAuth client. OPERATOR_API_BASE
    overrides if set (so it's stable behind the edge)."""
    base = os.environ.get("OPERATOR_API_BASE")
    if base:
        return base.rstrip("/") + "/api/gmc/oauth/callback"
    return str(request.base_url).rstrip("/") + "/api/gmc/oauth/callback"














# ─────────────────────────────────────────────────────────────────────────────
# Trustpilot reputation (OpenWeb Ninja) — the Store-Management → Trustpilot tab.
# ─────────────────────────────────────────────────────────────────────────────







@app.post("/api/stores/{store}/shopify/profile/pull")
def shopify_profile_pull(store: str) -> dict:
    """Pull the store-level facts (native language, currency, timezone, markets, catalog size) from
    Shopify using this store's saved admin token, and SAVE them into the connections single-source so
    they flow downstream (VisionScan language, pricing currency, scheduling timezone). Best-effort:
    returns {ok:false,error} if the token is missing or the read is unscoped — never 500s on that."""
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"unknown store: {store}")
    # Force a fresh mint: "Pull from Shopify" is the manual re-check, and it's exactly when the
    # store's app scopes may have just changed (a reinstall grants new scopes, but the ~24h-cached
    # token still carries the old ones). Clearing the cache makes the pull reflect reality now.
    connections.clear_token_cache(store)
    result = shopify.pull_profile(store)
    runlog.record(
        store, "shopify-profile", "pull", "done" if result.get("ok") else "failed",
        detail=result.get("error") or (result.get("profile") or {}).get("language") or "pulled",
    )
    return result


@app.get("/api/shopify/scopes")
def shopify_scopes() -> dict:
    """The Admin API scopes every part of the app needs — the single source of truth the
    Connections copy-box renders (so the pasted set can never drift from what the code needs)."""
    return {"scopes": connections.REQUIRED_SHOPIFY_SCOPES}


@app.post("/api/stores/{store}/verify")
def stores_verify(store: str) -> dict:
    """Verify a store's Shopify connection end-to-end: can we authenticate, and does the granted
    token carry EVERY scope the app needs? Read-only (mutates nothing). Returns
    {ok, connected, granted, missing, extra, auth_error}."""
    if store not in readers.list_stores():
        raise HTTPException(status_code=404, detail=f"unknown store: {store}")
    result = connections.verify_store(store)
    runlog.record(
        store, "shopify-verify", "check", "done" if result.get("ok") else "failed",
        detail=result.get("auth_error")
        or (f"missing {len(result['missing'])} scope(s)" if result.get("missing") else "all scopes granted"),
    )
    return result


@app.get("/api/settings")
def settings() -> dict:
    """Read-only backend/system view — paths, registered stores, step modes, counts."""
    return {
        "api_version": app.version,
        # Which business this deployment serves + where its data lives. tenant is a display
        # label (TENANT env); the real per-business isolation is data_root (GOOGLE_STORES_DATA
        # volume) + db_backend (its own Neon DATABASE_URL). repo_root (code) is shared across all.
        "tenant": config.tenant(),
        "repo_root": str(config.repo_root()),
        "data_root": str(config.data_root()),
        "data_root_isolated": str(config.data_root()) != str(config.repo_root()),
        "general_stores_dir": str(config.general_stores_dir()),
        "db_path": runlog.db_path(),
        "db_backend": db.backend(),
        # Per-business deployment wiring CHECKS, surfaced so the operator can verify (and be
        # guided to fix) each business's setup from inside the app. These two are bootstrap
        # infra (env vars), NOT app-entered credentials: DATABASE_URL is the app's own DB
        # connection (can't be stored in the DB it opens), and GOOGLE_STORES_DATA must match a
        # real mounted volume — so the app's job is to confirm them, not collect them.
        "deploy_checks": [
            {
                "id": "database",
                "label": "Database",
                "ok": db.backend() == "postgres",
                "status_ok": "Own Postgres connected — data persists across redeploys.",
                "status_warn": "Local SQLite — fine on your laptop, but on Railway the disk is wiped every redeploy.",
                "env": "DATABASE_URL",
                "fix": "Set DATABASE_URL to this business's own Neon pooled connection string (Railway → operator-api → Variables).",
            },
            {
                "id": "data_volume",
                "label": "Data volume",
                "ok": str(config.data_root()) != str(config.repo_root()),
                "status_ok": "Isolated data volume — this business's stores/listings/news are its own.",
                "status_warn": "Using the shared code folder — fine on your laptop, but on Railway this is ephemeral.",
                "env": "GOOGLE_STORES_DATA",
                "fix": "Add a Railway volume to operator-api, then set GOOGLE_STORES_DATA to its mount path (e.g. /data).",
            },
        ],
        "cors_origins": config.cors_origins(),
        "stores": readers.list_stores(),
        # Per-store operational health rollup — can it authenticate to Shopify, which catalog
        # path it runs, and the last automatic data-sync verdict. This is the System-tab answer
        # to "is everything syncing?" (detail lives in Connections + the Activity log).
        "stores_health": [
            {
                "store": s,
                "mode": readers.store_mode(s),
                "shopify_auth": connections.store_has_shopify_auth(s),
                "last_sync": store_sync.status(s),
            }
            for s in readers.list_stores()
        ],
        # slug → operator-set display name, so the ONE shared store selector (and every surface
        # that renders it) shows the friendly name while the slug stays the immutable data key.
        "store_labels": connections.store_labels(readers.list_stores()),
        "counts": runlog.counts(),
        # Storage footprint — the two numbers that drive the Railway bill at scale (DB size +
        # volume used %). Surfaced so growth is visible before it costs money; both reads are O(1).
        "storage": runlog.storage_stats(),
        "job_specs": jobs.specs(),
        "sku_states": readers.SKU_STATES,
        "capture_buckets": readers.CAPTURE_BUCKETS,
        "sourcing_1688_enabled": readers.sourcing_1688_enabled(),
    }


@app.put("/api/settings/tenant")
def settings_tenant_put(body: dict = Body(...)) -> dict:
    """Rename the business this deployment serves (the display label in System → Deployment).
    Persisted in app_settings so it survives a redeploy; an empty name restores the TENANT env
    default. Display-only — it doesn't change data isolation (that's GOOGLE_STORES_DATA + DB)."""
    name = (body.get("tenant") or body.get("name") or "").strip()
    tenant = config.set_tenant(name)
    runlog.record(None, "settings-tenant", tenant, "done", detail="renamed business")
    return {"tenant": tenant}


# ============================================================ Finance / P&L (native pl-dashboard port)
# The daily profit engine per store: Shopify sales (its OWN daily sync) − invoice COGS (READ from PM)
# − ad spend − fees, at frozen ECB FX. Per-store knobs live in Settings · Connections (group Finance);
# nothing here reads a credential from the environment directly. All handlers are best-effort.










_bg_inflight: set[str] = set()
_bg_lock = threading.Lock()


def _bg_run(kind: str, store: str, fn, base: dict | None = None) -> dict:
    """Run a heavy Shopify sync in a BACKGROUND thread so it never 502s the edge (a large store's
    order/variant/price pull runs for minutes). A per-(kind,store) in-flight guard collapses
    concurrent triggers. Returns immediately with running=true; the outcome lands in the run log and
    the client polls the relevant read as the data fills. Same pattern as the optimization / market-
    push syncs, generalized so every heavy sync endpoint gets it consistently."""
    key = f"{kind}:{store}"
    with _bg_lock:
        already = key in _bg_inflight
        if not already:
            _bg_inflight.add(key)

    def _job() -> None:
        try:
            res = fn()
            ok = res.get("ok", True) if isinstance(res, dict) else True
            detail = (json.dumps({k: v for k, v in res.items() if not isinstance(v, (list, dict))})[:300]
                      if isinstance(res, dict) else "")
            runlog.record(store, kind, store, "done" if ok else "failed", detail=detail)
        except Exception as e:  # noqa: BLE001 — the outcome must land in the log
            runlog.record(store, kind, store, "failed", detail=f"{type(e).__name__}: {e}")
        finally:
            with _bg_lock:
                _bg_inflight.discard(key)

    if not already:
        threading.Thread(target=_job, daemon=True, name=f"{kind}-{store}").start()
    return {"ok": True, "started": not already, "running": True, "store": store, **(base or {})}














# ============================================================ Company P&L (owner master-sheet mirror)
# The company-wide P&L — the whole business's monthly matrix (revenue rollup − OpEx − ad platforms −
# reserves − disputes − residual Shopify fee), distinct from the per-store daily Finance app. Computed
# NATIVELY from the app's own finance tables (fin_shopify_daily / fin_ad_spend / invoice COGS) plus a
# manual-OpEx layer (company_pl_manual) — no external ledger snapshot dependency.
@app.get("/api/trendtrack/status")
def trendtrack_status() -> dict:
    """Is the TrendTrack API token set + valid, and remaining credits — powers the Connections
    badge and tells the discovery job whether it can call TrendTrack directly (vs the DFS fallback)."""
    return trendtrack.status()


@app.get("/api/trendtrack/usage")
def trendtrack_usage() -> dict:
    """Raw TrendTrack credits / billing-period read."""
    return trendtrack.usage()












# ============================================================ Product Management (cost/invoice owner)
# PM OWNS the cost data Finance reads: supplier invoices → pm_invoice_lines (invoice COGS by
# order_date) + per-market cost + per-order margins. Every invoice write bumps cogs_version so P&L
# self-invalidates and "streams as now" across the split. Credentials via Connections; best-effort.
















# ── Market price-push suite (per-market pricing, faithful pl-dashboard port) ─────────────


_mp_sync_inflight: set[str] = set()
_mp_sync_lock = threading.Lock()
























































# ============================================================ Orders & Issues (native NN Operations port)
# Customer/supplier disputes against real Shopify orders: source-specific status ladders, an append-only
# event trail, a 3-step contact cadence, a refund ledger, ParcelPanel tracking, and Slack/Pumble
# escalation via the provider adapter. Shopify auth + chat creds + per-store ParcelPanel key all come
# from Settings · Connections; nothing reads a credential from the environment directly.






























































# ============================================================ Tasks & Gameplans (native NN tasks-app port)
# Operational to-dos (optionally per-store + list-scoped) and a per-store free-form gameplan. Internal
# operation state — no external credentials, nothing to configure in Connections.




















# ---------------------------------------------------------------- Store Management






