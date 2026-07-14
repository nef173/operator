"""Worker orchestration (Phase B, hybrid) — the operator stays the decision node.

This is NOT a self-driving daemon. It is a *prepare-and-wait* coordinator the operator
drives (a manual "Run scheduled steps now", or — later — a real always-on process flips
`worker_state.enabled` and calls `tick()` on a cadence). The design contract:

  WHAT  = a step (a `jobs.JOB_SPECS` id, or a synthetic higher-level step like the daily
          listing plan)
  HOW   = its autonomy mode, per step (operator-configurable):
            manual   → never run by a tick. The operator triggers it from the UI.
            suggest  → a tick does NOT run it; it drops a pending DECISION ("run X?")
                       into the operator's inbox. Nothing irreversible happens until the
                       operator approves — approval is what creates the work.
            auto     → a tick runs it now (only safe for cheap, deterministic, reversible
                       steps, or the downstream listing plan once the operator has set it).
  WHEN  = cadence (on-demand | daily | weekly) — stored config a real scheduler reads.

Operator posture (2026-06-20): once research/trend/keyword/product is done and a listing
PLAN is set, the LISTING step lists automatically each day. But the upstream judgment calls
— KEYWORD and TREND research — stay operator-decided (suggest); PRODUCT research is
operator-validated (suggest) by default and can be flipped to full-auto. So the system
PREPARES the work and waits for the operator's call, and the operator always has a
structured overview of what's running, what's waiting on them, and what's scheduled next.
`tick()` never advances a gated decision on its own — that is the whole point.
"""
from __future__ import annotations

import datetime as _dt
import os
import threading

from . import image_qa, jobs, readers, runlog, store_sync

# Synthetic steps that aren't a single job spec — they drive a higher-level action. The
# daily listing plan is the operator's "list automatically each day once the plan is set":
# it materializes the store's default gameplan's day-1 rows into jobs (draft-first; the
# build jobs themselves stay manual/needs-operator until creds exist, so nothing publishes).
_SYNTHETIC_STEPS: dict[str, dict] = {
    "daily-listings": {
        "id": "daily-listings",
        "title": "Daily listing plan",
        "surface": "listing",
        "kind": "plan",
        "spec_mode": "auto",
    },
    "store-data-sync": {
        "id": "store-data-sync",
        "title": "Store data sync",
        "surface": "operation",
        "kind": "sync",
        "spec_mode": "auto",
    },
    # Trend-research feeders shown alongside Trend Radar. News Radar pulls the GDELT velocity
    # read (global); Events Calendar is a static, always-current calendar — its tick just
    # re-reads it so the "list now / build ahead" counts stay fresh.
    "news-radar": {
        "id": "news-radar",
        "title": "News radar",
        "surface": "trend",
        "kind": "news",
        "spec_mode": "auto",
    },
    "events-calendar": {
        "id": "events-calendar",
        "title": "Events calendar",
        "surface": "trend",
        "kind": "events",
        "spec_mode": "auto",
    },
}

# Plain-English one-liner per step — what it actually does, in the operator's words. Shown
# under each step title in the Autonomy panel so the posture (Manual/Suggest/Auto) reads in
# context. Keep these short and jargon-free.
_STEP_DESCRIPTIONS: dict[str, str] = {
    "daily-listings": "Each day, turns the store's gameplan into draft listings — builds them unattended, never publishes.",
    "store-data-sync": "Pulls each connected store's Shopify data (profile, finance, orders, issues) so every sub-app stays fresh without clicking Sync.",
    "candidate-score": "Scores and ranks discovered product candidates so the strongest rise to the top of the queue.",
    "listing-snapshot": "Re-snapshots every live listing to track what changed since last time.",
    "trend-radar": "Scans for trending products and demand spikes, then surfaces the picks for you to choose from.",
    "news-radar": "Pulls the real-world news velocity read (GDELT) across all markets — the earliest signal, before the search breakout.",
    "events-calendar": "Keeps the holidays / seasons / world-events calendar current so the 'list now' and 'build ahead' windows are up to date.",
    "keyword-discovery": "Finds high-volume keywords worth building a listing around — you pick which to pursue.",
    "shopping-scan": "Reads the live Google Shopping page for a keyword: competitors, prices, and sub-segments.",
    "discover-general-stores": "Hunts new broad dropship stores to add to the competitor spy roster.",
    "bestseller-spy": "Re-checks tracked stores' best-sellers to catch products climbing fast.",
    "discover-niches": "Explores new niches worth entering, from keyword and demand signals.",
    "discover-niches-pain-first": "Explores niches starting from real customer pain (Reddit, reviews, ads).",
    "build-listing": "Builds a full draft listing — copy, images, variants — ready for your go-live.",
    "source-import": "Imports a researched product and matches a 1688 supplier (optional) before drafting.",
    "general-import-url": "Imports a product straight from a competitor or marketplace URL into a draft.",
    "catalog-scan": "Vision-scans the catalog to catch duplicate or near-duplicate products.",
}

# Recommended out-of-the-box posture (operator 2026-06-23: "most run automatic already
# beside the high-leverage decision"). Only the two LEADER research gates stay Suggest — they
# are the judgment calls. Everything else defaults to Auto: cheap deterministic maintenance
# self-schedules on a cadence; recon/build/import run Auto on-demand (they fire when chained
# off a leader approval, and stay draft-first so nothing ever auto-publishes — a build/import
# whose spec needs creds just queues a handoff instead of running).
_DEFAULT_AUTONOMY: dict[str, dict] = {
    # The two LEADER gates: keyword + trend research are the only judgment calls that surface a
    # scheduled DECISION for the operator ("which keyword/trend to build from"). Everything else
    # is a downstream CHECK — it runs auto or is chained off a leader approval (see
    # chain_after_promote), never its own scheduled decision, so the inbox stays lean.
    "trend-radar": {"mode": "suggest", "cadence": "weekly"},
    "keyword-discovery": {"mode": "suggest", "cadence": "weekly"},
    # Trend feeders — cheap data refreshes, no decision, so they default to Auto/weekly.
    "news-radar": {"mode": "auto", "cadence": "weekly"},
    "events-calendar": {"mode": "auto", "cadence": "weekly"},
    # Cheap deterministic maintenance — self-schedules so the pipeline stays fresh.
    "daily-listings": {"mode": "auto", "cadence": "daily"},
    "store-data-sync": {"mode": "auto", "cadence": "every-6-hours"},
    "candidate-score": {"mode": "auto", "cadence": "daily"},
    "listing-snapshot": {"mode": "auto", "cadence": "daily"},
    "bestseller-spy": {"mode": "auto", "cadence": "every-12-hours"},
    "discover-general-stores": {"mode": "auto", "cadence": "weekly"},
    "discover-niches": {"mode": "auto", "cadence": "weekly"},
    "discover-niches-pain-first": {"mode": "auto", "cadence": "weekly"},
    # Recon/build/import: Auto, but on-demand so a scheduled tick never starts them blind —
    # they run automatically when the operator names them OR when the pipeline chains them off
    # a promoted category. Build/import stay draft-first; if their spec needs creds, Auto just
    # queues a handoff (see StepRow "needs creds — Auto queues a handoff").
    "shopping-scan": {"mode": "auto", "cadence": "on-demand"},
    "build-listing": {"mode": "auto", "cadence": "on-demand"},
    "source-import": {"mode": "auto", "cadence": "on-demand"},
    "general-import-url": {"mode": "auto", "cadence": "on-demand"},
    "catalog-scan": {"mode": "auto", "cadence": "on-demand"},
}

_MODES = ("manual", "suggest", "auto")
_CADENCES = ("on-demand", "every-6-hours", "every-12-hours", "daily", "every-3-days", "weekly")

# How long a cadence "covers" — once a step fires, it is NOT due again until this elapses.
# This is what makes an always-on tick safe: it can run every few minutes without re-running
# a weekly step more than weekly. `on-demand` has no interval — it never fires on a scheduled
# pass (only when the operator names it explicitly). A small slack keeps a "daily" step from
# slipping a whole day when a tick lands a few minutes early.
_CADENCE_SECONDS: dict[str, int | None] = {
    "on-demand": None,
    "every-6-hours": 6 * 3600,
    "every-12-hours": 12 * 3600,
    "daily": 24 * 3600,
    "every-3-days": 3 * 24 * 3600,
    "weekly": 7 * 24 * 3600,
}
_CADENCE_SLACK = 600  # 10 min: "due" fires if within this much of the interval


def _due(store: str, step: str, cadence: str) -> tuple[bool, int | None]:
    """Is `step` due to fire for `store` under `cadence`? Returns (due, seconds_until_due).
    A step with no prior fire is always due. `on-demand` is never due on a scheduled pass."""
    interval = _CADENCE_SECONDS.get(cadence)
    if interval is None:  # on-demand → only runs when explicitly named
        return False, None
    last = runlog.step_last_fire(store, step)
    if not last:
        return True, 0
    try:
        elapsed = (_dt.datetime.now() - _dt.datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return True, 0
    remaining = interval - _CADENCE_SLACK - elapsed
    return (remaining <= 0), (None if remaining <= 0 else int(remaining))


def _steps_meta() -> list[dict]:
    """Unified list of SCHEDULABLE steps: the synthetic ones + every job spec that runs on a
    cadence. Reactive specs (e.g. the per-image fix actions fired against ONE image from the
    image-QA review) are excluded here — they're still creatable jobs, but you never schedule
    them, so they don't belong in the what/how/when autonomy panel."""
    meta = list(_SYNTHETIC_STEPS.values())
    for spec in jobs.specs():
        if not spec.get("schedulable", True):
            continue
        meta.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "surface": spec["surface"],
                "kind": "job",
                "spec_mode": spec["mode"],  # the spec's own can-run-locally flag
            }
        )
    for m in meta:
        m.setdefault("description", _STEP_DESCRIPTIONS.get(m["id"], ""))
    return meta


def _valid_steps() -> set[str]:
    return {m["id"] for m in _steps_meta()}


def _default_for(step: str) -> dict:
    return _DEFAULT_AUTONOMY.get(step, {"mode": "manual", "cadence": "on-demand"})


def config() -> list[dict]:
    """The what/how/when control panel — every schedulable step with its current autonomy."""
    overrides = runlog.autonomy_all()
    out: list[dict] = []
    for m in _steps_meta():
        cfg = overrides.get(m["id"]) or _default_for(m["id"])
        out.append(
            {
                "step": m["id"],
                "title": m["title"],
                "description": m.get("description", ""),
                "surface": m["surface"],
                "kind": m["kind"],
                "spec_mode": m["spec_mode"],
                "mode": cfg["mode"],
                "cadence": cfg["cadence"],
                "overridden": m["id"] in overrides,
            }
        )
    return out


def set_step(step: str, mode: str | None = None, cadence: str | None = None) -> dict:
    """Set a step's autonomy (HOW) and/or cadence (WHEN). Returns the merged config row."""
    if step not in _valid_steps():
        raise KeyError(step)
    overrides = runlog.autonomy_all()
    cur = overrides.get(step) or _default_for(step)
    new_mode = mode if mode is not None else cur["mode"]
    new_cadence = cadence if cadence is not None else cur["cadence"]
    if new_mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}")
    if new_cadence not in _CADENCES:
        raise ValueError(f"cadence must be one of {_CADENCES}")
    runlog.autonomy_set(step, new_mode, new_cadence)
    runlog.record(None, "autonomy-set", step, "done", detail=f"{new_mode}/{new_cadence}")
    return {"step": step, "mode": new_mode, "cadence": new_cadence}


def status() -> dict:
    """Everything the always-visible heartbeat strip + cockpit need: live worker state,
    what is running, what is queued, what is waiting on the operator, what is scheduled."""
    state = runlog.worker_state_get()
    recent_jobs = runlog.jobs_recent(limit=50)
    running = [j for j in recent_jobs if j["status"] == "running"]
    queued = [j for j in recent_jobs if j["status"] == "queued"]
    needs_operator = [j for j in recent_jobs if j["status"] == "needs-operator"]
    scheduled = [
        c for c in config()
        if c["mode"] in ("suggest", "auto") and c["cadence"] != "on-demand"
    ]
    pending = runlog.decisions_count("pending")
    return {
        "worker": state,
        "scheduler": scheduler_info(),
        "running": running,
        "queued": queued,
        "needs_operator_jobs": needs_operator,
        "pending_decisions": pending,
        "scheduled": scheduled,
        "counts": {
            "running": len(running),
            "queued": len(queued),
            "needs_operator": len(needs_operator),
            "pending_decisions": pending,
        },
    }


def set_enabled(enabled: bool) -> dict:
    """Arm/disarm automatic ticking. OFF by default (hybrid-first): when OFF, steps only run
    when the operator presses Run-now or triggers a step manually."""
    runlog.worker_state_set(enabled=enabled, status="armed" if enabled else "idle")
    runlog.record(None, "worker-enable" if enabled else "worker-disable", "worker", "done")
    return runlog.worker_state_get()


def _execute_daily_plan(store: str) -> dict:
    """Materialize the store's default gameplan's day-1 rows into jobs (the auto-daily list)."""
    gps = runlog.gameplan_list(store=store)
    default = next((g for g in gps if g.get("is_default")), gps[0] if gps else None)
    if not default:
        return {"ok": False, "reason": "no listing plan (gameplan) set for this store"}
    cfg = default.get("config") or {}
    window = cfg.get("window") or "week"
    try:
        per_day = max(1, min(50, int(cfg.get("per_day") or 50)))
    except (TypeError, ValueError):
        per_day = 50
    weights = cfg.get("weights") if isinstance(cfg.get("weights"), dict) else None
    plan = readers.listing_plan(window=window, per_day=per_day, store=store, weights=weights)
    items = [s for s in plan["schedule"] if s.get("day") == 1]
    res = jobs.execute_plan(store, items)
    return {"ok": True, "plan": default.get("name"), **res}


def _run_news_radar(cadence: str) -> dict:
    """News Radar sync — GLOBAL, not per-store. A multi-store tick would otherwise re-pull the
    same GDELT read once per store; guard so it syncs at most once per cadence window."""
    from . import news
    interval = _CADENCE_SECONDS.get(cadence)
    try:
        age = (readers.news_signals() or {}).get("synced_ago_seconds")
    except Exception:  # noqa: BLE001 — best-effort freshness read
        age = None
    if interval and isinstance(age, (int, float)) and age < (interval - _CADENCE_SLACK):
        return {"ok": True, "skipped": True, "reason": "news already fresh this window"}
    try:
        return news.sync("ALL")
    except Exception as e:  # noqa: BLE001 — a feeder sync must never crash the tick
        return {"ok": False, "error": str(e)}


def _run_events_calendar() -> dict:
    """Events calendar is static date-math (no external pull) — re-read it so the 'list now /
    build ahead' window counts reflect today. Cheap + always succeeds."""
    try:
        from . import events
        up = events.upcoming("ALL", 180)
        return {"ok": True, "events": (up.get("totals") or {}).get("events")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def tick(store: str, steps: list[str] | None = None, force: bool = False) -> dict:
    """Run one prepare-and-wait pass for a store.

    For each non-manual step (optionally filtered to `steps`):
      auto    → run it now (a job, or materialize the daily listing plan).
      suggest → create a PENDING decision carrying the action to run on approve (no side effect).
    Manual steps are never touched here.

    CADENCE GATING (what makes this safe to call 24/7): a step only fires when it is DUE under
    its cadence — an every-3-days step won't re-run on a tick that lands a day later. Each fire
    is stamped in the run ledger (`step_fire_record`) so the next tick reads a real last-fire.
    SUGGEST DE-DUP: a step that already has a pending decision is not re-suggested (no inbox
    flood). `force` (or naming a step explicitly via `steps`) bypasses the cadence wait — the
    operator's "run this now" — but suggest de-dup still holds. Returns a summary for the UI.
    """
    runlog.worker_state_set(status="running", last_tick=runlog._now())
    ran: list[dict] = []
    suggested: list[dict] = []
    skipped: list[dict] = []
    for cfg in config():
        sid = cfg["step"]
        explicit = steps is not None and sid in steps
        if steps is not None and not explicit:
            continue
        mode, kind, cadence = cfg["mode"], cfg["kind"], cfg["cadence"]
        if mode == "manual":
            skipped.append({"step": sid, "reason": "manual"})
            continue
        # Cadence gate — skip steps that are not due yet, unless the operator forced this pass
        # (force=True) or named this step explicitly. on-demand steps only run when named.
        forced = force or explicit
        if not forced:
            due, wait = _due(store, sid, cadence)
            if not due:
                reason = ("on-demand (run manually)" if cadence == "on-demand"
                          else f"not due ({_fmt_wait(wait)})")
                skipped.append({"step": sid, "reason": reason})
                continue
        if mode == "auto":
            if kind == "plan":
                res = _execute_daily_plan(store)
                ran.append({"step": sid, "kind": "plan", "result": res})
            elif kind == "sync":
                res = store_sync.run(store)
                ran.append({"step": sid, "kind": "sync", "result": res})
            elif kind == "news":
                # News Radar is a GLOBAL read (not per-store) — guard so a multi-store tick
                # syncs GDELT at most once per cadence window, not once per store.
                res = _run_news_radar(cadence)
                ran.append({"step": sid, "kind": "news", "result": res})
            elif kind == "events":
                # Events calendar is static/date-math — re-read to refresh the window counts.
                res = _run_events_calendar()
                ran.append({"step": sid, "kind": "events", "result": res})
            else:
                job = jobs.create(sid, store, {})
                ran.append({"step": sid, "job_id": job["id"], "status": job["status"]})
            runlog.step_fire_record(store, sid, detail=f"auto/{cadence}")
        elif mode == "suggest":
            if runlog.decision_pending_exists(store, f"worker:{sid}"):
                skipped.append({"step": sid, "reason": "already pending in inbox"})
                continue
            action = {"action": kind, "spec": sid, "store": store, "args": {}}
            did = runlog.decision_create(
                store,
                kind="run-step",
                title=f"Run {cfg['title']}?",
                summary=f"Scheduled ({cadence}) — approve to run this step for {store}.",
                payload=action,
                source=f"worker:{sid}",
            )
            suggested.append({"step": sid, "decision_id": did})
            runlog.step_fire_record(store, sid, detail=f"suggest/{cadence}")
    runlog.worker_state_set(status="idle", bump_tick=True)
    runlog.record(
        store, "worker-tick", f"{len(ran)} ran · {len(suggested)} suggested", "done",
    )
    return {
        "store": store,
        "ran": ran,
        "suggested": suggested,
        "skipped": skipped,
        "counts": {"ran": len(ran), "suggested": len(suggested), "skipped": len(skipped)},
    }


def _fmt_wait(seconds: int | None) -> str:
    """Human 'next in …' for a skipped-not-due step."""
    if not seconds or seconds <= 0:
        return "now"
    h = seconds // 3600
    if h >= 24:
        return f"next in ~{h // 24}d"
    if h >= 1:
        return f"next in ~{h}h"
    return f"next in ~{max(1, seconds // 60)}m"


def _decision_signal(d: dict) -> str | None:
    """A short, matchable key for a decision so a rejection learns against the right thing:
    the job spec, else the keyword/slug it concerns, else the kind. Future proposals of the
    same signal surface the past reason."""
    payload = d.get("payload") or {}
    for key in ("spec", "keyword", "slug", "term", "domain"):
        v = payload.get(key)
        if v:
            return str(v)
    return d.get("kind")


def decide(
    decision_id: int,
    approve: bool,
    reason: str | None = None,
    action: str | None = None,
) -> dict:
    """Resolve a pending decision. Approve runs its action (create a job, or run the listing
    plan) and stamps result_job_id where applicable — it goes into the proper pipeline. Reject
    records WHY (+ optionally what to change) as a durable LEARNING, so future proposals of the
    same kind/signal surface it and the pipeline stops re-suggesting what was turned down. The
    approval IS the authorization — the single seam where a suggestion becomes real work."""
    d = runlog.decision_get(decision_id)
    if d is None:
        raise KeyError(decision_id)
    if d["status"] != "pending":
        return {"ok": False, "reason": f"decision already {d['status']}", "decision": d}
    if not approve:
        out = runlog.decision_resolve(decision_id, "rejected")
        learning = None
        reason = (reason or "").strip()
        if reason:
            lid = runlog.learning_add(
                kind=d["kind"],
                reason=reason,
                store=d.get("store"),
                signal=_decision_signal(d),
                action=(action or "").strip() or None,
                decision_id=decision_id,
            )
            learning = {"id": lid, "kind": d["kind"], "reason": reason, "action": action}
        runlog.record(
            d.get("store"), "decision-reject", d["title"], "done",
            detail=(reason or None),
        )
        return {"ok": True, "decision": out, "job": None, "learning": learning}

    payload = d.get("payload") or {}
    store = payload.get("store") or d.get("store")
    job = None
    result = None
    action = payload.get("action")
    if action == "plan":
        result = _execute_daily_plan(store)
    elif action == "sync":
        result = store_sync.run(store)
    elif action == "image-qa-apply" and payload.get("slug"):
        # Approving an image-QA review applies the AI's PROPOSED verdicts as-is (the category
        # page is where the operator changes individual verdicts before applying). This is the
        # seam that lets the scan→apply step be promoted to the fully-automated worker path.
        result = image_qa.apply(store, payload["slug"], payload.get("verdicts") or [])
    elif action == "image-fix-run":
        # Approving the "Run image fixes" decision is what actually fires the FREE local fixes
        # (auto-clean / upscale). They adopt in place — overwriting the real on-disk file — so
        # the run is deliberately gated behind this approval rather than firing on apply().
        fired = []
        for fix in payload.get("fixes") or []:
            spec = fix.get("spec")
            if not spec:
                continue
            j = jobs.create(spec, store, fix.get("args") or {})
            fired.append({"spec": spec, "sku": fix.get("sku"), "file": fix.get("file"),
                          "job_id": j.get("id")})
        result = {"fired": fired, "count": len(fired)}
    elif action == "job" and payload.get("spec"):
        job = jobs.create(payload["spec"], store, payload.get("args") or {})
    out = runlog.decision_resolve(
        decision_id, "approved", result_job_id=(job["id"] if job else None)
    )
    runlog.record(d.get("store"), "decision-approve", d["title"], "done",
                  output=(job["command"] if job else None))
    return {"ok": True, "decision": out, "job": job, "result": result}


def chain_after_promote(store: str, keyword: str) -> dict:
    """Pipeline chain fired when the operator PROMOTES a candidate category (gate #1 — "which
    keyword/trend to build from"). Approving the category should kick the next step running on
    its own, without making the operator click "research" again: it auto-resolves the SKU plan's
    build terms and fires one product-find (Google-Shopping scan) job per term.

    This is the FIRST chain seam. Truthful ordering: promote → FIND PRODUCTS → (gate #3: validate
    found products + source) → store-check + 1688-check. The downstream checks (catalog-scan,
    china-*) genuinely need the found products first, so they are NOT fired here — firing them now
    would create empty/premature jobs. They run off the Sourcing Match surface once products exist.

    Each scan goes through `jobs.create`, which dedups needs-operator handoffs and runs in-app when
    the dep is reachable — so re-promoting the same category does not pile up duplicates."""
    from . import sku_plan  # lazy import — sku_plan has no dep on worker
    try:
        terms = sku_plan.research_targets(store, keyword)
    except Exception:  # noqa: BLE001
        terms = []
    # Cold promote (a Trend card, or a keyword with no SKU plan built yet) has no build terms.
    # Fall back to the head keyword itself (same as the other research_targets callers) so promote
    # ALWAYS fires a product-find: the shopping-scan on the head is what BUILDS the SKU plan
    # (its Google-Shopping sub-segment rows). Without this, promoting a fresh keyword silently
    # did nothing downstream.
    if not terms:
        terms = [keyword] if keyword else []
    if not terms:
        runlog.record(store, "promote-chain", keyword, "done", detail="no keyword to scan")
        return {"chained": False, "terms": [], "jobs": []}
    created = [jobs.create("shopping-scan", store, {"keyword": t}) for t in terms]
    runlog.record(store, "promote-chain", keyword, "done",
                  detail=f"auto-started product-find: {len(created)} scan(s) across {len(terms)} term(s)")
    return {"chained": True, "terms": terms, "jobs": created}


# Downstream check fired at gate #3. The store-check (catalog dedup) always runs — it is cheap
# and you always want to know if the product is already on the store. The 1688 sourcing match is
# OPTIONAL and operator-gated (a decision), NOT auto-chained: the operator chooses per category
# whether to validate+source on 1688 (and list off the 1688 offer) OR skip it and list directly
# off the competitor/marketplace research source — no forced step in between (operator 2026-06-23).
#   catalog-scan  = store-check (is it already on the store?) — runs BEFORE sourcing, always
#   china-verify  = 1688-check (reverse-image → enrich → VLM judge → matched.json) — OPTIONAL
_FOUND_VALIDATED_CHAIN = ("catalog-scan",)


def chain_after_found_validated(store: str, keyword: str) -> dict:
    """SECOND chain seam — fired at gate #3 ("the found products + their source look right").

    Once the operator has eyeballed the products that product-find turned up and confirmed they
    are worth pursuing, the store-check (catalog dedup) runs on its own. The 1688 sourcing match
    is NOT auto-fired: it is OPTIONAL, so it is dropped as ONE inbox DECISION the operator answers
    per category — APPROVE to run the 1688 reverse-image match + enrich and list off the 1688
    source, or SKIP to list directly off the competitor/marketplace research source (the build
    defaults to source_of_truth="researched" when no 1688 match exists). No step in between is
    forced (operator 2026-06-23).

    Honest about what "auto" means: catalog-scan + china-verify need AdsPower / Shopify Admin /
    a vision key / the TMAPI key, so `jobs.create` records each as a needs-operator handoff with
    the exact command (deduped). Their results land in the EXISTING Sourcing Match surface
    (Catalog check = Step 0, 1688 = Step 1). When the keys are present in-app the same call runs
    them directly; nothing else changes."""
    created = [
        jobs.create(spec, store, {"keyword": keyword, "subject": keyword})
        for spec in _FOUND_VALIDATED_CHAIN
    ]
    # The OPTIONAL 1688 sourcing match — a decision, not an auto-fired job. Approving fires
    # china-verify (the existing `action == "job"` worker path); skipping/rejecting lists off the
    # competitor/marketplace research source. This is the "1688 is optional, no step in between" gate.
    fix_decision_id = runlog.decision_create(
        store, kind="sourcing-1688",
        title=f"1688 sourcing match — {keyword}? (optional)",
        summary="APPROVE to run the 1688 reverse-image match + enrich, then list off the 1688 "
                "source. SKIP to list directly off the competitor / marketplace research source "
                "(no 1688 step in between).",
        payload={"action": "job", "store": store, "spec": "china-verify",
                 "args": {"keyword": keyword, "subject": keyword}},
        source="found-validated-chain",
    )
    runlog.record(store, "found-validated-chain", keyword, "done",
                  detail=f"store-check started ({len(created)} job); 1688 match offered as optional "
                         f"decision #{fix_decision_id}")
    return {"chained": True, "checks": list(_FOUND_VALIDATED_CHAIN), "jobs": created,
            "sourcing_1688_decision": fix_decision_id}


# ---------------------------------------------------------------------------
# Always-on scheduler — the "online, always-running worker".
#
# A single daemon thread woken on a poll interval. Each wake, IF the operator has armed the
# worker (`worker_state.enabled`), it runs one cadence-gated `tick()` per registered store.
# Cadence gating (above) means a frequent poll is cheap: a step only fires when DUE, so the
# poll interval just bounds how soon a due step starts — it does NOT control how often steps
# run (cadence does). The loop NEVER dies on an error (one store's failure can't stop the
# others or the daemon); it logs and continues. When the worker is disarmed it idles cheaply.
#
# Local/dev: started at FastAPI startup, armed via the UI toggle. Production (Railway): the
# same module is the worker process — this loop IS the 24/7 worker; the Neon swap in db.py is
# the only other change. The interval is OPERATOR_WORKER_POLL_SECONDS (default 300s).
# ---------------------------------------------------------------------------
_POLL_SECONDS = max(30, int(os.environ.get("OPERATOR_WORKER_POLL_SECONDS", "300")))
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()
_scheduler_guard = threading.Lock()
_last_loop: dict = {"at": None, "ran": 0, "suggested": 0, "stores": 0, "error": None}


def _scheduler_loop() -> None:
    while not _scheduler_stop.is_set():
        try:
            state = runlog.worker_state_get()
            if state.get("enabled"):
                _run_all_due()
        except Exception as exc:  # never let the daemon die
            _last_loop["error"] = f"{type(exc).__name__}: {exc}"
            try:
                runlog.record(None, "worker-loop-error", "scheduler", "failed", detail=str(exc))
            except Exception:
                pass
        _scheduler_stop.wait(_POLL_SECONDS)


def _run_all_due() -> dict:
    """One scheduled pass across every registered store (cadence-gated, never forced)."""
    # Self-heal first: handoffs recorded while a dependency was missing become real auto
    # jobs the moment the dependency exists (new image with the research venvs, key added).
    try:
        jobs.requeue_unblocked()
    except Exception as exc:  # never let the sweep break the pass
        runlog.record(None, "worker-requeue", "unblocked handoffs", "failed", detail=str(exc))
    stores = readers.list_stores()
    total_ran = total_suggested = 0
    for store in stores:
        try:
            res = tick(store, force=False)
            total_ran += res["counts"]["ran"]
            total_suggested += res["counts"]["suggested"]
        except Exception as exc:  # isolate per-store failure
            runlog.record(store, "worker-tick", "scheduled pass", "failed", detail=str(exc))
    _last_loop.update(
        at=runlog._now(), ran=total_ran, suggested=total_suggested,
        stores=len(stores), error=None,
    )
    return dict(_last_loop)


def start_scheduler() -> None:
    """Start the always-on daemon once (idempotent). Safe to call at app startup even when
    the worker is disarmed — the loop just idles until `worker_state.enabled` is flipped on."""
    global _scheduler_thread
    with _scheduler_guard:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop, name="operator-worker-scheduler", daemon=True
        )
        _scheduler_thread.start()


def stop_scheduler() -> None:
    """Signal the daemon to exit (used on shutdown / in tests)."""
    _scheduler_stop.set()


def scheduler_info() -> dict:
    """Diagnostics for Settings: is the daemon alive, the poll interval, and the last pass."""
    return {
        "running": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "poll_seconds": _POLL_SECONDS,
        "last_loop": dict(_last_loop),
    }
