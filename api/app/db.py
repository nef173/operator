"""Database connection seam — the ONE place the operator app opens its store.

Why this exists: when the pipeline runs 24/7 the always-on worker and the API write the
SAME database concurrently (jobs, run-log, gameplans, queues). This module makes that safe
and keeps the data path identical whether the code runs on a laptop or on Railway/Neon.

Today (and fully tested): local SQLite at operator-app/api/data/app.db, tuned for concurrent
writers —
  * WAL journal      → readers don't block the writer and vice-versa
  * busy_timeout      → a second writer WAITS briefly instead of erroring "database is locked"
  * synchronous=NORMAL→ durable enough for a run-log, much faster under load
Schema is created ONCE (idempotent), not on every call, so a high-frequency worker stays cheap.

Tomorrow (the 24/7 worker): set DATABASE_URL=postgres://… . That is the SINGLE swap point —
the Postgres adapter is added right here, behind `connect()`, and NOTHING upstream changes:
every runlog/jobs helper signature the worker and API call stays exactly the same. The schema
is plain ANSI SQL (TEXT/INTEGER + `?` placeholders) precisely so that swap is additive.
"""
from __future__ import annotations

import os
import sqlite3
import threading

from . import config

# The SQLite run-log lives under the per-deployment DATA root (config.data_root()), so when
# DATABASE_URL is NOT set each business keeps its own app.db on its own volume rather than
# inside the shared code checkout. With DATABASE_URL set this path is unused (Neon is the store).
_DB_PATH = config.data_root() / "operator-app" / "api" / "data" / "app.db"

_init_lock = threading.Lock()
_initialized = False

# Plain ANSI-SQL schema (TEXT/INTEGER only) so it ports to Neon Postgres unchanged.
_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        store TEXT,
        action TEXT NOT NULL,
        target TEXT,
        status TEXT NOT NULL,
        detail TEXT,
        output TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        updated TEXT NOT NULL,
        store TEXT,
        spec TEXT NOT NULL,
        mode TEXT NOT NULL,
        title TEXT,
        status TEXT NOT NULL,
        command TEXT,
        detail TEXT,
        output TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gameplans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        updated TEXT NOT NULL,
        store TEXT NOT NULL,
        name TEXT NOT NULL,
        config TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0
    )
    """,
    # decisions — the operator's "needs you" inbox. A SUGGEST-mode step (or, later, the
    # AI companion) writes a pending row carrying a JSON action descriptor; approving it
    # runs that action (today: create a job) and stamps result_job_id. This is what keeps
    # the operator the decision node — nothing irreversible advances without an approval row.
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        updated TEXT NOT NULL,
        store TEXT,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT,
        payload TEXT,
        status TEXT NOT NULL,
        source TEXT,
        result_job_id INTEGER
    )
    """,
    # autonomy — per-step config: WHAT (step = job-spec id) × HOW (manual|suggest|auto)
    # × WHEN (cadence). The worker reads this to decide what to run/suggest on a tick.
    """
    CREATE TABLE IF NOT EXISTS autonomy (
        step TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        cadence TEXT NOT NULL,
        updated TEXT NOT NULL
    )
    """,
    # worker_state — single-row heartbeat the always-visible status strip reads. enabled is
    # OFF by default (hybrid-first: the operator drives the tick now; a real always-on
    # process can flip this later).
    """
    CREATE TABLE IF NOT EXISTS worker_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        enabled INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'idle',
        last_tick TEXT,
        detail TEXT,
        ticks INTEGER NOT NULL DEFAULT 0,
        updated TEXT
    )
    """,
    # learnings — the "smart learning" memory. Every time the operator REJECTS a proposed
    # decision they say WHY (and optionally what to change); that becomes a durable learning
    # row keyed by the decision's kind + a matchable signal (spec / keyword / slug). Future
    # proposals of the same kind/signal surface these so the system stops re-suggesting what
    # was already turned down — the pipeline gets smarter from each rejection.
    """
    CREATE TABLE IF NOT EXISTS learnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        store TEXT,
        signal TEXT,
        reason TEXT NOT NULL,
        action TEXT,
        decision_id INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_learnings_kind ON learnings (kind, id)",
    # app_settings — a tiny key→JSON store for operator-tunable global config that isn't
    # per-store or per-step (e.g. the SKU-plan weight split: anchor/coverage %, source quota,
    # role floors). One row per setting key; value is a JSON blob the helper (de)serializes.
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated TEXT NOT NULL
    )
    """,
    # Helps the worker poll for outstanding work and the cost view group by spec.
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, mode, id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions (status, id)",
    # ── FINANCE / P&L (native port of the pl-dashboard "run" half) ─────────────────────────
    # Daily Shopify sales aggregates per store+date (the calcPL revenue side). One Shopify
    # sync path writes here (its OWN aggregate path — PM syncs per-order separately). Composite
    # PK (store_key,date) is valid in both SQLite and Postgres, so no AUTOINCREMENT translation.
    """
    CREATE TABLE IF NOT EXISTS fin_shopify_daily (
        store_key TEXT NOT NULL,
        date TEXT NOT NULL,
        gross_sales REAL NOT NULL DEFAULT 0,
        discounts REAL NOT NULL DEFAULT 0,
        revenue REAL NOT NULL DEFAULT 0,
        returns REAL NOT NULL DEFAULT 0,
        orders INTEGER NOT NULL DEFAULT 0,
        currency TEXT,
        timezone TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, date)
    )
    """,
    # Which dates have been fetched already (so a backfill is incremental, never re-pulling).
    """
    CREATE TABLE IF NOT EXISTS fin_fetched_dates (
        store_key TEXT NOT NULL,
        date TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (store_key, date)
    )
    """,
    # Per-ORDER facts, captured in the SAME sync_daily pass that fills fin_shopify_daily.
    # Exists ONLY to let the read path re-bucket revenue into an operator-chosen reporting TZ
    # (BKK/CET/NY) using created_at_utc — the daily rows are pre-bucketed in the STORE tz and
    # can't be re-grouped. `subtotal_shop` is currentSubtotalPriceSet in the SHOP currency: it is
    # exactly what fin_shopify_daily.revenue sums (revenue = Σ subtotal), so a rebucket at the
    # store's own tz reproduces the daily number to the cent. store_date = the store-local bucket
    # date the daily aggregate assigned this order to (parity + a NULL-created_at_utc fallback).
    """
    CREATE TABLE IF NOT EXISTS fin_order_facts (
        store_key TEXT NOT NULL,
        order_id TEXT NOT NULL,
        created_at_utc TEXT,
        store_date TEXT NOT NULL,
        subtotal_shop REAL NOT NULL DEFAULT 0,
        currency TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, order_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fin_order_facts_utc ON fin_order_facts (store_key, created_at_utc)",
    # Per-day manual overrides (operator-entered cog / fees + a note). calcPL's HIGHEST-precedence
    # COGS source (manual wins over invoice + formula). Replaces pl-dashboard's manual-entries.json.
    """
    CREATE TABLE IF NOT EXISTS fin_manual_entries (
        store_key TEXT NOT NULL,
        date TEXT NOT NULL,
        cog REAL,
        fees REAL,
        note TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, date)
    )
    """,
    # Daily ad spend per store+date+source (Google today; extensible). Ingested by the operator's
    # Google Ads Apps Script POST (authed by a per-store script key). Replaces google-ads.json.
    """
    CREATE TABLE IF NOT EXISTS fin_ad_spend (
        store_key TEXT NOT NULL,
        date TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'google',
        amount REAL NOT NULL DEFAULT 0,
        currency TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, date, source)
    )
    """,
    # PER-PRODUCT daily Google Ads metrics (shopping_performance_view), pushed by the SAME
    # Google Ads Script that pushes daily spend — no OAuth/API needed. item_id is the Merchant
    # Center offer id (Shopify's channel emits shopify_{CC}_{product}_{variant}); product_id is
    # the parsed Shopify product id the optimization snapshot joins on. Growth note (scale
    # memory): numeric-only rows, upserted in place by the script's lookback window — at 100
    # stores × ~500 advertised products this stays a few-hundred-MB/year class table.
    """
    CREATE TABLE IF NOT EXISTS fin_ads_product_daily (
        store_key TEXT NOT NULL,
        date TEXT NOT NULL,
        item_id TEXT NOT NULL,
        product_id TEXT,
        cost REAL NOT NULL DEFAULT 0,
        clicks INTEGER NOT NULL DEFAULT 0,
        impressions INTEGER NOT NULL DEFAULT 0,
        conversions REAL NOT NULL DEFAULT 0,
        conv_value REAL NOT NULL DEFAULT 0,
        currency TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, date, item_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fin_ads_product ON fin_ads_product_daily (store_key, product_id, date)",
    # FX cache — rates are FROZEN once fetched (Frankfurter/ECB, no credential). calcPL converts
    # Shopify + ad currencies to the store's reporting currency at the day's frozen rate.
    """
    CREATE TABLE IF NOT EXISTS fin_fx_rates (
        base TEXT NOT NULL,
        quote TEXT NOT NULL,
        date TEXT NOT NULL,
        rate REAL NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (base, quote, date)
    )
    """,
    # cogs_version — the ONE control-plane stamp that closes the split gap. PM bumps it on every
    # invoice_lines write; Finance keys its calcPL cache off it and self-invalidates on read, so
    # P&L never serves stale profit after an invoice upload — the PM→P&L "streams as now" coupling,
    # preserved across two standalone apps sharing this single DB.
    """
    CREATE TABLE IF NOT EXISTS fin_cogs_version (
        store_key TEXT PRIMARY KEY,
        version INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    # Per-store generated ad-script key (auto-generated, shown read-only in Finance so the operator
    # can paste it into their Google Ads Apps Script). Not a secret the operator types — generated.
    """
    CREATE TABLE IF NOT EXISTS fin_ad_script (
        store_key TEXT PRIMARY KEY,
        script_key TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # ── PRODUCT MANAGEMENT (native port; owns the cost tables Finance reads) ────────────────
    # Supplier invoices (one row per uploaded invoice document).
    """
    CREATE TABLE IF NOT EXISTS pm_supplier_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_key TEXT NOT NULL,
        filename TEXT,
        supplier TEXT,
        invoice_no TEXT,
        total_eur REAL,
        currency TEXT,
        uploaded_at TEXT NOT NULL,
        raw TEXT
    )
    """,
    # Invoice line items — the invoice-based COGS source calcPL SUMs by order_date. line_type
    # 'charge' = a cost, 'refund' = a supplier credit. This is the PM-owned table P&L READS (never
    # writes); the read is unidirectional, the only PM→P&L push is the cogs_version bump above.
    """
    CREATE TABLE IF NOT EXISTS pm_invoice_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_key TEXT NOT NULL,
        invoice_id INTEGER NOT NULL,
        order_no TEXT,
        order_date TEXT,
        sku TEXT,
        title TEXT,
        qty INTEGER NOT NULL DEFAULT 1,
        line_type TEXT NOT NULL DEFAULT 'charge',
        bill_cost_eur REAL NOT NULL DEFAULT 0,
        refund_amount_eur REAL NOT NULL DEFAULT 0,
        country TEXT,
        resolve_status TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pm_invoice_lines_date ON pm_invoice_lines (store_key, order_date)",
    "CREATE INDEX IF NOT EXISTS idx_pm_invoice_lines_invoice ON pm_invoice_lines (invoice_id)",
    # Per-market cost override (store × variant × country). PM-owned; feeds per-order margins.
    """
    CREATE TABLE IF NOT EXISTS pm_product_costs_market (
        store_key TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        country TEXT NOT NULL DEFAULT '',
        cost_override_eur REAL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, variant_id, country)
    )
    """,
    # Store-wide (country-agnostic) rolling landed cost per variant/SKU — the fallback the
    # margin tabs use when there's no per-market row. This is what makes CJdropshipping +
    # machine-format invoices (no ?variant= Link, no per-country cost) actually establish a
    # COGS: every charge line updates a running mean keyed by SKU (with variant_id when known).
    """
    CREATE TABLE IF NOT EXISTS pm_product_costs (
        store_key TEXT NOT NULL,
        variant_sku TEXT NOT NULL,
        variant_id TEXT,
        product_id TEXT,
        last_cost_eur REAL,
        avg_cost_eur REAL,
        sample_count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, variant_sku)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pm_product_costs_vid ON pm_product_costs (store_key, variant_id)",
    # Per-order Shopify sync (PM's OWN order-level path, distinct from Finance's daily aggregate).
    # created_at_utc kept so orders can be re-bucketed to the store timezone if needed.
    """
    CREATE TABLE IF NOT EXISTS pm_shopify_orders (
        store_key TEXT NOT NULL,
        order_id TEXT NOT NULL,
        order_no TEXT,
        created_at_utc TEXT,
        order_date TEXT,
        total REAL,
        currency TEXT,
        financial_status TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, order_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pm_shopify_order_variants (
        store_key TEXT NOT NULL,
        order_id TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        sku TEXT,
        title TEXT,
        qty INTEGER NOT NULL DEFAULT 1,
        price REAL,
        PRIMARY KEY (store_key, order_id, variant_id)
    )
    """,
    # Variant catalog cache (title / sku / price per variant) — the margin view joins order
    # variants to this for a readable per-product cost/price rollup.
    """
    CREATE TABLE IF NOT EXISTS pm_variants_cache (
        store_key TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        product_id TEXT,
        sku TEXT,
        title TEXT,
        price REAL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, variant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pm_product_status (
        store_key TEXT NOT NULL,
        product_id TEXT NOT NULL,
        status TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, product_id)
    )
    """,
    # Product Performance per-product flags: Exclude (hide from the optimization view + KPIs) and
    # a free-text Note. Server-persisted (was localStorage) so they sync across devices and the
    # Automation engine can read them. product_id stored as the numeric tail.
    """
    CREATE TABLE IF NOT EXISTS pm_optimization_flags (
        store_key TEXT NOT NULL,
        product_id TEXT NOT NULL,
        hidden INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, product_id)
    )
    """,
    # Automation rules for the Product Performance page: "when <conditions> on <window/scope>
    # → <action>". conditions + action are JSON. ANALYSIS-first — the engine reports which
    # products WOULD trigger; it never auto-executes (the operator applies).
    """
    CREATE TABLE IF NOT EXISTS pm_automation_rules (
        store_key TEXT NOT NULL,
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        win TEXT NOT NULL DEFAULT '30',
        scope TEXT NOT NULL DEFAULT 'total',
        conditions TEXT NOT NULL,
        action TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # Global on/off master switch for a store's automation (one row per store).
    """
    CREATE TABLE IF NOT EXISTS pm_automation_settings (
        store_key TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
    # Automation activity log — what the automation actually DID: which product/market, which rule,
    # which action, and the result (applied / flagged / failed). The "overview log what happened".
    """
    CREATE TABLE IF NOT EXISTS pm_automation_log (
        store_key TEXT NOT NULL,
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        rule_name TEXT,
        product_id TEXT,
        product_title TEXT,
        market TEXT,
        action TEXT NOT NULL,
        detail TEXT,
        result TEXT NOT NULL,
        vals TEXT
    )
    """,
    # Product Performance mutation history — every manual optimization mutation (status change,
    # exclude toggle, note edit, tag change) so the operator can see WHAT changed and REVERT it.
    # old_val/new_val are stored as text (JSON for tags). Pythago's per-row "history" icon.
    """
    CREATE TABLE IF NOT EXISTS pm_optimization_history (
        store_key TEXT NOT NULL,
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        product_id TEXT NOT NULL,
        product_title TEXT,
        field TEXT NOT NULL,
        old_val TEXT,
        new_val TEXT,
        label TEXT,
        reverted INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pm_opt_history ON pm_optimization_history (store_key, product_id, id)",
    # Saved filter presets for the Product Performance page — server-side (was localStorage) so they
    # persist across devices and are shared. state is the JSON filter blob. PK (store, name) → upsert.
    """
    CREATE TABLE IF NOT EXISTS pm_saved_filters (
        store_key TEXT NOT NULL,
        name TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (store_key, name)
    )
    """,
    # ── MARKET PRICE-PUSH SUITE (faithful port of pl-dashboard's catalog/price-push tables) ──
    # Per-market variant prices from Shopify priceLists + the drift snapshot written on every
    # successful push (last_pushed_*): the locked FX + the landed cost the price was set
    # against, so the Margin Review can flag "cost moved >=15% since this price was pushed".
    """
    CREATE TABLE IF NOT EXISTS pm_catalog_prices_cache (
        store_key TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        catalog_id TEXT NOT NULL,
        catalog_title TEXT,
        market_id TEXT,
        market_name TEXT,
        country_code TEXT,
        price_amount REAL,
        price_currency TEXT,
        price_list_id TEXT,
        last_pushed_at TEXT,
        last_pushed_local REAL,
        last_pushed_eur REAL,
        last_pushed_fx_rate REAL,
        last_pushed_fx_date TEXT,
        last_pushed_compare_local REAL,
        last_pushed_cost_eur REAL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, variant_id, catalog_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pm_catalog_prices_store ON pm_catalog_prices_cache (store_key, variant_id)",
    """
    CREATE TABLE IF NOT EXISTS pm_primary_markets_cache (
        store_key TEXT NOT NULL,
        market_id TEXT NOT NULL,
        market_name TEXT,
        country_code TEXT,
        currency TEXT,
        catalog_id TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, market_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pm_catalog_publications (
        store_key TEXT NOT NULL,
        catalog_id TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        product_id TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_key, catalog_id, variant_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pm_catalog_pubs_variant ON pm_catalog_publications (store_key, variant_id)",
    # Push audit trail (DB-backed so it survives restarts, unlike the source app's in-memory
    # ring buffer). Capped by the writer at ~500 rows per sweep.
    """
    CREATE TABLE IF NOT EXISTS pm_push_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        store_key TEXT,
        level TEXT NOT NULL DEFAULT 'info',
        msg TEXT NOT NULL
    )
    """,
    # Bulk-push job state (DB-backed; a restart orphan-fails running jobs at startup).
    """
    CREATE TABLE IF NOT EXISTS pm_push_jobs (
        job_id TEXT PRIMARY KEY,
        store_key TEXT NOT NULL,
        status TEXT NOT NULL,
        total INTEGER NOT NULL DEFAULT 0,
        current_index INTEGER NOT NULL DEFAULT 0,
        current_label TEXT,
        counts TEXT,
        error TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        started_at TEXT,
        finished_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # ── ORDERS & ISSUES (native port of NN Operations' dispute-management "run" half) ────────
    # Faithful port of NN's dm_* schema. Two dialect adaptations vs the better-sqlite3 original:
    #   * every `DEFAULT (datetime('now'))` is dropped — the module fills created_at/updated_at
    #     via a Python _now() (SQLite-only datetime() defaults don't translate to Postgres).
    #   * `id INTEGER PRIMARY KEY AUTOINCREMENT` stays (→ SERIAL on Neon); composite PKs unchanged.
    # Orders mirrored from Shopify (the operator picks a real order to open a dispute against).
    """
    CREATE TABLE IF NOT EXISTS dm_orders (
        order_id TEXT PRIMARY KEY,
        order_number TEXT,
        order_date TEXT,
        customer_name TEXT,
        customer_email TEXT,
        currency TEXT,
        total REAL,
        financial_status TEXT,
        fulfillment_status TEXT,
        item_count INTEGER NOT NULL DEFAULT 0,
        store_key TEXT,
        shipping_address TEXT,
        tracking_number TEXT,
        tracking_url TEXT,
        tracking_company TEXT,
        details_json TEXT,
        cancelled_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dm_order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        product_id TEXT,
        title TEXT,
        sku TEXT,
        qty INTEGER NOT NULL DEFAULT 1,
        price REAL,
        variant_id TEXT,
        variant_title TEXT,
        image_url TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dm_order_items_order ON dm_order_items (order_id)",
    # A dispute — the core issue record. scope whole|partial; status per its source's status set.
    """
    CREATE TABLE IF NOT EXISTS dm_disputes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT 'whole',
        status TEXT NOT NULL DEFAULT 'investigating',
        issue_category TEXT,
        issue_location TEXT,
        description TEXT,
        next_steps TEXT,
        supplier_response TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status_since TEXT,
        assignee TEXT,
        supplier_name TEXT,
        resolution_type TEXT,
        resolution_amount REAL,
        resolution_currency TEXT,
        resolved_at TEXT,
        source TEXT NOT NULL DEFAULT 'customer',
        cs_link TEXT,
        reminder_at TEXT,
        reminder_note TEXT,
        priority TEXT,
        supplier_action TEXT,
        over8_status TEXT,
        from_parcel INTEGER NOT NULL DEFAULT 0,
        product_related INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dm_disputes_order ON dm_disputes (order_id)",
    "CREATE INDEX IF NOT EXISTS idx_dm_disputes_status ON dm_disputes (status, id)",
    # Which covered order items a partial dispute is about (+ per-item solution / listing flag).
    """
    CREATE TABLE IF NOT EXISTS dm_dispute_items (
        dispute_id INTEGER NOT NULL,
        order_item_id INTEGER NOT NULL,
        solution TEXT,
        listing_updated TEXT,
        product_related INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (dispute_id, order_item_id)
    )
    """,
    # Append-only event log per dispute (created / status_change / note).
    """
    CREATE TABLE IF NOT EXISTS dm_dispute_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_id INTEGER NOT NULL,
        ts TEXT NOT NULL,
        author TEXT,
        kind TEXT NOT NULL DEFAULT 'note',
        text TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dm_dispute_events_dispute ON dm_dispute_events (dispute_id, id)",
    # Planned 3-step customer contact ladder (seq 1|2|3) — the follow-up cadence.
    """
    CREATE TABLE IF NOT EXISTS dm_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_id INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        planned_at TEXT,
        sent INTEGER NOT NULL DEFAULT 0,
        sent_at TEXT,
        channel TEXT,
        note TEXT,
        UNIQUE (dispute_id, seq)
    )
    """,
    # Refund ledger — issued refunds (independent CRUD surface + linked to a dispute/order).
    """
    CREATE TABLE IF NOT EXISTS dm_refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_id INTEGER,
        order_id TEXT,
        store_key TEXT,
        order_number TEXT,
        refund_date TEXT,
        product TEXT,
        refund_pct REAL,
        currency TEXT,
        order_amount REAL,
        refund_amount REAL,
        amount_usd REAL,
        cogs_recovered REAL,
        reason TEXT,
        processed_by TEXT,
        notes TEXT,
        supplier_refunded INTEGER NOT NULL DEFAULT 0,
        ticket_link TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dm_refunds_dispute ON dm_refunds (dispute_id)",
    # Per-dispute "seen" marker (unread badge in the queue).
    """
    CREATE TABLE IF NOT EXISTS dm_dispute_seen (
        dispute_id INTEGER PRIMARY KEY,
        seen_at TEXT NOT NULL
    )
    """,
    # Parcel tracking cache (ParcelPanel sync — external sync is a deferred manual job; table
    # holds whatever has been synced so the queue + create-issue-from-parcel path works).
    """
    CREATE TABLE IF NOT EXISTS dm_parcels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store TEXT NOT NULL,
        order_id TEXT,
        order_number TEXT,
        tracking_number TEXT,
        carrier TEXT,
        status TEXT,
        status_label TEXT,
        substatus TEXT,
        customer_name TEXT,
        customer_email TEXT,
        country TEXT,
        last_checkpoint TEXT,
        last_checkpoint_at TEXT,
        delivery_date TEXT,
        days_in_transit INTEGER,
        checkpoints_json TEXT,
        raw_json TEXT,
        handled INTEGER NOT NULL DEFAULT 0,
        order_date TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE (store, tracking_number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dm_parcels_store ON dm_parcels (store, handled)",
    # Reusable response templates (team-wide) — {order}/{customer}/{supplier} placeholders.
    """
    CREATE TABLE IF NOT EXISTS dm_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        body TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # Sticky notes (per-owner scratchpad).
    """
    CREATE TABLE IF NOT EXISTS dm_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT,
        body TEXT,
        color TEXT DEFAULT 'yellow',
        pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ── TASKS & GAMEPLANS (native port of NN Operations' tasks-app) ─────────────────────────
    # Operational to-dos (optionally per-store) + a lightweight assignee/list model.
    """
    CREATE TABLE IF NOT EXISTS tk_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        detail TEXT,
        store_key TEXT,
        list_id INTEGER,
        assignee TEXT,
        status TEXT NOT NULL DEFAULT 'todo',
        priority TEXT,
        due_at TEXT,
        done_at TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_tasks_status ON tk_tasks (status, id)",
    "CREATE INDEX IF NOT EXISTS idx_tk_tasks_store ON tk_tasks (store_key, status)",
    """
    CREATE TABLE IF NOT EXISTS tk_lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        color TEXT,
        sort INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    # Per-store gameplan — a free-form "what to do next on this store" doc, versioned by updated_at.
    """
    CREATE TABLE IF NOT EXISTS tk_store_plans (
        store_key TEXT PRIMARY KEY,
        body TEXT,
        updated_by TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    # Company P&L manual OpEx — the owner-level costs the store feed never sees (salaries, agency
    # fees, bank charges, one-time). Native replacement for the external company_pl.db's
    # manual_pl_entries: store-derived rows (revenue/COGS/ad-spend/fees) are computed from the
    # app's own finance tables; these manual rows layer on top to reach true company net profit.
    # Composite PK (year, month, slug) → portable upsert, no auto-id.
    """
    CREATE TABLE IF NOT EXISTS company_pl_manual (
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        slug TEXT NOT NULL,
        name TEXT NOT NULL,
        amount_eur REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (year, month, slug)
    )
    """,
    # ── Scale indexes (2026-07-10 audit): the hot group-by / join / filter columns on the PM +
    # market-push read paths that were doing full-store scans at high order/variant volume. All
    # composite, portable (sqlite + postgres), IF NOT EXISTS. See the parity/scale audit notes.
    "CREATE INDEX IF NOT EXISTS idx_pm_invoice_lines_orderno ON pm_invoice_lines (store_key, order_no)",
    "CREATE INDEX IF NOT EXISTS idx_pm_invoice_lines_sku ON pm_invoice_lines (store_key, sku)",
    "CREATE INDEX IF NOT EXISTS idx_pm_sov_variant ON pm_shopify_order_variants (store_key, variant_id)",
    "CREATE INDEX IF NOT EXISTS idx_pm_sov_order ON pm_shopify_order_variants (store_key, order_id)",
    "CREATE INDEX IF NOT EXISTS idx_pm_shopify_orders_date ON pm_shopify_orders (store_key, order_date)",
    "CREATE INDEX IF NOT EXISTS idx_pm_variants_cache_product ON pm_variants_cache (store_key, product_id)",
    "CREATE INDEX IF NOT EXISTS idx_pm_catalog_prices_pl ON pm_catalog_prices_cache (store_key, price_list_id)",
    "CREATE INDEX IF NOT EXISTS idx_pm_costs_market_upd ON pm_product_costs_market (store_key, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_pm_push_log_store ON pm_push_log (store_key, id)",
)

# Idempotent column additions to EXISTING tables (the CREATE TABLE IF NOT EXISTS statements
# above only apply to fresh DBs). Declared as (table, column, coltype) so we can CHECK for the
# column first (dialect-aware) rather than relying on ALTER's error semantics — Postgres aborts
# the whole transaction on a duplicate-column error, which silently swallowed the real ADD.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # #2 full-variant sync: live Shopify status + ghost-variant soft flags on the variant cache.
    ("pm_variants_cache", "status", "TEXT"),
    ("pm_variants_cache", "missing_in_shopify_at", "TEXT"),
    ("pm_variants_cache", "missing_source", "TEXT"),
    # #1 cost backfill: keep the variant_id an invoice line resolved to (from the ?variant= Link)
    # so per-market cost + the store-wide backfill can key on it, not just the SKU.
    ("pm_invoice_lines", "variant_id", "TEXT"),
    # Ad-spend provenance: WHICH Google Ads account pushed the row. A script pasted with the
    # wrong store key (account A posting as store B) silently overwrites B's real spend hourly —
    # observed live on celzoir (received decorsdeluxe's spend). The ingest warns when the
    # pushing account for a (store, date, source) row CHANGES.
    ("fin_ad_spend", "pushed_by", "TEXT"),
    # Invoice idempotency: sha256 of the uploaded file bytes. A byte-identical re-upload
    # (double-click / "did it go through?" retry) is the true duplicate; the old filename+total
    # heuristic false-positived two different invoices that shared a default name and a close total.
    ("pm_supplier_invoices", "content_hash", "TEXT"),
    # Product Performance per-row tags: app-side organizational tags (JSON array), merged with the
    # product's Shopify tags in the optimization view. Powers the tag filter + automation scoping.
    ("pm_optimization_flags", "tags", "TEXT"),
)

# Indexes that reference a MIGRATION-added column — must be created AFTER _apply_migrations, never
# inside _SCHEMA (on an existing DB the column doesn't exist yet when _SCHEMA runs, and on Postgres
# one failed statement aborts the whole schema transaction). Idempotent; run every init.
_POST_MIGRATION_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_pm_invoices_hash ON pm_supplier_invoices (store_key, content_hash)",
)


def _existing_columns(conn, table: str) -> set[str]:
    """Column names of `table` — via PRAGMA on SQLite, information_schema on Postgres."""
    try:
        if backend() == "postgres":
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                (table,),
            ).fetchall()
            return {r["column_name"] for r in rows}
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}
    except Exception:  # noqa: BLE001 — table may not exist yet; caller treats as empty
        return set()


def _apply_migrations(conn) -> None:
    """Add each declared column only when it's genuinely absent. Idempotent + backend-safe:
    no ALTER ever runs against an existing column, so Postgres never aborts the transaction."""
    by_table: dict[str, set[str]] = {}
    for table, col, coltype in _MIGRATIONS:
        cols = by_table.get(table)
        if cols is None:
            cols = by_table[table] = _existing_columns(conn, table)
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            cols.add(col)


def backend() -> str:
    """Which store we're talking to: 'postgres' when DATABASE_URL is set, else 'sqlite'."""
    return "postgres" if os.environ.get("DATABASE_URL") else "sqlite"


# ---------------------------------------------------------------------------
# Postgres adapter — the SINGLE swap point promised in the module docstring.
#
# Design goal: NOTHING upstream changes. Every runlog/jobs helper keeps calling
# `db.connect()` and then `conn.execute("… ? …", params)`, reading `cur.lastrowid`
# / `cur.rowcount`, doing `dict(row)` / `row["col"]`, and relying on `with conn:`
# to commit. To make psycopg behave exactly like the tuned sqlite3 connection we
# wrap it in three tiny shims below that translate the differences:
#   * `?`  -> `%s`                         (placeholder style)
#   * INSERT … (no ON CONFLICT) gets a `RETURNING id` appended so `.lastrowid` works
#   * dict rows                            (psycopg `dict_row`)
#   * `with conn:` commits on success / rolls back on error, then returns the
#     connection to a small pool (the helpers never .close(), so the context
#     manager is the release hook — this is also what keeps Neon connection use low)
#   * bare BEGIN/COMMIT/ROLLBACK strings   (only job_claim_next issues these)
#
# Concurrency note: SQLite's `BEGIN IMMEDIATE` row-lock in job_claim_next becomes a
# plain transaction here. The operator app runs a SINGLE in-process worker, so two
# workers never race for the same job; if a true multi-worker Postgres deploy is
# added later, add `FOR UPDATE SKIP LOCKED` to that one SELECT. Documented, not a
# silent gap.
# ---------------------------------------------------------------------------
_pg_pool = None
_pg_schema_ready = False


def _translate_sql(sql: str) -> tuple[str, bool]:
    """sqlite SQL -> postgres SQL. Returns (sql, wants_returning_id).

    Our placeholders are always `?` and never appear inside string literals, and the
    only `%` we'd ever emit is the translated placeholder, so a straight replace is safe.
    INSERTs that aren't upserts get `RETURNING id` so the wrapper can expose `.lastrowid`
    exactly like sqlite3 — every such table has an `id SERIAL` column."""
    out = sql.replace("?", "%s")
    head = out.lstrip().upper()
    wants_id = (
        head.startswith("INSERT")
        and "ON CONFLICT" not in head
        and "RETURNING" not in head
    )
    if wants_id:
        out = out.rstrip().rstrip(";") + " RETURNING id"
    return out, wants_id


class _PgResult:
    """Mimics the bits of sqlite3.Cursor the helpers use: fetchone/fetchall (dict rows),
    lastrowid, rowcount."""

    __slots__ = ("_cur", "lastrowid")

    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount if self._cur is not None else 0

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _PgConn:
    """Wraps a pooled psycopg connection to look like the tuned sqlite3 connection.

    Helpers use `with db.connect() as conn:` (commit on exit, never .close()) and
    job_claim_next uses `conn = db.connect(); try: … finally: conn.close()`. Both paths
    release the underlying connection back to the pool here, so connection use stays at
    most `max_size` — important on Neon's free connection budget."""

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()
        self._released = False
        self.isolation_level = None  # accepted + ignored (job_claim_next sets it)

    def execute(self, sql: str, params=()):
        stripped = sql.strip().upper().rstrip(";")
        # Bare transaction control — only job_claim_next issues these. psycopg manages the
        # transaction implicitly (autocommit off), so BEGIN is a no-op and COMMIT/ROLLBACK
        # map to the connection methods.
        if stripped in ("BEGIN", "BEGIN IMMEDIATE", "BEGIN IMMEDIATE TRANSACTION", "BEGIN TRANSACTION"):
            return _PgResult(None)
        if stripped == "COMMIT":
            self._conn.commit()
            return _PgResult(None)
        if stripped == "ROLLBACK":
            self._conn.rollback()
            return _PgResult(None)
        sql2, wants_id = _translate_sql(sql)
        cur = self._conn.cursor()
        cur.execute(sql2, tuple(params) if params else ())
        res = _PgResult(cur)
        if wants_id:
            row = cur.fetchone()
            res.lastrowid = row["id"] if row else None
        return res

    def _release(self, commit: bool) -> None:
        if self._released:
            return
        try:
            if commit:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._pool.putconn(self._conn)
            self._released = True

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        # job_claim_next's finally — commit anything pending and return to the pool.
        self._release(commit=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._release(commit=exc_type is None)
        return False


def _ensure_pg_schema(pool) -> None:
    """Create the schema on Postgres once. Same ANSI tables as SQLite; the only dialect
    fix is `INTEGER PRIMARY KEY AUTOINCREMENT` -> `SERIAL PRIMARY KEY` (everything else —
    TEXT/INTEGER, CHECK, ON CONFLICT, CREATE … IF NOT EXISTS — is already valid Postgres).
    Only ever called from within `_get_pg_pool`'s lock, so it doesn't re-lock (the module
    lock is not reentrant)."""
    global _pg_schema_ready
    if _pg_schema_ready:
        return
    with pool.connection() as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
        conn.commit()
        # Column adds run against a clean (committed) transaction and are existence-checked,
        # so a re-run never raises — no swallowed ALTER, no aborted transaction.
        _apply_migrations_pg(conn)
        conn.commit()
        # Indexes on migration-added columns run only now that the columns exist + are committed.
        for stmt in _POST_MIGRATION_INDEXES:
            conn.execute(stmt)
        conn.commit()
    _pg_schema_ready = True


def _apply_migrations_pg(conn) -> None:
    """Postgres column adds — check information_schema directly (the wrapped conn's `backend()`
    is postgres here)."""
    for table, col, coltype in _MIGRATIONS:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
            (table, col),
        ).fetchall()
        if not rows:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            conn.commit()


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        with _init_lock:
            if _pg_pool is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool

                pool = ConnectionPool(
                    os.environ["DATABASE_URL"],
                    min_size=1,
                    max_size=int(os.environ.get("DB_POOL_MAX", "5")),
                    kwargs={"row_factory": dict_row, "autocommit": False},
                    open=True,
                )
                _ensure_pg_schema(pool)
                _pg_pool = pool
    return _pg_pool


def db_path() -> str:
    return str(_DB_PATH)


def _connect_sqlite() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Concurrency tuning — the whole point of this seam for an always-on worker.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    """Create the schema once (idempotent). Safe to call at startup and lazily."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = _connect_sqlite()
        try:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            _apply_migrations(conn)
            for stmt in _POST_MIGRATION_INDEXES:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def connect() -> sqlite3.Connection:
    """Open a ready-to-use connection. Schema is ensured on first use, so every helper
    can call this standalone — exactly the simple interface the worker needs."""
    if backend() == "postgres":
        # >>> SINGLE SWAP POINT for the 24/7 worker — now wired (see adapter above). <<<
        # A pooled psycopg connection wrapped to be byte-for-byte API-compatible with the
        # tuned sqlite3 connection every helper expects. No helper signature changes.
        return _PgConn(_get_pg_pool())
    init()
    return _connect_sqlite()
