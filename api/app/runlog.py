"""Durable run-log + job store — the operator app's "memory of past work".

All connections come from the single seam in `db.py` (SQLite today, WAL-tuned so an
always-on worker + the API can write concurrently; one DATABASE_URL swap point to Neon).
The helper signatures here are the stable interface both the API and the future 24/7
worker call — they never change when the backing store does.

Tables share one DB:
  runs         — synchronous control-layer actions (set-state, promote, add-category)
  jobs         — heavier pipeline steps run as background/manual jobs (Phase 3, jobs.py)
  gameplans    — per-store saved Daily-Listings settings bundles
  decisions    — the operator's "needs you" inbox (suggest-mode + AI proposals)
  autonomy     — per-step what/how/when config the worker reads
  worker_state — single-row heartbeat for the always-visible status strip
"""
from __future__ import annotations

import datetime as _dt
import json

from . import db


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _conn():
    """A ready connection from the shared seam (schema ensured, concurrency-tuned)."""
    return db.connect()


def db_path() -> str:
    return db.db_path()


# ---------------------------------------------------------------- runs
def record(
    store: str | None,
    action: str,
    target: str | None,
    status: str,
    detail: str | None = None,
    output: str | None = None,
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO runs (ts, store, action, target, status, detail, output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), store, action, target, status, detail, output),
        )
        return int(cur.lastrowid)


def recent(limit: int = 50, store: str | None = None) -> list[dict]:
    with _conn() as conn:
        if store:
            rows = conn.execute(
                "SELECT * FROM runs WHERE store = ? ORDER BY id DESC LIMIT ?",
                (store, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def last_run(store: str, action: str) -> dict | None:
    """Most recent run row for one (store, action) — e.g. the last store-data-sync verdict,
    so the Connections/System UI can show sync freshness + failures without scanning runs."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE store = ? AND action = ? ORDER BY id DESC LIMIT 1",
            (store, action),
        ).fetchone()
    return dict(row) if row else None


def counts() -> dict:
    with _conn() as conn:
        runs = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    return {"runs": int(runs), "jobs": int(jobs)}


def storage_stats() -> dict:
    """Cost guardrail — the two numbers that drive the Railway bill at scale, surfaced so the
    operator SEES growth before it costs money (this app is built for many stores × products ×
    invoices). Both reads are O(1): the DB size is a single catalog query, the volume usage is
    one statvfs syscall (NOT a directory walk — that would be O(files) and slow at scale).

      * db_bytes    — Postgres db size (pg_database_size) or the SQLite file size.
      * volume_*    — used/total of the mounted data volume (Railway bills USED GB); the only
                      unbounded grower is staged product images under general-stores/, which
                      belong on the Shopify CDN, not this disk — watch used_pct here.
    """
    import os
    import shutil

    from . import config

    out: dict = {"db_backend": db.backend()}
    # ---- database size ----
    try:
        if db.backend() == "postgres":
            with _conn() as conn:
                row = conn.execute("SELECT pg_database_size(current_database()) AS n").fetchone()
                out["db_bytes"] = int(row["n"])
        else:
            out["db_bytes"] = os.path.getsize(db.db_path()) if os.path.exists(db.db_path()) else 0
    except Exception:
        out["db_bytes"] = None
    # ---- data volume usage (one syscall, no walk) ----
    try:
        du = shutil.disk_usage(str(config.data_root()))
        out["volume_total_bytes"] = int(du.total)
        out["volume_used_bytes"] = int(du.used)
        out["volume_free_bytes"] = int(du.free)
        out["volume_used_pct"] = round(du.used / du.total * 100, 1) if du.total else None
    except Exception:
        out["volume_total_bytes"] = out["volume_used_bytes"] = None
    return out


# ---------------------------------------------------------------- jobs
def job_create(
    store: str | None, spec: str, mode: str, title: str, status: str, command: str
) -> int:
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (ts, updated, store, spec, mode, title, status, command) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, store, spec, mode, title, status, command),
        )
        return int(cur.lastrowid)


def job_update(
    job_id: int, status: str, detail: str | None = None, output: str | None = None
) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, updated = ?, detail = COALESCE(?, detail), "
            "output = COALESCE(?, output) WHERE id = ?",
            (status, _now(), detail, output, job_id),
        )


def job_get(job_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def job_find_open(store: str | None, spec: str, command: str) -> dict | None:
    """Find an already-open `needs-operator` job that is IDENTICAL to one we're about to
    record (same store, spec, and exact command). Used to dedup the operator inbox: the
    auto/daily listing plan re-materializes the same day-1 rows every day, which would
    otherwise append a fresh `keyword-discovery`/`source-import` handoff each time. Distinct
    inputs (different URLs) produce different commands and are NOT collapsed."""
    with _conn() as conn:
        if store is None:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'needs-operator' AND spec = ? "
                "AND command = ? AND store IS NULL ORDER BY id DESC LIMIT 1",
                (spec, command),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'needs-operator' AND spec = ? "
                "AND command = ? AND store = ? ORDER BY id DESC LIMIT 1",
                (spec, command, store),
            ).fetchone()
    return dict(row) if row else None


def job_find_active(store: str | None, spec: str, command: str) -> dict | None:
    """Return the newest QUEUED/RUNNING job identical to one about to be created (same
    store + spec + exact command) — so `jobs.create` can collapse a duplicate instead of
    stacking it on the bounded worker pool. This is what stops the 24/7 scheduler AND the
    operator's Start/Run buttons from piling up several identical `keyword-discovery` /
    `bestseller-spy` runs for one store (the "runs too long" backlog). Distinct inputs
    (a different --keyword / --url → different command) produce a different command and are
    NOT collapsed, so a genuinely new run still queues."""
    with _conn() as conn:
        if store is None:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') AND spec = ? "
                "AND command = ? AND store IS NULL ORDER BY id DESC LIMIT 1",
                (spec, command),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') AND spec = ? "
                "AND command = ? AND store = ? ORDER BY id DESC LIMIT 1",
                (spec, command, store),
            ).fetchone()
    return dict(row) if row else None


def jobs_open_handoffs() -> list[dict]:
    """Every job still sitting in the operator's needs-you inbox. Feeds the self-heal sweep
    (jobs.requeue_unblocked): handoffs recorded while a local dependency was missing get
    re-queued as real auto jobs once that dependency appears (e.g. the research venvs
    shipped in a new image), instead of staying stale forever."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'needs-operator' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def jobs_orphaned_restart() -> list[dict]:
    """Jobs `reap_orphan_jobs` failed because a restart (a deploy) killed their process mid-run —
    NOT a real failure. Feeds the startup self-heal (jobs.requeue_orphaned_auto): the auto-local
    ones get re-queued so the 24/7 pipeline resumes, and the rest get superseded so a deploy never
    leaves a wall of stale 'failed' cards. Newest first so dedup keeps the most recent of each."""
    with _conn() as conn:
        # The LIKE pattern is a PARAMETER, not inlined: a literal '%' in the SQL string makes
        # psycopg raise "only '%s','%b','%t' are allowed as placeholders" (it reads '%'' as a bad
        # placeholder). Passing it as a bound value keeps this portable across SQLite + Postgres.
        rows = conn.execute(
            "SELECT id, spec, store, command, detail FROM jobs "
            "WHERE status = 'failed' AND detail LIKE ? "
            "ORDER BY id DESC LIMIT 500",
            ("orphaned by restart%",),
        ).fetchall()
    return [dict(r) for r in rows]


def reap_orphan_jobs(reason: str = "orphaned by restart") -> int:
    """Fail any job left `running` or `queued` from a PRIOR process. Called once at startup:
    the in-process thread pool does not survive a restart, so such rows can never complete on
    their own — without this they sit `running` forever and inflate the live-work counts. Safe
    because it runs before this process submits any job, so nothing genuinely in-flight is hit."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed', updated = ?, "
            "detail = COALESCE(detail, ?) WHERE status IN ('running', 'queued')",
            (_now(), reason),
        )
        return int(cur.rowcount or 0)


def prune(keep_runs: int = 10000, keep_jobs: int = 10000, keep_decisions: int = 5000) -> dict:
    """Bound DB growth so the run-log/jobs history can't balloon over months (which would
    grow Neon storage + slow queries). Keeps the NEWEST N rows and drops older ones —
    but NEVER touches live work: only terminal jobs (done/failed/superseded) and resolved
    decisions are eligible; queued/running/needs-operator jobs and pending decisions always
    stay. Called once at startup; cheap and idempotent. The `NOT IN (… ORDER BY id DESC
    LIMIT ?)` form is valid on both SQLite and Postgres."""
    with _conn() as conn:
        runs = conn.execute(
            "DELETE FROM runs WHERE id NOT IN "
            "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
            (keep_runs,),
        ).rowcount or 0
        jobs = conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed', 'superseded') AND id NOT IN "
            "(SELECT id FROM jobs WHERE status IN ('done', 'failed', 'superseded') "
            "ORDER BY id DESC LIMIT ?)",
            (keep_jobs,),
        ).rowcount or 0
        decisions = conn.execute(
            "DELETE FROM decisions WHERE status != 'pending' AND id NOT IN "
            "(SELECT id FROM decisions WHERE status != 'pending' ORDER BY id DESC LIMIT ?)",
            (keep_decisions,),
        ).rowcount or 0
    return {"runs": int(runs), "jobs": int(jobs), "decisions": int(decisions)}


def job_spec_counts(store: str | None = None) -> dict[str, int]:
    """How many jobs have been created per spec (optionally scoped to one store).

    Powers the cost estimate: spec-counts × the per-spec unit-cost recipe = estimated
    spend to date. Counts every job ever created (queued/running/done/failed/needs-operator)
    since each created job represents work the pipeline was asked to do."""
    with _conn() as conn:
        if store:
            rows = conn.execute(
                "SELECT spec, COUNT(*) AS n FROM jobs WHERE store = ? GROUP BY spec",
                (store,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT spec, COUNT(*) AS n FROM jobs GROUP BY spec"
            ).fetchall()
    return {r["spec"]: int(r["n"]) for r in rows}


def job_claim_next(modes: tuple[str, ...] = ("auto",)) -> dict | None:
    """Atomically claim the oldest queued job (default: auto specs) → mark it 'running' and
    return it; None if nothing is waiting. This is the 24/7 worker's "pull next work safely"
    primitive: BEGIN IMMEDIATE takes the write lock up front, so two concurrent workers can
    never grab the same job — the loser waits (busy_timeout) and then sees it's no longer
    queued. The API path (jobs.create) still runs jobs inline; this is the durable-worker hook."""
    if not modes:
        return None
    ph = ",".join("?" for _ in modes)
    conn = db.connect()
    conn.isolation_level = None  # we manage the transaction explicitly
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT id FROM jobs WHERE status = 'queued' AND mode IN ({ph}) "
            "ORDER BY id ASC LIMIT 1",
            tuple(modes),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        jid = int(row["id"])
        conn.execute(
            "UPDATE jobs SET status = 'running', updated = ? WHERE id = ?", (_now(), jid)
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return job_get(jid)


def jobs_recent(limit: int = 50, store: str | None = None) -> list[dict]:
    # 'superseded' jobs are duplicate handoffs that were collapsed into a single live one —
    # historical noise, hidden from the Activity feed and every count so the inbox stays lean.
    with _conn() as conn:
        if store:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE store = ? AND status != 'superseded' ORDER BY id DESC LIMIT ?",
                (store, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status != 'superseded' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- gameplans
# Per-store saved Daily-Listings settings bundles. config holds the full plan
# shape as JSON: {window, per_day, weights:{cat:pct}, method}. One gameplan per
# store may be the default (is_default=1); setting a new default clears others.
def _gameplan_row(row) -> dict:
    d = dict(row)
    try:
        d["config"] = json.loads(d["config"])
    except (json.JSONDecodeError, TypeError):
        d["config"] = {}
    d["is_default"] = bool(d["is_default"])
    return d


def gameplan_list(store: str | None = None) -> list[dict]:
    with _conn() as conn:
        if store:
            rows = conn.execute(
                "SELECT * FROM gameplans WHERE store = ? ORDER BY is_default DESC, id DESC",
                (store,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM gameplans ORDER BY store, is_default DESC, id DESC"
            ).fetchall()
    return [_gameplan_row(r) for r in rows]


def gameplan_get(gameplan_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM gameplans WHERE id = ?", (gameplan_id,)
        ).fetchone()
    return _gameplan_row(row) if row else None


def gameplan_create(
    store: str, name: str, config: dict, is_default: bool = False
) -> int:
    now = _now()
    with _conn() as conn:
        if is_default:
            conn.execute(
                "UPDATE gameplans SET is_default = 0 WHERE store = ?", (store,)
            )
        cur = conn.execute(
            "INSERT INTO gameplans (ts, updated, store, name, config, is_default) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, now, store, name, json.dumps(config), 1 if is_default else 0),
        )
        return int(cur.lastrowid)


def gameplan_update(
    gameplan_id: int,
    name: str | None = None,
    config: dict | None = None,
    is_default: bool | None = None,
) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM gameplans WHERE id = ?", (gameplan_id,)
        ).fetchone()
        if row is None:
            return None
        if is_default:
            conn.execute(
                "UPDATE gameplans SET is_default = 0 WHERE store = ?", (row["store"],)
            )
        conn.execute(
            "UPDATE gameplans SET "
            "name = COALESCE(?, name), "
            "config = COALESCE(?, config), "
            "is_default = COALESCE(?, is_default), "
            "updated = ? WHERE id = ?",
            (
                name,
                json.dumps(config) if config is not None else None,
                (1 if is_default else 0) if is_default is not None else None,
                _now(),
                gameplan_id,
            ),
        )
        out = conn.execute(
            "SELECT * FROM gameplans WHERE id = ?", (gameplan_id,)
        ).fetchone()
    return _gameplan_row(out)


def gameplan_delete(gameplan_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM gameplans WHERE id = ?", (gameplan_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- decisions
# The operator's "needs you" inbox. payload is a JSON action descriptor (today:
# {"action":"job","spec":...,"args":...}); approving a decision runs it.
def _decision_row(row) -> dict:
    d = dict(row)
    try:
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        d["payload"] = {}
    return d


def decision_create(
    store: str | None,
    kind: str,
    title: str,
    summary: str | None = None,
    payload: dict | None = None,
    source: str | None = None,
) -> int:
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (ts, updated, store, kind, title, summary, payload, "
            "status, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (now, now, store, kind, title, summary, json.dumps(payload or {}), source),
        )
        return int(cur.lastrowid)


def decisions_list(
    status: str | None = "pending", store: str | None = None, limit: int = 100
) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if store:
        clauses.append("store = ?")
        params.append(store)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM decisions {where} ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()
    return [_decision_row(r) for r in rows]


def decision_get(decision_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
    return _decision_row(row) if row else None


def decision_resolve(
    decision_id: int, status: str, result_job_id: int | None = None
) -> dict | None:
    with _conn() as conn:
        conn.execute(
            "UPDATE decisions SET status = ?, result_job_id = COALESCE(?, result_job_id), "
            "updated = ? WHERE id = ?",
            (status, result_job_id, _now(), decision_id),
        )
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
    return _decision_row(row) if row else None


def decisions_count(status: str = "pending") -> int:
    with _conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE status = ?", (status,)
        ).fetchone()["n"]
    return int(n)


def decision_pending_exists(store: str | None, source: str) -> bool:
    """Is there already an un-resolved decision from this worker source for this store?
    The always-on tick uses this to AVOID flooding the inbox with a duplicate "Run X?"
    every cycle — one pending suggestion per (store, step) is enough until it's resolved."""
    with _conn() as conn:
        if store is None:
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE status = 'pending' AND source = ? "
                "AND store IS NULL LIMIT 1",
                (source,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE status = 'pending' AND source = ? "
                "AND store = ? LIMIT 1",
                (source, store),
            ).fetchone()
    return row is not None


# ---------------------------------------------------------------- worker step ledger
def step_fire_record(store: str | None, step: str, detail: str | None = None) -> int:
    """Stamp that a schedulable step FIRED (ran or was suggested) for a store, so cadence
    gating has a durable last-fire timestamp. Recorded in the shared `runs` ledger under a
    reserved action so it shows in Activity and survives restarts (no extra table needed)."""
    return record(store, "step-fire", step, "done", detail=detail)


def step_last_fire(store: str | None, step: str) -> str | None:
    """ISO timestamp of the most recent fire of `step` for `store`, or None if never. Drives
    'is this step due yet?' so an always-on tick doesn't re-run an every-3-days step daily."""
    with _conn() as conn:
        if store is None:
            row = conn.execute(
                "SELECT ts FROM runs WHERE action = 'step-fire' AND target = ? "
                "AND store IS NULL ORDER BY id DESC LIMIT 1",
                (step,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT ts FROM runs WHERE action = 'step-fire' AND target = ? "
                "AND store = ? ORDER BY id DESC LIMIT 1",
                (step, store),
            ).fetchone()
    return row["ts"] if row else None


# ---------------------------------------------------------------- autonomy
def autonomy_all() -> dict[str, dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM autonomy").fetchall()
    return {r["step"]: {"mode": r["mode"], "cadence": r["cadence"]} for r in rows}


def autonomy_set(step: str, mode: str, cadence: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO autonomy (step, mode, cadence, updated) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(step) DO UPDATE SET mode = excluded.mode, "
            "cadence = excluded.cadence, updated = excluded.updated",
            (step, mode, cadence, _now()),
        )


# ---------------------------------------------------------------- learnings
# The "smart learning" memory built from rejections. signal is a short matchable key
# (job spec / keyword / slug) so future proposals of the same kind/signal can surface the
# past reason and stop re-suggesting what was already turned down.
def learning_add(
    kind: str,
    reason: str,
    store: str | None = None,
    signal: str | None = None,
    action: str | None = None,
    decision_id: int | None = None,
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO learnings (ts, kind, store, signal, reason, action, decision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), kind, store, signal, reason, action, decision_id),
        )
        return int(cur.lastrowid)


def learnings_recent(
    limit: int = 50, kind: str | None = None, store: str | None = None
) -> list[dict]:
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if store:
        clauses.append("store = ?")
        params.append(store)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM learnings {where} ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()
    return [dict(r) for r in rows]


def learnings_for(
    kind: str, store: str | None = None, signal: str | None = None, limit: int = 5
) -> list[dict]:
    """Learnings relevant to a NEW proposal of this kind: same kind, and (same store OR a
    global learning). Signal SCOPES the match — a learning carrying a signal (e.g. the spec
    `bestseller-spy`) only surfaces on a proposal with that SAME signal, so rejecting one step
    doesn't spuriously warn on every other step of the same kind. Signal-less learnings (broad
    "this kind is off-strategy" rules) always surface. This is what gets attached to a pending
    decision so the operator sees "you rejected similar before"."""
    # NOTE: portable SQL only — `store IS ?` is valid SQLite but a syntax error on Postgres
    # (it 500'd the whole pending-decisions inbox in production). `store = ?` never matches
    # when the param is NULL, and the `store IS NULL` arm already covers global learnings,
    # so the NULL-param case loses nothing.
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM learnings WHERE kind = ? AND (store = ? OR store IS NULL) "
            "AND (signal IS NULL OR signal = '' OR signal = ?) "
            "ORDER BY (CASE WHEN signal = ? THEN 1 ELSE 0 END) DESC, id DESC LIMIT ?",
            (kind, store, signal, signal, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def learnings_count() -> int:
    with _conn() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM learnings").fetchone()["n"])


# ---------------------------------------------------------------- app_settings
# A tiny key→JSON store for operator-tunable GLOBAL config that isn't per-store or
# per-step (today: the SKU-plan weight split). value is a JSON blob; callers pass/get
# plain dicts.
def setting_get(key: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def setting_set(key: str, value: dict) -> dict:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated = excluded.updated",
            (key, json.dumps(value), _now()),
        )
    return value


def setting_delete(key: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- worker_state
def worker_state_get() -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO worker_state (id, enabled, status, ticks, updated) "
                "VALUES (1, 0, 'idle', 0, ?)",
                (_now(),),
            )
            row = conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


def worker_state_set(
    enabled: bool | None = None,
    status: str | None = None,
    last_tick: str | None = None,
    detail: str | None = None,
    bump_tick: bool = False,
) -> dict:
    worker_state_get()  # ensure the row exists
    with _conn() as conn:
        conn.execute(
            "UPDATE worker_state SET "
            "enabled = COALESCE(?, enabled), "
            "status = COALESCE(?, status), "
            "last_tick = COALESCE(?, last_tick), "
            "detail = COALESCE(?, detail), "
            "ticks = ticks + ?, "
            "updated = ? WHERE id = 1",
            (
                None if enabled is None else (1 if enabled else 0),
                status,
                last_tick,
                detail,
                1 if bump_tick else 0,
                _now(),
            ),
        )
    return worker_state_get()
