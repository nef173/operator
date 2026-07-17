"""Read-only access to the Google Stores pipeline outputs on disk.

Every function is defensive: a missing file or directory yields an empty result
rather than raising, so the dashboard degrades gracefully when a part of the
pipeline has not produced output yet.

Data shapes (confirmed against live files):
  general-stores/<store>/listing-queue.json
      { store, created, updated,
        categories: { <slug>: { keyword, sv, capture_bucket, state, recon,
                                skus: { <sku>: { title, cogs, price, state, created } } } } }
  general-stores/<store>/candidate-queue.json   (discovery side; may not exist)
  dossiers/<slug>/dossier.md (+ supporting json)
  05-launch-niche-store/niche-launches/<slug>/
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, pricing, runlog, translate

# Pass-through add-ons (shipping protection / gift wrap / warranty) — not real catalog products.
# Inlined here (was product_mgmt._is_excluded_title) so the store-view filter stands alone in the
# lean build. Same canonical list the full build applied.
_EXCLUDED_ADDON_PATTERNS = (
    "shipping protection", "order protection", "route protection", "package protection", "seel",
    "versandschutz", "versandversicherung", "paketschutz", "protection colis", "protection envoi",
    "protezione spedizione", "protección envío", "seguro envío", "proteção envio",
    "verzendbescherming", "transportverzekering", "verzekerde verzending",
    "insured shipping", "shipping insurance", "assurance livraison", "assurance colis",
    "gift wrap", "gift wrapping", "giftwrap", "geschenkverpackung", "geschenkpapier",
    "emballage cadeau", "papier cadeau", "confezione regalo", "envoltorio regalo",
    "embalaje regalo", "papel regalo", "embrulho presente", "cadeauverpakking",
    "covervault", "cover vault",
)


def _is_excluded_addon_title(title: str | None) -> bool:
    """True if `title` is a pass-through add-on (shipping protection / gift wrap / warranty)."""
    t = (title or "").lower()
    return any(p in t for p in _EXCLUDED_ADDON_PATTERNS)


# Canonical state machine (mirrors 06-launch-general-store/scripts/listing_queue.py)
SKU_STATES = [
    "candidate", "keyword-clustered", "drafted", "live", "testing", "winner", "killed",
]
CAPTURE_BUCKETS = ["BREAKOUT", "LIST-NOW", "BUILD-AHEAD", "EVERGREEN", "SKIP"]

# Two distinct ways to list a product. The operator picks one in the "New Listing"
# selector; each runs a different engine. Kept here so the selector is backend-driven
# (one source of truth) rather than hard-coded in the UI.
LISTING_METHODS = [
    {
        "id": "source-import",
        "name": "General Listing",
        "tagline": "Re-skins one supplier product onto your store. KEPT: the product, its variants & specs. CHANGED: title, description, brand images, price — saved as a draft.",
        "engine": "google-stores",
        "job_spec": "source-import",
        # The "normal listing" path runs in the standalone listing app (same project's listing
        # engine). It is EMBEDDED below in-app (iframe), NOT opened as a new tab to a separate
        # deployment — it owns the per-step view incl. the "fix bad images" image-QA step the
        # operator validates. Suppresses the in-app job runner for this method. Swap the URL to
        # our own listing-app deployment when it's stood up. The two apps share data later.
        "external_app": "https://productlisting.up.railway.app/",
        "external_note": "Loads the listing app below — walk the steps here (incl. \u201cfix bad images\u201d). The Branded Listing below stays in-app.",
        "best_for": "Take one product you already found (an AliExpress / Temu / Shopify link) "
                    "and put it on your store as your own listing. What STAYS the same as the "
                    "supplier: the actual product, its variants (colour/size) and its specs. What "
                    "we CHANGE to make it yours: a keyword-first title (no brand name), a clean "
                    "template description, your-brand product photos, and a price set to your "
                    "margin. Fast and template-based — no deep per-product copywriting. The "
                    "classic Google-Shopping dropship listing.",
        "inputs": ["Source product URL", "Destination store"],
        "steps": [
            "Scrape the source (variants, images, price, specs)",
            "Keyword-stacked title (head keyword first, comma-separated, no brand)",
            "Basic Google-dropship description template (specs + trust strip — no AI prose)",
            "Price to margin (COGS × tier → .99 charm)",
            "AI brand images from the supplier photos (clean Google hero + gallery)",
            "Create its own Shopify product page — DRAFT first",
        ],
    },
    {
        "id": "research-pipeline",
        "name": "Branded Listing",
        "tagline": "Research-driven build, not a copy. KEPT: the supplier's real product shape & variants. BUILT FRESH: keyword title, full branded gallery, complete PDP, your price — a draft.",
        "engine": "google-stores",
        "job_spec": "build-listing",
        "best_for": "Start from a keyword/candidate the research found and build a richer, fully "
                    "branded listing (or a whole multi-product batch). What STAYS true to the "
                    "supplier: the real product, its shape and its variants — every image is "
                    "grounded in the supplier's own photos so the customer gets what they order. "
                    "What we BUILD FRESH: a keyword-first title, a full custom image gallery, a "
                    "complete product page (story / features / specs / FAQs), and a price to your "
                    "margin. Heavier than General Listing — this is the native Google Stores way for "
                    "products you want to invest in.",
        "inputs": [
            "Candidate / keyword from the queue OR a single product link",
            "Destination store",
        ],
        "steps": [
            "Pick a candidate / keyword cluster",
            "Lock supplier + save reference photos",
            "Generate the gallery (supplier-grounded, draft-first)",
            "Create DRAFT product + metafield PDP",
            "Verify, then operator go-live",
        ],
    },
]


# Ways to START a research run, grouped by surface (keyword / niche / product).
# Each surface has its own page in the UI; the cards mirror LISTING_METHODS so the
# "get started" selector is backend-driven (one source of truth) and grounded in the
# real pipeline slash-commands.
RESEARCH_METHODS = {
    # The keyword surface has ONE real way to FIND keywords (the Discovery Funnel) plus a
    # SUPPORT tool that deepens a keyword you already have. `role` keeps them visually
    # separate so the funnel isn't mistaken for "just one of two equal methods".
    "keyword": [
        {
            "id": "general-store-research",
            "name": "Discovery Funnel",
            "role": "discovery",
            "tagline": "Finds product keywords people are searching for and ranks the best ones.",
            "engine": "/general-store-research",
            "job_spec": "keyword-discovery",
            "best_for": "Finding what to list next. Start three ways: type a keyword, let it pick "
                        "what's hot right now (no keyword), or use one passed over from Trend Research.",
            "inputs": [
                "Type a keyword or topic, OR",
                "Leave it blank to let it pick what's trending now, OR",
                "A keyword handed over from Trend Research",
                "(optional) which store",
            ],
            "steps": [
                "Start from your keyword — or leave it blank to let it pick what's hot now",
                "Keep only the ones with enough demand",
                "Double-check them against real stores and marketplaces",
                "Rank them and drop duplicates",
                "Add the winners to your shortlist",
            ],
        },
        {
            "id": "competitor-shopping-scan",
            "name": "Google Shopping Scan",
            "role": "support",
            "tagline": "Supplementary: deepens one keyword by reading its live Google Shopping page "
                       "— real prices, titles, and top sellers.",
            "engine": "/competitor-shopping-scan",
            "job_spec": "shopping-scan",
            "best_for": "Not a way to find new keywords — a support pass that adds market detail to a "
                        "keyword you're already researching (prices, titles, sellers, category rows).",
            "inputs": ["A keyword you're already researching"],
            "steps": [
                "Read the live Google Shopping landscape for your keyword",
                "See the product types and category rows people search",
                "Note the usual prices and discounts",
                "Spot the title and image styles that work",
                "Save it as supporting research for your listings",
            ],
        },
    ],
    "niche": [
        {
            "id": "discover-niches",
            "name": "Keyword-First Scout",
            "tagline": "Researches one niche end to end and gives a clear go / wait / skip.",
            "engine": "/discover-niches",
            "job_spec": "discover-niches",
            "best_for": "A deep look at one niche — the landscape, the competitors, and a clear verdict.",
            "inputs": ["A niche, a direction, or nothing"],
            "steps": [
                "Pick the niche",
                "Check how much people search for it",
                "See how crowded it is",
                "Read which way the trend is going",
                "Give a go / wait / skip verdict",
            ],
        },
        {
            "id": "discover-niches-pain-first",
            "name": "Pain-First Scout",
            "tagline": "Starts from a real customer problem and finds the product gap behind it.",
            "engine": "/discover-niches-pain-first",
            "job_spec": "discover-niches-pain-first",
            "best_for": "Finding a niche from a real customer problem rather than a guess.",
            "inputs": ["A problem area to explore"],
            "steps": [
                "Find the customer pain people talk about",
                "Spot the product that would solve it",
                "Check that real demand exists",
                "Study the current top sellers",
                "Give a go / wait / skip verdict",
            ],
        },
    ],
    "trend": [
        {
            "id": "trend-radar",
            "name": "Trend Radar",
            "tagline": "Spots which products are heating up so you can sell them at the right time.",
            "engine": "/trend-radar",
            "job_spec": "trend-radar",
            "best_for": "Catching a product's momentum early — rising, in-season, steady, or fading.",
            "inputs": ["A keyword or topic", "(optional) region"],
            "steps": [
                "Check how interest has changed over time",
                "See if it's rising, seasonal, or steady",
                "Sort into sell-now, build-ahead, or skip",
                "Note the best months to sell",
                "Pass it on to keyword research",
            ],
        },
    ],
    "marketplace": [
        {
            "id": "marketplace-movers",
            "name": "Marketplace Movers",
            "tagline": "Finds products already selling in big numbers on the big marketplaces.",
            "engine": "/general-store-research",
            "job_spec": "keyword-discovery",
            "best_for": "Finding proven products by how much they're already selling elsewhere.",
            "inputs": ["A keyword or category", "(optional) which store"],
            "steps": [
                "Run the keyword through the finder",
                "Check how many are already selling",
                "Note a rough cost to compare against",
                "Rank by demand and cost",
                "Add the best to your shortlist",
            ],
        },
    ],
    "product": [
        {
            "id": "discover-general-stores",
            "name": "General-Store Discovery",
            "tagline": "Finds growing competitor stores worth watching.",
            "engine": "/discover-general-stores",
            "job_spec": "discover-general-stores",
            "best_for": "Growing your list of competitor stores to learn from.",
            "inputs": ["A starting store, or none"],
            "steps": [
                "Find growing US stores",
                "Keep only the broad, general ones",
                "Drop branded or shrinking ones",
                "Add their traffic info",
                "Add the rest to your watch list",
            ],
        },
        {
            "id": "competitor-best-seller-spy",
            "name": "Best-Seller Spy",
            "tagline": "Watches competitors daily and surfaces their rising best-sellers.",
            "engine": "/competitor-best-seller-spy",
            "job_spec": "bestseller-spy",
            "best_for": "Catching proven products early — what's newly climbing a competitor's "
                        "best-seller list.",
            "inputs": ["Your watched competitor stores"],
            "steps": [
                "Snapshot each store's best-sellers",
                "Compare today against yesterday",
                "Surface what's climbing",
                "Check the price and how new it is",
                "Pass the winners to keyword research",
            ],
        },
    ],
}


def _read_json(path: Path) -> dict | None:
    try:
        with path.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- dismissed trends
# Operator-curated hide list for the Trend Research surface. Lives in per-deployment
# DATA (data_root), so each business has its own hidden set and it survives reloads.
# A trend row is identified by (slug, keyword, geo); the file stores those keys.
def _dismissed_trends_path() -> Path:
    return config.data_root() / "operator-app" / "api" / "data" / "dismissed-trends.json"


def _trend_key(slug: object, keyword: object, geo: object) -> str:
    return "|".join((str(slug or ""), str(keyword or "").strip().lower(), str(geo or "")))


# Meta / non-product search terms that are NOT sellable product categories. Seeding a trend card on
# one of these makes Google Trends fan out into celebrity/news/gadget noise (its generic "trending"
# feed — "who owns bowflex", "new york times", "echo pop price") instead of a real demand signal.
# We (a) never use one as a default trend seed and (b) drop any that slip onto the trend surface.
_META_KEYWORDS = {
    "trending products", "trending product", "trending", "trending now", "trending items",
    "trending searches", "best products", "best sellers", "bestsellers", "best selling products",
    "top products", "hot products", "popular products", "viral products", "winning products",
    "products", "product", "amazon", "temu", "aliexpress", "shopify", "dropshipping",
    "dropshipping products", "ecommerce", "online shopping", "shopping", "gifts", "deals",
}


def is_meta_keyword(kw: object) -> bool:
    """True when `kw` is a generic meta/non-product search term (not a sellable category)."""
    k = re.sub(r"\s+", " ", str(kw or "").strip().lower())
    return (not k) or (k in _META_KEYWORDS)


def dismissed_trend_keys() -> set[str]:
    data = _read_json(_dismissed_trends_path())
    keys = data.get("keys") if isinstance(data, dict) else None
    return {str(k) for k in keys} if isinstance(keys, list) else set()


def dismiss_trend(slug: object, keyword: object, geo: object) -> dict:
    """Hide one trend keyword card. Idempotent; returns {dismissed, total}."""
    keys = dismissed_trend_keys()
    keys.add(_trend_key(slug, keyword, geo))
    path = _dismissed_trends_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump({"keys": sorted(keys)}, fh, indent=2)
    return {"dismissed": True, "total": len(keys)}


def restore_trends() -> dict:
    """Clear the entire dismissed set (un-hide all)."""
    path = _dismissed_trends_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump({"keys": []}, fh, indent=2)
    return {"restored": True, "total": 0}


# ---------------------------------------------------------------- dismissed sub-keywords (segments)
def _dismissed_segments_path() -> Path:
    return config.data_root() / "operator-app" / "api" / "data" / "dismissed-segments.json"


def _segment_key(store: object, head: object, term: object) -> str:
    return "|".join((str(store or ""), str(head or "").strip().lower(), str(term or "").strip().lower()))


def dismissed_segment_keys() -> set[str]:
    data = _read_json(_dismissed_segments_path())
    keys = data.get("keys") if isinstance(data, dict) else None
    return {str(k) for k in keys} if isinstance(keys, list) else set()


def dismiss_segment(store: object, keyword: object, term: object) -> dict:
    """Hide ONE sub-keyword from a head keyword's plan (the X on a sub-keyword row). Idempotent —
    keyword_discovery filters the hide-list, so it drops from the keyword page AND the SKU plan."""
    if not str(term or "").strip():
        return {"ok": False, "error": "term required"}
    keys = dismissed_segment_keys()
    keys.add(_segment_key(store, keyword, term))
    path = _dismissed_segments_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump({"keys": sorted(keys)}, fh, indent=2)
    return {"ok": True, "dismissed": True, "total": len(keys)}


# ---------------------------------------------------------------- stores / queues
def list_stores() -> list[str]:
    """Store keys that have a listing queue on disk."""
    base = config.general_stores_dir()
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and (p / "listing-queue.json").exists()
    )


_nn_sync_last = 0.0

def sync_nn_stores(force: bool = False) -> int:
    """Mirror NN Master Settings stores into the operator registry so a store
    added in NN's dashboard appears here automatically — same as every other NN
    app. Best-effort: if NN_BASE_URL / OPERATOR_SSO_SECRET aren't set, or NN is
    unreachable, it's a silent no-op (never breaks the store list). Throttled to
    once per 60s. Returns the count of newly-registered stores."""
    global _nn_sync_last
    base = (os.environ.get("NN_BASE_URL") or "").strip().rstrip("/")
    secret = (os.environ.get("OPERATOR_SSO_SECRET") or "").strip()
    if not base or not secret:
        return 0
    now = time.time()
    if not force and (now - _nn_sync_last) < 60:
        return 0
    _nn_sync_last = now
    try:
        req = urllib.request.Request(
            f"{base}/api/operator/stores", headers={"x-operator-key": secret}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[nn-sync] fetch failed: {exc}")
        return 0
    added = 0
    for s in (data.get("stores") or []):
        # Register under a readable slug from the NN NAME (e.g. "Ellese & Co."
        # -> "ellese-co"), not the opaque NN key ("yhbakm1w"), so the picker
        # reads like every other app. Falls back to the key if the name is empty.
        name = str((s or {}).get("name") or "").strip()
        key = str((s or {}).get("key") or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", (name or key).lower()).strip("-")[:40].strip("-")
        if not slug:
            continue
        if (config.general_stores_dir() / slug / "listing-queue.json").exists():
            continue
        try:
            add_store(slug)
            added += 1
        except Exception:
            pass  # bad slug or race with another request — skip, keep going
    if added:
        print(f"[nn-sync] registered {added} new store(s) from NN Master Settings")
    return added


# Store modes: which catalog PATH a store runs. `general` is the default (the broad
# multi-category catalog, apparel excluded); `fashion` reverses the apparel exclusion —
# the research funnel, competitor finding and listing all work ON fashion for that store;
# `both` runs the two together (general breadth AND apparel — nothing is excluded, and
# discovery seeds span both spreads). Every consumer reads STORE_MODE from disk.
STORE_MODES = ("general", "fashion", "both")


def add_store(key: str, mode: str = "general") -> str:
    """Register a new store: scaffold general-stores/<key>/listing-queue.json so the store key
    appears in list_stores() (and thus in Connections for per-store Shopify + Google setup).
    Same slug convention as the rest of the project (lowercase, hyphenated, ≤40 chars)."""
    slug = (key or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", slug):
        raise ValueError("store key must be lowercase letters/digits/hyphens, start alphanumeric, ≤40 chars")
    if mode not in STORE_MODES:
        raise ValueError(f"mode must be one of {STORE_MODES}")
    store_dir = config.general_stores_dir() / slug
    queue = store_dir / "listing-queue.json"
    if queue.exists():
        raise FileExistsError(f"store '{slug}' already exists")
    store_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    queue.write_text(
        json.dumps({"store": slug, "created": today, "updated": today, "mode": mode, "categories": {}}, indent=2),
        encoding="utf-8",
    )
    return slug


def store_mode(store: str) -> str:
    """The store's catalog path (general | fashion | both). Pre-mode queues have no field → general."""
    queue = listing_queue(store) or {}
    m = queue.get("mode")
    return m if m in STORE_MODES else "general"


def is_fashion(store: str) -> bool:
    """True when the store's catalog path INCLUDES apparel (fashion OR both). Gate every
    fashion-only feature on THIS, never on `store_mode(store) == "fashion"` — a bare `== "fashion"`
    silently excludes `both` stores. A `general` store is always False, so fashion logic never
    leaks to it."""
    return store_mode(store) in ("fashion", "both")


def is_general(store: str) -> bool:
    """True when the store runs the general breadth path (general OR both). Gate general-only logic
    on this; correctly includes `both`."""
    return store_mode(store) in ("general", "both")


def set_store_mode(store: str, mode: str) -> str:
    """Set a store's catalog path (general | fashion | both). The flag lives in the store's
    listing-queue.json (authoritative — the research/validation scripts read it from disk
    via STORE_MODE), so research + competitor finding + listing all follow it."""
    if mode not in STORE_MODES:
        raise ValueError(f"mode must be one of {STORE_MODES}")
    path = config.general_stores_dir() / store / "listing-queue.json"
    queue = _read_json(path)
    if queue is None:
        raise FileNotFoundError(f"store '{store}' not found")
    queue["mode"] = mode
    queue["updated"] = date.today().isoformat()
    path.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    return mode


def remove_store(key: str) -> str:
    """Unregister a store: delete its general-stores/<key>/ directory so it disappears from
    list_stores() (and thus from Connections). Its per-store credentials in the connections
    setting are cleared separately by the caller. No-op-safe: raises FileNotFoundError if the
    store key isn't registered."""
    slug = (key or "").strip().lower()
    # Validate BEFORE building the path — same slug rule as add_store(). Without
    # this, a key like ".." (or backslash segments on Windows) escapes the stores
    # root and shutil.rmtree() would recursively delete an arbitrary directory
    # (e.g. the whole data root). Path traversal on a destructive op.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", slug):
        raise ValueError("store key must be lowercase letters/digits/hyphens, start alphanumeric, ≤40 chars")
    base = config.general_stores_dir()
    store_dir = base / slug
    # Defense in depth: the resolved target must be a direct child of the stores root.
    if store_dir.resolve().parent != base.resolve():
        raise ValueError("invalid store key")
    if not store_dir.is_dir():
        raise FileNotFoundError(f"store '{slug}' not found")
    shutil.rmtree(store_dir)
    return slug


def listing_queue(store: str) -> dict | None:
    return _read_json(config.general_stores_dir() / store / "listing-queue.json")


def candidate_queue(store: str) -> dict | None:
    return _read_json(config.general_stores_dir() / store / "candidate-queue.json")


def remove_candidate(store: str, keyword: str) -> dict:
    """Dismiss a keyword from a store's discovery backlog (the X on the Keyword Research table).
    Deletes the matching candidate from candidate-queue.json — matched by keyword (case-insensitive)
    so the caller doesn't have to reproduce the script's slug rule. Returns {ok, removed}."""
    path = config.general_stores_dir() / store / "candidate-queue.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        return {"ok": False, "removed": 0, "error": "no candidate queue for this store"}
    cands = data.get("candidates")
    if not isinstance(cands, dict):
        return {"ok": False, "removed": 0, "error": "candidate queue is empty"}
    target = (keyword or "").strip().lower()
    drop = [slug for slug, c in cands.items()
            if isinstance(c, dict) and str(c.get("keyword") or slug).strip().lower() == target]
    for slug in drop:
        cands.pop(slug, None)
    if drop:
        data["updated"] = datetime.now(timezone.utc).date().isoformat()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": bool(drop), "removed": len(drop),
            "error": None if drop else "keyword not found in this store's backlog"}


# ---------------------------------------------------------------- keyword research (general-store funnel)
# The 6-lane discovery funnel (06-launch-general-store/scripts/candidate_queue.py).
# Lane 1 is the GATE (≥10k SV); lanes 2-6 stack validation confidence on a gated keyword.
KEYWORD_GATE_SV = 10_000
KEYWORD_LANES = [
    {"id": "keyword", "n": 1, "name": "Keyword gate", "role": "GATE",
     "what": "Search volume ≥10k + season/momentum bucket (season_classify)."},
    {"id": "shopping_scan", "n": 2, "name": "Shopping scan", "role": "validation",
     "what": "Live Google Shopping sub-segments, PLA price band, advertiser concentration."},
    {"id": "bestseller_spy", "n": 3, "name": "Best-seller spy", "role": "validation",
     "what": "Competitor Shopify rank-delta / %gain; not-in-store cross-ref."},
    {"id": "amazon", "n": 4, "name": "Amazon best-sellers", "role": "validation",
     "what": "Best Sellers (rank) + New Releases (fresh entrants) + review-count moat."},
    {"id": "marketplace", "n": 5, "name": "Marketplace", "role": "validation",
     "what": "Find products by keyword on AliExpress + Temu + 1688 (ranked by orders / sold-count)."},
    {"id": "meta", "n": 6, "name": "Meta ads", "role": "validation",
     "what": "Ad longevity / duplicate-creative count (supplementary feeder)."},
]
_VALIDATION_LANES = [l["id"] for l in KEYWORD_LANES if l["role"] == "validation"]


# The three sources the funnel expands a head keyword into sub-keywords from. Each kept
# sub-keyword is a candidate catalog entry — the per-keyword SKU batch (D11 breadth).
SEGMENT_SOURCES = [
    {"id": "shopping_scan", "name": "Shopping scan",
     "what": "Google Shopping category / intent rows — the live searched sub-segments."},
    {"id": "serp", "name": "Similar SERP",
     "what": "Similar / related SERP queries off the head term ('people also search for')."},
    {"id": "dataforseo", "name": "DataForSEO",
     "what": "Keyword-Planner-style related keywords — seed one keyword, get its similar "
             "keywords back, each with its own search volume (DFS related-keywords endpoint)."},
]


# A sub-keyword only earns a slot in the SKU plan if it maps to a SOURCEABLE PRODUCT — a
# real variant we can build & list. Informational / question / local / navigational queries
# describe no product, so they are skipped from the plan (the funnel surfaces them, but they
# never become a SKU). Matched as whole words / phrases against the sub-keyword.
_NON_SKU_PHRASES = (
    "how to", "near me", "near you", "for sale", "worth it", "good for", "bad for", "is it",
    "are they", "do they", "can you", "should i", "what is", "what are",
    "discount code", "free shipping",
    # SERP / local / retail-intent modifiers — a bad Google-Shopping product TITLE, not a variant
    "pick up", "pickup today", "pick up today", "in stock", "on sale", "deals on", "buy now",
    "for cheap", "how much", "home depot", "the home depot", "best buy", "sam s club", "b q",
)
_NON_SKU_WORDS = {
    "how", "why", "what", "when", "where", "who", "vs", "versus", "review", "reviews",
    "rating", "ratings", "benefit", "benefits", "meaning", "definition", "explained",
    "guide", "diy", "worth", "problem", "problems", "wiki", "reddit", "youtube",
    "amazon", "ebay", "aliexpress", "temu", "alibaba", "etsy", "walmart", "coupon",
    "discount", "cheap", "free", "used", "is", "are", "does", "do", "can", "should",
    # SERP-noise + retailer names that make a bad product title (Portable AC 'Lowe's'/'nearby'/'deals').
    # Deliberately CONSERVATIVE — omit shop/store/price/cost/online/target which appear in REAL
    # products ('shop vac', 'price gun', 'store organizer', 'dart target'); keep only distinctive
    # retailer names + unambiguous modifiers.
    "deals", "deal", "clearance", "nearby", "today", "cheapest", "best", "lowes", "lowe", "costco",
    "wayfair", "argos", "screwfix", "wickes", "currys", "kohls", "macys", "overstock", "newegg",
    "bestbuy", "homedepot", "ikea", "menards",
}


def _segment_is_buildable(term: str) -> bool:
    """True if a sub-keyword maps to a sourceable product variant (belongs in the SKU plan)."""
    t = (term or "").lower().strip()
    if not t:
        return False
    if any(p in t for p in _NON_SKU_PHRASES):
        return False
    words = set(re.findall(r"[a-z0-9]+", t))
    return not (words & _NON_SKU_WORDS)


def _to_float(v) -> float | None:
    """Best-effort numeric coercion for scan fields that may arrive as a string ("59.99"),
    a number, or junk. Returns None on anything unparseable — the web calls .toFixed() on
    price/rating, which throws (crashing the card to blank) on a string, so normalize here."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    # US-locale data (DFS US merchant): comma is a THOUSANDS separator, dot is the decimal.
    # "$1,299.00"→1299.0, "3,900"→3900.0 (votes), "59.99"→59.99, "4.6"→4.6.
    m = re.search(r"-?[\d,]*\.?\d+", str(v).replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _scan_slug(s: str) -> str:
    """Slug used by the shopping-scan job's output dir (must match jobs._slug exactly, so the
    per-keyword paid-scan.json can be located: shopping-scans/<store>/<slug>/paid-scan.json)."""
    out = "".join(ch if ch.isalnum() else "-" for ch in (s or "").lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40] or "scan"


def _candidate_segments(c: dict, store: str | None = None) -> list[dict]:
    """Normalize a candidate's EXPANDED sub-keywords (the 'barefoot shoes' → 'barefoot shoes
    for running / casual' map) into [{term, sv, source, price_band}].

    Reads the structured `segments` field, then the shopping_scan lane rows, then BRIDGES the
    post-promote Google-Shopping scan file (paid-scan.json) — which is where the real sub-keyword
    intent actually lands. Deduped case-insensitively, head term excluded. Only PRODUCT-BUILDABLE
    sub-keywords are kept — non-sourceable (info / question / local / navigational) queries are
    skipped, since the SKU plan is a list of products to source.
    """
    head = (c.get("keyword") or "").strip().lower()
    out: list[dict] = []
    seen: set[str] = set()

    def _add(term, sv=None, source=None, price_band=None):
        t = (term or "").strip()
        key = t.lower()
        if not t or key == head or key in seen:
            return
        seen.add(key)
        if not is_listable_keyword(t):
            return  # non-product OR bad-title sub-keyword ('Lowe's', 'deals on', 'pick up today',
                    # 'nearby', 'best') → never a SKU. Same central gate as trends/keywords/heads.
        out.append({"term": t, "sv": sv, "source": source, "price_band": price_band})

    for s in (c.get("segments") or []):
        if isinstance(s, str):
            _add(s, source="dataforseo")
        elif isinstance(s, dict):
            _add(s.get("term") or s.get("keyword") or s.get("segment"),
                 s.get("sv"), s.get("source"), s.get("price_band") or s.get("price"))
    scan = (c.get("lanes") or {}).get("shopping_scan") or {}
    for s in (scan.get("segments") or scan.get("rows") or []):
        if isinstance(s, str):
            _add(s, source="shopping_scan")
        elif isinstance(s, dict):
            _add(s.get("term") or s.get("segment") or s.get("keyword"),
                 s.get("sv"), "shopping_scan", s.get("price_band") or s.get("price"))
    # Bridge the post-promote Google-Shopping scan: shopping_scan_dfs.py writes the real intent to
    # shopping-scans/<store>/<slug>/paid-scan.json as `sub_keywords` + `category_rows` (both plain
    # term strings), but NOTHING read that file back — so every candidate showed 0 sub-keywords.
    # Read it here (best-effort; missing file → skip) so a scanned keyword's sub-ideas appear.
    if store and c.get("keyword"):
        scan_file = (config.data_root() / "operator-app" / "api" / "data" / "shopping-scans"
                     / store / _scan_slug(c.get("keyword")) / "paid-scan.json")
        if scan_file.is_file():
            try:
                sdata = json.loads(scan_file.read_text())
            except (ValueError, OSError):
                sdata = {}
            sv_map = {str(k).strip().lower(): v for k, v in (sdata.get("sub_keyword_sv") or {}).items()}
            for kw in (sdata.get("sub_keywords") or []):
                if isinstance(kw, str):
                    _add(kw, sv=sv_map.get(kw.strip().lower()), source="shopping_scan")
            for row in (sdata.get("category_rows") or []):
                if isinstance(row, str):
                    _add(row, sv=sv_map.get(row.strip().lower()), source="shopping_scan")
                elif isinstance(row, dict):
                    _add(row.get("term") or row.get("title") or row.get("label"),
                         row.get("sv"), "shopping_scan", row.get("price_band"))
    return out


# Junk + duplicate guards shared by the Trend AND Keyword surfaces (readers-side, so both lists
# are cleaned the same way with no vendored re-sync). Mirrors research_dfs._is_product_keyword.
_KW_JUNK_PHRASES = ("near me", "for sale", "for rent", "how to", "what is", "second hand")
_KW_JUNK_WORDS = {
    "repair", "repairs", "installation", "install", "installed", "fitting", "service", "servicing",
    "services", "company", "companies", "contractor", "contractors", "rental", "rentals", "hire",
    "quote", "quotes", "job", "jobs", "salary", "how", "what", "why", "when", "who", "vs", "versus",
    "review", "reviews", "rating", "meaning", "definition", "wiki", "reddit", "youtube", "login",
    "download", "holiday", "holidays", "flight", "flights", "hotel", "hotels", "insurance",
    "weather", "near", "map", "directions",
    # travel / leisure services — not products ("princess cruises 2027", "alaska cruise 2026")
    "cruise", "cruises", "vacation", "vacations", "resort", "resorts", "tour", "tours", "trip",
    "trips", "airline", "airlines", "timeshare", "flights", "airfare", "getaway", "excursion",
    # aviation / air travel — a whole non-product domain the general trend feed drags in
    # ("jet blue airways", "breeze airways", "plane"). "airways" catches every "* airways" carrier.
    "airways", "airplane", "airplanes", "plane", "planes", "aircraft", "aviation", "airport",
    "airports", "layover", "flying", "airfares",
    # travel DESTINATIONS — countries / vacation spots are never a dropship product. ONLY
    # unambiguous ones (deliberately EXCLUDES turkey/china/georgia/jersey/chile/jordan/brazil —
    # those double as real product words: turkey fryer, china dishware, jersey, chili, air jordan).
    "portugal", "spain", "italy", "greece", "france", "germany", "netherlands", "switzerland",
    "austria", "belgium", "iceland", "ireland", "scotland", "croatia", "morocco", "egypt",
    "maldives", "seychelles", "fiji", "bali", "phuket", "santorini", "mykonos", "ibiza", "cancun",
    "tulum", "maui", "oahu", "aruba", "bahamas", "jamaica", "barbados", "hawaii", "cabo", "cozumel",
    "vietnam", "cambodia", "peru", "colombia", "thailand", "indonesia", "philippines",
}
_DEDUP_STOP = {"for", "and", "the", "with", "to", "of", "a", "in", "on", "your", "best"}
# Bare ambiguous airline / travel words that are junk ALONE (Breeze Airways, Spirit Airlines) but
# fine inside a compound product ('cool breeze fan', 'spirit level'). Matched ONLY as the whole
# term, so the single-word junk is dropped while real compounds survive.
_EXACT_STANDALONE_JUNK = {"breeze", "spirit", "frontier", "allegiant", "southwest"}


def _is_product_keyword(term) -> bool:
    """A candidate/trend keyword must plausibly name a PRODUCT — not a service/info/local query
    or a bare number ('10', 'ac repair', 'near me', 'how to…')."""
    t = (term or "").strip().lower()
    if len(t) < 3 or re.fullmatch(r"[0-9]+", t):
        return False
    if t in _EXACT_STANDALONE_JUNK:                          # bare 'breeze'/'spirit' → junk; compounds OK
        return False
    if any(p in t for p in _KW_JUNK_PHRASES):
        return False
    return not (set(re.findall(r"[a-z0-9]+", t)) & _KW_JUNK_WORDS)


# Brands (not dropship-sourceable) + holiday/dated events — polluting the Trend + Keyword feeds
# ("dreo fan", "shark fan", "apple iphone 17 pro max", "father's day 2025", "june 2026"). NOT
# exhaustive (brands are infinite) — covers the common appliance/electronics/home/tech brands the
# trend feed surfaces; _MAJOR_BRANDS (defined later, resolved at call time) adds the appliance ones.
_EXTRA_BRANDS = {
    "dreo", "shark", "ninja", "apple", "iphone", "ipad", "airpods", "macbook", "samsung", "galaxy",
    "xiaomi", "anker", "levoit", "govee", "roomba", "irobot", "bissell", "dyson", "eufy", "tineco",
    "mellow", "bedsure", "sojoy", "comfyt", "utopia", "sable", "yeti", "stanley", "owala", "hydroflask",
    "contigo", "nespresso", "keurig", "crockpot", "vitamix", "nutribullet", "cosori", "lasko", "vornado",
    "nike", "adidas", "puma", "crocs", "lululemon", "nest", "sony", "bose", "jbl", "beats", "lego",
    "barbie", "pokemon", "nintendo", "playstation", "xbox", "tesla", "gopro", "fitbit", "garmin",
    "peloton", "casper", "purple", "tempurpedic", "solawave", "theragun", "hoover", "honeywell",
    "casio", "seiko", "citizen", "rolex", "fossil", "timex", "michael kors", "kors", "ray ban",
    "oakley", "under armour", "reebok", "vans", "converse", "columbia", "carhartt", "patagonia",
    "cricut", "kitchenaid", "cuisinart", "hamilton beach", "ninja", "shark", "roborock", "ecovacs",
    # automotive — car MAKES (a whole non-product domain: "toyota suv", "toyota camry"). The make
    # token catches every "toyota <model>". Plus a few unambiguous MODEL tokens for bare-model cards.
    # EXCLUDES generic-word models (focus/fit/soul/pilot/accord) + "jaguar" (animal-print goods).
    "toyota", "honda", "ford", "chevrolet", "chevy", "gmc", "nissan", "hyundai", "kia", "subaru",
    "mazda", "volkswagen", "audi", "bmw", "mercedes", "benz", "lexus", "acura", "infiniti",
    "cadillac", "chrysler", "dodge", "buick", "volvo", "porsche", "ferrari", "lamborghini",
    "maserati", "bentley", "bugatti", "jeep", "camry", "corolla", "rav4", "4runner", "tacoma",
    "tundra", "highlander", "sienna", "prius", "silverado", "camaro", "corvette", "escalade",
    "f150", "f250", "f350", "wrangler",
    # airlines — carrier BRANDS ("jetblue", "delta"). "airline/airlines/airways" (junk words) catch
    # the "* airways/airlines" carriers; these are the single-token brands those don't cover.
    "jetblue", "delta", "ryanair", "easyjet", "lufthansa", "emirates", "etihad", "qantas",
    "aeromexico", "allegiant", "avelo", "vueling", "condor", "westjet",
}
_EVENT_WORDS = {
    "christmas", "halloween", "thanksgiving", "easter", "valentine", "valentines", "hanukkah", "diwali",
    "ramadan", "father", "fathers", "mother", "mothers", "memorial", "veterans", "juneteenth",
    "independence", "cyber",  # cyber monday
    "fest", "festival", "expo", "convention", "carnival", "parade", "premiere", "tickets", "tour",
}
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december",
}
# Multi-word / joined brand names — a single-word token set can't catch these, so they're matched as
# a substring of the whole term ("newbalance 204l", "eight sleep pod", "bed jet climate comfort").
_BRAND_PHRASES = (
    "newbalance", "new balance", "eight sleep", "eightsleep", "8 sleep", "bed jet", "bedjet", "hydro flask",
    "instant pot", "hamilton beach", "michael kors", "ray ban", "under armour", "crock pot",
    "kitchen aid", "black decker", "black+decker", "stanley cup", "owala freesip", "ninja creami",
    # multi-word automotive / airline brands a single-token set can't catch
    "jet blue", "land rover", "range rover", "aston martin", "alfa romeo", "rolls royce",
    "air canada", "air france", "american airlines", "united airlines", "southwest airlines",
    "spirit airlines", "frontier airlines", "british airways", "breeze airways",
)


def _is_generic_product_trend(term) -> bool:
    """Stricter than _is_product_keyword: a TREND card (and its title-seed related searches) must be a
    GENERIC, sourceable product — NOT a brand ('dreo fan', 'apple iphone 17 pro max'), a holiday /
    dated event ('father's day 2025', 'june 2026'), or a service/info query. Brands & events aren't
    dropship-sourceable, so they pollute the feed."""
    t = (term or "").strip().lower()
    if not _is_product_keyword(t):
        return False
    if any(bp in t for bp in _BRAND_PHRASES):              # multi-word / joined brand name
        return False
    # tokenize WITH digits so alphanumeric model brands match ('rav4', '4runner', 'f150') — a
    # letters-only split turns 'rav4'→'rav' and silently misses them.
    words = set(re.findall(r"[a-z0-9]+", t))
    if words & _EXTRA_BRANDS or words & _MAJOR_BRANDS:      # branded (both sets resolved at call time)
        return False
    if words & _EVENT_WORDS:                                # holiday / seasonal event
        return False
    if (words & _MONTHS) and re.search(r"\b20\d\d\b", t):   # month + year → a date ("june 2026")
        return False
    return True


def purge_junk_dossiers(dry_run: bool = False) -> dict:
    """Quarantine dossiers whose SEED is non-product junk (fails _is_generic_product_trend) — the
    news-driven 'jet-blue' / 'toyota' / 'portugal' dossiers the trend feed auto-created. The read
    filter HIDES their cards, but the dossiers still sit on disk producing filtered cards + related-
    query expansion stragglers ('breeze'), so this actually REMOVES the source.

    Reversible by design: each junk dossier is MOVED to a sibling `_junk-quarantine/` dir, never
    hard-deleted — a mistaken catch can be restored by moving it back. Idempotent + best-effort
    (per-item errors are swallowed); safe to run on every boot. dry_run=True previews the hit list
    (would_quarantine) without moving anything. Seed is recovered from the slug (hyphens→spaces),
    which is how the slug was derived in the first place."""
    would: list[str] = []
    moved: list[str] = []
    for base in (config.dossiers_dir(), config.dossiers_pain_first_dir()):
        if not base.is_dir():
            continue
        quarantine = base / "_junk-quarantine"
        for p in sorted(base.iterdir()):
            if not p.is_dir() or p.name.startswith("_") or p.name.startswith("."):
                continue
            seed = p.name.replace("-", " ").strip()
            if _is_generic_product_trend(seed):
                continue  # real product dossier — keep
            would.append(p.name)
            if dry_run:
                continue
            try:
                quarantine.mkdir(exist_ok=True)
                dest = quarantine / p.name
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.move(str(p), str(dest))
                moved.append(p.name)
            except OSError:
                pass
    return {"junk_found": len(would), "would_quarantine": would, "quarantined": moved, "dry_run": dry_run}


def _nonproduct_path() -> Path:
    return config.data_root() / "operator-app" / "api" / "data" / "ai-nonproduct-keywords.json"


@functools.lru_cache(maxsize=1)
def _nonproduct_set_cached() -> frozenset:
    """The AI-flagged non-product hide-set, memoized for the process (invalidated by the sweep)."""
    try:
        d = json.loads(_nonproduct_path().read_text())
        return frozenset(str(t).strip().lower() for t in (d.get("terms") or []) if str(t).strip())
    except (OSError, ValueError):
        return frozenset()


def _is_nonproduct_kw(keyword: object) -> bool:
    """True if the AI product-gate has flagged this keyword as NOT a dropshippable product (bank,
    brand, motel, service, info query…) — the read filter drops it. Fast: in-memory set lookup."""
    return str(keyword or "").strip().lower() in _nonproduct_set_cached()


def is_listable_keyword(keyword: object) -> bool:
    """THE single gate every surface uses to decide whether a keyword is a listable dropship-PRODUCT
    keyword (clean enough to be a Google-Shopping title). Combines all three layers:
      1. deterministic product-domain filter (`_is_generic_product_trend`: brands/events/travel/
         services/auto/airline/dates),
      2. the AI non-product hide-set (`_is_nonproduct_kw`: banks/motels/perishables/oversized/…),
      3. title-quality (`_segment_is_buildable`: no SERP-modifier / retailer / local-intent noise —
         'deals on', 'pick up today', 'Lowe's', 'nearby', 'best').
    Applied IDENTICALLY at trends, keyword candidates, trend related-queries, sub-keyword segments,
    and SKU-plan heads — so a fix in one layer corrects every surface at once."""
    return (_is_generic_product_trend(keyword)
            and not _is_nonproduct_kw(keyword)
            and _segment_is_buildable(str(keyword or "")))


def sweep_nonproduct_keywords(limit: int = 2500, refresh: bool = False) -> dict:
    """Clean the whole app of non-dropshippable keywords: gather every keyword currently surfaced
    (trends + keyword candidates + their sub-keywords), AI-classify them (batched + cached), and
    PERSIST the confident non-products to the hide-set so trends/keyword reads drop them fast (no
    per-read LLM). Inclusive — only confident non-products are hidden (unsure kept; operator X still
    available). Idempotent; catches new leaks the deterministic blocklist can't know (capitalone /
    super 8 / 8sleep / blacks). Returns what it newly flagged."""
    from . import ai_product_gate
    terms: set[str] = set()
    try:
        for t in trends_overview().get("trends", []):
            k = str(t.get("keyword") or "").strip()
            if k:
                terms.add(k)
    except Exception:  # noqa: BLE001
        pass
    try:
        for c in (keyword_discovery().get("candidates") or []):
            k = str(c.get("keyword") or "").strip()
            if k:
                terms.add(k)
            for s in (c.get("segments") or []):
                sk = str(s.get("term") or "").strip()
                if sk:
                    terms.add(sk)
    except Exception:  # noqa: BLE001
        pass
    term_list = list(terms)[:limit]
    if not term_list:
        return {"swept": 0, "newly_flagged": [], "total_hidden": len(_nonproduct_set_cached())}
    verdicts = ai_product_gate.classify(term_list, refresh=refresh)  # batched + cached (refresh re-runs)
    flagged = {t.lower() for t in term_list if (verdicts.get(t.lower()) or {}).get("ok", True) is False}
    current = set(_nonproduct_set_cached())
    if refresh:
        # Fresh verdicts are authoritative for the swept terms: drop any now judged a product (e.g.
        # 'pool noodles' after a prompt fix), add the non-products. Untouched terms stay.
        swept = {t.lower() for t in term_list}
        merged = (current - swept) | flagged
    else:
        merged = current | flagged
    try:
        p = _nonproduct_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"terms": sorted(merged)}, indent=0))
    except OSError:
        pass
    _nonproduct_set_cached.cache_clear()  # so reads pick up the new hide-set immediately
    return {"swept": len(term_list), "newly_flagged": sorted(flagged), "total_hidden": len(merged)}


def _stem_word(w: str) -> str:
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]  # cooling -> cool
    if len(w) > 4 and w.endswith("es"):
        return w[:-2]  # boxes -> box, glasses -> glass
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]  # blankets -> blanket, coolers -> cooler
    return w


def _dedup_key(term) -> str:
    """A morphology-insensitive key so 'cool blanket' / 'cooling blanket' / 'cooling blankets'
    collapse to ONE ('blanket cool'), while 'cold blanket' stays distinct. Stems each word, drops
    stop-words, sorts — so plurals, -ing forms, and word order don't create separate entries."""
    words = [w for w in re.findall(r"[a-z0-9]+", (term or "").lower()) if w not in _DEDUP_STOP]
    return " ".join(sorted(_stem_word(w) for w in words))


# Sub-keyword pre-fill: gate-cleared candidates show "0 sub-keywords" until a Google-Shopping scan
# has run (that DFS scan writes the sub-segment map). It fires on Promote/Find, but so the reviewable
# map appears WITHOUT the operator touching each keyword, warm the biggest cold candidates in the
# background here — the SAME server-side `shopping-scan` job, bounded + deduped so it never storms
# the queue / DataForSEO. Once a scan lands, the candidate is no longer cold, so it isn't re-queued.
_SUBKW_WARMED: set[tuple[str, str]] = set()
_SUBKW_WARM_LOCK = threading.Lock()


def _has_paid_scan(store: str, keyword: str) -> bool:
    """True if a Google-Shopping paid-scan.json already exists for (store, keyword). Used to SKIP
    re-firing a (paid) DFS Merchant scan for a keyword already scanned — the scan output persists on
    the volume across redeploys, so the in-process dedup set alone would re-burn DFS credits after
    every deploy."""
    base = (config.data_root() / "operator-app" / "api" / "data" / "shopping-scans"
            / store / _scan_slug(keyword))
    return (base / "paid-scan.json").is_file() or bool(list(base.glob("*/paid-scan.json")))


def _fire_subkw_warm(cold: list[tuple[str, str, float]]) -> None:
    if not cold:
        return
    picks = sorted(cold, key=lambda t: -(t[2] or 0))[:3]  # biggest keywords first, cap per read
    fresh: list[tuple[str, str]] = []
    with _SUBKW_WARM_LOCK:
        for store, kw, _sv in picks:
            key = (store, (kw or "").strip().lower())
            if not key[1] or key in _SUBKW_WARMED:
                continue
            _SUBKW_WARMED.add(key)
            if _has_paid_scan(store, kw):  # already scanned (survives redeploys) — don't re-burn DFS
                continue
            fresh.append((store, kw))
    if not fresh:
        return

    def _work():
        try:
            from . import jobs
            for store, kw in fresh:
                try:
                    jobs.create("shopping-scan", store, {"keyword": kw})
                except Exception:  # noqa: BLE001 — one bad enqueue never blocks the rest
                    pass
        except Exception:  # noqa: BLE001 — warming is additive; never break the reader
            pass

    threading.Thread(target=_work, daemon=True).start()


def keyword_discovery() -> dict:
    """The general-store keyword discovery funnel: scored candidate backlog across stores.

    Reads each store's candidate-queue.json (produced by candidate_queue.py). Until the
    funnel has been run the backlog is empty — the page still documents the gate + lanes.

    Each candidate carries its EXPANDED sub-keyword map (`segments`) — the reviewable SKU
    building plan: the searched sub-segments a head keyword fans out into, with each kept
    sub-keyword flagged `in_catalog` if it's already a built category for that store.
    """
    stores = list_stores()
    candidates: list[dict] = []
    n_segments = 0
    dismissed_segs = dismissed_segment_keys()  # sub-keywords the operator X'd off a plan
    for store in stores:
        q = candidate_queue(store) or {}
        raw = q.get("candidates")
        items = list(raw.values()) if isinstance(raw, dict) else (raw or [])
        # head keyword of every already-built category for this store → segment "in catalog" flag
        built = {
            (cat.get("keyword") or slug or "").strip().lower()
            for slug, cat in ((listing_queue(store) or {}).get("categories") or {}).items()
        }
        built.discard("")
        for c in items:
            if not isinstance(c, dict):
                continue
            lanes = c.get("lanes") or {}
            present = [lid for lid in _VALIDATION_LANES if lid in lanes]
            segs = _candidate_segments(c, store)
            if dismissed_segs:
                segs = [s for s in segs
                        if _segment_key(store, c.get("keyword"), s.get("term")) not in dismissed_segs]
            for s in segs:
                s["in_catalog"] = s["term"].strip().lower() in built
            n_segments += len(segs)
            candidates.append({
                "store": store,
                "keyword": c.get("keyword"),
                "sv": c.get("sv"),
                "gate": c.get("gate"),
                "capture_bucket": c.get("capture_bucket"),
                "momentum": c.get("momentum"),
                "score": c.get("score"),
                "validation_lanes": present,
                "n_validation": len(present),
                "segments": segs,
                "n_segments": len(segs),
            })
    # Read-time cleanup so the LIST is clean even for candidates ingested before the gate had these
    # filters: drop non-product junk, then collapse morphological duplicates (cool blanket / cooling
    # blanket / cooling blankets → one, keeping the highest-scored). The stored queue is untouched.
    candidates = [c for c in candidates if is_listable_keyword(c.get("keyword"))]
    _best: dict[tuple, dict] = {}
    for c in candidates:
        k = (c.get("store"), _dedup_key(c.get("keyword")))
        if not k[1]:
            continue
        cur = _best.get(k)
        if cur is None or (c.get("score") or 0) > (cur.get("score") or 0):
            _best[k] = c
    candidates = list(_best.values())
    n_segments = sum(int(c.get("n_segments") or 0) for c in candidates)

    # Biggest-volume-first: rank by volume TIER (prime ≥200k → strong → solid → entry → below the
    # 10k floor), then by score within a tier. The 10k gate is the floor; the focus is the biggest
    # keywords (they earn the most listings + best winner odds), so a prime term outranks a smaller
    # one even if the smaller one scored a touch higher on the validation lanes.
    _tier_rank = {"prime": 0, "strong": 1, "solid": 2, "entry": 3, "below": 4}
    candidates.sort(key=lambda c: (
        _tier_rank.get(_volume_tier_name(c.get("sv"))["name"], 5),
        c.get("score") is None,
        -(c.get("score") or 0),
    ))
    # Autonomously pre-fill sub-keywords: any gate-cleared candidate still at 0 gets a background DFS
    # shopping-scan queued (bounded + deduped), so its sub-segment map fills without operator action.
    _fire_subkw_warm([(c["store"], c["keyword"], c.get("sv") or 0)
                      for c in candidates
                      if c.get("store") and c.get("keyword") and not c.get("n_segments")
                      and str(c.get("gate") or "").startswith("PASS")])
    return {
        "gate_sv": KEYWORD_GATE_SV,
        "lanes": KEYWORD_LANES,
        "capture_buckets": CAPTURE_BUCKETS,
        "segment_sources": SEGMENT_SOURCES,
        "stores": stores,
        "totals": {
            "stores": len(stores),
            "candidates": len(candidates),
            "gated_pass": sum(1 for c in candidates if str(c.get("gate") or "").startswith("PASS")),
            "segments": n_segments,
        },
        "candidates": candidates,
    }


def _iter_skus(queue: dict):
    """Yield (category_slug, category, sku_id, sku) for every SKU in a queue."""
    for slug, cat in (queue.get("categories") or {}).items():
        for sku_id, sku in (cat.get("skus") or {}).items():
            yield slug, cat, sku_id, sku


def store_summary(store: str) -> dict:
    """Counts + rollups for one store's listing queue."""
    queue = listing_queue(store) or {}
    cats = queue.get("categories") or {}
    sku_state_counts = {s: 0 for s in SKU_STATES}
    sku_total = 0
    for _slug, _cat, _sku_id, sku in _iter_skus(queue):
        sku_total += 1
        st = sku.get("state")
        if st in sku_state_counts:
            sku_state_counts[st] += 1
    m = queue.get("mode")
    return {
        "store": store,
        "mode": m if m in STORE_MODES else "general",
        "updated": queue.get("updated"),
        "categories": len(cats),
        "skus_total": sku_total,
        "skus_by_state": sku_state_counts,
    }


def store_categories(store: str) -> list[dict]:
    """Flat, UI-friendly category list with per-category SKU rows."""
    queue = listing_queue(store) or {}
    out: list[dict] = []
    for slug, cat in (queue.get("categories") or {}).items():
        skus = [
            {
                "id": sku_id,
                "title": sku.get("title"),
                "cogs": sku.get("cogs"),
                "price": sku.get("price"),
                "state": sku.get("state"),
                "created": sku.get("created"),
                # research ref from the found product — the competitor/marketplace product URL the
                # listing step sources + models from (full URL preserved end-to-end).
                "url": sku.get("url"),
                "image": sku.get("image"),
                "source": sku.get("source"),
                "sold": sku.get("sold"),
            }
            for sku_id, sku in (cat.get("skus") or {}).items()
        ]
        out.append({
            "slug": slug,
            "keyword": cat.get("keyword"),
            "sv": cat.get("sv"),
            "capture_bucket": cat.get("capture_bucket"),
            "state": cat.get("state"),
            "recon": cat.get("recon"),
            "skus": skus,
        })
    # Highest search-volume first (None sorts last).
    out.sort(key=lambda c: (c.get("sv") is None, -(c.get("sv") or 0)))
    return out


# ------------------------------------------------------------ category drill-down
# Per-category the pipeline writes far more than the queue summary carries:
#   <store>/<slug>/_expansion-spec.json   batch specs (variants, tags, seo, body_html)
#   <store>/<slug>/_dedup-report.json     vision near-dup clusters across SKUs
#   <store>/<slug>/recon.md               competitive intel (markdown)
#   <store>/<slug>/sku-<x>/images/        generated gallery (role-named NN-...-<role>)
#   <store>/<slug>/sku-<x>/_supplier-refs/ locked supplier photos + _manifest.txt
# This surfaces all of it as a single drill-down payload.
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# Known multi-word roles must be tried before a bare trailing token.
_ROLE_RE = re.compile(
    r"-(size-guide|use-anywhere|product-white|neck-pillow|scratch-resistant|[a-z0-9]+)"
    r"\.(?:png|jpe?g|webp)$",
    re.IGNORECASE,
)
_ORDER_RE = re.compile(r"^(\d+)")


def _store_dir(store: str) -> Path | None:
    """Resolve general-stores/<store>, refusing anything outside the root."""
    base = config.general_stores_dir().resolve()
    cand = (base / store).resolve()
    if cand == base or base not in cand.parents or not cand.is_dir():
        return None
    return cand


def _category_dir(store: str, slug: str) -> Path | None:
    sdir = _store_dir(store)
    if sdir is None:
        return None
    cand = (sdir / slug).resolve()
    if cand == sdir or sdir not in cand.parents or not cand.is_dir():
        return None
    return cand


def resolve_store_file(store: str, rel: str) -> Path | None:
    """Resolve a file under general-stores/<store>, traversal-safe."""
    sdir = _store_dir(store)
    if sdir is None:
        return None
    cand = (sdir / rel).resolve()
    if not cand.is_relative_to(sdir) or not cand.is_file():
        return None
    return cand


def _img_role(name: str) -> str | None:
    m = _ROLE_RE.search(name)
    return m.group(1).lower().replace("-", " ") if m else None


def _sku_images(rel_prefix: str, sku_dir: Path) -> list[dict]:
    """Generated gallery for one SKU, ordered by the NN- filename prefix."""
    idir = sku_dir / "images"
    if not idir.is_dir():
        return []
    out: list[dict] = []
    for p in idir.iterdir():
        if p.suffix.lower() not in _IMG_EXTS:
            continue
        m = _ORDER_RE.match(p.name)
        out.append({
            "file": p.name,
            "path": f"{rel_prefix}/{sku_dir.name}/images/{p.name}",
            "role": _img_role(p.name),
            "order": int(m.group(1)) if m else 999,
        })
    out.sort(key=lambda x: (x["order"], x["file"]))
    return out


def _supplier_refs(rel_prefix: str, sku_dir: Path) -> dict:
    sd = sku_dir / "_supplier-refs"
    if not sd.is_dir():
        return {"count": 0, "manifest": False, "files": []}
    files = [
        {"file": p.name, "path": f"{rel_prefix}/{sku_dir.name}/_supplier-refs/{p.name}"}
        for p in sorted(sd.iterdir())
        if p.suffix.lower() in _IMG_EXTS
    ]
    return {
        "count": len(files),
        "manifest": (sd / "_manifest.txt").is_file(),
        "files": files,
    }


def category_detail(store: str, slug: str) -> dict | None:
    cdir = _category_dir(store, slug)
    if cdir is None:
        return None
    rel = slug  # paths are relative to the store dir

    queue = listing_queue(store) or {}
    qcat = (queue.get("categories") or {}).get(slug) or {}
    qskus = qcat.get("skus") or {}

    spec = _read_json(cdir / "_expansion-spec.json") or _read_json(cdir / "_draft-spec.json") or {}
    spec_by_slug = {
        s.get("slug"): s for s in (spec.get("skus") or []) if isinstance(s, dict)
    }

    dedup_raw = _read_json(cdir / "_dedup-report.json")
    dedup = None
    if isinstance(dedup_raw, dict):
        clusters = dedup_raw.get("near_dup_clusters") or []
        dedup = {
            "skus_scanned": dedup_raw.get("skus_scanned"),
            "images_hashed": dedup_raw.get("images_hashed"),
            "max_per_product": dedup_raw.get("max_per_product"),
            "n_clusters": len(clusters),
            "n_violations": sum(1 for c in clusters if c.get("violation")),
            "clusters": [
                {"skus": c.get("skus") or [], "size": c.get("size"),
                 "violation": bool(c.get("violation"))}
                for c in clusters
            ],
        }

    docs = [
        n for n in ("recon.md", "recon-supplement-meta-amazon-temu.md")
        if (cdir / n).is_file()
    ]

    skus: list[dict] = []
    for d in sorted(cdir.glob("sku-*")):
        if not d.is_dir():
            continue
        sku_id = d.name                       # sku-ice-silk
        slug_key = d.name[len("sku-"):]       # ice-silk (matches spec slug)
        q = qskus.get(sku_id) or {}
        sp = spec_by_slug.get(slug_key) or {}
        variants = sp.get("variants") or []
        images = _sku_images(rel, d)
        srefs = _supplier_refs(rel, d)
        skus.append({
            "id": sku_id,
            "slug": slug_key,
            "title": q.get("title") or sp.get("title"),
            "state": q.get("state"),
            "cogs": q.get("cogs"),
            "price": q.get("price"),
            "created": q.get("created"),
            "tags": sp.get("tags") or [],
            "variants": [
                {"size": v.get("size"), "price": v.get("price")}
                for v in variants if isinstance(v, dict)
            ],
            "seo_title": sp.get("seo_title"),
            "seo_description": sp.get("seo_description"),
            "body_html": sp.get("body_html"),
            "images": images,
            "n_images": len(images),
            "supplier_refs": srefs,
            "has_spec": bool(sp),
        })

    return {
        "store": store,
        "slug": slug,
        "keyword": qcat.get("keyword"),
        "sv": qcat.get("sv"),
        "capture_bucket": qcat.get("capture_bucket"),
        "state": qcat.get("state"),
        "product_type": spec.get("product_type"),
        "vendor": spec.get("vendor"),
        "category_fullname": spec.get("_category_fullname"),
        "subject": spec.get("subject"),
        "spec_status": spec.get("status"),
        "n_spec_skus": len(spec.get("skus") or []),
        "docs": docs,
        "dedup": dedup,
        "skus": skus,
    }


# ---------------------------------------------------------------- dossiers
def _dossier_dir(slug: str) -> Path | None:
    """Resolve a dossier folder, refusing anything outside the dossiers root."""
    base = config.dossiers_dir().resolve()
    cand = (base / slug).resolve()
    if cand == base or base not in cand.parents or not cand.is_dir():
        return None
    return cand


# Markdown doc kinds, matched on filename so the UI can label/order them.
_DOC_KINDS = [
    ("report", ("-report.md", "dossier.md")),
    ("strategy", ("-launch-strategy.md", "launch-strategy.md")),
    ("icp", ("-icp.md", "icp.md")),
    ("framing", ("framing.md",)),
]


def _doc_kind(name: str) -> str:
    low = name.lower()
    for kind, suffixes in _DOC_KINDS:
        if any(low.endswith(s) for s in suffixes):
            return kind
    return "other"


# Preference order when a dossier carries several keyword-data files (geo/variant splits).
_KEYWORD_FILE_PREFS = [
    "keyword-data.json", "keyword-data-us.json", "keyword-data-expanded.json",
]


def _pick_keyword_file(d: Path) -> Path | None:
    for name in _KEYWORD_FILE_PREFS:
        if (d / name).exists():
            return d / name
    return next(iter(sorted(d.glob("keyword-data*.json"))), None)


def _keyword_rows(d: Path) -> tuple[list[dict], str | None, str | None]:
    """Per-keyword real numbers: SV, CPC, competition, and the monthly SV series."""
    kf = _pick_keyword_file(d)
    if kf is None:
        return [], None, None
    data = _read_json(kf) or {}
    rows: list[dict] = []
    for r in data.get("results") or []:
        series = sorted(
            (
                {"year": p.get("year"), "month": p.get("month"), "sv": p.get("search_volume")}
                for p in (r.get("monthly_searches_trend") or [])
                if p.get("year") and p.get("month")
            ),
            key=lambda p: (p["year"], p["month"]),
        )
        rows.append({
            "keyword": r.get("keyword"),
            "sv": r.get("monthly_searches"),
            "cpc": r.get("cpc"),
            "competition": r.get("competition"),
            "competition_index": r.get("competition_index"),
            "low_bid": r.get("low_top_of_page_bid"),
            "high_bid": r.get("high_top_of_page_bid"),
            "sv_series": series,
        })
    rows.sort(key=lambda r: (r["sv"] is None, -(r["sv"] or 0)))
    return rows, data.get("location"), kf.name


def _iso_date(s) -> date | None:
    """Parse a leading 'YYYY-MM-DD' into a date; None if unusable."""
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, TypeError):
        return None


def _recent_growth(series: list, dates: list | None = None) -> dict:
    """Recent-momentum read for the rising-horizon tabs.

    The raw interest series is ~weekly. Every horizon is measured BACKWARD from
    the series' OWN latest point — Google Trends weekly data lags ~1-2 wk, so the
    freshest available point is the best read of "now"; whether that "now" is
    actually current is a separate freshness gate (`days_old`/`stale`, stamped by
    the caller against today). Each band compares a recent window vs an earlier
    one over its named span (ages measured from the latest data point):
      week    "rising now"        last 2 wk vs the prior 4 wk   (age 0-14 vs 14-42 d)
      month   "rising this month" this month vs last month       (age 0-28 vs 28-56 d)
      quarter "rising 1-3 months" this month vs 1-3 months ago   (age 0-28 vs 28-91 d)
    `horizon` = the shortest-span window whose rise clears the threshold, so a
    fresh spike reads as "rising now" and a slow climb falls through to quarter.
    A `breakout` (a sharp recent spike — e.g. a slow 1-3 month climber that
    suddenly jumps in month 2) is force-promoted to "rising now" so we catch it
    ASAP. When `dates` are missing/misaligned, fall back to positional slicing of
    the series tail.
    """
    vals = [float(v) for v in series if isinstance(v, (int, float))]

    # (age_in_days, value) pairs measured back from the series' latest point.
    dated: list[tuple[int, float]] = []
    if dates and len(dates) == len(series):
        parsed = [(_iso_date(ds), v) for ds, v in zip(dates, series)]
        valid = [(dv, v) for dv, v in parsed if dv is not None and isinstance(v, (int, float))]
        if valid:
            anchor = max(dv for dv, _ in valid)
            dated = [((anchor - dv).days, float(v)) for dv, v in valid]
    use_dates = len(dated) >= 4

    def _wmean(lo: int, hi: int) -> float | None:
        pts = [v for age, v in dated if lo <= age < hi]
        return sum(pts) / len(pts) if pts else None

    def ratio_days(recent_hi: int, base_hi: int) -> float | None:
        rm, bm = _wmean(0, recent_hi), _wmean(recent_hi, base_hi)
        if rm is None or bm is None or bm <= 0:
            return None
        return round(rm / bm, 2)

    def ratio_pos(recent_n: int, base_n: int) -> float | None:
        if len(vals) < recent_n + base_n:
            return None
        recent = vals[-recent_n:]
        base = vals[-(recent_n + base_n):-recent_n]
        rm, bm = sum(recent) / len(recent), sum(base) / len(base)
        if bm <= 0:
            return None
        return round(rm / bm, 2)

    if use_dates:
        week = ratio_days(14, 42)     # rising now:       0-14 d vs 14-42 d
        month = ratio_days(28, 56)    # rising this month: 0-28 d vs 28-56 d
        quarter = ratio_days(28, 91)  # rising 1-3 months: 0-28 d vs 28-91 d
    else:
        week = ratio_pos(2, 4)
        month = ratio_pos(4, 4)
        quarter = ratio_pos(4, 9)

    # Breakout: a sharp, RECENT jump worth catching immediately. The peak of the
    # recent window clears 1.5x the trailing baseline. Today-anchored, so a stale
    # series (no points in the recent window) cannot raise a phantom breakout.
    BREAK = 1.5
    breakout = False
    if use_dates:
        recent_pts = [v for age, v in dated if age < 21]
        base_pts = [v for age, v in dated if 21 <= age < 63]
        if recent_pts and base_pts:
            bm = sum(base_pts) / len(base_pts)
            if bm > 0 and max(recent_pts) >= BREAK * bm:
                breakout = True
    elif len(vals) >= 7:
        base = vals[-7:-1]
        bm = sum(base) / len(base) if base else 0
        if bm > 0 and vals[-1] >= BREAK * bm:
            breakout = True

    THRESH = 1.15
    horizon = None
    if week is not None and week >= THRESH:
        horizon = "week"
    elif month is not None and month >= THRESH:
        horizon = "month"
    elif quarter is not None and quarter >= THRESH:
        horizon = "quarter"
    if breakout:  # a fresh breakout is the most actionable signal
        horizon = "week"

    return {"week": week, "month": month, "quarter": quarter,
            "horizon": horizon, "breakout": breakout}


def _monthly_series(series: list, dates: list) -> list[dict]:
    """Collapse the ~weekly interest series into calendar-month means.

    Weekly Google Trends data is noisy and lags ~1 month, so a month-resolution
    view reads cleaner and is honest about currency. Returns chronological
    `[{"m": "YYYY-MM", "v": <rounded mean interest>}]` aligned by `raw_dates`;
    empty when dates are missing/misaligned.
    """
    if not dates or len(dates) != len(series):
        return []
    buckets: dict[str, list[float]] = {}
    for d, v in zip(dates, series):
        if not isinstance(d, str) or len(d) < 7 or not isinstance(v, (int, float)):
            continue
        buckets.setdefault(d[:7], []).append(float(v))
    return [
        {"m": m, "v": round(sum(vs) / len(vs), 1)}
        for m, vs in sorted(buckets.items())
        if vs
    ]


def _keyword_sv_map(d: Path) -> dict[str, dict]:
    """Keyword (lowercased) → real monthly search volume, from DataForSEO keyword data.

    Google Trends gives normalized 0–100 interest; `keyword-data.json` carries the
    *actual* monthly search-volume series (`monthly_searches_trend`). Joining them lets a
    trend card show real volume per month ("May 18.1k · Jun 22.2k …"), not just interest.
    Returns `{kw: {"sv": <headline volume>, "monthly_sv": [{"m": "YYYY-MM", "v": int}]}}`.
    """
    data = _read_json(d / "keyword-data.json")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return {}
    out: dict[str, dict] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        kw = (r.get("keyword") or "").strip().lower()
        if not kw:
            continue
        monthly_sv = sorted(
            (
                {"m": f"{int(t['year']):04d}-{int(t['month']):02d}", "v": t.get("search_volume")}
                for t in (r.get("monthly_searches_trend") or [])
                if isinstance(t, dict) and t.get("year") and t.get("month")
            ),
            key=lambda x: x["m"],
        )
        out[kw] = {"sv": r.get("monthly_searches"), "monthly_sv": monthly_sv}
    return out


def _norm_related(rq: object) -> dict:
    """Normalize a keyword's related-query payload from trends.json.

    The DFS trends scraper stores `{"top": [{"query","search_interest"}],
    "rising": [{"query","change"}]}` where a rising `change` is either an int
    %-increase or the string "Breakout" (>5000%). We pass it through defensively
    so the trend cards can surface the *rising* sub-queries (the "portable"
    signal) — i.e. how the searched intent under a head keyword is moving right
    now, which is what maps into the 5-variant titles.
    """
    if not isinstance(rq, dict):
        return {"top": [], "rising": []}
    def _clean(items: object, val_key: str) -> list[dict]:
        out: list[dict] = []
        for q in (items if isinstance(items, list) else []):
            if not isinstance(q, dict):
                continue
            query = (q.get("query") or "").strip()
            # The rising/top related queries become the card's "Leading:" + title seeds — hold them
            # to the SAME central listable bar (brands / events / AI non-products / SERP-modifier
            # noise) so they never seed a bad title.
            if not query or not is_listable_keyword(query):
                continue
            out.append({"query": query, val_key: q.get(val_key)})
        return out
    return {
        "top": _clean(rq.get("top"), "search_interest"),
        "rising": _clean(rq.get("rising"), "change"),
    }


def _trend_rows(d: Path) -> list[dict]:
    """Google Trends interest series per keyword (skips error-only entries)."""
    data = _read_json(d / "trends.json")
    if not isinstance(data, list):
        return []
    sv_map = _keyword_sv_map(d)
    out: list[dict] = []
    for r in data:
        if not isinstance(r, dict) or r.get("error") or "raw_series" not in r:
            continue
        dates = r.get("raw_dates") or []
        series = r.get("raw_series") or []
        sv = sv_map.get((r.get("keyword") or "").strip().lower(), {})
        out.append({
            "keyword": r.get("keyword"),
            "geo": r.get("geo"),
            "trend_verdict": r.get("trend_verdict"),
            "evergreen_verdict": r.get("evergreen_verdict"),
            "growth_ratio": r.get("growth_ratio_recent_vs_early"),
            "peak_month": r.get("peak_month"),
            "trough_month": r.get("trough_month"),
            "mean_interest": r.get("mean_interest"),
            "raw_series": series,
            "raw_dates": dates,
            "monthly": _monthly_series(series, dates),
            "sv": sv.get("sv"),
            "monthly_sv": sv.get("monthly_sv") or [],
            "related_queries": _norm_related(r.get("related_queries")),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
        })
    return out


def _buyer_voice(d: Path) -> dict | None:
    """Parsed buyer-voice corpus (Step 03 ICP mining) — verbatim quotes + tag rollups.

    Only some dossiers carry one; returns None when absent. The shape is uniform:
      { quotes: [{text, source, source_ref, tags}], tag_counts, source_counts,
        key_findings_summary }
    """
    bf = next(iter(sorted(d.glob("*buyer-voice*.json"))), None)
    data = _read_json(bf) if bf else None
    if not isinstance(data, dict):
        return None
    quotes = [
        {
            "text": q.get("text"),
            "source": q.get("source"),
            "source_ref": q.get("source_ref"),
            "tags": q.get("tags") or [],
        }
        for q in (data.get("quotes") or [])
        if isinstance(q, dict) and q.get("text")
    ]
    tag_counts = data.get("tag_counts") or {}
    # Sorted (tag, count) so the UI can render a ranked tag cloud without re-sorting.
    top_tags = sorted(
        ((k, v) for k, v in tag_counts.items() if isinstance(v, (int, float))),
        key=lambda kv: -kv[1],
    )
    return {
        "file": bf.name,
        "n_quotes": len(quotes),
        "quotes": quotes,
        "tag_counts": tag_counts,
        "top_tags": [{"tag": k, "count": v} for k, v in top_tags],
        "source_counts": data.get("source_counts") or {},
        "key_findings": _key_findings(data.get("key_findings_summary")),
    }


def _key_findings(kf: object) -> list[dict]:
    """Normalize the key-findings summary into a uniform list of {label, text}.

    Across dossiers this field is either a plain string (one finding) or a dict
    keyed by theme (e.g. {"incumbent_service_failure": "..."}). React can't render
    a raw object, so flatten to a list the frontend can always map over.
    """
    if isinstance(kf, str):
        s = kf.strip()
        return [{"label": None, "text": s}] if s else []
    if isinstance(kf, dict):
        out: list[dict] = []
        for k, v in kf.items():
            if not isinstance(v, str):
                v = str(v)
            v = v.strip()
            if not v:
                continue
            label = str(k).replace("_", " ") if k else None
            out.append({"label": label, "text": v})
        return out
    if isinstance(kf, list):
        out = []
        for item in kf:
            if isinstance(item, str) and item.strip():
                out.append({"label": None, "text": item.strip()})
        return out
    return []


# Domain-discovery JSONs use several filename conventions across dossiers; some are
# hand-edited and occasionally malformed, so this is best-effort + tolerant.
_DOMAIN_GLOBS = ("*brandable*.json", "*domain-discovery*.json", "domain-search*.json")


def _domains(d: Path) -> dict | None:
    """Brandable + auction domain candidates from the Step 04 domain-discovery output."""
    seen: set[str] = set()
    files: list[Path] = []
    for pat in _DOMAIN_GLOBS:
        for p in sorted(d.glob(pat)):
            if p.name not in seen:
                seen.add(p.name)
                files.append(p)
    brandable: list[dict] = []
    auctions: list[dict] = []
    used: str | None = None
    for p in files:
        data = _read_json(p)
        if not isinstance(data, dict):
            continue  # malformed / hand-edited file — skip silently
        bm = data.get("brandable_matches")
        am = data.get("auction_matches")
        if not bm and not am:
            continue
        used = p.name
        for m in bm or []:
            if isinstance(m, dict) and m.get("name"):
                brandable.append(_domain_row(m))
        for m in am or []:
            if isinstance(m, dict) and m.get("name"):
                auctions.append(_domain_row(m))
        break  # first file that actually has matches wins
    if not brandable and not auctions:
        return None
    brandable.sort(key=lambda r: -(r["brandability_score"] or 0))
    return {"file": used, "brandable": brandable, "auctions": auctions}


def _domain_row(m: dict) -> dict:
    return {
        "name": m.get("name"),
        "status": m.get("status"),
        "source": m.get("source"),
        "price_usd": m.get("price_usd"),
        "valuation_usd": m.get("valuation_usd"),
        "is_premium": m.get("is_premium"),
        "bid_count": m.get("bid_count"),
        "end_time_iso": m.get("end_time_iso"),
        "brandability_score": m.get("brandability_score"),
        "listing_url": m.get("listing_url"),
        "notes": m.get("notes"),
    }


def dossier_detail(slug: str) -> dict | None:
    """Everything the Niche & Keyword Research detail page renders for one niche.

    Surfaces the REAL numbers the discovery pipeline captured — per-keyword search
    volume + CPC + competition + the monthly SV series, the Google Trends interest
    series, and the SERP price/merchant summary — plus the buyer-voice corpus,
    domain candidates, and an inventory of the markdown docs and chart images on
    disk (served via the /file and /image endpoints).
    """
    d = _dossier_dir(slug)
    if d is None:
        return None

    keywords, location, kw_file = _keyword_rows(d)
    trends = _trend_rows(d)
    serp_summary = _read_json(d / "serp-summary.json")
    buyer_voice = _buyer_voice(d)
    domains = _domains(d)

    docs = [
        {"name": p.name, "kind": _doc_kind(p.name)}
        for p in sorted(d.glob("*.md"))
    ]
    docs.sort(key=lambda doc: ({"report": 0, "strategy": 1, "framing": 2, "icp": 3}.get(doc["kind"], 9), doc["name"]))
    images = [p.name for p in sorted(d.glob("*.png")) + sorted(d.glob("*.jpg"))]

    total_sv = sum(k["sv"] for k in keywords if k["sv"]) or 0
    top = keywords[0] if keywords else None

    return {
        "slug": slug,
        "is_pool": slug.startswith("_"),
        "location": location,
        "keyword_file": kw_file,
        "summary": {
            "n_keywords": len(keywords),
            "total_sv": total_sv,
            "top_keyword": top["keyword"] if top else None,
            "top_sv": top["sv"] if top else None,
            "n_trends": len(trends),
            "n_docs": len(docs),
            "n_images": len(images),
            "n_quotes": buyer_voice["n_quotes"] if buyer_voice else 0,
            "n_domains": (len(domains["brandable"]) + len(domains["auctions"])) if domains else 0,
        },
        "keywords": keywords,
        "trends": trends,
        "serp_summary": serp_summary,
        "buyer_voice": buyer_voice,
        "domains": domains,
        "docs": docs,
        "images": images,
    }


def resolve_dossier_file(slug: str, rel: str) -> Path | None:
    """Resolve a file inside a dossier, refusing path traversal."""
    d = _dossier_dir(slug)
    if d is None:
        return None
    target = (d / rel).resolve()
    if not target.is_relative_to(d) or not target.is_file():
        return None
    return target


def list_dossiers() -> list[dict]:
    base = config.dossiers_dir()
    if not base.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(base.iterdir()):
        if not p.is_dir():
            continue
        report = next(iter(p.glob("*-report.md")), None) or (
            p / "dossier.md" if (p / "dossier.md").exists() else None
        )
        out.append({
            "slug": p.name,
            "is_pool": p.name.startswith("_"),
            "has_report": report is not None,
            "report": report.name if report else None,
        })
    return out


def trends_overview() -> dict:
    """Cross-pipeline trend signal aggregator for the Trend Research surface.

    Walks every keyword-first dossier (trends.json via _trend_rows) AND every pain-first
    dossier (its own trends.json), surfacing each keyword's Google Trends interest read so
    the operator can scan momentum in one place. Pure on-disk; empty until trend data exists.
    """
    rows: list[dict] = []

    base = config.dossiers_dir()
    if base.is_dir():
        for p in sorted(base.iterdir()):
            if not p.is_dir():
                continue
            for t in _trend_rows(p):
                rows.append({**t, "slug": p.name, "pipeline": "keyword-first"})

    pbase = config.dossiers_pain_first_dir()
    if pbase.is_dir():
        for p in sorted(pbase.iterdir()):
            if not p.is_dir() or p.name.startswith("_"):
                continue
            raw = _read_json(p / "trends.json")
            if not isinstance(raw, list):
                continue
            for t in raw:
                if not isinstance(t, dict) or t.get("error") or "raw_series" not in t:
                    continue
                tdates = t.get("raw_dates") or []
                tseries = t.get("raw_series") or []
                rows.append({
                    "keyword": t.get("keyword"),
                    "geo": t.get("geo"),
                    "trend_verdict": t.get("trend_verdict"),
                    "evergreen_verdict": t.get("evergreen_verdict"),
                    "growth_ratio": t.get("growth_ratio_recent_vs_early"),
                    "peak_month": t.get("peak_month"),
                    "trough_month": t.get("trough_month"),
                    "mean_interest": t.get("mean_interest"),
                    "raw_series": tseries,
                    "raw_dates": tdates,
                    "monthly": _monthly_series(tseries, tdates),
                    "related_queries": _norm_related(t.get("related_queries")),
                    "first_date": tdates[0] if tdates else None,
                    "last_date": tdates[-1] if tdates else None,
                    "slug": p.name,
                    "pipeline": "pain-first",
                })

    # Drop meta/non-product cards (e.g. a legacy "trending products" seed) — they carry Google's
    # generic trending noise, not a real product-demand signal, and pollute the momentum ranking +
    # the "title seeds" the listing side reads. Done before bucketing so counts reflect reality.
    rows = [r for r in rows if not is_meta_keyword(r.get("keyword"))]

    # Operator-curated hide list (per-deployment). Drop dismissed keyword cards before
    # any bucketing/counting so the totals reflect what the operator actually sees.
    dismissed = dismissed_trend_keys()
    if dismissed:
        rows = [r for r in rows if _trend_key(r.get("slug"), r.get("keyword"), r.get("geo")) not in dismissed]

    # Hold trends to a GENERIC-PRODUCT bar: drop service/info/local junk AND brands ('dreo fan',
    # 'shark fan') AND dated events ('father's day 2025', 'june 2026') — none are dropship-sourceable
    # product trends, so they pollute the feed.
    rows = [r for r in rows if is_listable_keyword(r.get("keyword"))]

    def _verdict_bucket(v: str | None) -> str:
        s = (v or "").lower()
        if "break" in s or "surg" in s or "rising" in s or "up" in s:
            return "rising"
        if "declin" in s or "fad" in s or "down" in s:
            return "declining"
        if "season" in s:
            return "seasonal"
        if "evergreen" in s or "stable" in s or "flat" in s:
            return "evergreen"
        return "other"

    today = date.today()
    for r in rows:
        r["bucket"] = _verdict_bucket(r.get("trend_verdict")) if r.get("trend_verdict") else _verdict_bucket(r.get("evergreen_verdict"))
        # Recent-momentum read on the raw interest series (≈ weekly points),
        # anchored to TODAY via raw_dates: how strongly is the keyword rising over
        # short / mid / longer windows. Powers the rising-horizon tabs (rising now
        # / this month / 1–3 months) + breakout capture.
        g = _recent_growth(r.get("raw_series") or [], r.get("raw_dates") or [])
        r["growth_week"] = g["week"]
        r["growth_month"] = g["month"]
        r["growth_quarter"] = g["quarter"]
        r["horizon"] = g["horizon"]
        r["breakout"] = g["breakout"]
        # How stale is this signal vs today — "rising now" is only honest if the
        # series reaches ~today. weekly Trends data should be ≤10 d old when fresh.
        ld = _iso_date(r.get("last_date"))
        r["days_old"] = (today - ld).days if ld else None
        r["stale"] = r["days_old"] is not None and r["days_old"] > 10
        r.pop("raw_dates", None)

    # Collapse morphological/plural duplicates ('cool blanket' / 'cooling blanket' / 'cooling
    # blankets') into ONE card per market — keep the strongest signal (breakout, then recent-month
    # growth, then SV). 'cold blanket' stays distinct.
    _tbest: dict[tuple, dict] = {}
    for r in rows:
        k = (r.get("geo"), _dedup_key(r.get("keyword")))
        if not k[1]:
            continue
        cur = _tbest.get(k)
        rank = (int(bool(r.get("breakout"))), r.get("growth_month") or 0, r.get("sv") or 0)
        if cur is None or rank > (
                int(bool(cur.get("breakout"))), cur.get("growth_month") or 0, cur.get("sv") or 0):
            _tbest[k] = r
    rows = list(_tbest.values())

    # Rising/seasonal first (most actionable), then by growth ratio desc.
    order = {"rising": 0, "seasonal": 1, "evergreen": 2, "other": 3, "declining": 4}
    rows.sort(key=lambda r: (order.get(r["bucket"], 5), -(r.get("growth_ratio") or 0)))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1

    ages = [r["days_old"] for r in rows if r.get("days_old") is not None]
    data_age_days = min(ages) if ages else None  # freshest signal available

    return {
        "totals": {
            "trends": len(rows),
            "rising": counts.get("rising", 0),
            "seasonal": counts.get("seasonal", 0),
            "evergreen": counts.get("evergreen", 0),
            "declining": counts.get("declining", 0),
            "horizon_week": sum(1 for r in rows if r.get("horizon") == "week"),
            "horizon_month": sum(1 for r in rows if r.get("horizon") == "month"),
            "horizon_quarter": sum(1 for r in rows if r.get("horizon") == "quarter"),
            "breakout": sum(1 for r in rows if r.get("breakout")),
            "stale": sum(1 for r in rows if r.get("stale")),
            "data_age_days": data_age_days,
        },
        "trends": rows,
    }


def news_signals() -> dict:
    """Read the news-radar snapshot (news-radar/news.json) — the EARLIEST leading signal.

    News-volume velocity (GDELT, via news_radar.py) leads the Google-search breakout by
    hours-to-days on acute events. This surfaces the persisted snapshot for the Trend
    Research surface's Early-signals tab: per-theme state (BREAKOUT / RISING / FLAT), surge
    ratio, alert date (when an operator watching would've been pinged), distinct outlets, and
    the product keywords each theme drives. Pure on-disk; stable empty shape until synced.
    """
    empty = {
        "synced_at": None,
        "synced_ago_seconds": None,
        "geo": None,
        "timespan": None,
        "params": {},
        "signals": [],
        "totals": {"signals": 0, "breakout": 0, "rising": 0, "flat": 0, "no_data": 0},
        "has_snapshot": False,
    }
    raw = _read_json(config.news_dir() / "news.json")
    if not isinstance(raw, dict):
        return empty

    signals = [s for s in (raw.get("signals") or []) if isinstance(s, dict)]
    # Keep the most actionable first: BREAKOUT, then RISING, then by surge magnitude.
    state_order = {"BREAKOUT": 0, "RISING": 1, "FLAT": 2, "NO_DATA": 3}
    signals.sort(key=lambda s: (state_order.get(s.get("state"), 9),
                                -float(s.get("surge_ratio") or 0)))

    synced_at = raw.get("synced_at")
    ago = None
    if isinstance(synced_at, str):
        try:
            from datetime import datetime, timezone
            ts = datetime.strptime(synced_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ago = max(int((datetime.now(timezone.utc) - ts).total_seconds()), 0)
        except (ValueError, TypeError):
            ago = None

    def _count(state: str) -> int:
        return sum(1 for s in signals if s.get("state") == state)

    return {
        "synced_at": synced_at,
        "synced_ago_seconds": ago,
        "geo": raw.get("geo"),
        "timespan": raw.get("timespan"),
        "params": raw.get("params") or {},
        "signals": signals,
        "totals": {
            "signals": len(signals),
            "breakout": _count("BREAKOUT"),
            "rising": _count("RISING"),
            "flat": _count("FLAT"),
            "no_data": _count("NO_DATA"),
        },
        "has_snapshot": True,
    }


# ---------------------------------------------------------------- product optimization
def optimization_view(store: str) -> dict:
    """Read the optimization snapshot the sync job wrote (general-stores/<store>/optimization.json).

    Pure on-disk + traversal-safe. Returns a stable shape whether or not a snapshot exists yet,
    so the page can always render ("never synced" empty-state vs a real table). Never raises.
    The snapshot itself is built by optimization.build_snapshot — this only surfaces it + adds a
    derived `net` (revenue − refunds) per window/total and a `synced_ago_seconds` staleness read.
    """
    empty = {
        "store": store,
        "synced_at": None,
        "synced_ago_seconds": None,
        "currency": None,
        "data_start_date": None,
        "windows": [7, 14, 30],
        "orders_scanned": 0,
        "truncated": False,
        "ads_connected": False,
        "totals": {},
        "products": [],
        "has_snapshot": False,
        "error": None,
    }
    path = resolve_store_file(store, "optimization.json")
    if path is None:
        return empty
    snap = _read_json(path)
    if not isinstance(snap, dict):
        return empty

    # Drop pass-through add-ons (Shipping Protection / order-protection / gift wrap / warranty)
    # from the Product Performance view — they're not real catalog products and skew the KPIs.
    # Reuses PM's canonical exclusion list (same one margin_checker / order_margins apply), on
    # READ so it takes effect on the existing snapshot without a re-sync. Lazy import avoids a
    # module-load cycle (product_mgmt ↔ readers).
    try:
        prods = snap.get("products")
        if isinstance(prods, list):
            snap["products"] = [p for p in prods if not _is_excluded_addon_title((p or {}).get("title"))]
    except Exception:  # noqa: BLE001 — never let the filter break the view
        pass

    # Recompute the window totals as Σ(visible rows) so the KPI cards always tie to what's shown
    # after the add-on filter above (orders stays the scan count). Ad-spend fully re-reconciles on
    # the next sync — with the add-on gone it no longer dilutes the revenue-share split.
    _tot = snap.get("totals")
    _rows = snap.get("products") or []
    if isinstance(_tot, dict):
        for _wk, _t in _tot.items():
            if not isinstance(_t, dict):
                continue
            for _f in ("qty", "clicks", "impressions"):
                _t[_f] = int(sum(int(((p.get("windows") or {}).get(_wk) or {}).get(_f) or 0) for p in _rows))
            for _f in ("cv", "refunds", "cog", "cost", "conversions", "conv_value"):
                _t[_f] = round(sum(float(((p.get("windows") or {}).get(_wk) or {}).get(_f) or 0) for p in _rows), 2)

    def _net(window: dict) -> dict:
        if not isinstance(window, dict):
            return window
        cv = float(window.get("cv") or 0)
        rf = float(window.get("refunds") or 0)
        net = cv - rf
        window["net"] = round(net, 2)
        # Per-product economics (cross-app COGS made visible): profit = net revenue − COGS
        # (PM's invoice-derived landed cost, joined into the snapshot) − ad spend (Google Ads
        # Script per-product cost, when connected). margin = profit / net revenue. This is what
        # turns Product Performance from "what's earning" into "what actually MAKES money".
        cog = float(window.get("cog") or 0)
        ad = float(window.get("cost") or 0)  # ad spend for this product/window (0 until Ads Script)
        profit = net - cog - ad
        window["profit"] = round(profit, 2)
        # Margin % is profit as a share of GROSS revenue (cv), NOT of net. Dividing by net blew up to
        # absurd values (−657137%) on heavily-refunded products where net collapses toward €0 while
        # profit stays large-negative. Gross base is bounded + matches the Variant Breakdown modal.
        window["margin_pct"] = round(profit / cv * 100, 1) if cv > 0 else None
        return window

    for w in (snap.get("totals") or {}).values():
        _net(w)
    for prod in snap.get("products") or []:
        for w in (prod.get("windows") or {}).values():
            _net(w)
        # Per-market (country) windows get the same net/profit/margin derivation.
        for mkt in (prod.get("markets") or {}).values():
            if isinstance(mkt, dict):
                for w in mkt.values():
                    _net(w)

    # Merge server-persisted per-product flags (Exclude + Note) onto each row.
    try:
        from . import optimization as _opt
        _flags = _opt.get_flags(store)
    except Exception:  # noqa: BLE001
        _flags = {}
    for prod in snap.get("products") or []:
        _pid = str(prod.get("product_id") or "").rsplit("/", 1)[-1]
        _f = _flags.get(_pid)
        prod["hidden"] = bool(_f and _f.get("hidden"))
        prod["note"] = _f.get("note") if _f else None
        # Tags shown/filtered on = the product's Shopify tags UNION the app-side optimization tags.
        _shop_tags = prod.get("tags") or []
        _app_tags = (_f.get("tags") if _f else None) or []
        prod["tags"] = sorted({str(t) for t in [*_shop_tags, *_app_tags] if str(t).strip()})
        prod["app_tags"] = _app_tags  # the editable subset (Shopify tags are read-only here)

    synced_ago = None
    synced_at = snap.get("synced_at")
    if synced_at:
        try:
            ts = datetime.fromisoformat(str(synced_at).replace("Z", "+00:00"))
            synced_ago = int((datetime.now(timezone.utc) - ts).total_seconds())
        except ValueError:
            synced_ago = None

    snap["synced_ago_seconds"] = synced_ago
    snap["has_snapshot"] = True
    snap.setdefault("error", None)
    return snap


# ---------------------------------------------------------------- listing game plan
# The DAILY listing calendar that sits in front of both import methods. It pools the
# product pipeline from ALL research surfaces — keyword candidates, trend rows, and
# winning competitor products — and lays them out on a per-day schedule across a chosen
# window (this week = 7 days / this month = 30 days). The operator's primary lever is
# `per_day`: how many products to list each day. Window × per_day = the daily capacity;
# the highest-priority pooled items fill those dated slots.
#
# Each item carries a TIMING horizon:
#   now    ← trending / winning right now  (BREAKOUT + LIST-NOW keywords, rising trends,
#                                           climbing/fresh winning products)
#   month  ← build ahead of the curve      (BUILD-AHEAD keywords, seasonal trends)
# EVERGREEN is intentionally DROPPED here — it belongs only to the niche-store path, not
# the general store. SKIP / declining / un-gated signals are excluded.
#
# This is a PLANNING surface — it does not execute the import. Each item carries a
# suggested listing method the operator can switch (source-import vs research-pipeline).
_PLAN_HORIZON = {
    "BREAKOUT": "now",
    "LIST-NOW": "now",
    "BUILD-AHEAD": "month",
}
_PLAN_WINDOWS = {"week": 7, "month": 30}
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# DEPRECATED: the old momentum dial's default, kept only so `trend_bias` args from saved
# gameplans / old clients still parse. Trending keywords are their own weighted lane now.
_PLAN_TREND_BIAS_DEFAULT = 50


# THE KEYWORD IS STILL THE ATOM, but TRENDING is its own LANE (operator 2026-07-07 —
# supersedes the trend_bias reorder dial, which is now accepted-and-ignored for old saved
# gameplans). The three keyword lanes are EXCLUSIVE — a keyword lives in exactly one:
#   trending_now   — rising-momentum keywords (breakout/rising trend signal), whether they
#                    came from the trend feed or are gate-cleared candidates that are rising.
#                    NOT the same as list-now: list-now is steady in-season demand; trending
#                    is the momentum subset, listed with urgency while the spike is live.
#   keyword_now    — steady gate-cleared keywords whose capture window is NOW (in season).
#   keyword_ahead  — steady gate-cleared keywords to build ahead of season (1–3 mo).
# Exclusivity is what makes a separate trending WEIGHT safe (the old parallel-trend-weights
# design double-counted because the same keyword sat in two pools; this one can't).
# Product lanes are unchanged. Each category has a DEFAULT weight (%); sums to 100.
PLAN_CATEGORIES = [
    {"id": "trending_now",  "name": "Trending now",                    "source": "trend",       "horizon": "now",   "weight": 15},
    {"id": "keyword_now",   "name": "Keyword · list now (in season)", "source": "keyword",     "horizon": "now",   "weight": 25},
    {"id": "keyword_ahead", "name": "Keyword · build ahead (1–3 mo)",  "source": "keyword",     "horizon": "month", "weight": 15},
    {"id": "winning",       "name": "Google competitor",      "source": "winning",     "horizon": "now",   "weight": 20},
    {"id": "marketplace",   "name": "Marketplace",            "source": "marketplace", "horizon": "now",   "weight": 10},
    {"id": "amazon",        "name": "Amazon best-sellers",    "source": "amazon",      "horizon": "now",   "weight": 10},
    {"id": "meta",          "name": "Meta ad winners",        "source": "meta",        "horizon": "now",   "weight": 5},
]
_PLAN_CAT_IDS = [c["id"] for c in PLAN_CATEGORIES]
_PLAN_DEFAULT_WEIGHTS = {c["id"]: c["weight"] for c in PLAN_CATEGORIES}
# candidate-queue-derived categories are per-store; trend + winning are cross-pipeline
# research signals not bound to one store, so the store selector leaves them in place.
_PLAN_STORE_BOUND = {"keyword_now", "keyword_ahead", "marketplace", "amazon", "meta"}


_FINDING_CACHE_SUBDIRS = ("marketplace", "temu", "alibaba", "amazon")


def _has_finding_cache(term: str) -> bool:
    """Cheap existence probe — has ANY finding lane cached results for this term? Lets the plan's
    found-count skip the (cache-only but thread-pooled) lane read for the many keywords nobody has
    run a Find on yet, so summing across every planned keyword stays fast."""
    try:
        slug = _scan_slug(term)
        base = config.spy_data_dir() / "finding-cache"
        return any((base / sub / f"{slug}.json").is_file() for sub in _FINDING_CACHE_SUBDIRS)
    except OSError:
        return False


def _candidate_found_products(c: dict) -> int:
    """How many real products the discovery lanes have already FOUND for this keyword — the
    candidate-queue lane stamps PLUS any results a live Find has already CACHED (marketplace +
    Amazon). Without the cached side this read 0 while the Find-Products drill-in showed 40: the
    live find fetches + caches but never stamps back onto the candidate, so the dashboard / plan
    'products located' count silently disagreed with the drill-in. Cache-only (fetch=False) — never
    triggers a network fetch, and the cheap cache probe above skips the lane read entirely for
    keywords nobody has run a Find on, so summing across every planned keyword stays cheap. Same
    finding lanes as found_products_for_head, so the two counts stay consistent."""
    seen: set[str] = set()
    n = 0
    for lane in (c.get("lanes") or {}).values():
        if isinstance(lane, dict):
            for p in _lane_products(lane):
                key = (p.get("url") or p.get("title") or "").strip().lower()
                if key:
                    seen.add(key)
                n += 1
    kw = (c.get("keyword") or "").strip()
    if kw and _has_finding_cache(kw):
        try:  # cached live-find results — dedup against the stamps already counted
            extra = _marketplace_lane_products(c.get("store") or "", kw, {kw.lower()}, seen, fetch=False)
            extra += _amazon_lane_products(kw, {kw.lower()}, seen, fetch=False)
            n += len(extra)
        except Exception:  # noqa: BLE001 — the count is best-effort; never break the plan build
            pass
    return n


def _plan_keyword_pools(store: str | None) -> dict[str, list[dict]]:
    """Gate-cleared keyword candidates split into EXCLUSIVE lanes: rising-momentum keywords
    go to `trending_now`; steady ones split list-now / build-ahead by capture horizon."""
    tmap = {
        (t.get("keyword") or "").strip().lower(): t
        for t in trends_overview().get("trends", [])
        if t.get("keyword")
    }
    trending_items: list[dict] = []
    now_items: list[dict] = []
    ahead_items: list[dict] = []
    for c in keyword_discovery().get("candidates", []):
        if store and c.get("store") != store:
            continue
        bucket = str(c.get("capture_bucket") or "").upper()
        horizon = _PLAN_HORIZON.get(bucket)
        if horizon is None:
            if bucket in ("SKIP", "EVERGREEN") or not str(c.get("gate") or "").upper().startswith("PASS"):
                continue
            horizon = "month"
        t = tmap.get((c.get("keyword") or "").strip().lower())
        tb = t.get("bucket") if t else None
        is_rising = str(tb or "").lower() in ("rising", "seasonal")
        category = "trending_now" if is_rising else ("keyword_now" if horizon == "now" else "keyword_ahead")
        item = {
            "source": "trend" if is_rising else "keyword",
            "category": category,
            "keyword": c.get("keyword"),
            "store": c.get("store"),
            "sv": c.get("sv"),
            "capture_bucket": c.get("capture_bucket"),
            "gate": c.get("gate"),
            "score": c.get("score"),
            "n_validation": c.get("n_validation"),
            "momentum": c.get("momentum"),
            "trend_bucket": tb,
            "is_rising": is_rising,
            "horizon": horizon,
            "method": "research-pipeline",
            "found_products": _candidate_found_products(c),
        }
        if is_rising:
            trending_items.append(item)
        elif horizon == "now":
            now_items.append(item)
        else:
            ahead_items.append(item)
    for lst in (trending_items, now_items, ahead_items):
        lst.sort(key=lambda x: (x.get("score") is None, -(x.get("score") or 0)))
    return {"trending_now": trending_items, "keyword_now": now_items, "keyword_ahead": ahead_items}


def _plan_trend_keywords() -> list[dict]:
    """Rising/seasonal trend rows expressed as KEYWORDS for the `trending_now` lane.

    The keyword is still the atom — a trend row is a keyword discovered via momentum. Every
    rising trend query becomes a `trending_now` item (its capture horizon survives as a label:
    week → now, month/quarter → build-ahead timing). Net-new only: `_plan_pools` drops any
    whose keyword already has a gate-cleared candidate (that candidate is the richer record —
    SV / gate / found products — and already sits in `trending_now` itself), so a keyword is
    never double-counted across lanes.
    """
    out: list[dict] = []
    for t in trends_overview().get("trends", []):
        if str(t.get("bucket") or "").lower() not in ("rising", "seasonal"):
            continue
        h = str(t.get("horizon") or "").lower()
        if h not in ("week", "month", "quarter"):
            continue
        horizon = "now" if h == "week" else "month"
        out.append({
            "source": "trend",
            "category": "trending_now",
            "keyword": t.get("keyword"),
            "store": t.get("slug"),
            "sv": None,
            "score": t.get("growth_ratio"),
            "momentum": t.get("growth_week"),
            "trend_bucket": t.get("bucket"),
            "is_rising": True,
            "horizon": horizon,
            "method": "research-pipeline",
            "found_products": 0,
        })
    return out


def _plan_winning_pool() -> list[dict]:
    """Winning competitor products (climbing / fresh movers) → source-import items."""
    out: list[dict] = []
    for p in new_products(only_new=True).get("products", []):
        out.append({
            "source": "winning",
            "category": "winning",
            "keyword": p.get("title"),
            "store": p.get("competitor"),
            "sv": None,
            "score": p.get("rank_delta"),
            "momentum": "fresh" if p.get("is_fresh") else "gainer",
            "horizon": "now",
            "method": "source-import",
            "price": p.get("price"),
            "image": p.get("image"),
            "url": p.get("url"),
        })
    out.sort(key=lambda x: (not x.get("momentum") == "fresh", -(x.get("score") or 0)))
    return out


def _plan_lane_pool(category: str, source: str, rows: list[dict], score_key: str) -> list[dict]:
    """Marketplace / Amazon / Meta stamped-signal rows → plan items."""
    out: list[dict] = []
    for r in rows:
        out.append({
            "source": source,
            "category": category,
            "keyword": r.get("keyword"),
            "store": r.get("store"),
            "sv": r.get("sv"),
            "score": r.get("score") if r.get("score") is not None else r.get(score_key),
            "momentum": r.get(score_key),
            "horizon": "now",
            "method": "source-import",
            "price": r.get("price"),
            "image": r.get("image"),
            "url": r.get("url"),
        })
    return out


def _plan_pools(store: str | None) -> dict[str, list[dict]]:
    """Build every category's prioritized pool. Store filters the candidate-queue lanes.

    The three keyword lanes are EXCLUSIVE: rising keywords (gate-cleared candidates with a
    rising trend signal + net-new keywords from the trend feed) live ONLY in `trending_now`;
    steady keywords split list-now / build-ahead. A keyword that is both gate-cleared and
    rising stays a single `trending_now` item (the candidate wins — richer record: sv / gate /
    found products), so nothing is double-counted and each lane's weight is a real share.
    """
    pools: dict[str, list[dict]] = {}
    kw_pools = _plan_keyword_pools(store)
    pools.update(kw_pools)

    # Append NET-NEW rising keywords from the trend feed (ones without a gate-cleared
    # candidate of their own) to the trending lane, then rank: gate-cleared candidates first
    # (they carry SV + found products — listable immediately), each block by score desc.
    kw_seen = {
        (it.get("keyword") or "").strip().lower()
        for lst in kw_pools.values() for it in lst
    }
    trending = pools.setdefault("trending_now", [])
    for it in _plan_trend_keywords():
        kw = (it.get("keyword") or "").strip().lower()
        if not kw or kw in kw_seen:
            continue
        kw_seen.add(kw)
        trending.append(it)
    trending.sort(key=lambda x: (
        x.get("gate") is None,               # gate-cleared candidates first
        x.get("score") is None,
        -(x.get("score") or 0),
    ))

    pools["winning"] = _plan_winning_pool()

    def _filt(rows: list[dict]) -> list[dict]:
        return [r for r in rows if not store or r.get("store") == store]

    pools["marketplace"] = _plan_lane_pool(
        "marketplace", "marketplace", _filt(marketplace_movers().get("movers", [])), "orders")
    pools["amazon"] = _plan_lane_pool(
        "amazon", "amazon", _filt(amazon_movers().get("movers", [])), "pct_gain")
    pools["meta"] = _plan_lane_pool(
        "meta", "meta", _filt(meta_dropship().get("winners", [])), "ad_longevity_days")
    return pools


def _round_robin(*pools: list[dict]) -> list[dict]:
    """Interleave pre-sorted pools so the daily plan mixes ALL research methods evenly."""
    out: list[dict] = []
    i = 0
    pools = [p for p in pools if p]
    while pools:
        live = [p for p in pools if i < len(p)]
        if not live:
            break
        for p in live:
            out.append(p[i])
        i += 1
    return out


def _resolve_weights(weights: dict | None) -> dict[str, int]:
    """Sanitize an operator weight map → {category_id: int>=0}, falling back to defaults."""
    if not isinstance(weights, dict):
        return dict(_PLAN_DEFAULT_WEIGHTS)
    out: dict[str, int] = {}
    for cid in _PLAN_CAT_IDS:
        try:
            out[cid] = max(0, int(round(float(weights.get(cid, _PLAN_DEFAULT_WEIGHTS[cid])))))
        except (TypeError, ValueError):
            out[cid] = _PLAN_DEFAULT_WEIGHTS[cid]
    return out if any(out.values()) else dict(_PLAN_DEFAULT_WEIGHTS)


def listing_plan(window: str = "week", per_day: int = 50,
                 store: str | None = None, weights: dict | None = None,
                 trend_bias: int = _PLAN_TREND_BIAS_DEFAULT) -> dict:
    """Daily listing calendar pooled from every research category across a window.

    window = "week" (7 days) | "month" (30 days). per_day = products to list each day.
    Window × per_day = daily capacity. The source mix is controlled by a per-category
    WEIGHT (%) — each category fills round(weight/total × capacity) of the slots from its
    prioritized pool; short pools are back-filled from the rest by weight order so the
    calendar stays full. `store` filters the candidate-queue lanes (keyword / marketplace /
    amazon / meta); winning is cross-pipeline and always available. `trend_bias` is
    DEPRECATED and ignored (kept so old saved gameplans / clients don't 400): trending
    keywords are their own weighted `trending_now` lane now, not a reorder dial.
    """
    window = window if window in _PLAN_WINDOWS else "week"
    per_day = max(1, min(50, int(per_day)))
    try:
        trend_bias = max(0, min(100, int(trend_bias)))
    except (TypeError, ValueError):
        trend_bias = _PLAN_TREND_BIAS_DEFAULT
    days = _PLAN_WINDOWS[window]
    capacity = days * per_day

    w = _resolve_weights(weights)
    pools = _plan_pools(store)
    total_w = sum(w.values()) or 1

    # First pass: each weighted category takes its target share of the capacity.
    selected: dict[str, list[dict]] = {}
    idx: dict[str, int] = {}
    for cid in _PLAN_CAT_IDS:
        target = round(w[cid] / total_w * capacity) if w[cid] > 0 else 0
        selected[cid] = pools.get(cid, [])[:target]
        idx[cid] = len(selected[cid])

    # Back-fill any remaining capacity (categories whose pool was shorter than their
    # target leave room) from the leftover items, walking categories by weight desc.
    chosen = sum(len(v) for v in selected.values())
    by_weight = sorted(_PLAN_CAT_IDS, key=lambda c: -w[c])
    while chosen < capacity:
        progressed = False
        for cid in by_weight:
            if w[cid] <= 0:
                continue
            pool = pools.get(cid, [])
            if idx[cid] < len(pool):
                selected[cid].append(pool[idx[cid]])
                idx[cid] += 1
                chosen += 1
                progressed = True
                if chosen >= capacity:
                    break
        if not progressed:
            break

    # Interleave the selected per-category slices (heaviest weight first) so each DAY is a
    # representative cross-section of the mix rather than one category clustered together.
    ordered = _round_robin(*[selected[c] for c in by_weight])[:capacity]

    start = date.today()
    schedule: list[dict] = []
    for i, it in enumerate(ordered):
        day_idx = i // per_day
        d = start + timedelta(days=day_idx)
        schedule.append({**it, "day": day_idx + 1, "date": d.isoformat()})

    day_rows: list[dict] = []
    for day_idx in range(days):
        d = start + timedelta(days=day_idx)
        items = [s for s in schedule if s["day"] == day_idx + 1]
        day_rows.append({
            "day": day_idx + 1,
            "date": d.isoformat(),
            "weekday": _WEEKDAYS[d.weekday()],
            "items": items,
        })

    pool_total = sum(len(v) for v in pools.values())
    categories = [{
        **{k: c[k] for k in ("id", "name", "source")},
        "weight": w[c["id"]],
        "available": len(pools.get(c["id"], [])),
        "scheduled": len(selected.get(c["id"], [])),
    } for c in PLAN_CATEGORIES]

    return {
        "params": {"window": window, "per_day": per_day, "days": days,
                   "capacity": capacity, "store": store, "trend_bias": trend_bias},
        "trend_bias": trend_bias,
        "start_date": start.isoformat(),
        "windows": [
            {"id": "week", "label": "This week", "days": 7},
            {"id": "month", "label": "This month", "days": 30},
        ],
        "stores": list_stores(),
        "days": day_rows,
        "schedule": schedule,
        "categories": categories,
        "weights": w,
        # Coarse sources for any summary consumer. Trending is its own exclusive lane now;
        # `rising` = the trending pool size (kept under the keyword entry for old consumers).
        "sources": [
            {"id": "trend", "name": "Trending now",
             "count": len(pools.get("trending_now", [])),
             "rising": len(pools.get("trending_now", []))},
            {"id": "keyword", "name": "Keyword research",
             "count": len(pools.get("keyword_now", [])) + len(pools.get("keyword_ahead", [])),
             "rising": len(pools.get("trending_now", []))},
            {"id": "winning", "name": "Winning Products", "count": len(pools.get("winning", []))},
        ],
        "methods": [{"id": m["id"], "name": m["name"]} for m in LISTING_METHODS],
        "totals": {
            "pool": pool_total,
            "scheduled": len(schedule),
            "unscheduled": max(0, pool_total - len(schedule)),
            "capacity": capacity,
            "days": days,
        },
    }


# ---------------------------------------------------------------- sourcing match (1688 / Alibaba)
# The OPTIONAL validation gate that sits between Product Research and the Listing Plan:
# before a chosen product goes onto the listing calendar, confirm a Chinese factory
# actually makes it (1688 / Alibaba) at a wholesale price. The toolkit lives at
# 05-launch-niche-store/china-source-match (alibaba_bulk.py → match_china.py VLM judge →
# matched.json with IDENTICAL / UNCERTAIN / DIFFERENT verdicts). This reader surfaces any
# matched.json outputs found on disk + the manual ingestion commands. It is intentionally
# a thin, honest scaffold (empty-but-shaped) — the auto bulk pipeline is deferred until
# the data shape is locked (the step is still operator-flagged "maybe").
_MATCH_VERDICTS = ("IDENTICAL", "UNCERTAIN", "DIFFERENT")

# Source-of-truth precedence for the LISTING build. We lead with 1688/Alibaba: an IDENTICAL
# match means the 1688 listing IS the source of truth (its gallery/variants/specs/price ground
# the listing). If 1688 doesn't have it (DIFFERENT), fall back to the researched source
# (Google/competitor/AliExpress research ref). UNCERTAIN must be eyeballed before committing.
_SOURCE_OF_TRUTH = {"IDENTICAL": "1688", "UNCERTAIN": "verify", "DIFFERENT": "researched"}


# ---------------------------------------------------------------- CN → EN translation
# 1688's sku_props / specs come back in Chinese (颜色: 红色, 风速档位: 3档). Translation lives
# in `translate` (glossary fast-path + cached LLM fallback for arbitrary spec text). These
# thin wrappers keep the historical call sites unchanged.
def _has_cjk(s: str) -> bool:
    return translate.has_cjk(s)


def _to_english(s: str | None) -> str:
    return translate.to_english(s)


def _collect_cjk(obj, into: set[str]) -> None:
    """Recursively gather every Chinese-bearing string value in `obj` (a parsed matched.json
    fragment) so translate.prime() can translate the whole page-load in ONE batched LLM call
    and cache it — before any _to_english() runs during row building."""
    if isinstance(obj, str):
        if translate.has_cjk(obj):
            into.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_cjk(v, into)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_cjk(v, into)


def _norm_variants(obj: dict) -> list[str]:
    """Normalize a research/1688 product's variant data into readable label strings.

    Handles the shapes the pipeline emits: a plain ``variants`` list (strings or
    {name/title/value} dicts), or 1688's ``sku_props`` (list of
    {prop_name/name, values:[{name/value}]}) from tmapi_1688.py --enrich.
    Chinese labels are translated to English via the SKU glossary.
    """
    # Translate at the TOKEN level (name + each value individually) before composing, so each
    # translatable unit is an atomic raw label that translate.prime() has already cached.
    out: list[str] = []
    raw = obj.get("variants")
    if isinstance(raw, list):
        for v in raw:
            if isinstance(v, str):
                out.append(_to_english(v))
            elif isinstance(v, dict):
                label = v.get("name") or v.get("title") or v.get("value") or v.get("option")
                if label:
                    out.append(_to_english(str(label)))
    props = obj.get("sku_props")
    if isinstance(props, list):
        for p in props:
            if not isinstance(p, dict):
                continue
            name = p.get("prop_name") or p.get("name") or p.get("prop")
            vals = p.get("values") or p.get("value") or []
            labels: list[str] = []
            if isinstance(vals, list):
                for x in vals:
                    if isinstance(x, str):
                        labels.append(_to_english(x))
                    elif isinstance(x, dict):
                        lab = x.get("name") or x.get("value") or x.get("title")
                        if lab:
                            labels.append(_to_english(str(lab)))
            elif isinstance(vals, str):
                labels.append(_to_english(vals))
            name_en = _to_english(str(name)) if name else ""
            if name_en and labels:
                out.append(f"{name_en}: {', '.join(labels)}")
            elif labels:
                out.extend(labels)
    # de-dupe preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def _norm_specs(obj: dict) -> list[list[str]]:
    """Normalize a product's specs into [key, value] pairs.

    Accepts ``specs`` as a dict ({key: value}) or a list ([{name/key, value}] or
    [[k, v]] or ["k: v"]). Returns a list of two-string pairs for table rendering.
    """
    raw = obj.get("specs")
    out: list[list[str]] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            out.append([str(k), str(v)])
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                k = item.get("name") or item.get("key") or item.get("attr")
                v = item.get("value") or item.get("val")
                if k is not None:
                    out.append([str(k), str(v) if v is not None else ""])
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append([str(item[0]), str(item[1])])
            elif isinstance(item, str):
                if ":" in item:
                    k, v = item.split(":", 1)
                    out.append([k.strip(), v.strip()])
                else:
                    out.append([item.strip(), ""])
    # translate both key and value CN→EN for an all-English spec table
    return [[_to_english(k), _to_english(v)] for k, v in out]


def _match_provenance(r: dict, best: dict) -> tuple[bool, str]:
    """Decide whether a match record was actually JUDGED (vision/agent) or is an unverified stub.

    This is the integrity gate. A real match_china.py record carries a ``provenance`` stamp
    (judge=openrouter -> vision VLM, judge=agent -> Claude Code packet verification) and/or a
    ``judged`` array of per-candidate VLM verdicts. A hand-typed ``{verdict, best}`` stub has
    NEITHER — so it is flagged ``unverified`` and can never be honoured as a build's source of
    truth (see sourcing_match: an unverified verdict is forced to source_of_truth="verify").
    """
    prov = r.get("provenance") if isinstance(r.get("provenance"), dict) else {}
    judged = r.get("judged")
    # Automated vision judge ran (explicit stamp, or a per-candidate VLM verdict array).
    if prov.get("vision") or prov.get("judge") == "openrouter":
        return True, "vision"
    if isinstance(judged, list) and any(isinstance(j, dict) and "verdict" in j for j in judged):
        return True, "vision"
    # Claude Code agent verified the gallery via packet.json — a real (if manual) judgement.
    if prov.get("judge") == "agent":
        return True, "agent"
    # No record of HOW the verdict was produced -> treat as an unverified stub.
    return False, "unverified"


# The learning store: operator feedback on whether the AI's 1688 find was a good match.
# A flat JSON dict keyed by the match identity, living alongside the matched.json outputs.
# This is what closes the loop — the judge's verdicts become a labelled dataset the judge
# can later calibrate against (and the operator can audit the AI's hit-rate today).
_MATCH_FEEDBACK_FILE = "match-feedback.json"


def _match_key(r: dict) -> str:
    """Stable identity for a match row so feedback survives re-runs. Prefer the build slug,
    then the chosen 1688 offer id, then the subject."""
    best = r.get("best") if isinstance(r.get("best"), dict) else {}
    return str(
        r.get("slug")
        or best.get("offer_id")
        or r.get("offer_id")
        or r.get("subject")
        or r.get("handle")
        or ""
    )


def _read_match_feedback() -> dict:
    fb = _read_json(config.china_source_match_dir() / _MATCH_FEEDBACK_FILE)
    return fb if isinstance(fb, dict) else {}


@functools.lru_cache(maxsize=1)
def _search_pools(_mtime: float) -> list[list[dict]]:
    """The raw 1688 image-search candidate pools saved by the search step (one pool of ~20
    real offers per researched product), keyed only so lru_cache busts when the file changes.
    Each pool is a list of offer dicts (offer_id/title/url/image/price/...)."""
    hits = list(config.china_source_match_dir().glob("**/search_results.json"))
    if not hits:
        hits = list(config.china_source_match_dir().parent.glob("**/search_results.json"))
    pools: list[list[dict]] = []
    for p in hits:
        data = _read_json(p)
        rows = data if isinstance(data, list) else (data.get("results") if isinstance(data, dict) else [])
        for row in rows if isinstance(rows, list) else []:
            cands = row.get("candidates") if isinstance(row, dict) else None
            if isinstance(cands, list) and cands:
                pools.append([c for c in cands if isinstance(c, dict)])
    return pools


def _pool_for_offer(offer_id: str | None) -> list[dict]:
    """Find the saved image-search pool whose candidate set CONTAINS this picked offer —
    that join (best offer ∈ pool) is what links a matched row back to the real candidates
    the search returned, so the review window can show the alternatives, not just the pick."""
    oid = str(offer_id or "").strip()
    if not oid:
        return []
    hits = list(config.china_source_match_dir().glob("**/search_results.json")) or \
        list(config.china_source_match_dir().parent.glob("**/search_results.json"))
    mtime = max((p.stat().st_mtime for p in hits), default=0.0)
    for pool in _search_pools(mtime):
        if any(str(c.get("offer_id") or "") == oid for c in pool):
            return pool
    return []


def _candidate_card(c: dict, *, picked: bool, verdict: str | None = None,
                    confidence=None, matching_variant=None, differences=None) -> dict:
    """One candidate offer normalised for the review UI (image + url + verdict + picked)."""
    return {
        "offer_id": c.get("offer_id"),
        "url": c.get("url"),
        "image": c.get("image") or ((c.get("images") or [None])[0]),
        "title": _to_english(c.get("title")) or c.get("title"),
        "verdict": str(verdict or c.get("verdict") or "CANDIDATE").upper(),
        "confidence": confidence if confidence is not None else c.get("confidence"),
        "sold": c.get("sold"),
        "supplier": c.get("supplier"),
        "price": c.get("price"),
        "currency": c.get("currency"),
        "matching_variant": _to_english(matching_variant) or None,
        "differences": [_to_english(d) for d in differences] if differences else None,
        "picked": picked,
    }


def _candidate_list(r: dict, best: dict) -> list[dict]:
    """The per-candidate set for the review UI — each as image + url + verdict, with the
    chosen offer flagged `picked`. Prefers the VLM `judged` array (a real match_china run);
    when that's absent (a seeded row), FALLS BACK to the real image-search candidate pool
    saved in search_results.json so the operator can still open and compare the alternatives
    the AI picked from — not just a lone link to the pick."""
    picked_id = str(best.get("offer_id") or "") or None
    judged = r.get("judged")
    if isinstance(judged, list):
        out: list[dict] = []
        for c in judged:
            if not isinstance(c, dict):
                continue
            diffs = c.get("differences") if isinstance(c.get("differences"), list) else None
            out.append(_candidate_card(
                c, picked=picked_id is not None and str(c.get("offer_id") or "") == picked_id,
                verdict=c.get("verdict"), confidence=c.get("confidence"),
                matching_variant=c.get("matching_variant"), differences=diffs))
        return out
    # No VLM verdicts on disk → surface the saved search pool (best pinned first).
    pool = _pool_for_offer(picked_id)
    if not pool:
        return []
    out = []
    for c in pool:
        is_pick = picked_id is not None and str(c.get("offer_id") or "") == picked_id
        out.append(_candidate_card(
            c, picked=is_pick,
            verdict=(str(r.get("verdict") or best.get("verdict") or "IDENTICAL").upper()
                     if is_pick else "CANDIDATE"),
            confidence=(r.get("confidence") if is_pick else None)))
    out.sort(key=lambda c: (not c["picked"]))  # AI's pick first, then the alternatives
    return out


def record_match_feedback(
    key: str,
    verdict: str,
    correct_offer_id: str | None = None,
    note: str | None = None,
    ai_verdict: str | None = None,
    ai_offer_id: str | None = None,
    subject: str | None = None,
) -> dict:
    """Persist operator feedback on a 1688 match into the learning store, return the
    updated learning summary. `verdict` = 'good' (AI's find was right) | 'bad' (wrong)."""
    key = (key or "").strip()
    if not key:
        raise ValueError("a match key is required")
    verdict = (verdict or "").strip().lower()
    if verdict not in ("good", "bad"):
        raise ValueError("verdict must be 'good' or 'bad'")
    base = config.china_source_match_dir()
    base.mkdir(parents=True, exist_ok=True)
    store = _read_match_feedback()
    store[key] = {
        "verdict": verdict,
        "correct_offer_id": (correct_offer_id or None),
        "note": (note or None),
        "ai_verdict": (ai_verdict or None),
        "ai_offer_id": (ai_offer_id or None),
        "subject": (subject or None),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (base / _MATCH_FEEDBACK_FILE).write_text(json.dumps(store, indent=2), encoding="utf-8")
    return _learning_summary(store)


def _learning_summary(store: dict) -> dict:
    reviewed = len(store)
    good = sum(1 for v in store.values() if isinstance(v, dict) and v.get("verdict") == "good")
    bad = sum(1 for v in store.values() if isinstance(v, dict) and v.get("verdict") == "bad")
    return {
        "reviewed": reviewed,
        "good": good,
        "bad": bad,
        # operator-agreement rate: how often the AI's 1688 find was judged a good match.
        "accuracy": round(good / reviewed, 3) if reviewed else None,
    }


def _apply_operator_feedback(row: dict, fb: dict) -> None:
    """Close the learning loop on a match ROW in place. An operator who eyeballed the
    candidates is the HIGHEST-authority judge — their call OVERRIDES the VLM verdict, and
    because every downstream consumer (resolve_source_of_truth → the listing build) reads
    these rows, the correction grounds the build with NO re-run of the toolkit:
      good                 -> confirm the AI's pick: operator-verified IDENTICAL, 1688 truth
      bad + correct_offer  -> swap in the operator's chosen offer as the 1688 source of truth
      bad + no correction  -> the AI's find is wrong and nothing replaces it: drop 1688,
                              fall back to the researched source.
    """
    verdict = fb.get("verdict")
    if verdict == "good":
        row["verified"] = True
        row["verification"] = "operator"
        row["verdict"] = "IDENTICAL"
        row["source_of_truth"] = "1688"
        row["operator_reviewed"] = "confirmed"
        return
    if verdict == "bad":
        row["verified"] = True
        row["verification"] = "operator"
        correct = str(fb.get("correct_offer_id") or "").strip()
        if correct:
            cand = next(
                (c for c in (row.get("candidates") or [])
                 if str(c.get("offer_id") or "") == correct),
                None,
            )
            # the FULL raw offer (variants/specs/price/gallery) from the saved search pool,
            # so the operator's chosen product fully GROUNDS the build — the listing is made
            # from this offer, not just re-linked by id. Falls back to the lighter UI card.
            raw = next(
                (c for c in _pool_for_offer(correct)
                 if str(c.get("offer_id") or "") == correct),
                None,
            )
            pick = raw or cand or {}
            row["corrected_from"] = row.get("offer_id")
            row["offer_id"] = correct
            row["verdict"] = "IDENTICAL"
            row["source_of_truth"] = "1688"
            row["operator_reviewed"] = "corrected"
            row["url"] = pick.get("url") or f"https://detail.1688.com/offer/{correct}.html"
            row["image"] = pick.get("image") or ((pick.get("images") or [None])[0]) or row.get("image")
            if pick.get("price") is not None:
                row["price"] = pick.get("price")
            if pick.get("currency"):
                row["currency"] = pick.get("currency")
            if pick.get("sold") is not None:
                row["sold"] = pick.get("sold")
            if raw:  # only a full offer carries trustworthy variant/spec structure
                row["variants"] = _norm_variants(raw)
                row["specs"] = _norm_specs(raw)
            # re-flag the candidate set so the chosen offer reads as the pick in the review UI.
            for c in (row.get("candidates") or []):
                if isinstance(c, dict):
                    c["picked"] = str(c.get("offer_id") or "") == correct
        else:
            row["verdict"] = "DIFFERENT"
            row["source_of_truth"] = "researched"
            row["operator_reviewed"] = "rejected"


def _store_from_filename(name: str, data: object) -> str | None:
    """Which business a sourcing/catalog file belongs to. Prefer an explicit `store` written
    inside the file; otherwise fall back to the filename prefix (`<store>-<slug>.matched.json`)
    matched against the registered store keys — longest match wins so multi-word keys are safe."""
    if isinstance(data, dict) and data.get("store"):
        return str(data["store"])
    known = sorted(list_stores(), key=len, reverse=True)
    for key in known:
        if name == key or name.startswith(f"{key}-") or name.startswith(f"{key}."):
            return key
    return None


def sourcing_match(store: str | None = None) -> dict:
    """Surface 1688/Alibaba sourcing-match results (matched.json) + the manual commands.

    When `store` is given, only that business's files are surfaced — the active store in the
    sidebar drives this so two businesses in one app never see each other's sourcing rows."""
    base = config.china_source_match_dir()
    feedback = _read_match_feedback()
    results: list[dict] = []
    files: list[str] = []
    want = (store or "").strip() or None
    if base.is_dir():
        # PASS 1 — read every matched.json and collect all Chinese labels, so we can prime the
        # CN→EN translation cache in ONE batch (glossary + cached LLM) before building rows.
        parsed: list[tuple[str, dict, str | None]] = []  # (filename, row, store)
        cjk_labels: set[str] = set()
        # match_china.py writes `matched.json` (default) — accept any *matched*.json.
        for p in sorted(base.glob("**/*matched*.json")):
            data = _read_json(p)
            rows = data if isinstance(data, list) else (data.get("results") if isinstance(data, dict) else None)
            if not isinstance(rows, list):
                continue
            file_store = _store_from_filename(p.name, data)
            if want and file_store and file_store != want:
                continue
            files.append(p.name)
            for r in rows:
                if isinstance(r, dict):
                    parsed.append((p.name, r, file_store))
                    _collect_cjk(r, cjk_labels)
        translate.prime(list(cjk_labels))

        # PASS 2 — build the rows with a warm translation cache.
        for p_name, r, file_store in parsed:
            if True:
                # The chosen 1688 offer: seed files store it under `best`; a real
                # match_china.py --judge openrouter run nests it under `matched`.
                best = (
                    r.get("best") if isinstance(r.get("best"), dict)
                    else r.get("matched") if isinstance(r.get("matched"), dict)
                    else {}
                )
                verdict = str(r.get("verdict") or best.get("verdict") or "UNCERTAIN").upper()
                # The researched product this 1688 candidate was matched AGAINST (the left side
                # of the comparison): the Amazon/competitor/Google listing Product Research found.
                src = r.get("source") if isinstance(r.get("source"), dict) else (
                    r.get("query") if isinstance(r.get("query"), dict) else {}
                )
                research = None
                if src:
                    research = {
                        "title": src.get("title") or src.get("subject"),
                        "price": src.get("price"),
                        "currency": src.get("currency") or "USD",
                        "url": src.get("url"),
                        "image": src.get("image"),
                        "platform": src.get("platform") or src.get("source"),
                        "rating": src.get("rating"),
                        "reviews": src.get("reviews"),
                        "variants": _norm_variants(src),
                        "specs": _norm_specs(src),
                    }
                verified, verification = _match_provenance(r, best)
                # INTEGRITY GATE: an unverified verdict (a stub with no vision/agent provenance)
                # can NEVER set the build's grounding source — force it to "verify" regardless of
                # what verdict was typed. This is what stops a seeded stub masquerading as truth.
                sot = _SOURCE_OF_TRUTH.get(verdict, "verify") if verified else "verify"
                key = _match_key(r)
                results.append({
                    "key": key,
                    "subject": r.get("subject") or r.get("name") or r.get("handle") or r.get("query"),
                    "slug": r.get("slug"),
                    "verdict": verdict,
                    "verified": verified,
                    "verification": verification,
                    "source_of_truth": sot,
                    "confidence": r.get("confidence") if r.get("confidence") is not None else best.get("confidence"),
                    "offer_id": best.get("offer_id") or r.get("offer_id"),
                    "price": best.get("price") or r.get("price"),
                    "currency": best.get("currency"),
                    "sold": best.get("sold") or r.get("sold"),
                    "url": best.get("url") or r.get("url"),
                    "image": best.get("image") or r.get("image"),
                    "n_candidates": r.get("n_candidates"),
                    "note": r.get("note"),
                    # 1688 side variants/specs (from tmapi_1688.py --enrich: sku_props + specs)
                    "variants": _norm_variants(best),
                    "specs": _norm_specs(best),
                    # the full "N judged" candidate set (image + url + per-candidate verdict),
                    # so the operator can open them and rate whether the AI's find was good.
                    "candidates": _candidate_list(r, best),
                    # operator's learning-loop feedback on THIS match (good/bad find), if any.
                    "feedback": feedback.get(key),
                    # set when the operator's review overrode the AI: "confirmed"/"corrected"/"rejected".
                    "operator_reviewed": None,
                    # if corrected, the AI's original offer id that the operator replaced.
                    "corrected_from": None,
                    # left side of the side-by-side: what we researched and matched against
                    "research": research,
                    "store": file_store,
                    "source": p_name,
                })

    # LEARNING LOOP — let operator reviews override the AI verdict before anything downstream
    # (totals, resolve_source_of_truth, the listing build) reads these rows.
    for row in results:
        fb = row.get("feedback")
        if isinstance(fb, dict):
            _apply_operator_feedback(row, fb)

    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in _MATCH_VERDICTS}
    unverified = sum(1 for r in results if not r["verified"])
    rel = str(config.china_source_match_dir())
    return {
        "available": len(results) > 0,
        "files": files,
        "results": results,
        "totals": {"matched": len(results), "unverified": unverified, **counts},
        # the learning loop's running tally — how often the operator judged the AI's find good.
        "learning": _learning_summary(feedback),
        # The manual hybrid path (mirrors the Amazon/Meta lanes): the operator runs the
        # toolkit, the app reads the matched.json it produces.
        "commands": {
            "find": "python alibaba_bulk.py --keywords '<english product kw>' --out candidates.json",
            "enrich": "python tmapi_1688.py --in candidates.json --enrich --out candidates.json",
            "judge": "python match_china.py --in candidates.json --judge agent --min-conf 0.85 --out matched.json",
        },
        "dir": rel,
        "note": (
            "Lead with 1688/Alibaba — it is checked FIRST and sets the listing's source of truth. "
            "An IDENTICAL match means the 1688 listing IS the source of truth (its gallery / variants / "
            "specs ground the build — PRICE stays from the research source, never the 1688 wholesale "
            "number). If 1688 doesn't have it (DIFFERENT), fall back "
            "to the RESEARCHED source (Google / competitor / AliExpress research ref). UNCERTAIN = eyeball "
            "before committing. Run the toolkit below; matched.json verdicts appear here."
        ),
    }


_SOURCING_1688_KEY = "sourcing_1688_enabled"


def sourcing_1688_enabled() -> bool:
    """Is the 1688 sourcing workflow ON? Default ON. When OFF, listing builds never ground on a
    matched 1688 factory — they build straight from their researched source — and the resolver
    short-circuits so no 1688 source-of-truth is injected anywhere downstream."""
    try:
        v = runlog.setting_get(_SOURCING_1688_KEY)
    except Exception:
        v = None
    if isinstance(v, dict):
        return bool(v.get("enabled", True))
    return True


def set_sourcing_1688_enabled(enabled: bool) -> bool:
    """Persist the 1688-workflow on/off flag; returns the saved value."""
    runlog.setting_set(_SOURCING_1688_KEY, {"enabled": bool(enabled)})
    return bool(enabled)


def resolve_source_of_truth(key: str) -> dict:
    """Resolve which source grounds a product's LISTING build — 1688-first, 2-step fallback.

    Given a product key (its subject/title, slug, or a source URL), look it up in the
    sourcing-match results and decide the grounding source for the listing build:
      IDENTICAL on 1688 -> source_of_truth="1688"      (build from the 1688 offer:
                                                         gallery / variants / specs — PRICE stays from
                                                         the research source, NOT the 1688 wholesale number)
      DIFFERENT         -> source_of_truth="researched" (1688 doesn't have it → ground on the
                                                         researched source: Google/competitor/AliExpress ref)
      UNCERTAIN         -> source_of_truth="verify"     (eyeball before committing)
      no 1688 row at all-> source_of_truth="researched" (resolved=False — never validated against 1688,
                                                         so the listing defaults to its researched origin)

    This is the single resolver both listing paths (slug build + URL import) call so the
    source-of-truth flows into the build automatically and identically.
    """
    if not sourcing_1688_enabled():
        return {
            "resolved": False,
            "disabled": True,
            "key": key,
            "source_of_truth": "researched",
            "source_url": None,
            "offer_id": None,
            "price": None,
            "verdict": None,
            "confidence": None,
            "note": ("1688 sourcing is turned OFF — listings build straight from their researched "
                     "source (Google / competitor / AliExpress ref). Turn it on to ground builds "
                     "on a matched 1688 factory (gallery / variants / specs — price still comes from "
                     "the research source, never the 1688 wholesale number)."),
        }
    sm = sourcing_match()
    rows = sm.get("results", []) if isinstance(sm, dict) else []
    k = (key or "").strip().lower()
    match = None
    if k:
        for r in rows:
            subj = (r.get("subject") or "").strip().lower()
            url = (r.get("url") or "").strip().lower()
            if subj and (k == subj or k in subj or subj in k):
                match = r
                break
            if url and (k == url or k in url or url in k):
                match = r
                break
    if match is None:
        return {
            "resolved": False,
            "key": key,
            "source_of_truth": "researched",
            "source_url": None,
            "offer_id": None,
            "price": None,
            "verdict": None,
            "confidence": None,
            "note": ("No 1688/Alibaba match on file for this product — the listing defaults to its "
                     "researched source. Run Sourcing Match (1688-first) to confirm a factory before building."),
        }
    # sourcing_match() already forced source_of_truth -> "verify" for any unverified (stub) row,
    # so an unverified match can never resolve to a 1688 build-grounding source here.
    sot = match.get("source_of_truth") or "researched"
    verified = bool(match.get("verified"))
    return {
        "resolved": True,
        "key": key,
        "source_of_truth": sot,
        "verified": verified,
        "verification": match.get("verification"),
        # only the 1688 offer URL is a build-grounding source; researched/verify keep the researched origin
        "source_url": match.get("url") if sot == "1688" else None,
        "offer_id": match.get("offer_id"),
        "price": match.get("price"),
        "verdict": match.get("verdict"),
        "confidence": match.get("confidence"),
        "note": (
            "Unverified match (no vision/agent judgement on file) — do NOT ground the build on this. "
            "Run match_china.py (VLM judge) to confirm before committing a source of truth."
            if not verified else
            "1688 has it — build from the 1688 offer (gallery / variants / specs; price stays from the research source, not the 1688 wholesale number)."
            if sot == "1688" else
            "1688 doesn't have it — ground the listing on the researched source."
            if sot == "researched" else
            "Uncertain 1688 match — eyeball before committing the grounding source."
        ),
    }


# ---------------------------------------------------------------- catalog dedup (Step 0, pre-1688)
# The "is it already on the store?" gate that runs BEFORE sourcing-match. After Product
# Research picks a product but before we spend a 1688/Alibaba sourcing call, catalog_scan.py
# scans the store's OWN live catalog and flags any researched product the store already sells
# — judged on the product IMAGE (background-invariant, the same VLM identity judge match_china
# uses), not the title. This reader surfaces any catalog-match-<store>.json the toolkit wrote
# + the index/check commands. Same thin-but-honest scaffold shape as sourcing_match().
_CATALOG_VERDICTS = ("ALREADY_LISTED", "NEW", "UNCERTAIN")


def catalog_scan(store: str | None = None) -> dict:
    """Surface catalog-dedup results (catalog-match-<store>.json) + the manual commands.

    When `store` is given, only that business's store-check rows are surfaced."""
    base = config.china_source_match_dir()
    want = (store or "").strip() or None
    results: list[dict] = []
    files: list[str] = []
    indexed: list[dict] = []
    if base.is_dir():
        for p in sorted(base.glob("**/catalog-match*.json")):
            data = _read_json(p)
            if not isinstance(data, dict):
                continue
            rows = data.get("results")
            if not isinstance(rows, list):
                continue
            file_store = data.get("store") or _store_from_filename(p.name, data)
            if want and file_store and file_store != want:
                continue
            files.append(p.name)
            store = file_store
            for r in rows:
                if not isinstance(r, dict):
                    continue
                results.append({
                    "subject": r.get("subject"),
                    "verdict": str(r.get("verdict") or "UNCERTAIN").upper(),
                    "confidence": r.get("confidence"),
                    "matched_handle": r.get("matched_handle"),
                    "matched_title": r.get("matched_title"),
                    "store_price": r.get("store_price"),
                    "store_currency": r.get("store_currency"),
                    "store_url": r.get("store_url"),
                    "n_checked": r.get("n_checked"),
                    "n_already_listed": r.get("n_already_listed"),
                    "cap": r.get("cap"),
                    "recommended_action": r.get("recommended_action"),
                    "store": store,
                    "source": p.name,
                })
        # the cached catalog index(es) the `index` subcommand wrote
        for p in sorted(base.glob("**/catalog-index*.json")):
            d = _read_json(p)
            if isinstance(d, dict) and d.get("count") is not None:
                idx_store = d.get("store") or _store_from_filename(p.name, d)
                if want and idx_store and idx_store != want:
                    continue
                indexed.append({"store": idx_store, "count": d.get("count"),
                                "generated_at": d.get("generated_at"), "file": p.name})

    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in _CATALOG_VERDICTS}
    return {
        "available": len(results) > 0,
        "files": files,
        "indexes": indexed,
        "results": results,
        "totals": {
            "checked": len(results),
            "already_listed": counts["ALREADY_LISTED"],
            "new": counts["NEW"],
            "uncertain": counts["UNCERTAIN"],
        },
        "commands": {
            "index": "python catalog_scan.py index --store <store> --out catalog-index-<store>.json",
            "check": ("python catalog_scan.py check --store <store> --in researched.json "
                      "--index catalog-index-<store>.json --judge openrouter --out catalog-match-<store>.json"),
        },
        "dir": str(config.china_source_match_dir()),
        "note": (
            "Step 0 BEFORE 1688 sourcing — scan the store's own catalog so we don't re-source a "
            "product we already sell. Identity is judged on the product IMAGE (background-invariant), "
            "so the same product shot on a white supplier background vs a store lifestyle scene still "
            "matches. Verdicts: ALREADY_LISTED (skip sourcing) / NEW (proceed) / UNCERTAIN (eyeball)."
        ),
    }


# ---------------------------------------------------------------- pain-first pipeline (01b)
# The parallel pain-first niche-discovery pipeline (Amin 14-step). Uniform per-niche
# schema: verdict.json (GO/HOLD/SKIP + 7 gates), signals.json, amazon.json,
# trends.json, meta-ads.json, pain-mine.json. Entirely distinct from the keyword-first
# dossiers/ pipeline above.
_PAIN_GATE_LABELS = {
    "pain_strong": "Pain strong",
    "sample_messages_strong": "Sample messages",
    "demand_confirmed": "Demand confirmed",
    "failing_strong": "Solutions failing",
    "aov_pass": "AOV pass",
    "repurchase_pass": "Repurchase",
    "proven_ads": "Proven ads",
}


def _pain_dir(slug: str) -> Path | None:
    base = config.dossiers_pain_first_dir().resolve()
    cand = (base / slug).resolve()
    if cand == base or base not in cand.parents or not cand.is_dir():
        return None
    return cand


def list_pain_first() -> list[dict]:
    """Pain-first niches with their GO/HOLD/SKIP verdict + score (for the list view)."""
    base = config.dossiers_pain_first_dir()
    if not base.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(base.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        v = _read_json(p / "verdict.json") or {}
        gates = v.get("gates") or {}
        out.append({
            "slug": p.name,
            "verdict": v.get("verdict"),
            "score": v.get("score"),
            "max_score": v.get("max_score"),
            "gates_passed": sum(1 for g in gates.values() if g),
            "gates_total": len(gates),
            "has_verdict": bool(v),
        })
    # GO first, then by score desc.
    rank = {"GO": 0, "HOLD": 1, "SKIP": 2}
    out.sort(key=lambda r: (rank.get(r["verdict"], 9), -(r["score"] or 0)))
    return out


def pain_first_detail(slug: str) -> dict | None:
    """Full pain-first dossier: verdict gates, trend reads, demand signals, ad intel."""
    d = _pain_dir(slug)
    if d is None:
        return None
    v = _read_json(d / "verdict.json") or {}
    gates = v.get("gates") or {}
    signals = _read_json(d / "signals.json") or {}
    meta = _read_json(d / "meta-ads.json") or {}

    trends = []
    raw_trends = _read_json(d / "trends.json")
    if isinstance(raw_trends, list):
        for t in raw_trends:
            if not isinstance(t, dict):
                continue
            trends.append({
                "keyword": t.get("keyword"),
                "geo": t.get("geo"),
                "trend_verdict": t.get("trend_verdict"),
                "evergreen_verdict": t.get("evergreen_verdict"),
                "growth_ratio": t.get("growth_ratio_recent_vs_early"),
                "mean_interest": t.get("mean_interest"),
                "slope": t.get("slope_per_period"),
                "peak_month": t.get("peak_month"),
            })

    # sample_messages is a dict of {signal: {count, present, examples}}.
    sample_messages = [
        {
            "signal": k,
            "count": (sv or {}).get("count"),
            "present": (sv or {}).get("present"),
            "examples": ((sv or {}).get("examples") or [])[:4],
        }
        for k, sv in (signals.get("sample_messages") or {}).items()
        if isinstance(sv, dict)
    ]

    return {
        "slug": slug,
        "verdict": v.get("verdict"),
        "score": v.get("score"),
        "max_score": v.get("max_score"),
        "gates": [
            {"id": k, "label": _PAIN_GATE_LABELS.get(k, k), "passed": bool(gates.get(k))}
            for k in (_PAIN_GATE_LABELS if gates else {})
            if k in gates
        ],
        "growing_count": v.get("growing_count"),
        "max_median": v.get("max_median"),
        "table_stakes": v.get("table_stakes") or [],
        "low_star_reviews": [
            {
                "asin": r.get("asin"),
                "rating": r.get("rating"),
                "title": r.get("title"),
                "body": r.get("body"),
            }
            for r in (v.get("low_star_reviews") or [])
            if isinstance(r, dict)
        ],
        "trends": trends,
        "sample_messages": sample_messages,
        "repurchase": signals.get("repurchase"),
        "meta_ads": {
            "n_ads_90d": meta.get("n_ads_90d"),
            "top_brands": meta.get("top_brands"),
            "longest_ad_days": meta.get("longest_ad_days"),
            "hook_archetype": meta.get("hook_archetype"),
            "offer_archetype": meta.get("offer_archetype"),
            "visual_archetype": meta.get("visual_archetype"),
            "emotional_angle": meta.get("emotional_angle"),
            "scale_signal": meta.get("scale_signal"),
        } if meta else None,
        "docs": [{"name": p.name, "kind": _doc_kind(p.name)} for p in sorted(d.glob("*.md"))],
    }


def resolve_pain_first_file(slug: str, rel: str) -> Path | None:
    d = _pain_dir(slug)
    if d is None:
        return None
    target = (d / rel).resolve()
    if not target.is_relative_to(d) or not target.is_file():
        return None
    return target


# ---------------------------------------------------------------- product research (competitor spy)
# Monthly-visit tiers for Google-Shopping competitors (memory: reference_spy_competitor_visit_tiers).
# T3 350K-1M+ / T2 100-350K / T1 30-100K. Below 30K = untiered.
SPY_TIERS = [
    ("T3", 350_000),
    ("T2", 100_000),
    ("T1", 30_000),
]
# Month labels for the 6-point history series (memory: store_traffic.json history = Dec2025..May2026).
SPY_HISTORY_MONTHS = ["Dec", "Jan", "Feb", "Mar", "Apr", "May"]
# A store down more than this MoM is flagged for removal (memory: spy dashboard downtrend rule).
SPY_DROP_FLAG_PCT = -30.0


def _tier_of(visits: int | None) -> str | None:
    if not visits:
        return None
    for label, floor in SPY_TIERS:
        if visits >= floor:
            return label
    return None


def _mom_pct(history: list | None) -> float | None:
    """Month-over-month % from the last two history points (the downtrend signal)."""
    if not history or len(history) < 2:
        return None
    prev, last = history[-2], history[-1]
    if not prev:
        return None
    return round((last - prev) / prev * 100, 1)


def _spy_domains() -> list[str]:
    """Tracked competitor domains from stores.txt (ignores blank + comment lines)."""
    path = config.general_store_scripts_dir() / "stores.txt"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def spy_roster() -> dict:
    """Google-competitor spy roster: merge traffic + breadth-class per tracked store.

    Joins three on-disk artifacts produced by the discovery/spy funnel:
      stores.txt          — the tracked domains
      store_traffic.json  — TrendTrack visits / us_share / history / meta-ads
      store_class.json    — GENERAL/NICHE breadth verdict + departments
    """
    scripts = config.general_store_scripts_dir()
    domains = _spy_domains()
    traffic = (_read_json(scripts / "store_traffic.json") or {}).get("stores") or {}
    class_list = _read_json(scripts / "store_class.json") or []
    class_by_store = {c.get("store"): c for c in class_list if isinstance(c, dict)}
    # Google Ads Transparency footprint per domain (ads_transparency.py snapshot):
    # how many ads each store actively runs on Google + the Shopping/PLA split — the
    # positive complement to the META-ADS=0 pure-Google gate.
    ads_tc = (_read_json(scripts / "ads_transparency.json") or {}).get("stores") or {}

    # Anchor on the union of tracked domains and any domain we have traffic for.
    all_domains = list(dict.fromkeys([*domains, *traffic.keys()]))

    rows: list[dict] = []
    for dom in all_domains:
        t = traffic.get(dom) or {}
        c = class_by_store.get(dom) or {}
        g = ads_tc.get(dom) or {}
        visits = t.get("monthly_visits")
        history = t.get("history") or []
        mom = _mom_pct(history)
        # Other markets the store also draws traffic from (TrendTrack geo split, US excluded).
        # `top_countries` is code->share (0-1) when the store was enriched; absent otherwise.
        tc = t.get("top_countries") or {}
        other_markets = sorted(
            ({"country": str(k).upper(), "share": v}
             for k, v in tc.items()
             if str(k).upper() != "US" and isinstance(v, (int, float)) and v > 0),
            key=lambda m: -m["share"],
        )[:4]
        rows.append({
            "domain": dom,
            "monthly_visits": visits,
            "tier": _tier_of(visits),
            "us_share": t.get("us_share"),
            "other_markets": other_markets,
            "products": t.get("products"),
            "category": t.get("category"),
            "active_meta_ads": t.get("active_meta_ads"),
            "google_ads_count": g.get("google_ads_count") if g else None,
            "google_ads_shopping": g.get("shopping_ads") if g else None,
            "google_ads_capped": bool(g.get("capped")) if g else False,
            "google_ads_last_shown": g.get("last_shown") if g else None,
            "created": t.get("created"),
            "history": history,
            "mom_pct": mom,
            "flag_remove": mom is not None and mom <= SPY_DROP_FLAG_PCT,
            "verdict": c.get("verdict"),
            "dominant_dept": c.get("dominant_dept"),
            "dominant_share": c.get("dominant_share"),
            "distinct_departments": c.get("distinct_departments"),
            "n_collections": c.get("n_collections"),
            "has_traffic": bool(t),
        })

    # Highest monthly visits first (unknowns last).
    rows.sort(key=lambda r: (r["monthly_visits"] is None, -(r["monthly_visits"] or 0)))

    tier_counts = {label: 0 for label, _ in SPY_TIERS}
    flagged = 0
    for r in rows:
        if r["tier"]:
            tier_counts[r["tier"]] += 1
        if r["flag_remove"]:
            flagged += 1

    # Persist the roster (stores.txt + enrichment JSONs) to the volume so the weekly
    # discover-general-stores finds — which write to the ephemeral image dir — survive the next
    # redeploy. Best-effort; never break the read. Boot restores it via spy_roster_persist.hydrate().
    try:
        from . import spy_roster_persist
        spy_roster_persist.persist()
    except Exception:  # noqa: BLE001
        pass

    return {
        "updated": (_read_json(scripts / "store_traffic.json") or {}).get("updated"),
        "history_months": SPY_HISTORY_MONTHS,
        "totals": {
            "tracked": len(rows),
            "by_tier": tier_counts,
            "flagged_downtrend": flagged,
            "general": sum(1 for r in rows if r["verdict"] == "GENERAL"),
            "niche": sum(1 for r in rows if r["verdict"] == "NICHE"),
            "running_google_ads": sum(1 for r in rows if (r.get("google_ads_count") or 0) > 0),
        },
        "ads_checked": (_read_json(scripts / "ads_transparency.json") or {}).get("updated"),
        "stores": rows,
    }


# ---------------------------------------------------------------- spy roster writers (remove / admit)
def _normalize_domain(raw: str) -> str:
    """bare host, lower-cased, no scheme / path / www. — matches how stores.txt stores it."""
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].strip()
    if d.startswith("www."):
        d = d[4:]
    return d


def spy_remove_store(domain: str) -> dict:
    """Drop a tracked competitor from the roster: rewrite stores.txt without that domain
    and prune its store_traffic.json / store_class.json entries. Comments/blanks preserved."""
    scripts = config.general_store_scripts_dir()
    target = _normalize_domain(domain)
    path = scripts / "stores.txt"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {"ok": False, "removed": False, "reason": "stores.txt not found", "remaining": 0}

    kept: list[str] = []
    removed = False
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and _normalize_domain(s) == target:
            removed = True
            continue
        kept.append(ln)
    if removed:
        path.write_text("\n".join(kept) + "\n")
        # Prune the cached analytics so the dropped store doesn't re-appear via the traffic union.
        for fname, key in (("store_traffic.json", "stores"),):
            doc = _read_json(scripts / fname)
            if isinstance(doc, dict) and isinstance(doc.get(key), dict) and target in doc[key]:
                doc[key].pop(target, None)
                (scripts / fname).write_text(json.dumps(doc, indent=2))
        cls = _read_json(scripts / "store_class.json")
        if isinstance(cls, list):
            pruned = [c for c in cls if _normalize_domain(c.get("store", "")) != target]
            if len(pruned) != len(cls):
                (scripts / "store_class.json").write_text(json.dumps(pruned, indent=2))

    if removed:
        try:
            from . import spy_roster_persist
            spy_roster_persist.persist()  # save the roster edit to the volume (survives redeploy)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "removed": removed, "domain": target, "remaining": len(_spy_domains())}


_ADS_TC_MOD = None


def _ads_tc_module():
    """Lazy-load 06's ads_transparency.py (not a package) by file path, once."""
    global _ADS_TC_MOD
    if _ADS_TC_MOD is None:
        import importlib.util
        path = config.general_store_scripts_dir() / "ads_transparency.py"
        spec = importlib.util.spec_from_file_location("ads_transparency", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ADS_TC_MOD = mod
    return _ADS_TC_MOD


def _scan_google_ads(domain: str, max_pages: int = 4) -> dict | None:
    """Google Ads Transparency footprint for one domain (admission gate). None on failure."""
    try:
        return _ads_tc_module().scan_domain(domain, max_pages)
    except Exception:  # import / network — caller treats as "could not verify"
        return None


def _merge_ads_transparency(results: dict) -> None:
    """Persist per-domain transparency results into ads_transparency.json so the spy
    column reflects them — captured at admission, not re-checked on a schedule."""
    if not results:
        return
    scripts = config.general_store_scripts_dir()
    path = scripts / "ads_transparency.json"
    data = _read_json(path) or {}
    stores = data.get("stores") or {}
    for dom, res in results.items():
        if res:
            stores[dom] = res
    data["stores"] = stores
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data.setdefault("source", "google_ads_transparency_center")
    try:
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def spy_admit_stores(domains: list[str], require_google_ads: bool = True) -> dict:
    """Admit candidate domains to stores.txt — GATED on live Google Shopping ads.

    The roster is pure-Google-Shopping advertisers, so a candidate is only added if the
    Google Ads Transparency Center shows it actively running ads (google_ads_count > 0).
    Non-advertisers (and domains we couldn't verify) are returned in `skipped` with a
    reason instead of being added. Set require_google_ads=False to bypass the gate.
    The footprint we fetch is persisted to ads_transparency.json for the spy column."""
    scripts = config.general_store_scripts_dir()
    path = scripts / "stores.txt"
    have = {_normalize_domain(d) for d in _spy_domains()}

    # Dedup the request against itself + what's already tracked.
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in domains or []:
        d = _normalize_domain(raw)
        if d and d not in have and d not in seen:
            seen.add(d)
            candidates.append(d)

    to_add: list[str] = []
    skipped: list[dict] = []
    ads_results: dict[str, dict] = {}
    for d in candidates:
        res = _scan_google_ads(d) if require_google_ads else None
        if not require_google_ads:
            to_add.append(d)
            continue
        if res is None or res.get("error"):
            skipped.append({"domain": d, "reason": "could not verify Google ads — try again",
                            "google_ads_count": None})
        elif (res.get("google_ads_count") or 0) <= 0:
            skipped.append({"domain": d, "reason": "no live Google Shopping ads in Transparency Center",
                            "google_ads_count": 0})
        else:
            # Only persist the footprint of stores we actually admit — the spy column reads
            # ads_transparency.json keyed by tracked domain, so skipped domains add no value.
            ads_results[d] = res
            to_add.append(d)

    if to_add:
        existing = ""
        try:
            existing = path.read_text()
        except OSError:
            pass
        block = "\n# --- admitted via operator app (Google-ads gated) ---\n" + "\n".join(to_add) + "\n"
        path.write_text((existing.rstrip("\n") + "\n" if existing else "") + block)
    _merge_ads_transparency(ads_results)
    if to_add:
        try:
            from . import spy_roster_persist
            spy_roster_persist.persist()  # save the admitted stores to the volume (survives redeploy)
        except Exception:  # noqa: BLE001
            pass
    return {
        "ok": True,
        "admitted": to_add,
        "n_admitted": len(to_add),
        "skipped": skipped,
        "n_skipped": len(skipped),
        "tracked": len(_spy_domains()),
    }


def spy_candidates() -> dict:
    """Discovered candidate stores awaiting roster admission (the 'research new stores' review).

    Reads, if present, a discovery output on disk — preferred `discovered_candidates.json`
    (list of {domain, monthly_visits, mom, us_share, products, note}) or the TSV that
    `discover_stores.py --out candidates.tsv` emits. Candidates already tracked are dropped.
    When nothing is on disk the surface is empty and the UI shows the discovery command."""
    scripts = config.general_store_scripts_dir()
    have = {_normalize_domain(d) for d in _spy_domains()}
    rows: list[dict] = []
    source: str | None = None

    doc = _read_json(scripts / "discovered_candidates.json")
    raw_list = doc.get("candidates") if isinstance(doc, dict) else doc
    if isinstance(raw_list, list):
        source = "discovered_candidates.json"
        for c in raw_list:
            if not isinstance(c, dict):
                continue
            dom = _normalize_domain(c.get("domain") or c.get("store") or "")
            if dom and dom not in have:
                rows.append({
                    "domain": dom,
                    "monthly_visits": c.get("monthly_visits"),
                    "tier": _tier_of(c.get("monthly_visits")),
                    "mom": c.get("mom"),
                    "us_share": c.get("us_share"),
                    "products": c.get("products"),
                    "note": c.get("note"),
                })
    else:
        tsv = scripts / "candidates.tsv"
        if tsv.is_file():
            source = "candidates.tsv"
            try:
                tlines = tsv.read_text().splitlines()
            except OSError:
                tlines = []
            header = [h.strip().lower() for h in tlines[0].split("\t")] if tlines else []
            for ln in tlines[1:]:
                cols = ln.split("\t")
                rec = dict(zip(header, cols))
                dom = _normalize_domain(rec.get("domain") or rec.get("store") or cols[0])
                if dom and dom not in have:
                    visits = rec.get("monthly_visits") or rec.get("visits")
                    try:
                        visits = int(float(visits)) if visits else None
                    except ValueError:
                        visits = None
                    rows.append({
                        "domain": dom, "monthly_visits": visits, "tier": _tier_of(visits),
                        "mom": rec.get("mom"), "us_share": rec.get("us_share"),
                        "products": None, "note": rec.get("note"),
                    })

    rows.sort(key=lambda r: (r["monthly_visits"] is None, -(r["monthly_visits"] or 0)))
    return {
        "source": source,
        "count": len(rows),
        "candidates": rows,
        "discover_cmd": "/discover-general-stores",
    }


# ---------------------------------------------------------------- best-seller movers (spy lane 1)
# The "real winner" read on a best-seller board is the TOP 30: a product that just BROKE
# INTO the top 30 (prior rank below 30 or brand-new) is a fresh demand spike worth
# sourcing; one that's CLIMBING inside the top 30 is accelerating. Everything below 30 is
# noise. We classify every mover against this top-30 lens and surface those two states.
SPY_TOP30 = 30
_TOP30_ORDER = {"entered": 0, "climbing": 1}  # sort weight; None/other → 3


def _top30_state(rank, prior_rank, rank_delta, is_fresh: bool) -> str | None:
    """entered = newly in the top 30 · climbing = moving up while already in the top 30."""
    in_now = isinstance(rank, int) and rank <= SPY_TOP30
    if not in_now:
        return None
    was_in = isinstance(prior_rank, int) and prior_rank <= SPY_TOP30
    if not was_in or is_fresh:
        return "entered"
    if (rank_delta or 0) > 0:
        return "climbing"
    return None


def _movement_row(m: dict) -> dict:
    rank = m.get("rank")
    prior = m.get("prior_rank")
    delta = m.get("rank_delta")
    fresh = bool(m.get("is_fresh"))
    state = _top30_state(rank, prior, delta, fresh)
    return {
        "handle": m.get("handle"),
        "title": m.get("title"),
        "class": m.get("class"),
        "rank": rank,
        "prior_rank": prior,
        "rank_delta": delta,
        "price": m.get("price"),
        "image": m.get("image"),
        "url": m.get("url"),
        "is_fresh": fresh,
        "days_old": m.get("days_old"),
        "in_top30": isinstance(rank, int) and rank <= SPY_TOP30,
        "top30": state,  # "entered" | "climbing" | None
    }


def bestseller_movers() -> dict:
    """Competitor best-seller rank-movers (06 spy lane 1: Shopify products.json rank-diff).

    Reads movers.json (06-launch-general-store/scripts/movers.json): per tracked store,
    the products whose best-seller rank moved between two snapshots — gainers (climbing
    = demand signal) and fallers. The product set a winning competitor is pushing.
    """
    # Volume location first (where the in-app spy job writes — survives redeploys), then the
    # legacy scripts-dir location (pre-existing local history from manual runs).
    data = _read_json(config.spy_data_dir() / "movers.json") or _read_json(
        config.general_store_scripts_dir() / "movers.json")
    if not isinstance(data, dict):
        return {"generated_at": None, "totals": {}, "stores": []}
    stores: list[dict] = []
    for s in data.get("stores") or []:
        if not isinstance(s, dict):
            continue
        movers = [_movement_row(m) for m in (s.get("movers") or []) if isinstance(m, dict)]
        # The strongest demand signal first: products ENTERING the top 30, then those
        # CLIMBING inside the top 30, then the rest of the gainers, then fallers.
        movers.sort(key=lambda m: (
            _TOP30_ORDER.get(m["top30"], 3),
            m["class"] != "gainer",
            -(m["rank_delta"] or 0),
        ))
        stores.append({
            "store": s.get("store"),
            "prior_date": s.get("prior_date"),
            "latest_date": s.get("latest_date"),
            "latest_count": s.get("latest_count"),
            "prior_count": s.get("prior_count"),
            "comparable_depth": s.get("comparable_depth"),
            "summary": s.get("summary"),
            "movers": movers,
            "n_gainers": sum(1 for m in movers if m["class"] == "gainer"),
            "n_fallers": sum(1 for m in movers if m["class"] == "faller"),
            "n_entered_top30": sum(1 for m in movers if m["top30"] == "entered"),
            "n_climbing_top30": sum(1 for m in movers if m["top30"] == "climbing"),
        })
    totals = dict(data.get("totals") or {})
    totals["entered_top30"] = sum(s["n_entered_top30"] for s in stores)
    totals["climbing_top30"] = sum(s["n_climbing_top30"] for s in stores)
    return {
        "generated_at": data.get("generated_at"),
        "top30": SPY_TOP30,
        "totals": totals,
        "stores": stores,
    }


# ---------------------------------------------------------------- per-store best-seller snapshots
def bestseller_snapshots(store: str | None = None, limit: int = 12) -> dict:
    """Current best-seller boards per tracked store (the live top-ranked products).

    Reads the latest dated snapshot under scripts/snapshots/<domain>/<YYYY-MM-DD>.json
    (produced by the Best-Seller Spy). Unlike `bestseller_movers` (which is a rank-DIFF
    between two days), this is the raw current top-N rank board — what the old standalone
    dashboard's "Best Sellers" tab showed: rank · image · title · price · age per store.
    """
    # Volume location first (in-app spy job output), then the legacy scripts-dir history.
    snap_root = config.spy_data_dir() / "snapshots"
    if not snap_root.is_dir():
        snap_root = config.general_store_scripts_dir() / "snapshots"
    if not snap_root.is_dir():
        return {"generated_at": None, "totals": {"stores": 0, "products": 0}, "stores": []}

    domains = _spy_domains()
    # Iterate tracked domains first (stable order), then any extra snapshot dirs.
    seen: list[str] = []
    for d in domains:
        if (snap_root / d).is_dir():
            seen.append(d)
    for child in sorted(snap_root.iterdir()):
        if child.is_dir() and child.name not in seen:
            seen.append(child.name)
    if store:
        seen = [d for d in seen if d == store]

    stores: list[dict] = []
    total_products = 0
    latest_overall: str | None = None
    for dom in seen:
        dates = sorted(
            p for p in (snap_root / dom).glob("*.json") if p.stem[:4].isdigit()
        )
        if not dates:
            continue
        snap = _read_json(dates[-1]) or {}
        prods = snap.get("products") or []
        prods = sorted(prods, key=lambda p: (p.get("rank") is None, p.get("rank") or 0))
        rows = [
            {
                "rank": p.get("rank"),
                "handle": p.get("handle"),
                "title": p.get("title"),
                "vendor": p.get("vendor"),
                "price": p.get("price"),
                "compare_at": p.get("compare_at"),
                "image": p.get("image"),
                "url": p.get("url"),
                "created_at": p.get("created_at"),
                "variant_count": p.get("variant_count"),
            }
            for p in prods[:limit]
            if isinstance(p, dict)
        ]
        total_products += len(rows)
        date = snap.get("date") or dates[-1].stem
        if date and (latest_overall is None or date > latest_overall):
            latest_overall = date
        stores.append({
            "store": dom,
            "date": date,
            "count": snap.get("count") or len(prods),
            "depth": snap.get("depth"),
            "price_note": snap.get("price_note"),
            "products": rows,
        })

    return {
        "generated_at": latest_overall,
        "totals": {"stores": len(stores), "products": total_products},
        "stores": stores,
    }


# ---------------------------------------------------------------- store duplicate-products scanner
# When the Best-Seller Spy surfaces a competitor's winning products, we only want the
# ones we DON'T already carry. This dedups competitor movers against our own listing
# queue (category keywords + SKU titles) so the Market-Signals feed is "new products only".
_STOPWORDS = {
    "the", "a", "an", "for", "with", "and", "of", "to", "in", "on", "by", "your",
    "our", "set", "pack", "new", "pcs", "pc", "pro", "max", "plus", "kit", "size",
    "color", "colour", "style", "premium", "best", "top", "free", "sale",
}
# A competitor product counts as "already carried" at/above this token-overlap.
_DUP_JACCARD = 0.6


def _norm_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _our_catalog_terms() -> list[dict]:
    """Token-sets for everything we already list: category keywords + SKU titles."""
    terms: list[dict] = []
    for store in list_stores():
        q = listing_queue(store) or {}
        for slug, cat in (q.get("categories") or {}).items():
            label = cat.get("keyword") or slug
            toks = _norm_tokens(label)
            if toks:
                terms.append({"label": label, "tokens": toks, "store": store})
            for sku in (cat.get("skus") or {}).values():
                title = sku.get("title")
                t = _norm_tokens(title)
                if t:
                    terms.append({"label": title, "tokens": t, "store": store})
    return terms


def _match_in_store(title_tokens: set[str], catalog: list[dict]) -> dict | None:
    """Return the catalog entry a competitor product duplicates, else None.

    Two ways to count as already-carried: (a) one of our category/SKU token-sets is
    fully contained in the competitor title, or (b) strong Jaccard overlap.
    """
    if not title_tokens:
        return None
    for entry in catalog:
        toks = entry["tokens"]
        if toks and toks <= title_tokens:
            return entry
        inter = toks & title_tokens
        union = toks | title_tokens
        if union and len(inter) / len(union) >= _DUP_JACCARD:
            return entry
    return None


# Public seams so the product-duplicate check (photo_dedup) can reuse the SAME store-match
# logic the Sourcing-Match store-check and new-products scanner use — one source of truth
# for "do we already list this".
def catalog_terms(store: str | None = None) -> list[dict]:
    """Token-sets for everything a store already lists (categories + SKU titles)."""
    terms = _our_catalog_terms()
    return [t for t in terms if store is None or t.get("store") == store]


def norm_tokens(text: str | None) -> set[str]:
    """Normalize a title into its identity token-set (same seam as the store-match)."""
    return _norm_tokens(text)


def match_in_store(title_tokens: set[str], catalog: list[dict]) -> dict | None:
    """The catalog entry a title duplicates (already-carried), else None."""
    return _match_in_store(title_tokens, catalog)


def titles_same_product(a_tokens: set[str], b_tokens: set[str]) -> bool:
    """True when two product titles are the SAME product by identity tokens — one contained
    in the other, or strong Jaccard overlap (same threshold as the store-match)."""
    if not a_tokens or not b_tokens:
        return False
    if a_tokens <= b_tokens or b_tokens <= a_tokens:
        return True
    inter = a_tokens & b_tokens
    union = a_tokens | b_tokens
    return bool(union) and len(inter) / len(union) >= _DUP_JACCARD


def new_products(only_new: bool = True) -> dict:
    """Competitor best-seller products MINUS the ones we already carry.

    The "store duplicate-products scanner". Population = the FULL current best-seller boards
    across every tracked store (bestseller_snapshots — the live top-N rank board per store),
    NOT just the rank-diff movers. The rank-diff (bestseller_movers) needs two daily snapshots
    to exist, so using it as the SOLE source starved this feed down to a handful of rows even
    though dozens of stores have hundreds of best-sellers on file. Here the momentum signal
    (climbing / fresh-entrant) is layered on as a RANKING + badge, never a gate: every
    competitor best-seller we don't already list shows up, with the movers floated to the top.
    """
    catalog = _our_catalog_terms()
    boards = bestseller_snapshots()  # full current top-N boards: every store, hundreds of SKUs
    movers = bestseller_movers()     # sparse rank-diff momentum — used only to TAG/sort, not gate

    # Index the momentum signal by (store, handle) and (store, title) so a board product can be
    # tagged with its rank_delta / fresh / gainer status when a matching mover row exists.
    mo_index: dict[tuple[str, str], dict] = {}
    for s in movers.get("stores") or []:
        store = s.get("store") or ""
        for m in s.get("movers") or []:
            h = (m.get("handle") or "").strip().lower()
            t = (m.get("title") or "").strip().lower()
            if h:
                mo_index[(store, h)] = m
            if t:
                mo_index.setdefault((store, t), m)

    scanned = 0
    duplicates = 0
    items: list[dict] = []
    for st in boards.get("stores") or []:
        store = st.get("store") or ""
        for p in st.get("products") or []:
            if not isinstance(p, dict):
                continue
            scanned += 1
            title = p.get("title")
            match = _match_in_store(_norm_tokens(title), catalog)
            if match is not None:
                duplicates += 1
            mo = (
                mo_index.get((store, (p.get("handle") or "").strip().lower()))
                or mo_index.get((store, (title or "").strip().lower()))
                or {}
            )
            rank = p.get("rank")
            item = {
                "handle": p.get("handle"),
                "title": title,
                "class": mo.get("class"),
                "rank": rank,
                "prior_rank": mo.get("prior_rank"),
                "rank_delta": mo.get("rank_delta"),
                "price": p.get("price"),
                "image": p.get("image"),
                "url": p.get("url"),
                "is_fresh": mo.get("is_fresh"),
                "days_old": mo.get("days_old"),
                "in_top30": rank is not None and rank <= 30,
                "top30": mo.get("top30"),
                "competitor": store,
                "in_store": match is not None,
                "matched_term": match["label"] if match else None,
                "matched_store": match["store"] if match else None,
            }
            if not (only_new and item["in_store"]):
                items.append(item)
    # Movers float to the top: fresh entrants, then gainers, then biggest climb — and within the
    # rest, by current best-seller rank (rank 1 = a store's #1 seller).
    items.sort(key=lambda i: (
        not i.get("is_fresh"),
        i.get("class") != "gainer",
        -(i.get("rank_delta") or 0),
        i.get("rank") if i.get("rank") is not None else 9999,
    ))
    return {
        "generated_at": boards.get("generated_at") or movers.get("generated_at"),
        "catalog_terms": len(catalog),
        "totals": {
            "scanned": scanned,
            "duplicates": duplicates,
            "new": scanned - duplicates,
            "shown": len(items),
        },
        "products": items,
    }


# ---------------------------------------------------------------- per-keyword product drill-in
# Each discovery lane (amazon / marketplace / meta) is stamped onto a gated keyword. A lane
# can carry MANY found products under `lane["products"]` (the funnel finds e.g. 50 listings
# per keyword and validates a subset), so the overview shows a per-keyword "N found · M
# validated" count and a window-in-window drill-in. When a lane is a single legacy stamp
# (just url/image/price on the lane itself) we treat it as one found product so older data
# still surfaces. `validated` defaults to True only for an explicit single-stamp; in a
# products[] list each item carries its own `validated` flag.
_PRODUCT_FIELDS = (
    "title", "price", "cogs", "orders", "sold_count", "reviews", "pct_gain",
    "ad_longevity_days", "dup_creatives", "image", "url", "store_name", "note", "rank",
)


def _norm_product(p: dict, *, validated_default: bool = True) -> dict:
    out = {k: p.get(k) for k in _PRODUCT_FIELDS}
    out["validated"] = bool(p.get("validated", validated_default))
    return out


def _lane_products(lane: dict) -> list[dict]:
    """Normalize a lane stamp into a list of found products (possibly empty), each stamped with the
    listing-rule verdict (validated + reject_reason) via _stamp_rule_verdict."""
    raw = lane.get("products")
    if isinstance(raw, list):
        return [_stamp_rule_verdict(_norm_product(p, validated_default=False))
                for p in raw if isinstance(p, dict)]
    # Legacy single stamp — surface it as one found product; still run the listing-rule verdict.
    if any(lane.get(k) is not None for k in _PRODUCT_FIELDS):
        return [_stamp_rule_verdict(_norm_product(lane, validated_default=True))]
    return []


def _lane_counts(products: list[dict]) -> tuple[int, int]:
    """(found, valid) for a per-keyword product list. 'Valid' = passes the listing rules (generic /
    sourceable — see _listable), NOT the raw stored flag, so the count is meaningful even for
    live-lane products that arrive without a verdict."""
    return len(products), sum(1 for p in products if _listable(p))


# Google Shopping is RETAIL-dominated — big-box chains + major manufacturer brands, which are NOT
# the dropship competitors the operator sources + copies. A dropshipper wants the generic /
# independent listings (Adrinfly, SIMZLIFE, Kissair, COWSAR, Homcom…), not Hisense at Best Buy.
# These lists strip the non-competitors from 'found products' so the list is sourceable.
# First-party retail sellers — filter their OWN listings (exact seller / domain match) but keep
# third-party marketplace listings ('Walmart - Tayoyo'), because the PRODUCT (a generic brand) is
# still sourceable — the operator's complaint was BRANDS, not the platform a generic sells on.
# Marketplaces + big-box retailers — DROP any listing sold through one, INCLUDING third-party
# "Walmart - <seller>" (operator 2026-07-09: never surface Walmart/Amazon/eBay/etc. as a
# "competitor" — we want independent Google-Shopping dropship STORES only). Supersedes the earlier
# "keep marketplace generics" rule.
_MARKETPLACES = (
    "amazon", "walmart", "wal mart", "ebay", "etsy", "target", "best buy", "bestbuy", "home depot",
    "the home depot", "lowes", "lowe s", "costco", "sams club", "sam s club", "wayfair", "overstock",
    "newegg", "bjs", "aliexpress", "temu", "sears", "kmart", "staples", "office depot", "menards",
    "qvc", "hsn", "kohls", "kohl s", "macys", "macy s", "ace hardware", "true value",
    "tractor supply", "harbor freight", "northern tool", "bed bath", "michaels", "hobby lobby",
    "shein", "wish", "walgreens", "cvs", "ikea", "canadian tire", "rona", "argos", "currys",
)
# Major manufacturer brands — real brands the operator can't source generically. NOT exhaustive
# (brands are infinite) — the AI competitor pass (ai_product_gate.classify_sellers) generalizes.
_MAJOR_BRANDS = {
    "hisense", "insignia", "frigidaire", "midea", "ge", "lg", "samsung", "whirlpool", "toshiba",
    "honeywell", "danby", "haier", "kenmore", "dometic", "delonghi", "emerson", "shinco", "tcl",
    "panasonic", "electrolux", "dyson", "shark", "ninja", "keurig", "bosch", "philips", "sony",
    "blackdecker", "cuisinart", "kitchenaid", "vornado", "lasko", "rheem", "carrier", "daikin",
    "costway", "bedjet", "tacool", "kissair", "dreo", "arctic", "zafro", "shinco", "joy pebble",
    "commercial cool", "black decker", "nexair", "acekool",
}


def _norm_brand(s: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def _store_url(domain: object, seller: object, fallback: object) -> str | None:
    """Best store link for a found competitor: the domain field, else the seller when it IS a
    domain ('zerobreeze.com'), else the google shopping page (DFS often omits the merchant URL)."""
    d = str(domain or "").strip().strip("/")
    if d:
        return f"https://{d}"
    s = str(seller or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", s):  # seller name is itself a domain
        return f"https://{s}"
    return str(fallback).strip() if fallback else None


def _is_dropship_competitor(title: object, seller: object, domain: object) -> bool:
    """Deterministic first pass — True only for a plausibly INDEPENDENT store listing. Drops any
    marketplace / big-box seller (incl. third-party 'Walmart - Tayoyo', matched on the leading
    seller name OR the domain) and known manufacturer brands. The AI competitor pass
    (ai_product_gate.classify_sellers) then catches NOVEL brands this static list can't know."""
    sell = _norm_brand(seller)
    dom = re.sub(r"[^a-z0-9]", "", str(domain or "").lower())
    for mp in _MARKETPLACES:
        m = mp.replace(" ", "")
        if sell == mp or sell.startswith(mp + " ") or (dom and (dom == m or dom == m + "com")):
            return False
    words = set(_norm_brand(title).split()) | set(sell.split())
    return not (words & _MAJOR_BRANDS)


# ---------------------------------------------------------------- listing-rule verdict ("valid")
# A found product is LISTABLE ("valid") only if it's a generic / sourceable item. The operator
# brand-swaps and sells generics, so a trademarked manufacturer brand in the title or seller
# (Dreo, Midea, Whynter, Dyson…) is NOT listable. This is the same _MAJOR_BRANDS gate the competitor
# lanes already use; the AI seller pass (ai_product_gate.classify_sellers) generalizes novel brands
# upstream. Making "valid" mean "passes the rules" is what fixes the old "N found · 0 valid" (every
# freshly-found product used to arrive stamped validated=False and nothing ever flipped it).
def _rule_reject_reason(p: dict) -> str:
    """'' = the product passes the listing rules; otherwise the reason it doesn't."""
    words = set(_norm_brand(p.get("title")).split()) | set(_norm_brand(p.get("store_name")).split())
    hit = sorted(words & _MAJOR_BRANDS)
    return f"brand: {hit[0]}" if hit else ""


def _listable(p: dict) -> bool:
    """True if a found product passes the listing rules (generic / sourceable)."""
    return not _rule_reject_reason(p)


def _stamp_rule_verdict(p: dict) -> dict:
    """Stamp validated + reject_reason from the listing rules onto a normalized product, so every
    product (stamped-lane AND live-lane) carries a real verdict the UI + funnel can read."""
    reason = _rule_reject_reason(p)
    p["validated"] = not reason
    p["reject_reason"] = reason or None
    return p


def _maybe_funnel_no_valid(store: str, keyword: str, products: list[dict]) -> None:
    """FUNNEL-TO-DECISIONS: when a keyword found products but NONE pass the listing rules (all
    branded / not sourceable), raise ONE deduped Decision so the operator steers (skip / allow a
    brand / relax the rule) instead of the keyword silently dead-ending at "0 valid". Only fires
    when something WAS found and zero of it is listable; deduped per (store, keyword). Additive —
    a failure here never breaks the finder."""
    if not store or not keyword or not products:
        return
    if any(_listable(p) for p in products):
        return  # the rules ARE satisfied — at least one generic/sourceable option exists
    try:
        from collections import Counter

        from . import runlog
        src = f"find-no-valid:{keyword.strip().lower()}"
        if runlog.decision_pending_exists(store, src):
            return
        reasons = Counter((p.get("reject_reason") or _rule_reject_reason(p) or "not sourceable")
                          for p in products)
        top = ", ".join(f"{r} ×{n}" for r, n in reasons.most_common(4))
        runlog.decision_create(
            store, kind="find-no-valid",
            title=f"{keyword} — {len(products)} found, 0 pass the listing rules",
            summary=(f"Every product found for “{keyword}” was rejected by the listing "
                     f"rules ({top}). The finder couldn't surface a generic / sourceable option. "
                     f"Decide: turn it down to SKIP this keyword, or reject-with-note to steer — "
                     f"e.g. allow a specific brand, or relax the rule for this niche."),
            payload={"action": "none", "store": store, "keyword": keyword,
                     "found": len(products), "reasons": dict(reasons)},
            source=src,
        )
    except Exception:  # noqa: BLE001 — the funnel is additive; never break the finder
        pass


def _dismissed_found_path() -> Path:
    return config.data_root() / "operator-app" / "api" / "data" / "dismissed-found.json"


def _found_key(store: object, keyword: object, ident: object) -> str:
    return "|".join((str(store or ""), str(keyword or "").strip().lower(), str(ident or "").strip().lower()))


def dismissed_found_keys() -> set[str]:
    data = _read_json(_dismissed_found_path())
    keys = data.get("keys") if isinstance(data, dict) else None
    return {str(k) for k in keys} if isinstance(keys, list) else set()


def dismiss_found(store: object, keyword: object, ident: object) -> dict:
    """Hide ONE found product (the X on a product card) for a head keyword. `ident` = its title (or
    url). Idempotent; found_products_for_head filters the hide-list so it stays gone across scans."""
    if not str(ident or "").strip():
        return {"ok": False, "error": "ident (title or url) required"}
    keys = dismissed_found_keys()
    keys.add(_found_key(store, keyword, ident))
    path = _dismissed_found_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump({"keys": sorted(keys)}, fh, indent=2)
    return {"ok": True, "dismissed": True, "total": len(keys)}


def _token_in_words(token: str, words: set[str]) -> bool:
    """Word-level keyword match: exact, plural-tolerant, and prefix ONLY for long tokens (so
    'conditioner'→'conditioners' matches but 'hat' does NOT match 'shatterproof')."""
    for w in words:
        if w == token or w == token + "s" or w + "s" == token:
            return True
        if len(token) > 4 and w.startswith(token):
            return True
    return False


def _kw_match(text: str, tok_sets: list[list[str]]) -> bool:
    """True if `text` matches ANY term (a term = ALL its tokens present, word-boundary/plural-tolerant).
    tok_sets is [[tokens of term1], [tokens of term2], …] (keyword + AI synonyms)."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return any(all(_token_in_words(tk, words) for tk in ts) for ts in tok_sets if ts)


def _spy_snapshot_roots() -> list[Path]:
    """Both catalog-snapshot roots: the volume (where the live spy job writes) + the scripts dir."""
    return [config.spy_data_dir() / "snapshots", config.general_store_scripts_dir() / "snapshots"]


def _fetch_catalog(domain: str, ttl_days: int = 7) -> list[dict]:
    """Live fetch of a competitor's Shopify /products.json (first 250) → normalized product list,
    cached to the volume. The fallback when no spy snapshot exists, so this works on a fresh deploy
    without waiting for the bestseller-spy job (same mechanism the spy uses)."""
    import urllib.request
    cache = config.spy_data_dir() / "catalog-cache" / f"{domain}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("products") or []
    except (OSError, ValueError, KeyError):
        pass
    # Plain fetch first (fast, works for most Shopify stores); on a bot-block (403/503 — e.g.
    # Cloudflare) / timeout / non-Shopify, fall back to the BD Web Unlocker (the same unlocker that
    # fetches AliExpress — verified past nikinewyork.com's 503). Otherwise protected competitors are
    # silently invisible to the competitor-catalog lane.
    prods = _catalog_plain(domain) or _catalog_via_bd(domain)
    if prods:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "products": prods}))
        except OSError:
            pass
    return prods


def _parse_catalog(data: dict, domain: str) -> list[dict]:
    prods: list[dict] = []
    for p in (data.get("products") or []):
        if not isinstance(p, dict):
            continue
        imgs = p.get("images") or []
        variants = p.get("variants") or []
        handle = p.get("handle")
        prods.append({
            "title": p.get("title"),
            "handle": handle,
            "product_type": p.get("product_type"),
            "vendor": p.get("vendor"),
            "image": (imgs[0].get("src") if imgs and isinstance(imgs[0], dict) else None),
            "price": (variants[0].get("price") if variants and isinstance(variants[0], dict) else None),
            "compare_at": (variants[0].get("compare_at_price") if variants and isinstance(variants[0], dict) else None),
            "url": f"https://{domain}/products/{handle}" if handle else None,
        })
    return prods


def _catalog_plain(domain: str) -> list[dict]:
    """Plain /products.json fetch. [] on any error (bot-block / timeout / non-Shopify)."""
    import urllib.request
    try:
        req = urllib.request.Request(f"https://{domain}/products.json?limit=250",
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; competitor-spy)"})
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            return _parse_catalog(json.loads(resp.read().decode("utf-8", "replace")), domain)
    except Exception:  # noqa: BLE001 — bot-blocked / unreachable → let the BD fallback try
        return []


def _catalog_via_bd(domain: str) -> list[dict]:
    """BD Web-Unlocker fetch of /products.json — for Cloudflare/bot-protected competitor stores that
    503/403 the plain fetch. Uses the same unlocker zone as AliExpress. [] on no BD creds / any error."""
    import urllib.request
    from . import connections
    tok = (connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
    zone = (connections.runtime_get("BRIGHTDATA_SERP_ZONE") or "web_unlocker1").strip()
    if not (tok and zone):
        return []
    body = json.dumps({"zone": zone, "url": f"https://{domain}/products.json?limit=250",
                       "format": "raw"}).encode()
    try:
        req = urllib.request.Request("https://api.brightdata.com/request", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:  # noqa: S310 (trusted BD endpoint)
            return _parse_catalog(json.loads(r.read().decode("utf-8", "replace")), domain)
    except Exception:  # noqa: BLE001 — advisory; store stays unscanned
        return []


def _latest_catalog(domain: str) -> list[dict]:
    """Latest product catalog for a tracked competitor: the spy bestseller snapshot if present,
    else a cached live /products.json fetch (so this works on live without pre-cached snapshots)."""
    for root in _spy_snapshot_roots():
        d = root / domain
        if not d.is_dir():
            continue
        for f in sorted((p for p in d.glob("*.json")), key=lambda p: p.name, reverse=True):
            data = _read_json(f)
            prods = data.get("products") if isinstance(data, dict) else None
            if prods:
                return prods
    return _fetch_catalog(domain)


def _bd_fetch_md(url: str, timeout: int = 45) -> str:
    """Fetch a URL as MARKDOWN via the BD Web Unlocker (past bot walls). Empty on no creds / error."""
    import urllib.request
    from . import connections
    tok = (connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
    zone = (connections.runtime_get("BRIGHTDATA_SERP_ZONE") or "web_unlocker1").strip()
    if not (tok and zone):
        return ""
    body = json.dumps({"zone": zone, "url": url, "format": "raw", "data_format": "markdown"}).encode()
    try:
        req = urllib.request.Request("https://api.brightdata.com/request", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted BD endpoint)
            return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


_PLINK = re.compile(r"/collections/all/products/([a-z0-9][a-z0-9\-]*)\)")
_BS_SKIP = {"shipping-protection"}


# Add-on / non-catalog products that ALWAYS top best-selling order (every order includes them) — noise,
# not real sellers. Mirrors 06-launch-general-store/scripts/bestseller_snapshot.py's junk filter.
_BS_JUNK = ("shipping-protection", "package-protection", "route-protection", "order-protection",
            "delivery-protection", "protection-plan", "insurance", "warranty", "-vip", "vip-",
            "gift-card", "giftcard", "donation", "deposit", "sample-", "-sample", "subscription",
            "membership", "extended-warranty", "onward", "seel", "corso", "worry-free", "-tip", "tip-")
_BS_HANDLE_RE = re.compile(r"/products/([a-z0-9][a-z0-9\-]*)")
_BS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0 Safari/537.36")


_BESTSELLER_MAX_PAGES = 10  # HARD CAP: always scan the TOP 10 pages of /collections/all?sort_by=best-selling


def _bd_residential_proxy(country: str = "us") -> str | None:
    """A ROTATING Bright Data RESIDENTIAL proxy routed to `country` (default **US** — never CN for US
    stores). Reuses the residential zone + password already assembled into `BD_CN_PROXY`, re-routed off
    CN, so no new zone is provisioned. Residential IPs rotate per request → beats the datacenter-IP
    rate-limit / bot-block on the plain best-seller fetch. None if no BD residential proxy is set up."""
    from . import connections
    cn = (connections.runtime_get("BD_CN_PROXY") or "").strip()
    if not cn:
        return None
    if "-country-" in cn:  # swap CN routing → US (…-country-cn:pw@… → …-country-us:pw@…)
        return re.sub(r"-country-[a-z]{2}(:)", rf"-country-{country}\1", cn, count=1)
    return cn  # no country pin → already rotating/global


def _http_get_text(url: str, proxy: str | None = None, timeout: int = 12) -> str:
    """GET `url` as text, optionally through a proxy. Raises on failure (caller handles)."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _BS_UA, "Accept-Language": "en-US,en;q=0.9"})
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open(req, timeout=timeout) as r:  # noqa: S310
            return r.read().decode("utf-8", "replace")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.read().decode("utf-8", "replace")


def _record_bs_fetch(domain: str, geo: str, status: str, detail: str, *, used_proxy: bool,
                     proxy_available: bool) -> None:
    """Persist the best-seller-fetch outcome per domain so failures are VISIBLE — the operator can see
    WHEN and WHY the `/collections/all?sort_by=best-selling` fetch fell back (datacenter-IP bot-block /
    rate-limit / no residential proxy configured). Surfaced by `bestseller_fetch_health()` in the
    Connections-health panel. Rolling last ~60 domains."""
    path = config.spy_data_dir() / "bestseller-fetch-log.json"
    try:
        log = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        log = {}
    if not isinstance(log, dict):
        log = {}
    log[domain] = {"status": status, "detail": detail, "used_proxy": used_proxy,
                   "proxy_available": proxy_available, "geo": geo,
                   "ts": datetime.now(timezone.utc).isoformat()}
    if len(log) > 60:
        for k in sorted(log, key=lambda kk: log[kk].get("ts", ""))[:-60]:
            log.pop(k, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(log))
    except OSError:
        pass


def bestseller_fetch_health() -> dict:
    """Recent best-seller-fetch failures + whether a rotating residential proxy is configured — for the
    Connections-health panel so a 'why did the competitor scan fall back?' is answerable at a glance."""
    path = config.spy_data_dir() / "bestseller-fetch-log.json"
    try:
        log = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        log = {}
    log = log if isinstance(log, dict) else {}
    fails = {d: v for d, v in log.items() if isinstance(v, dict) and v.get("status") != "ok"}
    proxy = bool(_bd_residential_proxy())
    recent = sorted(fails.items(), key=lambda kv: kv[1].get("ts", ""), reverse=True)[:8]
    if not log:
        msg = "No competitor best-seller scans run yet."
    elif not fails:
        msg = f"OK — last {len(log)} store fetch(es) succeeded."
    else:
        why = "rotating residential proxy IS configured (BD_CN_PROXY re-routed)" if proxy else \
              "NO residential proxy configured — datacenter IP gets rate-limited/bot-blocked; set BD_CN_PROXY"
        msg = f"{len(fails)} store(s) fell back to the merchandising signal — {why}."
    return {"ok": not fails, "status": "ok" if not fails else "degraded", "message": msg,
            "proxy_configured": proxy,
            "recent_failures": [{"domain": d, "detail": v.get("detail"), "used_proxy": v.get("used_proxy"),
                                 "ts": v.get("ts")} for d, v in recent]}


def _bestseller_order(domain: str, depth: int = 2000, ttl_days: int = 7, geo: str = "US",
                      max_pages: int = _BESTSELLER_MAX_PAGES) -> list[str]:
    """The store's REAL best-seller order — product handles in `/collections/all?sort_by=best-selling`
    DOM order, junk-filtered. HARD-CAPPED at the TOP 10 PAGES (`_BESTSELLER_MAX_PAGES`) — deep enough to
    catch a side-category keyword whose best-sellers sit past the global top-50 (nikinewyork's aircon is
    #43/#73/#157/#179), bounded so cost/latency stay predictable for every keyword. Fetched with a
    PLAIN `urllib` GET + a browser User-Agent — the SAME method as the working Step-06 best-seller spy.
    robots.txt disallows `/collections/*sort_by*`, but that only makes BRIGHT DATA self-censor (Web
    Unlocker + Scraping Browser both refuse it); a plain request just fetches the page. Cached per
    (domain, geo). `[]` if the store bot-blocks the datacenter IP (→ caller falls back)."""
    geo = (geo or "US").upper()
    # -v5: bump on the rotating-residential-proxy path (orders re-fetch via the proxy where needed).
    cache = config.spy_data_dir() / "bestseller-order-cache-v5" / f"{domain}__{geo.lower()}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("order") or []
    except (OSError, ValueError, KeyError):
        pass
    import time
    # A store's best-seller order is ONE order — it is NOT split by market (US/UK targeting lives in the
    # DISCOVERY lanes, not here). Ordered fetch chain (first that works wins, reused for every page):
    # plain datacenter IP → ROTATING residential proxy (fresh IP per request) when the store rate-limits
    # / bot-blocks the datacenter IP.
    _res_px = _bd_residential_proxy()  # rotating residential — None if BD_CN_PROXY isn't set up
    methods = [("plain", None)] + ([("residential proxy", _res_px)] if _res_px else [])
    working = None  # (label, proxy) once a method succeeds — reused for every page
    used_label = ""
    seen: set[str] = set()
    order: list[str] = []
    fail_reason = ""
    start = time.monotonic()
    budget_s = 40.0  # overall wall-clock — a slow store must never hang the scan for minutes
    for page in range(1, max_pages + 1):
        if time.monotonic() - start > budget_s:
            fail_reason = fail_reason or "wall-clock budget hit (slow store)"
            break
        url = f"https://{domain}/collections/all?sort_by=best-selling&page={page}"
        html = None
        if working is not None:  # reuse the method that worked on page 1 (retry once)
            for _attempt in range(2):
                try:
                    html = _http_get_text(url, proxy=working[1], timeout=12)
                    break
                except Exception:  # noqa: BLE001 — transient / throttle → retry
                    time.sleep(0.8)
        else:  # find the first working method in the chain
            for lbl, px in methods:
                try:
                    html = _http_get_text(url, proxy=px, timeout=(15 if px else 12))
                    working, used_label = (lbl, px), lbl
                    break
                except Exception:  # noqa: BLE001 — this method blocked → try the next
                    continue
        if html is None:
            fail_reason = f"page {page} blocked — tried [{', '.join(_l for _l, _ in methods)}]"
            break
        # dedup INSIDE the loop — a product link appears several times per page (grid + nav + "you may
        # also like"), so filtering only against prior-page `seen` would repeat every product ~4× and
        # push real depth off the end. First DOM occurrence = best-selling rank.
        page_new = 0
        for h in _BS_HANDLE_RE.findall(html):
            if h in seen:
                continue
            seen.add(h)
            page_new += 1
            if not any(j in h for j in _BS_JUNK):
                order.append(h)
        if page_new == 0:  # page had only already-seen handles = past the end
            break
        if len(order) >= depth:
            break
        time.sleep(0.4)  # pace deep pagination
    order = order[:depth]
    _proxy_avail = bool(_res_px)
    _used_proxy = bool(working and working[1])
    # RECORD the outcome so failures are visible (why it can fail: bot-block / rate-limit / no proxy;
    # and for UK, whether it got true GB data or fell back to the store's global order).
    if order:
        _record_bs_fetch(domain, geo, "ok", f"{len(order)} products via {used_label or 'plain'}",
                         used_proxy=_used_proxy, proxy_available=_proxy_avail)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "order": order}))
        except OSError:
            pass
    else:
        _record_bs_fetch(domain, geo, "fail", fail_reason or "no products returned",
                         used_proxy=_used_proxy, proxy_available=_proxy_avail)
    return order


def _enrich_products(domain: str, handles: list[str], ttl_days: int = 7) -> dict:
    """Per-handle {title, price, image, url} via `/products/<handle>.json` (plain GET), for the handful
    of keyword-matched best-sellers. Bounded parallel; persisted per store so repeat scans are instant.
    Missing entries just fall back to a handle-derived title."""
    import urllib.request
    cache_path = config.spy_data_dir() / "product-enrich-cache" / f"{domain}.json"
    cache = _read_json(cache_path)
    cache = cache if isinstance(cache, dict) else {}
    fresh = cache.get("ts")
    if fresh:
        try:
            if (datetime.now(timezone.utc) - datetime.fromisoformat(fresh)).days >= ttl_days:
                cache = {}
        except ValueError:
            cache = {}
    have = cache.get("p") or {}
    todo = [h for h in handles if h not in have]

    def _one(h: str) -> tuple[str, dict]:
        try:
            req = urllib.request.Request(f"https://{domain}/products/{h}.json",
                                         headers={"User-Agent": _BS_UA})
            with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310
                p = (json.loads(r.read().decode("utf-8", "replace")) or {}).get("product") or {}
        except Exception:  # noqa: BLE001
            return h, {}
        variants = p.get("variants") or []
        prices = [float(v["price"]) for v in variants if isinstance(v, dict) and v.get("price")]
        imgs = p.get("images") or []
        return h, {"title": p.get("title") or "", "product_type": p.get("product_type") or "",
                   "price": (min(prices) if prices else None),
                   "image": (imgs[0].get("src") if imgs and isinstance(imgs[0], dict) else None),
                   "url": f"https://{domain}/products/{h}"}

    if todo:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=10) as ex:
            for h, meta in ex.map(lambda hh: _one(hh), todo[:60]):
                have[h] = meta
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "p": have}))
        except OSError:
            pass
    return {h: have.get(h) or {} for h in handles}


def _dexcape_md(md: str) -> str:
    """Strip markdown backslash-escapes BD sometimes emits (`\\(` `\\_pos` `\\[` `\\$`) so handle/title
    regexes match whether or not the source was escaped."""
    for a, b in (("\\(", "("), ("\\)", ")"), ("\\[", "["), ("\\]", "]"), ("\\_", "_"), ("\\$", "$")):
        md = md.replace(a, b)
    return md


_SEARCH_BLOCK = re.compile(
    r"\[(?P<txt>[^\[\]]{0,400}?)\]\(/products/(?P<handle>[a-z0-9][a-z0-9\-]*)\?[^)]*?_pos=(?P<pos>\d+)",
    re.DOTALL)


_SEARCH_HTML_LINK = re.compile(r"/products/([a-z0-9][a-z0-9\-]*)\?[^\"'\s>]*?_pos=(\d+)")


def _search_keyword_products(domain: str, keyword: str, max_pages: int = 15) -> list[dict]:
    """The competitor's products for a keyword via its OWN storefront search (`/search?q=<kw>`).
    Shopify matches server-side, so this returns exactly the keyword-relevant products (incl. deep-
    catalog ones a first-page /products.json misses), each with the store's RELEVANCE rank (`_pos`).

    PLAIN-FIRST + DEEP (2026-07): a plain `urllib` GET of the search HTML is fast (~1s/page) and lets
    us walk the FULL result set (a store with 175 aircon results needs ~15 pages, not the old 3). We
    take handle + relevance from the HTML; the title is the humanized handle (Shopify handles ARE the
    slugified title, so this reads the same) and price is filled later for the products that matter
    (validated best-sellers get real title+price from _enrich_products). BD-markdown fallback for a
    store that bot-blocks the plain search fetch. Returns [{handle,title,price,relevance_rank,url}]."""
    import urllib.parse
    import urllib.request
    q = urllib.parse.quote_plus((keyword or "").strip())
    out: list[dict] = []
    seen: set[str] = set()
    for pg in range(1, max_pages + 1):
        url = f"https://{domain}/search?q={q}&type=product&page={pg}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _BS_UA,
                                                       "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
                html = r.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — plain blocked → BD fallback below
            break
        added = 0
        for m in _SEARCH_HTML_LINK.finditer(html):
            h = m.group(1)
            if h in seen or h in _BS_SKIP:
                continue
            seen.add(h)
            out.append({"handle": h, "relevance_rank": int(m.group(2)),
                        "title": h.replace("-", " ").title(), "price": None,
                        "url": f"https://{domain}/products/{h}"})
            added += 1
        if added == 0:  # empty/beyond last page
            break

    if not out:  # plain search bot-blocked → BD Web-Unlocker markdown (slower, capped)
        for pg in range(1, min(max_pages, 4) + 1):
            md = _dexcape_md(_bd_fetch_md(f"https://{domain}/search?q={q}&type=product&page={pg}") or "")
            if not md:
                break
            added = 0
            for m in _SEARCH_BLOCK.finditer(md):
                h = m.group("handle")
                if h in seen or h in _BS_SKIP:
                    continue
                seen.add(h)
                txt = m.group("txt") or ""
                pm = (re.search(r"Sale price\s*(?:from\s*)?\$?([\d,]+\.?\d*)", txt)
                      or re.search(r"\$([\d,]+\.?\d*)", txt))
                title = next((ln.strip() for ln in txt.splitlines()
                              if len(ln.strip()) > 6 and "$" not in ln and "price" not in ln.lower()
                              and ln.strip().lower() not in ("quick view", "sale", "add to cart")), None)
                out.append({"handle": h, "relevance_rank": int(m.group("pos")),
                            "title": title or h.replace("-", " ").title(),
                            "price": (pm.group(1).replace(",", "") if pm else None),
                            "url": f"https://{domain}/products/{h}"})
                added += 1
            if added == 0:
                break

    out.sort(key=lambda p: p.get("relevance_rank") or 9999)
    return out


def _shop_json(domain: str, path: str, timeout: int = 8):
    """Fetch a ROBOTS-ALLOWED Shopify JSON path (/collections.json, /collections/<h>/products.json)
    plain-first, BD Web-Unlocker RAW fallback for bot-protected stores. None on failure. NOTE: never
    use this for `?sort_by=*` URLs — those match Shopify's `Disallow: /collections/*sort_by*` and BOTH
    the Web Unlocker AND the Scraping Browser refuse them (robots 'brob' block)."""
    import urllib.request
    url = f"https://{domain}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; competitor-spy)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — bot-block/unreachable → BD fallback
        pass
    from . import connections
    tok = (connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
    zone = (connections.runtime_get("BRIGHTDATA_SERP_ZONE") or "web_unlocker1").strip()
    if not (tok and zone):
        return None
    body = json.dumps({"zone": zone, "url": url, "format": "raw"}).encode()
    try:
        req = urllib.request.Request("https://api.brightdata.com/request", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:  # noqa: S310 (trusted BD endpoint)
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _store_collections(domain: str, ttl_days: int = 7) -> list[dict]:
    """The store's collections (/collections.json — robots-allowed) → [{handle,title,count}], cached."""
    cache = config.spy_data_dir() / "collections-cache" / f"{domain}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("collections") or []
    except (OSError, ValueError, KeyError):
        pass
    data = _shop_json(domain, "/collections.json?limit=250")
    cols: list[dict] = []
    for c in ((data or {}).get("collections") or []):
        h = (c.get("handle") or "").strip().lower()
        if h:
            cols.append({"handle": h, "title": (c.get("title") or "").strip(),
                         "count": int(c.get("products_count") or 0)})
    if cols:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "collections": cols}))
        except OSError:
            pass
    return cols


def _collection_products(domain: str, handle: str, ttl_days: int = 7, max_pages: int = 2) -> list[dict]:
    """Ordered products of one collection (its configured/manual order — /collections/<h>/products.json,
    robots-allowed, NO sort_by) as [{handle, price}]. Position = index. Cached per (domain, collection).
    The price is authoritative (structured JSON), unlike the theme-dependent /search markdown price."""
    # NB: cache dir is versioned (-v2) — the v1 files stored {"handles":[…]}; this returns {"products"}.
    cache = config.spy_data_dir() / "collection-products-cache-v2" / f"{domain}__{handle}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("products") or []
    except (OSError, ValueError, KeyError):
        pass
    out: list[dict] = []
    for pg in range(1, max_pages + 1):
        data = _shop_json(domain, f"/collections/{handle}/products.json?limit=250&page={pg}")
        prods = (data or {}).get("products") or []
        if not prods:
            break
        for p in prods:
            h = (p.get("handle") or "").strip()
            if not h:
                continue
            variants = p.get("variants") or []
            price = variants[0].get("price") if variants and isinstance(variants[0], dict) else None
            out.append({"handle": h, "price": price})
        if len(prods) < 250:
            break
    if out:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "products": out}))
        except OSError:
            pass
    return out


# Collections whose NAME marks the store's own promoted/best-selling set (curated, not the sort).
_BESTSELLER_COL_PATTERNS = (
    "best-sell", "bestsell", "best-selling", "top-sell", "top-seller", "topseller", "trending",
    "popular", "most-loved", "favourite", "favorite", "our-pick", "staff-pick", "bestseller",
    "winner", "featured", "must-have", "frontpage", "hot-pick", "top-pick", "top-rated")

# price-tier / non-category collections ('$10-$19.99', 'under-20', '30-39') — a price filter every
# category spills across, so it must NOT be treated as a keyword's category.
_PRICE_COL_RE = re.compile(r"[\$£€]|\bunder\b|\bover\b|\bprice\b|\d+\s*[-–—]\s*\d+")


def _merchandising_maps(domain: str, terms: list[str], kw_handles: set[str]) -> dict:
    """RELIABLE sales signal via the store's OWN merchandising (robots-allowed collection JSON, NO
    sort_by). A keyword product is a proven seller if the store placed it in its Best-Sellers collection
    or in the keyword's own category collection — vs deep-catalog stock that only lives in all-products.

    Which collections to read is decided by CONTENT, not name: we fetch every non-empty collection and
    keep those that actually contain the keyword products (so the aircon category is found whether it's
    called 'Cooling & Air Care', 'air-conditioners', or 'Summer'). Best-seller-named collections are
    always read. Everything cached per store, so repeat keyword scans on the same store are instant."""
    cols = _store_collections(domain)
    bestseller_map: dict[str, dict] = {}
    category_map: dict[str, dict] = {}
    price_by_handle: dict[str, str] = {}
    bs_names: list[str] = []
    cat_names: list[str] = []
    # Bounded parallel fetch of candidate collections (skip empties + the all-products catch-all).
    cands = [c for c in cols if c["count"] > 0 and c["handle"] not in ("all-products", "all", "frontpage-all")]
    import concurrent.futures as _cf
    fetched: dict[str, list[dict]] = {}
    with _cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_collection_products, domain, c["handle"]): c for c in cands[:40]}
        for f in futs:
            c = futs[f]
            try:
                fetched[c["handle"]] = f.result(timeout=40) or []
            except Exception:  # noqa: BLE001
                fetched[c["handle"]] = []
    for c in cands:
        prods = fetched.get(c["handle"]) or []
        if not prods:
            continue
        handles = [p["handle"] for p in prods]
        for p in prods:
            if p.get("price") is not None:
                price_by_handle.setdefault(p["handle"], str(p["price"]))
        blob = f"{c['handle']} {c['title']}".lower()
        is_bestseller = any(pat in blob for pat in _BESTSELLER_COL_PATTERNS)
        # A category collection = one that actually holds this keyword's products (content-based, so the
        # aircon category is found whatever it's named). Threshold ≥2 keyword members so a GENERIC
        # collection (Men, Women's Bags) that only caught 1 fuzzy Shopify-search match never registers
        # as the category — a real category ('Cooling & Air Care') holds many. This is what filters
        # Shopify's fuzzy /search noise, robustly, without depending on synonym coverage.
        # SKIP price-tier collections ('$10-$19.99', 'under-20', '30-39') — they're a price filter, not a
        # product category, and every category spills across them so they falsely validate everything.
        is_price_col = bool(_PRICE_COL_RE.search(blob))
        kw_members = kw_handles.intersection(handles)
        if is_bestseller:
            bs_names.append(c["title"] or c["handle"])
            for i, h in enumerate(handles):
                bestseller_map.setdefault(h, {"collection": c["title"] or c["handle"], "pos": i + 1})
        elif len(kw_members) >= 2 and not is_price_col:
            cat_names.append(c["title"] or c["handle"])
            for i, h in enumerate(handles):
                if h in kw_handles:
                    category_map.setdefault(h, {"collection": c["title"] or c["handle"], "pos": i + 1})
    return {"bestseller": bestseller_map, "category": category_map, "price_by_handle": price_by_handle,
            "bestseller_collections": bs_names, "category_collections": cat_names,
            "n_collections": len(cols)}


_GENERIC_MODIFIERS = frozenset({
    "air", "mini", "small", "large", "portable", "usb", "rechargeable", "wireless", "home", "office",
    "room", "bedroom", "desk", "desktop", "personal", "electric", "smart", "new", "best", "set", "pack",
    "with", "for", "and", "the", "pcs", "piece", "unit", "cool", "hot", "led", "digital", "handheld"})


def _bestseller_keyword_matches(domain: str, bs_order: list[str], terms: list[str],
                                tok_sets: list[list[str]], keyword: str,
                                kw_handles: set[str] | None = None,
                                seed_titles: list[str] | None = None,
                                price_by_handle: dict | None = None) -> list[dict]:
    """Walk the store's best-seller order and keep the products matching the keyword, RANKED by their
    best-seller position — the store's proven sellers FOR that keyword (a #73 room-cooler beats a
    #2000 dead-stock match). A best-seller is a candidate if it matches a strong keyword token OR it's
    in the store's own `/search` set (Shopify's match — recall for names the synonyms miss) → enrich
    titles → AI-verify to drop off-target ('wine-cooler' ≠ aircon). Fail-open."""
    kw_handles = kw_handles or set()
    strong = {t for term in terms for t in re.findall(r"[a-z0-9]+", term.lower())
              if len(t) >= 3 and t not in _GENERIC_MODIFIERS}
    # data-driven recall: a token recurring across the store's OWN /search hits for this keyword is a
    # strong signal for it in THIS store — catches 'cooler' for aircon when the synonym list only gave
    # 'air conditioner' variants. AI-verify still precision-filters, so a stray descriptor is harmless.
    if seed_titles:
        from collections import Counter
        cnt = Counter(t for title in seed_titles
                      for t in set(re.findall(r"[a-z0-9]+", (title or "").lower()))
                      if len(t) >= 4 and t not in _GENERIC_MODIFIERS)
        strong |= {t for t, c in cnt.items() if c >= 3}
    cands: list[tuple[str, int]] = []
    for i, h in enumerate(bs_order):
        words = set(re.findall(r"[a-z0-9]+", h.replace("-", " ")))
        if h in kw_handles \
                or (strong and any(_token_in_words(tk, words) for tk in strong)) \
                or any(all(_token_in_words(tk, words) for tk in ts) for ts in tok_sets if ts):
            cands.append((h, i + 1))
    if not cands:
        return []
    # CAP the candidate set to the top ~80 best-sellers (cands is in best-seller-rank order) — a broad
    # keyword ('storage') matches a huge slice of a general store, and we only want its TOP proven
    # sellers anyway; this also bounds the enrich + AI-verify cost.
    cands = cands[:80]
    handles = [h for h, _ in cands]
    enr = _enrich_products(domain, handles)
    titles = [(enr.get(h, {}).get("title") or h.replace("-", " ")) for h in handles]
    verified = None
    try:
        from . import ai_product_gate
        verified = ai_product_gate.match_catalog_titles(keyword, titles)  # set[int] | None
    except Exception:  # noqa: BLE001
        verified = None
    if verified is None:
        # AI unavailable/failed → STRICT fallback (NOT keep-all): require the RAW keyword's tokens or a
        # FULL synonym term in the title, not the loose single strong tokens that formed the candidate
        # set — so a broad keyword doesn't validate every loosely-matched best-seller (a charging cable,
        # a razor for 'storage'). Precision over recall here since the AI can't adjudicate.
        kw_toks = [t for t in re.findall(r"[a-z0-9]+", (keyword or "").lower()) if len(t) > 2]
        strict_sets = [ts for ts in ([kw_toks, *tok_sets]) if len(ts) >= 1]
        verified = set()
        for _i, (_h, _r) in enumerate(cands):
            _w = set(re.findall(r"[a-z0-9]+", (str(titles[_i]) + " " + _h.replace("-", " ")).lower()))
            if any(all(_token_in_words(tk, _w) for tk in ts) for ts in strict_sets):
                verified.add(_i)
    out: list[dict] = []
    seen_titles: set[str] = set()
    for idx, (h, rank) in enumerate(cands):
        if verified is not None and idx not in verified:
            continue
        e = enr.get(h, {})
        title = e.get("title") or h.replace("-", " ").title()
        # dedup near-identical re-listings of the SAME product (dropship stores list a winner 3×) — by
        # normalized title AND by handle-stem (drops '…-2'/near-variant handles the title misses).
        tkey = re.sub(r"[^a-z0-9]+", "", title.lower())[:60]
        hkey = re.sub(r"-?\d+$", "", h)[:60]
        if (tkey and tkey in seen_titles) or (hkey and hkey in seen_titles):
            continue
        seen_titles.add(tkey)
        seen_titles.add(hkey)
        price = (str(e["price"]) if e.get("price") is not None else None) \
            or (price_by_handle or {}).get(h)  # fall back to the /search price when per-item enrich 503s
        out.append({"handle": h, "title": title, "price": price,
                    "image": e.get("image"), "url": e.get("url") or f"https://{domain}/products/{h}",
                    "tier": "bestseller", "bestseller_rank": rank, "collection": None,
                    "collection_rank": None, "validated": True})
    return out


def competitor_keyword_scan(domain: str, keyword: str, pages: int = 8, limit: int = 50,
                            geo: str = "US") -> dict:
    """VALIDATED-SALES competitor keyword scan (the 'Google competitor catalog keyword' check).

    Rank the keyword as its OWN tier list off the store's REAL best-seller order (2026-07 — matches the
    Step-06 best-seller spy method). A store's best-selling *aircon* can sit at global best-seller rank
    #73/#157/#179 behind unrelated all-time winners — the point is to surface THOSE and drop deep-catalog
    dead stock:

      1. BEST-SELLER ORDER — `_bestseller_order` reads `/collections/all?sort_by=best-selling` deep
         (top ~300) via a PLAIN request + browser UA (robots.txt only makes BRIGHT DATA self-censor;
         a plain GET just fetches it). Cached per (domain, geo).
      2. KEYWORD-MATCH THE ORDER — `_bestseller_keyword_matches` keeps the best-sellers matching the
         keyword (handle-token, AI-verified), RANKED by best-seller position → the per-keyword tier
         list. These are `validated`. (This catches a #73 room-cooler that Shopify `/search` misses.)
      3. LISTED = the store's other `/search?q=<kw>` products not in the best-seller top ~300 — listed
         but not proven sellers (deep-catalog).

    Fallback: if the store bot-blocks the plain best-seller fetch (empty order), fall back to the
    robots-allowed MERCHANDISING signal (Best-Sellers + category collection JSON, `_merchandising_maps`).
    `geo` is passed through (best-seller order is US from the server IP; market differences are minor)."""
    domain = re.sub(r"^https?://|/.*$|^www\.", "", str(domain or "").strip().lower())
    kw = (keyword or "").strip()
    geo = (geo or "US").upper()

    # 2-day RESULT cache — the finding lane calls this up to 15× per find and the Spy modal re-opens it;
    # caching the whole result skips the repeat /search + enrich + AI-verify (cost + latency). The
    # best-seller order underneath is separately cached 7d.
    _cache = (config.spy_data_dir() / "competitor-scan-cache-v5"
              / f"{domain}__{re.sub(r'[^a-z0-9]+', '-', kw.lower())[:60]}__{geo.lower()}.json")
    try:
        _c = json.loads(_cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(_c["ts"])).days < 2:
            return _c["result"]
    except (OSError, ValueError, KeyError):
        pass

    def _finish(result: dict) -> dict:
        try:
            _cache.parent.mkdir(parents=True, exist_ok=True)
            _cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "result": result}))
        except OSError:
            pass
        return result

    # AI-ASSIST (standard on every keyword, not just the aircon example): the LLM expands the keyword
    # into buyer synonyms / spec variants so the match generalizes — 'aircon'→air conditioner/cooler,
    # 'dog bed'→orthopedic/pet bed, etc. Fail-open to the bare keyword when no gateway is configured.
    terms = [kw]
    ai_used = False
    try:
        from . import ai_find_assist
        syn = ai_find_assist.expand_terms(kw)
        if syn:
            terms += [s for s in syn if s]
            ai_used = True
    except Exception:  # noqa: BLE001 — no LLM → plain keyword only
        pass
    tok_sets = [ts for ts in ([x for x in re.findall(r"[a-z0-9]+", t.lower()) if len(x) > 2]
                              for t in terms) if ts]

    # keyword set from the store's own search (for the LISTED tail + the keyword_matches count).
    kw_products = _search_keyword_products(domain, kw)
    if not kw_products:
        for i, p in enumerate(_catalog_keyword_hits(kw, tok_sets, _fetch_catalog(domain), domain)):
            h = (p.get("handle") or "").strip()
            if h:
                kw_products.append({"handle": h, "relevance_rank": i + 1, "title": p.get("title") or h,
                                    "price": (str(p.get("price")) if p.get("price") is not None else None),
                                    "url": p.get("url") or f"https://{domain}/products/{h}"})

    # 1) the real best-seller order (plain GET) → keyword-matched, ranked = validated sellers.
    search_handles = {p["handle"] for p in kw_products if p.get("handle")}
    bs_order = _bestseller_order(domain, geo=geo)  # top 10 pages of /collections/all?sort_by=best-selling
    if bs_order:
        validated = _bestseller_keyword_matches(
            domain, bs_order, terms, tok_sets, kw, search_handles,
            seed_titles=[p.get("title") for p in kw_products],
            price_by_handle={p["handle"]: p.get("price") for p in kw_products
                             if p.get("handle") and p.get("price")})
        vh = {v["handle"] for v in validated}
        listed = [{**p, "tier": "listed", "bestseller_rank": None, "collection": None,
                   "collection_rank": None, "validated": False}
                  for p in kw_products if p.get("handle") not in vh]
        results = (validated + listed)[:limit]
        n_kw = len(vh | {p["handle"] for p in kw_products if p.get("handle")})
        _ai = " · AI-assisted match" if ai_used else ""
        _cap = f"top 10 pages of /collections/all?sort_by=best-selling ({len(bs_order)} products){_ai}"
        if validated:
            note = (f"Ranked off {domain}'s live best-seller order — {_cap}. "
                    f"{len(validated)} proven seller{'s' if len(validated) != 1 else ''} for “{kw}”"
                    f"{' — top at best-seller #' + str(validated[0]['bestseller_rank']) if validated else ''}.")
        else:
            note = (f"{n_kw} product{'s' if n_kw != 1 else ''} listed for “{kw}”, but none appear in "
                    f"{domain}'s {_cap} — deep-catalog / no sales for this keyword.")
        return _finish({"domain": domain, "keyword": kw, "geo": geo, "method": "bestseller-order",
                        "keyword_matches": n_kw, "bestsellers_seen": len(bs_order),
                        "bestseller_collections": [], "category_collections": [],
                        "validated": validated[:limit], "n_validated": len(validated),
                        "results": results, "note": note})

    # 2) FALLBACK — store bot-blocked the plain best-seller fetch → robots-allowed merchandising signal.
    kw_handles = {p["handle"] for p in kw_products if p.get("handle")}
    maps = _merchandising_maps(domain, terms, kw_handles)
    bestseller_map, category_map, price_by_handle = maps["bestseller"], maps["category"], maps["price_by_handle"]
    _T = {"bestseller": 0, "category": 1, "listed": 2}
    rows: list[dict] = []
    for p in kw_products:
        h = p.get("handle")
        bs, cat = bestseller_map.get(h), category_map.get(h)
        if bs:
            tier, coll, pos = "bestseller", bs["collection"], bs["pos"]
        elif cat:
            tier, coll, pos = "category", cat["collection"], cat["pos"]
        else:
            tier, coll, pos = "listed", None, None
        rows.append({**p, "price": (price_by_handle.get(h) or p.get("price")), "tier": tier,
                     "collection": coll, "collection_rank": pos, "validated": tier != "listed",
                     "bestseller_rank": None})
    # AI-verify the collection-validated set (fail-open): the merchandising signal validates EVERY
    # keyword product in a matched category collection, which over-includes (an AC surfaced for
    # 'humidifier' because they share the Cooling collection). The AI drops the off-target ones →
    # demote to 'listed'. Same AI-assist as the best-seller-order path, so both methods stay precise.
    cand = [r for r in rows if r["validated"]]
    if cand:
        try:
            from . import ai_product_gate
            _idx = ai_product_gate.match_catalog_titles(kw, [str(r.get("title") or "") for r in cand])
            if _idx is not None:
                for i, r in enumerate(cand):
                    if i not in _idx:
                        r.update({"tier": "listed", "collection": None, "collection_rank": None,
                                  "validated": False})
        except Exception:  # noqa: BLE001 — AI down → keep the collection matches (fail-open)
            pass
    rows.sort(key=lambda r: (_T[r["tier"]], r["collection_rank"] or 10**6, r.get("relevance_rank") or 10**6))
    validated = [r for r in rows if r["validated"]]
    signal_cols = maps["bestseller_collections"] + maps["category_collections"]
    if not kw_products:
        note = f"{domain} lists nothing for “{kw}”."
    elif validated:
        note = ("Best-seller order unavailable (store bot-blocked the fetch) — validated instead against "
                + ", ".join(f"“{c}”" for c in signal_cols[:4]) + f". {len(validated)} of {len(kw_products)}.")
    else:
        note = (f"{domain} lists {len(kw_products)} for “{kw}” but the best-seller order is unavailable "
                f"and none are in a merchandised collection. Shown by relevance.")
    return _finish({"domain": domain, "keyword": kw, "geo": geo, "method": "merchandising-collections",
                    "keyword_matches": len(kw_products),
                    "bestsellers_seen": len(bestseller_map) + len(category_map),
                    "bestseller_collections": maps["bestseller_collections"],
                    "category_collections": maps["category_collections"],
                    "validated": validated[:limit], "n_validated": len(validated),
                    "results": rows[:limit], "note": note})


def _ai_catalog_match_path() -> Path:
    return config.data_root() / "operator-app" / "api" / "data" / "ai-catalog-match.json"


def _catalog_keyword_hits(keyword: str, tok_sets: list[list[str]], catalog: list[dict],
                          domain: str, use_ai: bool = True) -> list[dict]:
    """Raw catalog products matching the keyword: TOKEN match first (cheap, word-boundary), and —
    only when use_ai and a store yields NOTHING — an AI title match (bridges synonyms/abbreviations
    like 'AC'↔'air conditioner' + drops accessories). AI result cached per keyword+domain; fail-safe
    (AI down → just the token matches, never a silent empty)."""
    if not catalog:
        return []
    hits = []
    for p in catalog:
        if not isinstance(p, dict):
            continue
        words = set(re.findall(r"[a-z0-9]+", f"{p.get('title') or ''} {p.get('product_type') or ''}".lower()))
        if any(all(_token_in_words(tk, words) for tk in ts) for ts in tok_sets if ts):
            hits.append(p)
    if hits or not use_ai:
        return hits
    titles = [str(p.get("title") or "") for p in catalog if isinstance(p, dict)]
    ck = f"{(keyword or '').strip().lower()}|{domain}|{len(titles)}"
    cache = _read_json(_ai_catalog_match_path())
    cache = cache if isinstance(cache, dict) else {}
    if ck in cache:
        matched = set(cache[ck])
    else:
        from . import ai_product_gate
        got = ai_product_gate.match_catalog_titles(keyword, titles)
        if got is None:
            return []  # AI unavailable / errored → no synonym hits (token match was already empty)
        matched = {titles[i] for i in got if 0 <= i < len(titles)}
        cache[ck] = sorted(matched)
        try:
            path = _ai_catalog_match_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache))
        except OSError:
            pass
    return [p for p in catalog if isinstance(p, dict) and str(p.get("title") or "") in matched]


def _store_catalog_hits(domain: str, keyword: str, tok_sets: list[list[str]], source: str,
                        use_snapshot: bool, use_ai: bool = True) -> list[dict]:
    """One competitor store's catalog products matching the keyword, normalized to found-product
    cards. use_snapshot=True reads the spy bestseller snapshot (roster stores), else a live fetch.
    use_ai gates the synonym AI fallback (on for Google-Shopping dropshippers who provably carry the
    product; off for the broad roster scan to avoid a wasted AI call per irrelevant general store)."""
    cat = _latest_catalog(domain) if use_snapshot else _fetch_catalog(domain)
    out: list[dict] = []
    for p in _catalog_keyword_hits(keyword, tok_sets, cat, domain, use_ai=use_ai):
        url = str(p.get("url") or "").strip()
        out.append({
            "title": p.get("title"),
            "image": p.get("image") or None,
            "price": _to_float(p.get("price")),
            "price_regular": _to_float(p.get("compare_at")),
            "url": url or None,
            "domain": domain,
            "store_name": domain,
            "rank": p.get("rank"),
            "source": source,
            "validated": False,
        })
    return out


def _discovered_dropshipper_domains(cap: int = 40) -> list[str]:
    """Dropshipper domains the Google-Shopping lane has DISCOVERED + verified (discovered-dropshippers
    .json, all stores unioned). Scanned by the competitor-catalog lane alongside the curated spy
    roster so the two lanes COMPOUND: Google Shopping finds niche-relevant dropshippers → their
    catalogs feed the competitor-catalog keyword scan — WITHOUT auto-polluting the operator-curated
    roster (stores.txt). Bounded + the per-store catalog is 7d-cached, so steady-state stays fast."""
    path = config.data_root() / "operator-app" / "api" / "data" / "discovered-dropshippers.json"
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(d, dict):
        return []
    doms: list[str] = []
    for v in d.values():
        for dom in (v or []):
            s = str(dom or "").strip().lower().replace("www.", "")
            if s and s not in doms:
                doms.append(s)
    return doms[:cap]


def _competitor_bestseller_products(keyword: str, domains: list[str], seen: set[str],
                                    per_store: int = 25, total_cap: int = 40,
                                    budget_s: float = 25.0) -> list[dict]:
    """The competitors' BEST-SELLING products for a keyword — VALIDATED SALES, ranked by best-seller
    position, each with its FULL product-page URL (so it feeds the listing handoff directly). Runs
    `competitor_keyword_scan` across the stores that CARRY the keyword (passed in from the fast catalog
    pass, so only relevant stores get the deep best-seller scan), in parallel; each store's best-seller
    order is cached 7 days. This is the sales-validated core of the competitor-catalog finding lane — a
    store's #74 room-cooler it actually SELLS beats a #2000 catalog match that never moved.

    Bounded to the top ~15 carrying stores, `total_cap` products, and a `budget_s` wall-clock so it
    never stalls the synchronous find: cached stores return instantly; a store whose best-seller order
    isn't cached yet keeps fetching in the BACKGROUND (warming its 7d cache) and is picked up on the
    next find. One slow/blocked store is skipped, never sinking the lane."""
    doms = list(dict.fromkeys((d or "").strip().lower() for d in (domains or []) if (d or "").strip()))
    doms = doms[:15]
    if not doms:
        return []
    import concurrent.futures as _cf
    rows: list[dict] = []
    ex = _cf.ThreadPoolExecutor(max_workers=min(8, len(doms)))
    futs = {ex.submit(competitor_keyword_scan, d, keyword): d for d in doms}
    try:
        for f in _cf.as_completed(futs, timeout=budget_s):
            d = futs[f]
            try:
                res = f.result()
            except Exception:  # noqa: BLE001 — one slow/blocked store never sinks the lane
                continue
            for p in (res.get("validated") or [])[:per_store]:
                url = (p.get("url") or "").strip()
                key = url.lower() or (p.get("title") or "").strip().lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                rank = p.get("bestseller_rank") or p.get("collection_rank")
                rows.append({
                    "title": p.get("title"),
                    "url": url or None,                 # FULL product URL → listing research ref
                    "image": p.get("image"),
                    "price": _to_float(p.get("price")),
                    "price_regular": None,
                    "domain": d,
                    "store_name": d,
                    "rank": rank,
                    "bestseller_rank": p.get("bestseller_rank"),
                    "source": "competitor_bestseller",
                    "validated": True,
                })
    except _cf.TimeoutError:
        pass  # over budget → return what finished; the rest keep warming their caches in the background
    finally:
        ex.shutdown(wait=False)  # don't block the find on stores still warming
    # a store's #12 seller and another's #14 are both strong; lower best-seller rank = sells more.
    rows.sort(key=lambda x: x.get("rank") or 10**6)
    return rows[:total_cap]


def spy_catalog_products_for_keyword(keyword: str, limit: int = 60) -> list[dict]:
    """THE Find-Products source the operator asked for: scan the tracked GOOGLE dropship-competitor
    roster's catalogs (spy snapshots) for products matching the keyword — PLUS the dropshippers the
    Google-Shopping lane has discovered (so the two lanes compound). Real independent Google
    competitors (META-ADS=0 gate), real product-page URLs + images + prices from their own store.
    Empty for a keyword no tracked competitor carries — the signal to track more in that category."""
    kw = (keyword or "").strip()
    tokens = [t for t in re.findall(r"[a-z0-9]+", kw.lower()) if len(t) > 2]
    if not tokens:
        return []
    tok_sets = [tokens]
    # Per-store fetch + keyword match (token, else AI synonym match) run in parallel — so the first
    # search isn't 23 sequential fetches/AI calls; after the first warm everything is cached.
    out: list[dict] = []
    seen: set[str] = set()
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=10) as ex:
        scan_domains = list(dict.fromkeys([*_spy_domains(), *_discovered_dropshipper_domains()]))
        futs = [ex.submit(_store_catalog_hits, d, kw, tok_sets, "spy_catalog", True, False)
                for d in scan_domains]
        for f in futs:
            try:
                rows = f.result(timeout=40) or []
            except Exception:  # noqa: BLE001
                rows = []
            for c in rows:
                key = (c.get("url") or c.get("title") or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(c)
                if len(out) >= limit:
                    return out
    return out


def _trendtrack_competitors(store: str, term: str, ttl_days: int = 7) -> list[dict]:
    """Dropship competitors for a keyword from TrendTrack Meta ads (independent stores + product-page
    links + creatives + ad-longevity). Metered, so cache per (store, term) with a TTL — a re-open
    within the window is free. Best-effort: no token / API error → []."""
    from . import trendtrack
    if not trendtrack.has_token():
        return []
    cache = (config.data_root() / "operator-app" / "api" / "data" / "trendtrack-ads"
             / store / f"{_scan_slug(term)}.json")
    try:
        c = json.loads(cache.read_text())
        ts = datetime.fromisoformat(c["ts"])
        if (datetime.now(timezone.utc) - ts).days < ttl_days:
            return c.get("products") or []
    except (OSError, ValueError, KeyError):
        pass
    res = trendtrack.competitors_for_keyword(term, limit=30)
    prods = res.get("products") or []
    if res.get("ok"):
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "products": prods}))
        except OSError:
            pass
    return prods


def _ai_filter_competitors(products: list[dict]) -> list[dict]:
    """AI 2nd pass over shopping-scan competitors: drop any whose seller the LLM judges to be a
    marketplace or manufacturer brand (novel ones the static _MARKETPLACES/_MAJOR_BRANDS lists
    can't know). Fail-open — keeps everything on error/unsure. Strips the transient `_seller`."""
    if not products:
        return products
    try:
        from . import ai_product_gate
        sellers = [s for s in (str(p.get("_seller") or "").strip() for p in products) if s]
        verdicts = ai_product_gate.classify_sellers(sellers) if sellers else {}
        kept: list[dict] = []
        for p in products:
            s = str(p.get("_seller") or "").strip().lower()
            p.pop("_seller", None)
            if s and (verdicts.get(s) or {}).get("indie", True) is False:
                continue  # confident marketplace / brand → drop
            kept.append(p)
        return kept
    except Exception:  # noqa: BLE001 — AI is advisory; never drop everything on failure
        for p in products:
            p.pop("_seller", None)
        return products


# Marketplaces / big-box — never a dropshipper store (ported from discover_stores_dfs._SKIP_DOMAINS
# + the general-store harvest blocklist). A Google-Shopping seller on one of these is excluded.
_MARKETPLACE_DOMAINS = {
    "amazon.com", "walmart.com", "target.com", "ebay.com", "etsy.com", "wayfair.com", "homedepot.com",
    "lowes.com", "bestbuy.com", "costco.com", "aliexpress.com", "temu.com", "macys.com", "kohls.com",
    "overstock.com", "google.com", "shop.app", "tiktok.com", "samsclub.com", "acehardware.com",
    "wish.com", "newegg.com", "qvc.com", "hsn.com", "bhphotovideo.com", "menards.com", "staples.com",
}


def _is_marketplace_domain(dom: object) -> bool:
    d = str(dom or "").strip().lower().replace("www.", "")
    return d in _MARKETPLACE_DOMAINS or any(d.endswith("." + m) for m in _MARKETPLACE_DOMAINS)


def _seller_domain(domain: object, seller: object) -> str | None:
    """The competitor store's domain from a Google-Shopping listing: the domain field, else the
    seller when it IS a domain ('zerobreeze.com'). None for marketplaces / name-only sellers."""
    dom = re.sub(r"[^a-z0-9.\-]", "", str(domain or "").strip().lower()).replace("www.", "")
    if dom and "." in dom and not _is_marketplace_domain(dom):
        return dom
    s = str(seller or "").strip().lower().replace("www.", "")
    if re.fullmatch(r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", s) and not _is_marketplace_domain(s):
        return s
    return None


def _record_discovered_dropshippers(store: str, domains: set[str]) -> None:
    """Grow the 'who dropships this on Google' list: append newly-seen dropshipper domains to
    discovered-dropshippers.json (candidates the operator can promote into the spy roster)."""
    path = config.data_root() / "operator-app" / "api" / "data" / "discovered-dropshippers.json"
    try:
        cur = json.loads(path.read_text())
        cur = cur if isinstance(cur, dict) else {}
    except (OSError, ValueError):
        cur = {}
    known = set(cur.get(store) or [])
    fresh = known | {d for d in domains if d}
    if fresh != known:
        cur[store] = sorted(fresh)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cur, indent=2))
        except OSError:
            pass


def record_sponsored_advertisers(store: str, advertisers: list[str]) -> dict:
    """The paying Google-Shopping advertisers for a keyword (from the BD Sponsored-PLA capture) are
    PROVEN active dropship competitors — record their domains as discovered dropshippers so the
    competitor-catalog lane scans their catalogs (compounding, like the Google-Shopping lane's own
    discoveries). An advertiser that's already a domain → used directly; a name → resolved via
    seller_domain (BD-backed, cached). Marketplaces/big-box retailers drop out (no Shopify
    /products.json to scan, and the marketplace filters). Returns {recorded: [...]}."""
    from . import seller_domain
    domains: set[str] = set()
    for a in (advertisers or [])[:30]:
        a = str(a or "").strip()
        if not a:
            continue
        if "." in a and " " not in a:
            dom = a.lower().replace("www.", "")
            if not _is_marketplace_domain(dom):
                domains.add(dom)
        elif not _is_marketplace_seller(a):
            d = seller_domain.resolve(a)
            if d and not _is_marketplace_domain(d):
                domains.add(d)
    if domains:
        _record_discovered_dropshippers(store, domains)
    return {"recorded": sorted(domains)}


def _store_type_cache_path() -> Path:
    return config.data_root() / "operator-app" / "api" / "data" / "ai-store-type.json"


def _is_dropshipper_store(domain: str, catalog: list[dict]) -> bool:
    """True ONLY for a generic-product DROPSHIPPER — excludes manufacturer BRANDS (zerobreeze =
    Zero Breeze AC, sylvansport = campers) and marketplaces. Uses the AI store-type classifier
    (cached per domain — a store's type doesn't change); deterministic fallback on AI failure =
    a general store (≥10 distinct product types) is a dropshipper, a narrow single-line catalog a brand."""
    dom = str(domain or "").strip().lower().replace("www.", "")
    if not dom:
        return False
    cache = _read_json(_store_type_cache_path())
    cache = cache if isinstance(cache, dict) else {}
    t = cache.get(dom)
    if t is None:
        from . import ai_product_gate
        titles = [str(p.get("title") or "") for p in (catalog or [])[:25] if isinstance(p, dict)]
        t = ai_product_gate.classify_store_type(dom, titles)
        if t is None:  # AI unavailable → breadth heuristic
            types = {str(p.get("product_type") or "").strip().lower()
                     for p in (catalog or []) if isinstance(p, dict) and p.get("product_type")}
            t = "dropshipper" if len(types) >= 10 else "brand"
        cache[dom] = t
        try:
            path = _store_type_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache))
        except OSError:
            pass
    return t == "dropshipper"


def _gs_store_products(domain: str, keyword: str, tok_sets: list[list[str]]) -> tuple[bool, list[dict]]:
    """One Google-Shopping-discovered store: fetch its catalog, KEEP only if it's a real dropshipper
    (not a brand/marketplace), then scan the catalog for the keyword. Returns (is_dropshipper, rows)."""
    cat = _fetch_catalog(domain)
    if not cat or not _is_dropshipper_store(domain, cat):
        return (False, [])
    rows: list[dict] = []
    for p in _catalog_keyword_hits(keyword, tok_sets, cat, domain, use_ai=True):
        url = str(p.get("url") or "").strip()
        rows.append({
            "title": p.get("title"), "image": p.get("image") or None,
            "price": _to_float(p.get("price")), "price_regular": _to_float(p.get("compare_at")),
            "url": url or None, "domain": domain, "store_name": domain,
            "source": "google_shopping", "validated": False,
        })
    return (True, rows)


_GS_SCAN_FIRED: set[tuple[str, str]] = set()
_GS_SCAN_LOCK = threading.Lock()


def _fire_shopping_scan(store: str, keyword: str) -> None:
    """Background-fire a Google-Shopping scan (DFS Merchant) for a store+keyword with no paid-scan
    yet — so the NEXT find has PLA sellers to resolve→scan (Failure-A recovery). Deduped per
    (store,keyword) per process; the scan job writes shopping-scans/<store>/<slug>/paid-scan.json."""
    key = (store, (keyword or "").strip().lower())
    if not key[1]:
        return
    with _GS_SCAN_LOCK:
        if key in _GS_SCAN_FIRED:
            return
        _GS_SCAN_FIRED.add(key)

    def _work() -> None:
        try:
            from . import jobs
            jobs.create("shopping-scan", store, {"keyword": keyword})
        except Exception:  # noqa: BLE001 — warming is additive; never break the reader
            pass

    threading.Thread(target=_work, daemon=True, name=f"gs-scan-{key[1][:20]}").start()


_PLA_RECORDED: set[tuple[str, str]] = set()
_PLA_LOCK = threading.Lock()


def _record_pla_advertisers_once(store: str, keyword: str) -> None:
    """If a Sponsored-PLA capture (the `sponsored-plas` job / paid_shopping_scan_bd.py) exists for this
    store+keyword, record its advertisers as discovered dropshippers (once, in the background) so the
    competitor-catalog lane scans their catalogs — closing the loop from 'who's PAYING to advertise
    this keyword' to 'their products in your finds'. Deduped per process; a retry is allowed until a
    capture file exists."""
    key = (store, (keyword or "").strip().lower())
    if not key[1]:
        return
    with _PLA_LOCK:
        if key in _PLA_RECORDED:
            return
        _PLA_RECORDED.add(key)
    path = (config.data_root() / "operator-app" / "api" / "data" / "shopping-scans" / store
            / _scan_slug(keyword) / "plas" / "sponsored-plas.json")
    if not path.is_file():
        with _PLA_LOCK:
            _PLA_RECORDED.discard(key)  # no capture yet — allow a retry once one exists
        return

    def _work() -> None:
        try:
            advs = (json.loads(path.read_text()) or {}).get("advertisers") or []
            if advs:
                record_sponsored_advertisers(store, advs)
        except Exception:  # noqa: BLE001 — additive; never break the reader
            pass

    threading.Thread(target=_work, daemon=True, name=f"pla-rec-{key[1][:16]}").start()


_MARKETPLACE_SELLER_RE = re.compile(
    r"\b(amazon|ebay|walmart|aliexpress|ali\s*express|temu|etsy|wish|alibaba|newegg|"
    r"overstock|kohl|costco|sam'?s\s*club|target|best\s*buy)\b", re.I)


def _is_marketplace_seller(seller: object) -> bool:
    """A Google-Shopping seller NAME that is a marketplace / big-box (not a specific product source)."""
    return bool(_MARKETPLACE_SELLER_RE.search(str(seller or "")))


def _google_shopping_catalog_products(store: str, keyword: str, wanted: set[str],
                                      seen: set[str]) -> list[dict]:
    """Google Shopping lane. TWO outputs from the paid-scan.json (DFS Merchant Google-Shopping grid):
    (A) DIRECT — surface the PLA products themselves (Google Shopping's real products for the keyword;
        drop only marketplaces, KEEP brand + niche-store listings as competitor + demand + pricing
        intel). This is the reliable primary output — it returns products whenever a scan exists.
    (B) 2-STAGE (additive) — treat the listings as a SOURCE of DROPSHIPPER stores: resolve seller
        name→domain, keep only verified independent dropshippers, scan each store's whole catalog for
        the keyword, and record the discovered dropshippers to grow the roster (compounds the
        competitor-catalog lane). Fires a background scan when the store has none yet."""
    tok_sets = [ts for ts in ([w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2] for t in wanted) if ts]
    if not tok_sets:
        return []
    scan_dir = config.data_root() / "operator-app" / "api" / "data" / "shopping-scans" / store
    out: list[dict] = []
    domains: set[str] = set()
    seller_names: set[str] = set()
    had_scan = False
    for term in wanted:
        base = scan_dir / _scan_slug(term)
        # Every market's scan — US at <slug>/paid-scan.json + any <slug>/<market>/paid-scan.json (GB…).
        scan_files = [base / "paid-scan.json"] if (base / "paid-scan.json").is_file() else []
        scan_files += sorted(base.glob("*/paid-scan.json"))
        if scan_files:
            had_scan = True
        for sf in scan_files:
            try:
                sdata = json.loads(sf.read_text())
            except (ValueError, OSError):
                continue
            for p in (sdata.get("products") or []):
                if not isinstance(p, dict):
                    continue
                seller = str(p.get("seller") or "").strip()
                dom0 = str(p.get("domain") or "").strip().lower().replace("www.", "")
                # (A) DIRECT: the PLA product itself — drop marketplaces, keep everything else as the
                # real Google-Shopping product for the keyword (competitor + demand + pricing intel).
                if (not _is_marketplace_seller(seller) and not _is_marketplace_domain(dom0)
                        and _keyword_relevant(p.get("title"), wanted)):
                    key = (str(p.get("url") or "") or str(p.get("title") or "")).strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        cur = p.get("price_current")
                        out.append({
                            "title": p.get("title"),
                            "image": p.get("image_url") or p.get("image"),
                            "price": _to_float(cur if cur is not None else p.get("price")),
                            "price_regular": _to_float(p.get("price_regular")),
                            "url": p.get("url") or p.get("shopping_url"),
                            "store_name": seller or dom0 or "Google Shopping",
                            "source": "google_shopping", "validated": False,
                        })
                # (B) 2-stage store discovery: independent dropshippers only (drop marketplaces + brands)
                if not _is_dropship_competitor(p.get("title"), p.get("seller"), p.get("domain")):
                    continue
                dom = _seller_domain(p.get("domain"), p.get("seller"))
                if dom:
                    domains.add(dom)
                elif seller:
                    seller_names.add(seller)  # name-only → resolve below
    # Resolve name-only sellers → domains (DFS Merchant returns names, never domains). Cached +
    # bounded + parallel. Stage B then gates brand-vs-dropshipper on the resolved stores.
    if seller_names:
        from . import seller_domain
        import concurrent.futures as _cf
        names = sorted(seller_names)[:12]  # bound the DFS-SERP fan-out per find (cached per seller)
        with _cf.ThreadPoolExecutor(max_workers=min(10, len(names))) as ex:
            for d in ex.map(seller_domain.resolve, names):
                if d and not _is_marketplace_domain(d):
                    domains.add(d)
    # Stage B: verify each candidate store is a real dropshipper (AI store-type classifier), scan its
    # catalog for the keyword, record verified dropshippers to grow the roster. Additive to (A).
    if domains:
        import concurrent.futures as _cf
        dropshipper_domains: set[str] = set()
        with _cf.ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_gs_store_products, d, keyword, tok_sets): d for d in domains}
            for f in futs:
                try:
                    is_ds, rows = f.result(timeout=45)
                except Exception:  # noqa: BLE001
                    is_ds, rows = False, []
                if is_ds:
                    dropshipper_domains.add(futs[f])
                for c in rows:
                    key = (c.get("url") or c.get("title") or "").strip().lower()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    out.append(c)
        if dropshipper_domains:
            _record_discovered_dropshippers(store, dropshipper_domains)
    # Feed any captured Sponsored-PLA advertisers (the paying competitors) into finding — records
    # them as discovered dropshippers so the competitor-catalog lane scans their catalogs.
    _record_pla_advertisers_once(store, keyword)
    # Failure-A recovery: no scan for this store yet → fire a background Google-Shopping scan (DFS
    # Merchant) so the NEXT find has PLA products to surface + sellers to resolve.
    if not had_scan:
        _fire_shopping_scan(store, keyword)
    return out


# The finding METRIC, made explicit: each source runs its OWN keyword search (AliExpress listing
# sorted by orders / Temu top-sales / 1688 search / Amazon keyword search) and we rank by DEMAND
# (sold count / bought_past_month) where the source has it. This is a light relevance guard on top:
# keep a found product only if its title shares a meaningful token (>2 chars) with the keyword, so
# the rare off-topic result the marketplace search returns can't leak into the finds.
def _scrub(s: object) -> object:
    """Strip ASCII control bytes (0x00-0x1F, 0x7F). Some geo-localized 1688/AliExpress titles carry
    raw control characters that break strict JSON consumers (the browser's JSON.parse). None-safe."""
    if not isinstance(s, str):
        return s
    return re.sub(r"[\x00-\x1f\x7f]", " ", s).strip()


def _scrub_products(products: list[dict]) -> list[dict]:
    """Clean control bytes out of the free-text fields of every found product before it's serialized."""
    for p in products:
        if isinstance(p, dict):
            for k in ("title", "note", "store_name"):
                if isinstance(p.get(k), str):
                    p[k] = _scrub(p[k]) or None
    return products


def _keyword_relevant(title: object, wanted: set[str]) -> bool:
    # Alphabetic tokens only — a number ('2026') isn't an English word, so a mostly-non-Latin title
    # with a model number still counts as "can't token-match → trust the source's search".
    toks = {w for term in wanted for w in re.findall(r"[a-z]+", str(term).lower()) if len(w) > 2}
    if not toks:
        return True
    title_toks = {w for w in re.findall(r"[a-z]+", str(title or "").lower()) if len(w) > 1}
    if not title_toks:
        return True  # no English words (geo-localized 1688/Temu title) — trust the source's own search
    return bool(title_toks & toks)


# ---------------------------------------------------------------- Amazon lane (finding lane 4)
def _amazon_cached(term: str, ttl_days: int = 7, fetch: bool = True) -> list[dict]:
    """Cached amazon_search(term) — {"ts","products"} on the volume, 7-day TTL, so repeat 'Find
    products' clicks for the same term don't re-bill DataForSEO. [] on any failure (advisory lane).
    fetch=False = CACHE-ONLY (never calls DFS) — for the cheap overviews (movers / plan pools) that
    must not trigger a live-fetch storm across every candidate; they surface only already-found terms."""
    cache = config.spy_data_dir() / "finding-cache" / "amazon" / f"{_scan_slug(term)}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("products") or []
    except (OSError, ValueError, KeyError):
        pass
    if not fetch:
        return []  # cache-only mode — never live-fetch (keeps overview endpoints cheap)
    # Creds MUST come from Connections (DB) via runtime_get — this runs IN-PROCESS in the FastAPI
    # request, and shopping_scan_dfs._load_creds() only sees os.environ / a .env (empty on the live
    # deploy, where DataForSEO creds live in Connections). Subprocess jobs get them via as_env().
    import base64 as _b64
    from . import shopping_scan_dfs, connections
    _u = (connections.runtime_get("DATAFORSEO_USERNAME") or "").strip()
    _p = (connections.runtime_get("DATAFORSEO_PASSWORD") or "").strip()
    _auth = _b64.b64encode(f"{_u}:{_p}".encode()).decode() if (_u and _p) else None
    prods = shopping_scan_dfs.amazon_search(term, geo="US", depth=30, auth=_auth)
    if prods:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "products": prods}))
        except OSError:
            pass
    return prods


def _amazon_lane_products(keyword: str, wanted: set[str], seen: set[str], fetch: bool = True) -> list[dict]:
    """Amazon finding lane — LIVE DataForSEO merchant/amazon/products search per build term (fetched
    in parallel, cached 7d). Amazon is a RESEARCH source in the operator's model: proven demand
    (bought_past_month + review count) + a retail price benchmark, never the supplier. Rows stamped
    source='amazon' with a demand note so the card shows WHY the product is a contender.
    fetch=False = CACHE-ONLY (overviews) — returns lane rows only for terms already found + cached."""
    import concurrent.futures as _cf
    terms = sorted(wanted)
    if not terms:
        return []
    with _cf.ThreadPoolExecutor(max_workers=min(6, len(terms))) as ex:
        batches = list(ex.map(lambda t: _amazon_cached(t, fetch=fetch), terms))
    out: list[dict] = []
    for prods in batches:
        for p in prods:
            title = (p.get("title") or "").strip()
            url = (p.get("url") or "").strip()
            key = (url or title).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            demand = p.get("bought_past_month")
            bits: list[str] = []
            if demand:
                bits.append(f"{int(demand):,}+ bought/mo")
            if p.get("reviews"):
                bits.append(f"{int(p['reviews']):,} reviews")
            if p.get("is_best_seller"):
                bits.append("Best Seller")
            elif p.get("is_amazon_choice"):
                bits.append("Amazon's Choice")
            out.append({
                "title": title or None,
                "image": p.get("image") or None,
                "price": p.get("price"),
                "price_regular": p.get("price_regular"),
                "url": url or None,
                "store_name": "Amazon",
                "source": "amazon",
                "rating": p.get("rating"),
                "reviews": p.get("reviews"),
                "bought_past_month": demand,
                "note": " · ".join(bits) or None,
                "validated": False,
            })
    # Relevance guard on top of Amazon's own keyword search — drop the occasional off-topic result.
    out = [r for r in out if _keyword_relevant(r.get("title"), wanted)]
    # Strongest demand first (bought/mo, then review depth), capped so one live-fetched Amazon page
    # doesn't bury the catalog / marketplace lanes in the aggregated 'Find products' view.
    out.sort(key=lambda p: (-(p.get("bought_past_month") or 0), -(p.get("reviews") or 0)))
    return out[:24]


# ---------------------------------------------------------------- Marketplace lane (finding lane 5)
# MARKETPLACE = AliExpress / 1688 / Temu (the dropship-SOURCING marketplaces) — NOT US retailers
# (Walmart/Target/Best Buy). AliExpress is the one server-scrapable from the API process (Bright
# Data Web Unlocker on the listing page); Temu needs a local CDP session + aged cookies and 1688 is
# x5sec-walled (Apify only), so neither runs from the server yet. AliExpress gives the operator's
# real marketplace signals: SOLD count (proven demand) + struck compare-at (COGS basis). RESEARCH /
# discovery source only — the private agent is the supplier, never the AliExpress listing.
def _aliexpress_cached(term: str, fetch: bool = True, ttl_days: int = 7) -> list[dict]:
    """Cached AliExpress search(term) — {"ts","products"} on the volume, 7-day TTL (a BD Web-Unlocker
    fetch is slow + billable, so repeat 'Find products' clicks reuse it). fetch=False = CACHE-ONLY
    (movers / plan-pool overviews — never triggers a live BD fetch across every candidate).
    Creds come from Connections via runtime_get (in-process — os.environ is empty on the live deploy):
    BRIGHTDATA_API_TOKEN + the Web-Unlocker zone (BRIGHTDATA_SERP_ZONE, live value 'web_unlocker1')."""
    cache = config.spy_data_dir() / "finding-cache" / "marketplace" / f"{_scan_slug(term)}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("products") or []
    except (OSError, ValueError, KeyError):
        pass
    if not fetch:
        return []  # cache-only mode — no live BD fetch
    from . import aliexpress_search, connections
    token = (connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
    zone = (connections.runtime_get("BRIGHTDATA_UNLOCKER_ZONE")
            or connections.runtime_get("BRIGHTDATA_SERP_ZONE") or "web_unlocker1").strip()
    prods = aliexpress_search.search(term, token, zone, geo="US")
    if prods:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "products": prods}))
        except OSError:
            pass
    return prods


_TEMU_WARMING: set[str] = set()
_TEMU_WARM_LOCK = threading.Lock()


def _fire_temu_warm(term: str) -> None:
    """The Apify Temu actor run is slow (server-side render, ~1-3 min) — too slow to await inside a
    Find request. So on a cache miss we fire ONE deduped daemon thread that runs the actor and writes
    the 7d cache; the Find returns without Temu now, and the NEXT Find for the term serves Temu from
    cache. Best-effort; a deploy kills the thread and the next miss re-fires. Mirrors the
    AliExpress/1688 finding-warm pattern for the slow source."""
    key = _scan_slug(term)
    with _TEMU_WARM_LOCK:
        if key in _TEMU_WARMING:
            return
        _TEMU_WARMING.add(key)

    def _run() -> None:
        try:
            from . import apify_temu, connections
            tok = (connections.runtime_get("APIFY_TOKEN") or "").strip()
            prods = apify_temu.search(term, tok, geo="US", max_items=20, timeout=240)
            if prods:
                cache = config.spy_data_dir() / "finding-cache" / "temu" / f"{_scan_slug(term)}.json"
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(
                    {"ts": datetime.now(timezone.utc).isoformat(), "products": prods}))
        except Exception:  # noqa: BLE001 — advisory warm, never crash the thread
            pass
        finally:
            with _TEMU_WARM_LOCK:
                _TEMU_WARMING.discard(key)

    threading.Thread(target=_run, daemon=True, name=f"temu-warm-{key[:20]}").start()


def _temu_cached(term: str, fetch: bool = True, ttl_days: int = 7) -> list[dict]:
    """Cached Temu search(term) via the Bright Data Temu dataset — {"ts","products"} on the volume,
    7d TTL. The BD dataset snapshot is ASYNC (minutes), so this NEVER fetches inline: a cache hit is
    served; a miss fires a background warm (_fire_temu_warm — long wait → writes the cache) and returns
    [] now, so Temu fills in on a later Find without ever hanging the request. fetch=False = pure
    cache-only (overviews; no warm fired). Dataset id (BRIGHTDATA_TEMU_DATASET) optional — blank
    auto-resolves the account's subscribed Temu dataset; [] until a snapshot completes / if none
    subscribed."""
    cache = config.spy_data_dir() / "finding-cache" / "temu" / f"{_scan_slug(term)}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("products") or []
    except (OSError, ValueError, KeyError):
        pass
    if not fetch:
        return []
    # Temu keyword-finding runs via the Apify actor (crw/temu-products-scraper) — the ONLY working
    # Temu source (BrightData can't scrape Temu: verified across Web Unlocker / Scraping Browser /
    # screenshot — the account's BD Temu dataset is URL-collect only). The actor run is slow
    # (server-side render), so warm it in a background thread and Temu fills in on a later Find. Warm
    # only when APIFY_TOKEN is set; without it Temu is skipped and the lane runs on AliExpress + 1688.
    from . import connections
    if (connections.runtime_get("APIFY_TOKEN") or "").strip():
        _fire_temu_warm(term)
    return []


def _mp_row(p: dict, store_name: str) -> dict:
    """Normalize an AliExpress / Temu / 1688 product into a marketplace lane row. All three are
    PRODUCT-FINDING sources (find products for a trend/keyword) — first-class candidates to list,
    not a COGS lookup. sold_count = demand where the source has it (AliExpress/Temu); 1688 has none,
    so its note carries supplier + MOQ instead. price is the product's own price."""
    sold = p.get("sold")
    bits: list[str] = []
    if sold:
        bits.append(f"{int(sold):,} sold")
    if p.get("moq"):
        bits.append(f"MOQ {p.get('moq')}")
    if p.get("supplier"):
        bits.append(str(p.get("supplier"))[:28])
    return {
        "title": (p.get("title") or "").strip() or None,
        "image": p.get("image") or None,
        "price": p.get("price"),
        "price_regular": p.get("compare_at"),
        "cogs": p.get("compare_at"),   # struck compare-at (AliExpress/Temu); None for 1688 — NOT a COGS tool
        "sold_count": sold,
        "rating": p.get("rating"),
        "reviews": p.get("reviews"),
        "url": (p.get("url") or "").strip() or None,
        "store_name": store_name,
        "source": "marketplace",
        "note": p.get("sold_text") or (" · ".join(bits) or None),
        "validated": False,
    }


def _method_on(m: str) -> bool:
    """Is a finding method enabled per the operator's SKU-plan source_weights (weight > 0)? No
    weights persisted → every method on. Module-level so every finder shares one gate."""
    try:
        from . import sku_plan
        mw = sku_plan.saved_settings().get("source_weights") or {}
    except Exception:  # noqa: BLE001
        mw = {}
    return (not mw) or float(mw.get(m, 0) or 0) > 0


def find_keyword_products(keyword: str, terms: list[str] | None = None,
                          fetch: bool = True) -> list[dict]:
    """Store-INDEPENDENT product research for a trend/keyword: the marketplace (AliExpress + Temu +
    1688) + Amazon lanes = 'find products from a trend or keyword'. Shared by the SKU-plan drill-in
    AND the trend/keyword FindProducts modal so product research finds the SAME real products
    everywhere (not a launcher-URL guide). Respects the operator's per-method source_weights.
    fetch=False = CACHE-ONLY (the instant read path); fetch=True = live fetch (the background warm)."""
    wanted = {t.strip().lower() for t in (terms or [keyword]) if (t or "").strip()}
    if not wanted:
        wanted = {(keyword or "").strip().lower()}
    # AI-assist is STANDARD on every finding surface (operator's ask): on a live fetch, widen the
    # search with the LLM's related buyer phrasings so the parallel lanes fan out across them. Only on
    # fetch=True (the warm) — cache-only overviews reuse the already-warmed ai-terms cache for free.
    if fetch:
        try:
            from . import ai_find_assist
            wanted |= {t.strip().lower() for t in ai_find_assist.expand_terms(keyword) if (t or "").strip()}
        except Exception:  # noqa: BLE001 — advisory; never break the finder
            pass
    seen: set[str] = set()
    out: list[dict] = []
    if _method_on("marketplace"):
        out += _marketplace_lane_products("", keyword, wanted, seen, fetch=fetch)
    if _method_on("amazon"):
        out += _amazon_lane_products(keyword, wanted, seen, fetch=fetch)
    return _scrub_products(out)


# ── Instant finds: read the finding cache synchronously (fast), warm cold keywords in the background.
# The live fetches (BD Web Unlocker for AliExpress, the 1688 collector, DFS for Amazon) take ~30-45s,
# too slow to block a request — so the endpoint returns the CACHE immediately and, if a keyword is
# cold, fires ONE deduped background thread to fetch → cache. The client re-polls while `warming`.
_FINDING_WARMING: set[str] = set()
_FINDING_WARM_LOCK = threading.Lock()


def _fire_finding_warm(keyword: str, terms: list[str] | None = None) -> None:
    """Fetch all finding sources (fetch=True) → populate the 7d cache, in a daemon thread. Deduped
    per keyword so concurrent 'Find products' clicks don't stack fetches. Best-effort; a deploy kills
    it and the next click re-fires."""
    key = (keyword or "").strip().lower()
    if not key:
        return
    with _FINDING_WARM_LOCK:
        if key in _FINDING_WARMING:
            return
        _FINDING_WARMING.add(key)

    def _run() -> None:
        try:
            find_keyword_products(keyword, terms, fetch=True)
        except Exception:  # noqa: BLE001 — advisory warm, never crash the thread
            pass
        finally:
            with _FINDING_WARM_LOCK:
                _FINDING_WARMING.discard(key)

    threading.Thread(target=_run, daemon=True, name=f"find-warm-{key[:24]}").start()


def find_keyword_products_instant(keyword: str, terms: list[str] | None = None) -> tuple[list[dict], bool]:
    """The instant read: cache-only finds now (fast) + (products, warming). If nothing is cached yet,
    fire a background warm and return warming=True so the client re-polls until the cache fills."""
    products = find_keyword_products(keyword, terms, fetch=False)
    warming = False
    if not products and (_method_on("marketplace") or _method_on("amazon")):
        _fire_finding_warm(keyword, terms)
        with _FINDING_WARM_LOCK:
            warming = (keyword or "").strip().lower() in _FINDING_WARMING
    return products, warming


def gs_debug(store: str, keyword: str) -> dict:
    """Google-Shopping lane chain diagnostic for a keyword: does a paid-scan exist? how many PLA
    products + which sellers? how many the lane surfaces (direct + 2-stage)? — pinpoints where the
    lane produces vs stalls (no scan yet / marketplace-only / brand-only sellers)."""
    kw = (keyword or "").strip()
    scan_dir = config.data_root() / "operator-app" / "api" / "data" / "shopping-scans" / store
    base = scan_dir / _scan_slug(kw)
    scan_files = [base / "paid-scan.json"] if (base / "paid-scan.json").is_file() else []
    scan_files += sorted(base.glob("*/paid-scan.json"))
    prods: list[dict] = []
    for sf in scan_files:
        try:
            prods += (json.loads(sf.read_text()).get("products") or [])
        except (OSError, ValueError):
            pass
    info: dict = {
        "store": store, "keyword": kw, "scan_files": len(scan_files), "scan_products": len(prods),
        "sellers": sorted({str(p.get("seller") or "") for p in prods if p.get("seller")})[:15],
        "with_domain": sum(1 for p in prods if p.get("domain")),
    }
    try:
        out = _google_shopping_catalog_products(store, kw, {kw.lower()}, set())
        info["lane_products"] = len(out)
        info["lane_sample"] = [(r.get("store_name"), (r.get("title") or "")[:36]) for r in out[:5]]
    except Exception as e:  # noqa: BLE001
        info["lane_error"] = repr(e)[:200]
    return info


def temu_datasets() -> dict:
    """The account's Bright Data Temu-matching datasets (name + id) via GET /datasets/list — a single
    fast HTTP call, NO snapshot trigger. Lets the operator/diagnostic confirm a keyword-DISCOVER Temu
    dataset is subscribed (auto-resolve picks the first name-match, which may be a URL-collect type
    that can't discover-by-keyword). `total_datasets` sanity-checks the token can read the Dataset API."""
    from . import connections
    import urllib.request as _u
    tok = (connections.runtime_get("BRIGHTDATA_DATASET_TOKEN")
           or connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
    if not tok:
        return {"error": "no BD token configured (BRIGHTDATA_DATASET_TOKEN / BRIGHTDATA_API_TOKEN)"}
    try:
        with _u.urlopen(_u.Request("https://api.brightdata.com/datasets/list", method="GET",
                        headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"}),
                        timeout=25) as r:  # noqa: S310 (trusted BD endpoint)
            alld = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)[:200]}
    dl = alld if isinstance(alld, list) else (alld.get("datasets") or alld.get("data") or [])
    temu = [{"id": d.get("id"), "name": (d.get("name") or "")}
            for d in dl if isinstance(d, dict) and "temu" in (d.get("name") or "").lower()]
    return {"total_datasets": len(dl) if isinstance(dl, list) else None, "temu_matches": temu[:12]}


def temu_web_probe(keyword: str) -> dict:
    """Evidence probe for the Temu Web-Unlocker path — does the BD Web Unlocker return extractable
    Temu product data for a keyword? The account's only Temu dataset is URL-collect (can't
    discover-by-keyword), so keyword finds need this path; this confirms viability before a parser."""
    from . import temu_search, connections
    tok = (connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
    zone = (connections.runtime_get("BRIGHTDATA_UNLOCKER_ZONE")
            or connections.runtime_get("BRIGHTDATA_SERP_ZONE") or "web_unlocker1").strip()
    if not tok:
        return {"error": "no BRIGHTDATA_API_TOKEN"}
    return temu_search.probe((keyword or "").strip(), tok, zone)


def finding_lane_health(keyword: str) -> dict:
    """Per-source finding-lane health for one keyword — runs each marketplace sub-source (AliExpress,
    1688/TMAPI, Temu) + Amazon in ISOLATION with error capture, so the operator (and diagnostics) can
    see WHICH sources are live vs dry vs erroring, and why. LIVE fetch (fetch=True) — call sparingly.
    Reports the TMAPI client-load + token state and the Temu dataset resolution so a 0 is explainable
    (import failed / no token / no dataset / source genuinely empty) rather than a silent blank."""
    from . import connections, alibaba_search, temu_search
    kw = (keyword or "").strip()
    out: dict = {"keyword": kw, "sources": {}}
    if not kw:
        return out
    wanted = {kw.lower()}

    def _probe(name: str, fn) -> None:
        try:
            rows = fn() or []
            out["sources"][name] = {"count": len(rows),
                                    "sample": [(r.get("title") or "")[:48] for r in rows[:2]]}
        except Exception as e:  # noqa: BLE001 — a probe never raises; it records the failure
            out["sources"][name] = {"error": repr(e)[:200]}

    # AliExpress + Amazon: cache-only (fast) — they're already proven live; the point of this probe is
    # the flaky sources (1688/TMAPI live + fast, Temu dataset). Keeps the endpoint responsive.
    _probe("aliexpress", lambda: _aliexpress_cached(kw, fetch=False))
    out["tmapi"] = {"token": bool(connections.runtime_get("TMAPI_TOKEN")),
                    "client_loaded": getattr(alibaba_search, "_canonical", None) is not None}
    _probe("alibaba_1688", lambda: _alibaba_cached(kw, fetch=True))
    try:
        resolved = temu_search.resolve_dataset_id(
            (connections.runtime_get("BRIGHTDATA_DATASET_TOKEN")
             or connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip())
    except Exception as e:  # noqa: BLE001
        resolved = f"resolve-error: {repr(e)[:90]}"
    out["temu_dataset"] = {"configured": bool(connections.runtime_get("BRIGHTDATA_TEMU_DATASET")),
                           "resolved": resolved}
    # List the account's Temu-matching datasets (name + id) — auto-resolve picks the first name-match,
    # which may be the WRONG TYPE (a URL-collect dataset can't discover-by-keyword). Shows what's
    # actually subscribed so the operator/diagnostic can pick a keyword-discover dataset.
    try:
        import urllib.request as _u
        _t = (connections.runtime_get("BRIGHTDATA_DATASET_TOKEN")
              or connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
        with _u.urlopen(_u.Request("https://api.brightdata.com/datasets/list", method="GET",
                        headers={"Authorization": f"Bearer {_t}", "Accept": "application/json"}),
                        timeout=25) as _r:  # noqa: S310 (trusted BD endpoint)
            _all = json.loads(_r.read().decode("utf-8", "replace"))
        _dl = _all if isinstance(_all, list) else (_all.get("datasets") or _all.get("data") or [])
        out["temu_dataset"]["total_datasets"] = len(_dl) if isinstance(_dl, list) else None
        out["temu_dataset"]["temu_matches"] = [
            {"id": d.get("id"), "name": (d.get("name") or "")[:64]}
            for d in _dl if isinstance(d, dict) and "temu" in (d.get("name") or "").lower()][:8]
    except Exception as e:  # noqa: BLE001
        out["temu_dataset"]["list_error"] = repr(e)[:150]
    # Temu: report each BD-dataset snapshot stage so a 0 is explainable (trigger sid? snapshot ready
    # in the wait window? how many raw rows?) — distinguishes async-timeout from genuinely-empty.
    temu_stage: dict = {"client_loaded": getattr(temu_search, "_canonical", None) is not None}
    try:
        _tok = (connections.runtime_get("BRIGHTDATA_DATASET_TOKEN")
                or connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
        _ds = ((connections.runtime_get("BRIGHTDATA_TEMU_DATASET") or "").strip()
               or (resolved if isinstance(resolved, str) and resolved.startswith("gd_") else ""))
        temu_stage["dataset"] = _ds[:22]
        if temu_search._canonical is not None and _ds and _tok:
            _sid = temu_search._canonical.trigger(_ds, _tok, [{"keyword": kw}], by="keyword")
            temu_stage["sid"] = str(_sid)[:26]
            # Short, non-blocking probe — the BD Temu snapshot is async (minutes); we only confirm the
            # trigger works here. Actual rows come via the background warm (_fire_temu_warm) → cache.
            _ready = temu_search._canonical.wait(_sid, _tok, timeout=12)
            temu_stage["snapshot_ready_in_12s"] = bool(_ready)
            if _ready:
                temu_stage["fetch_rows"] = len(temu_search._canonical.fetch(_sid, _tok) or [])
    except Exception as e:  # noqa: BLE001
        temu_stage["error"] = repr(e)[:200]
    out["sources"]["temu"] = temu_stage
    _probe("amazon", lambda: _amazon_lane_products(kw, wanted, set(), fetch=False))
    return out


def _alibaba_cached(term: str, fetch: bool = True, ttl_days: int = 7) -> list[dict]:
    """Cached Alibaba/1688 keyword search(term) via TMAPI (/1688/search/items) — 7d TTL. fetch=False
    = CACHE-ONLY (overviews). Token from Connections (TMAPI_TOKEN — the working 1688 API; the old BD
    Scraper-Studio collector was async + kept timing out to []). [] on any failure / missing token."""
    cache = config.spy_data_dir() / "finding-cache" / "alibaba" / f"{_scan_slug(term)}.json"
    try:
        c = json.loads(cache.read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(c["ts"])).days < ttl_days:
            return c.get("products") or []
    except (OSError, ValueError, KeyError):
        pass
    if not fetch:
        return []
    from . import alibaba_search, connections
    tok = (connections.runtime_get("TMAPI_TOKEN") or "").strip()
    # TMAPI /1688/search/items is a synchronous single call (no collector polling) — returns the
    # 1688 offers for the keyword directly, so no inline wait / warm-then-repeat dance.
    prods = alibaba_search.search(term, tok, max_items=20)
    if prods:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "products": prods}))
        except OSError:
            pass
    return prods


def _marketplace_lane_products(store: str, keyword: str, wanted: set[str], seen: set[str],
                               fetch: bool = True) -> list[dict]:
    """Marketplace finding lane — the operator's dropship-SOURCING marketplaces (AliExpress + Temu),
    NEVER US retailers. Per build term it fetches AliExpress (BD Web Unlocker listing sorted by
    ORDERS) + Temu (Apify top-sales actor) CONCURRENTLY → proven demand (sold count) + COGS basis
    (struck compare-at). RESEARCH source only; the private agent is the supplier. Temu is skipped
    silently when APIFY_TOKEN is unset / the Apify account is out of credit (→ AliExpress-only).
    (1688/Alibaba is a wholesale-COGS collector — Chinese titles, async — kept out of the live lane.)
    `store` unused (both store-independent). fetch=False = CACHE-ONLY (movers / plan pools)."""
    import concurrent.futures as _cf
    terms = sorted(wanted)
    if not terms:
        return []
    # (source, term) fan-out over the 3 sourcing marketplaces — all network-bound + independent, so
    # run concurrently: a multi-term drill-in ≈ the slowest single fetch, not the sum. AliExpress +
    # Temu carry demand (sold count); Alibaba/1688 carries the real wholesale COGS + supplier.
    jobs: list[tuple[str, str]] = [(src, t) for t in terms for src in ("aliexpress", "temu", "alibaba")]

    def _one(job: tuple[str, str]) -> list[dict]:
        src, term = job
        if src == "aliexpress":
            rows = [_mp_row(p, "AliExpress") for p in _aliexpress_cached(term, fetch=fetch)]
        elif src == "temu":
            rows = [_mp_row(p, "Temu") for p in _temu_cached(term, fetch=fetch)]
        else:
            rows = [_mp_row(p, "Alibaba") for p in _alibaba_cached(term, fetch=fetch)]
        return rows[:40]  # generous per-source ceiling; the round-robin below balances representation

    with _cf.ThreadPoolExecutor(max_workers=min(9, len(jobs))) as ex:
        batches = list(ex.map(_one, jobs))
    # Bucket by source (deduped, relevance-filtered, demand-sorted within source) so the round-robin
    # can represent every marketplace that returned — while still letting ONE hot source fill toward
    # the aggregate when the others are dry. bamboo-sheets returned AliExpress-only; the old flat
    # rows[:10] + out[:30] starved it at 10 even though AliExpress had far more to give.
    by_source: dict[str, list[dict]] = {}
    for (src, _term), rows in zip(jobs, batches):
        bucket = by_source.setdefault(src, [])
        for r in rows:
            key = (r.get("url") or r.get("title") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            # AliExpress serves English titles → drop the occasional off-topic result. 1688 (TMAPI)
            # and Temu return TRANSLATED titles (Spanish "Sábanas de Bambú", Chinese) whose latin
            # tokens never equal the English keyword — the English filter false-drops them (this is
            # exactly why 1688 read 0 for "bamboo sheets" despite TMAPI returning matches). Those
            # sources already keyword-searched server-side, so trust them.
            if src == "aliexpress" and not _keyword_relevant(r.get("title"), wanted):
                continue
            bucket.append(r)
    for rows in by_source.values():
        # Strongest demand first (most sold); 1688/Alibaba (no sold-count) keeps stable order.
        rows.sort(key=lambda r: -(r.get("sold_count") or 0))
    # Round-robin one row from each non-empty source per cycle, up to the aggregate cap — represents
    # every source that returned when several do, and drains a single source when it's the only one.
    pools = [by_source[s] for s in ("aliexpress", "temu", "alibaba") if by_source.get(s)]
    out: list[dict] = []
    _AGG = 50
    while pools and len(out) < _AGG:
        for pool in list(pools):
            if not pool:
                pools.remove(pool)
                continue
            out.append(pool.pop(0))
            if len(out) >= _AGG:
                break
    return out


def _allocate_lanes(buckets: dict[str, list], base: dict[str, float], target: int,
                    priority: list[str]) -> dict[str, int]:
    """Two-pass weight-proportional allocation with dry-lane redistribution.

    Pass 1 gives every lane min(available, its weighted quota). Pass 2 hands the budget freed by
    lanes that could NOT fill their quota (a dry Google lane, etc.) to lanes that still have supply,
    walking `priority` order — so when the high-weight lanes come back empty the target is filled
    from the lanes that CAN supply (marketplace) instead of the find collapsing to a handful. When
    every lane supplies its quota there is no surplus, so the weighted split holds exactly (no lane
    floods past its share). The total never exceeds `target`."""
    take = {lane: min(len(items), max(0, int(base.get(lane, 0) or 0)))
            for lane, items in buckets.items()}
    surplus = max(0, int(target) - sum(take.values()))
    for lane in priority:
        if surplus <= 0:
            break
        leftover = len(buckets.get(lane, [])) - take.get(lane, 0)
        if leftover > 0:
            grab = min(leftover, surplus)
            take[lane] = take.get(lane, 0) + grab
            surplus -= grab
    return take


def found_products_for_head(store: str, keyword: str, terms: list[str] | None = None) -> dict:
    """Every product the discovery lanes have stamped for a head keyword's plan, aggregated
    across all lanes (Google shopping-scan, best-seller spy, Amazon, marketplace, Meta). The
    'Find products' handoff fires a scan per BUILD TERM (the anchor + selected Tier-2), each of
    which lands as its own candidate — so this walks every build term's candidate, not just the
    head, to show ALL products the plan turned up. `terms` is the plan's build-term list (falls
    back to the head keyword alone). Surfaced as a drill-in so the SKU-plan table stays clean and
    the found products live behind a click-in. Empty until lanes collect, with each product
    stamped with the lane it was found in so the operator can SEE the source."""
    wanted = {t.strip().lower() for t in (terms or [keyword]) if (t or "").strip()}
    if not wanted:
        wanted = {(keyword or "").strip().lower()}
    # AI-assist finding — widen the search with related buyer phrasings (synonyms + form/spec variants)
    # so the parallel marketplace lanes fan out across them and surface products a single literal query
    # misses. Cached + fail-open: no LLM gateway configured -> ai_terms is just [keyword], unchanged.
    try:
        from . import ai_find_assist
        ai_terms = ai_find_assist.expand_terms(keyword)
    except Exception:  # noqa: BLE001 — advisory; never break the finder
        ai_terms = [keyword]
    wanted |= {t.strip().lower() for t in ai_terms if (t or "").strip()}
    q = candidate_queue(store) or {}
    raw = q.get("candidates")
    items = list(raw.values()) if isinstance(raw, dict) else (raw or [])
    cands = [
        c for c in items
        if isinstance(c, dict) and (c.get("keyword") or "").strip().lower() in wanted
    ]
    lane_label = {l["id"]: l["name"] for l in KEYWORD_LANES}
    products: list[dict] = []
    seen: set[str] = set()
    for cand in cands:
        for lane_id, lane in (cand.get("lanes") or {}).items():
            if not isinstance(lane, dict):
                continue
            for p in _lane_products(lane):
                key = (p.get("url") or p.get("title") or "").strip().lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                if not p.get("store_name"):
                    # stamp lane provenance so the card shows WHERE it was found
                    p["store_name"] = lane_label.get(lane_id, lane_id)
                products.append(p)

    # Respect the operator's per-method settings (SKU-planner source_weights): a method with weight 0
    # is OFF, so it isn't fetched/shown here (e.g. 'meta = 0' actually stops the Meta calls). No
    # weights stored → every method on, uncapped.
    try:
        from . import sku_plan
        _settings = sku_plan.saved_settings()
        _mw = _settings.get("source_weights") or {}
    except Exception:  # noqa: BLE001
        _settings, _mw = {}, {}

    def _method_on(m: str) -> bool:
        return (not _mw) or float(_mw.get(m, 0) or 0) > 0

    # Gather each enabled lane UNCAPPED into its own bucket, sharing `seen` so lanes stay deduped.
    # Capping happens AFTER, in one weight-proportional pass with dry-lane redistribution (below) —
    # so the two structurally-dry Google lanes don't just forfeit their budget and collapse the find.
    buckets: dict[str, list[dict]] = {}

    # PRIMARY source (operator's model): the tracked GOOGLE dropship-competitor roster. Two-stage:
    # (1) fast catalog scan of each store's /products.json → keyword matches + WHICH stores carry it;
    # (2) BEST-SELLER-VALIDATE the carrying stores (competitor_keyword_scan — proven sellers off the
    # real /collections/all?sort_by=best-selling order, ranked, full product URLs) → these LEAD the
    # lane. So the operator sees the competitors' actual best-selling SKUs for the keyword first, then
    # broader catalog matches. Both carry real product-page URLs for the listing handoff.
    if _method_on("competitor_catalog"):
        cat_raw: list[dict] = []
        carry: list[str] = []
        _cat_seen: set[str] = set()
        for term in wanted:
            for c in spy_catalog_products_for_keyword(term):
                key = (c.get("url") or c.get("title") or "").strip().lower()
                if key and key in _cat_seen:
                    continue
                if key:
                    _cat_seen.add(key)
                cat_raw.append(c)
                dom = (c.get("domain") or "").strip().lower()
                if dom and dom not in carry:
                    carry.append(dom)
        bestsellers = _competitor_bestseller_products(keyword, carry, seen)  # proven sellers, into `seen`
        cat: list[dict] = list(bestsellers)
        for c in cat_raw:  # catalog matches not already surfaced as a proven best-seller
            key = (c.get("url") or c.get("title") or "").strip().lower()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            cat.append(c)
        buckets["competitor_catalog"] = cat

    # Google Shopping 2-stage: dropshipper STORES from the paid-scan listings (drop marketplaces +
    # brands) → scan each store's CATALOG for the keyword; records discovered dropshippers.
    if _method_on("google_shopping"):
        buckets["google_shopping"] = _google_shopping_catalog_products(store, keyword, wanted, seen)

    # Meta lane: dropship advertisers running Meta ads for the keyword (TrendTrack) — supplementary.
    if _method_on("meta"):
        meta_products: list[dict] = []
        for term in wanted:
            for c in _trendtrack_competitors(store, term):
                if not isinstance(c, dict):
                    continue
                key = (c.get("url") or c.get("domain") or c.get("title") or "").strip().lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                c["source"] = "meta"
                meta_products.append(c)
        buckets["meta"] = meta_products

    # Amazon (lane 4) + Marketplace (lane 5: AliExpress + Temu + 1688). Fetched live (fetch=True) —
    # fast on a warm 7d cache, ~30-45s only on a truly-cold keyword.
    if _method_on("amazon"):
        buckets["amazon"] = _amazon_lane_products(keyword, wanted, seen)
    if _method_on("marketplace"):
        buckets["marketplace"] = _marketplace_lane_products(store, keyword, wanted, seen)

    # Weight-proportional allocation WITH dry-lane redistribution. The find targets the plan's
    # products_per_build. Each lane first gets its weighted share (Settings → "Where products come
    # from"); then the budget freed by lanes that came back thin is handed to the lanes that CAN
    # supply, in operator-priority order. So a Google-led 40/40/20 split whose two Google lanes are
    # dry (no PLA dropshipper capture / tracked stores don't carry the term — common for many
    # keywords) fills toward the target from marketplace instead of collapsing to ~10. When every
    # lane supplies, the split holds and marketplace stays at its ~20% share (no flood).
    target = int(_settings.get("products_per_build") or 0) or 60
    if _mw:
        try:
            base = sku_plan.source_quota(target, _mw)
        except Exception:  # noqa: BLE001 — allocation is additive; never break the finder
            base = {}
        take = _allocate_lanes(buckets, base, target,
                               priority=["competitor_catalog", "google_shopping",
                                         "amazon", "marketplace", "meta"])
    else:
        take = {lane: len(items) for lane, items in buckets.items()}  # no weights → show everything

    # Assemble in display order — the operator's catalog model leads, then Google, then the finders —
    # each trimmed to its allocation. Fresh live finds precede the candidate-queue stamped history.
    live: list[dict] = []
    for lane in ("competitor_catalog", "google_shopping", "amazon", "marketplace", "meta"):
        live.extend((buckets.get(lane) or [])[: take.get(lane, 0)])
    products = live + products

    # Operator-hidden products (the X on a card) — drop across ALL lanes so they stay gone.
    dismissed = dismissed_found_keys()
    if dismissed:
        products = [p for p in products
                    if _found_key(store, keyword, p.get("title")) not in dismissed
                    and _found_key(store, keyword, p.get("url")) not in dismissed]
    products = _scrub_products(products)  # strip control bytes so the JSON never breaks the client
    for _p in products:
        _stamp_rule_verdict(_p)  # ensure EVERY product (live lanes too) carries validated + reason
    found, validated = _lane_counts(products)
    # Funnel-to-decisions: found something but nothing listable → raise ONE deduped decision so the
    # keyword doesn't silently dead-end at "0 valid". Deduped + guarded; never breaks the finder.
    _maybe_funnel_no_valid(store, keyword, products)
    return {
        "store": store,
        "keyword": keyword,
        "found": found,
        "validated": validated,
        "products": products,
        "ai_terms": ai_terms,  # the AI-assist search phrasings the lanes fanned out across
    }


# ---------------------------------------------------------------- amazon best-sellers (spy lane 4)
def amazon_movers() -> dict:
    """Amazon Best Sellers + New Releases feed (06 spy lane 4): rank + review-count moat.

    Amazon retired the standalone "Movers & Shakers" nav; the two live, scrapeable demand
    boards are Best Sellers (https://www.amazon.com/gp/bestsellers/ — highest-ranked =
    proven demand) and New Releases (https://www.amazon.com/gp/new-releases/ — fresh
    fast-rising entrants). The operator browses those via `amazon_browse()` launchers.

    There is no recurring Amazon JSON dump on disk yet — the canonical ingestion path is
    `candidate_queue.py add-signal --lane amazon --pct-gain <f> --reviews <int>`, which
    stamps `lanes.amazon = {pct_gain, reviews, seen}` onto a gated keyword (pct_gain doubles
    as a best-seller rank-climb signal). We surface stamped signals PLUS the LIVE Amazon finds
    the 'Find products' drill-in has cached (cache-only read — no DFS fetch here), so this
    overview and the SKU-plan amazon pool reflect real DataForSEO Amazon data as keywords are
    researched — the same source of truth as found_products_for_head.
    """
    rows: list[dict] = []
    for store in list_stores():
        q = candidate_queue(store) or {}
        raw = q.get("candidates")
        items = list(raw.values()) if isinstance(raw, dict) else (raw or [])
        for c in items:
            if not isinstance(c, dict):
                continue
            kw = (c.get("keyword") or "").strip()
            lane = (c.get("lanes") or {}).get("amazon")
            stamped = _lane_products(lane) if isinstance(lane, dict) else []
            # Fold in LIVE amazon finds cached by the 'Find products' drill-in (cache-only — no
            # DFS fetch, so this overview stays cheap across every candidate). Same source of truth
            # as found_products_for_head, so the movers view + SKU-plan amazon pool stay consistent.
            live = _amazon_lane_products(kw, {kw.lower()}, set(), fetch=False) if kw else []
            keys = {(p.get("url") or p.get("title") or "").lower() for p in stamped}
            products = stamped + [p for p in live
                                  if (p.get("url") or p.get("title") or "").lower() not in keys]
            if not products and not isinstance(lane, dict):
                continue
            found, validated = _lane_counts(products)
            reviews = lane.get("reviews") if isinstance(lane, dict) else None
            if reviews is None and live:
                reviews = max((p.get("reviews") or 0 for p in live), default=0) or None
            bought = max((p.get("bought_past_month") or 0 for p in live), default=0) or None
            first = products[0] if products else {}
            rows.append({
                "store": store,
                "keyword": kw or c.get("keyword"),
                "sv": c.get("sv"),
                "gate": c.get("gate"),
                "score": c.get("score"),
                "pct_gain": lane.get("pct_gain") if isinstance(lane, dict) else None,
                "reviews": reviews,
                "bought_past_month": bought,
                "price": first.get("price"),
                "image": first.get("image"),
                "url": first.get("url"),
                "store_name": first.get("store_name") or "Amazon",
                "seen": lane.get("seen") if isinstance(lane, dict) else None,
                "found": found,
                "validated": validated,
                "products": products,
            })
    # Strongest demand first — stamped %rank-gain, else live bought/mo, else review depth.
    rows.sort(key=lambda r: -((r.get("pct_gain") or 0) * 1_000_000
                              + (r.get("bought_past_month") or 0) * 10
                              + (r.get("reviews") or 0)))
    return {
        "lane": {"id": "amazon", "n": 4, "name": "Amazon best-sellers & new releases",
                 "what": "Best Sellers (rank) + New Releases (fresh entrants) + review-count moat.",
                 "signal": "High best-seller rank = proven demand; a new-release climbing fast = early trend; high review count = depth/moat."},
        "ingest_cmd": "candidate_queue.py add-signal --lane amazon --pct-gain <float> --reviews <int>",
        "totals": {
            "signals": len(rows),
            "keywords": len(rows),
            "found": sum(r.get("found", 0) for r in rows),
            "validated": sum(r.get("validated", 0) for r in rows),
        },
        "movers": rows,
    }


# ---------------------------------------------------------------- meta dropship (spy lane 6)
def meta_dropship() -> dict:
    """Meta dropship-winners feed (06 spy lane 6): ad longevity + duplicate-creative count.

    As with Amazon, there is no recurring Meta JSON dump on disk — `classify_advertiser.py`
    produces one-off DROPSHIP/BRAND/REVIEW triage, and the durable path is
    `candidate_queue.py add-signal --lane meta --ad-longevity-days <int> --dup-creatives
    <int> --price --image --url --store-name --note`, stamping `lanes.meta`. We surface
    what has been stamped so the overview is wired and correctly shaped (empty until run).
    """
    rows: list[dict] = []
    for store in list_stores():
        q = candidate_queue(store) or {}
        raw = q.get("candidates")
        items = list(raw.values()) if isinstance(raw, dict) else (raw or [])
        for c in items:
            if not isinstance(c, dict):
                continue
            lane = (c.get("lanes") or {}).get("meta")
            if not isinstance(lane, dict):
                continue
            products = _lane_products(lane)
            found, validated = _lane_counts(products)
            rows.append({
                "store": store,
                "keyword": c.get("keyword"),
                "sv": c.get("sv"),
                "gate": c.get("gate"),
                "score": c.get("score"),
                "ad_longevity_days": lane.get("ad_longevity_days"),
                "dup_creatives": lane.get("dup_creatives"),
                "price": lane.get("price"),
                "image": lane.get("image"),
                "url": lane.get("url"),
                "store_name": lane.get("store_name"),
                "note": lane.get("note"),
                "seen": lane.get("seen"),
                "found": found,
                "validated": validated,
                "products": products,
            })
    # Longest-running ad first (longevity = proven winner under Meta auction).
    rows.sort(key=lambda r: (r.get("ad_longevity_days") is None, -(r.get("ad_longevity_days") or 0)))
    return {
        "lane": {"id": "meta", "n": 6, "name": "Meta ads",
                 "what": "Ad longevity / duplicate-creative count (supplementary feeder).",
                 "signal": "90+ day ad run = proven; many duplicate creatives = a winner being scaled."},
        "ingest_cmd": ("candidate_queue.py add-signal --lane meta --ad-longevity-days <int> "
                       "--dup-creatives <int> --price <f> --url <u> --store-name <s> --note <n>"),
        "totals": {
            "signals": len(rows),
            "keywords": len(rows),
            "found": sum(r.get("found", 0) for r in rows),
            "validated": sum(r.get("validated", 0) for r in rows),
        },
        "winners": rows,
    }


# ---------------------------------------------------------------- marketplace movers (spy lane 5)
def marketplace_movers() -> dict:
    """Marketplace-movers feed (06 spy lane 5): AliExpress orders / Temu sold-count + COGS.

    Like Amazon/Meta there is no recurring marketplace JSON dump on disk — the durable path
    is `candidate_queue.py add-signal --lane marketplace --orders <int> --sold-count <int>
    --cogs <float> ...`, stamping `lanes.marketplace`. AliExpress + Temu (and 1688 as a
    domestic COGS floor) are RESEARCH/discovery sources here, never the supplier (the
    private sourcing agent is). We surface stamped signals PLUS the LIVE AliExpress finds the
    'Find products' drill-in has cached (CACHE-ONLY here — no BD fetch across every candidate), so
    this overview and the SKU-plan marketplace pool reflect real AliExpress data (sold count + COGS
    basis) — the same source of truth as found_products_for_head, as keywords get researched.
    """
    rows: list[dict] = []
    for store in list_stores():
        q = candidate_queue(store) or {}
        raw = q.get("candidates")
        items = list(raw.values()) if isinstance(raw, dict) else (raw or [])
        for c in items:
            if not isinstance(c, dict):
                continue
            kw = (c.get("keyword") or "").strip()
            lane = (c.get("lanes") or {}).get("marketplace")
            ld = lane if isinstance(lane, dict) else {}
            stamped = _lane_products(lane) if isinstance(lane, dict) else []
            # Fold in the LIVE AliExpress finds cached by the 'Find products' drill-in (CACHE-ONLY —
            # no BD Web-Unlocker fetch here, so the overview stays cheap across every candidate). Same
            # source of truth as found_products_for_head, so this overview + the SKU-plan marketplace
            # pool stay consistent with the drill-in (sold count + COGS basis, AliExpress).
            live = _marketplace_lane_products(store, kw, {kw.lower()}, set(), fetch=False) if kw else []
            keys = {(p.get("url") or p.get("title") or "").lower() for p in stamped}
            products = stamped + [p for p in live
                                  if (p.get("url") or p.get("title") or "").lower() not in keys]
            if not products and not ld:
                continue
            found, validated = _lane_counts(products)
            first = products[0] if products else {}
            rows.append({
                "store": store,
                "keyword": kw or c.get("keyword"),
                "sv": c.get("sv"),
                "gate": c.get("gate"),
                "score": c.get("score"),
                "orders": ld.get("orders"),
                "sold_count": ld.get("sold_count"),
                "cogs": ld.get("cogs"),
                "price": ld.get("price") if ld.get("price") is not None else first.get("price"),
                "image": ld.get("image") or first.get("image"),
                "url": ld.get("url") or first.get("url"),
                "store_name": ld.get("store_name") or first.get("store_name"),
                "note": ld.get("note"),
                "seen": ld.get("seen"),
                "found": found,
                "validated": validated,
                "products": products,
            })
    # Stamped orders/sold-count first (real demand proof), then live marketplace finds by count.
    rows.sort(key=lambda r: (
        (r.get("orders") or r.get("sold_count")) is None,
        -((r.get("orders") or 0) + (r.get("sold_count") or 0)),
        -(r.get("found") or 0),
    ))
    return {
        "lane": {"id": "marketplace", "n": 5, "name": "Marketplace movers",
                 "what": "AliExpress orders / Temu sold-count + COGS basis (1688 = domestic floor).",
                 "signal": "High orders/sold-count = proven demand; struck-through compare-at = COGS basis."},
        "ingest_cmd": ("candidate_queue.py add-signal --lane marketplace --orders <int> "
                       "--sold-count <int> --cogs <f> --url <u> --store-name <s> --note <n>"),
        "sources": ["AliExpress", "Temu", "1688"],
        "totals": {
            "signals": len(rows),
            "keywords": len(rows),
            "found": sum(r.get("found", 0) for r in rows),
            "validated": sum(r.get("validated", 0) for r in rows),
        },
        "movers": rows,
    }


# Browse the marketplaces' OWN native best-seller views — a launcher into the real pages
# the operator researches in (proven-demand discovery), NOT the stamped lane above. Each
# platform exposes its demand ranking differently:
#   Temu       — a dedicated Best-Sellers channel with native 7 / 14 / 30-day top-selling toggles.
#   AliExpress — no day-window; results sorted by total orders (SortType=total_tranpro_desc).
#   1688       — ranked by transactions/成交 (sortType=booked); domestic COGS floor, research only.
# Categories map to a search term (robust — avoids guessing each platform's internal category
# IDs); "All" opens the platform's bestseller landing where it has one. URL templates live here
# (single source of truth) so they are trivially fixable; the client composes the final URL.
_MARKETPLACE_BROWSE_CATEGORIES = [
    {"label": "All", "term": ""},
    {"label": "Pet", "term": "pet supplies"},
    {"label": "Kitchen", "term": "kitchen gadgets"},
    {"label": "Home", "term": "home decor"},
    {"label": "Beauty", "term": "beauty tools"},
    {"label": "Outdoor", "term": "outdoor gear"},
    {"label": "Tech / gadgets", "term": "gadgets"},
    {"label": "Baby", "term": "baby products"},
    {"label": "Car", "term": "car accessories"},
    {"label": "Fitness", "term": "fitness equipment"},
    {"label": "Tools", "term": "tools"},
    {"label": "Office", "term": "office supplies"},
]

_MARKETPLACE_BROWSE_PLATFORMS = [
    {
        "id": "temu",
        "name": "Temu",
        "color": "var(--state-winner)",
        "supports_all": True,
        # Temu's 7/14/30-day is a native on-page toggle on the Best-Sellers channel — it is JS
        # state, NOT a URL param we can set, so the page always OPENS on Temu's default 30-day
        # window. We default the selector to 7-day (fastest movers) and tell the operator to tap
        # the matching day-tab once the page loads. Do NOT fake a query string (opt_level etc. is
        # ignored — verified: the link still lands on 30-day).
        "timeframes": [
            {"label": "7-day", "value": "7"},
            {"label": "14-day", "value": "14"},
            {"label": "30-day", "value": "30"},
        ],
        "default_timeframe": "7",
        "timeframe_hint": (
            "Temu opens on its 30-day default — tap the highlighted “Within last {label}” tab on "
            "the page to switch to your window (fastest movers = 7-day)."
        ),
        "all_url": "https://www.temu.com/channel/best-sellers.html",
        "search_tpl": "https://www.temu.com/search_result.html?search_key={kw}",
        "sort_note": "Best-Sellers channel · 7-day window = fastest movers (tap the day-tab on the page)",
    },
    {
        "id": "aliexpress",
        "name": "AliExpress",
        "color": "var(--state-testing)",
        "supports_all": False,  # no stable global bestseller landing — drive by category/keyword
        "timeframes": [],
        "all_url": None,
        "search_tpl": "https://www.aliexpress.com/w/wholesale-{kw}.html?SortType=total_tranpro_desc",
        "sort_note": "Sorted by total orders (SortType=total_tranpro_desc) = proven demand",
    },
    {
        "id": "1688",
        "name": "1688",
        "color": "var(--state-testing)",
        "supports_all": False,
        "timeframes": [],
        "all_url": None,
        "search_tpl": "https://s.1688.com/selloffer/offer_search.htm?keywords={kw}&sortType=booked",
        "sort_note": "Ranked by transactions/成交 (sortType=booked) · product-finding (research), not the supplier",
    },
]


def marketplace_browse() -> dict:
    """Static launcher spec for the marketplaces' native best-seller views (Temu / AliExpress / 1688)."""
    return {
        "platforms": _MARKETPLACE_BROWSE_PLATFORMS,
        "categories": _MARKETPLACE_BROWSE_CATEGORIES,
        "note": (
            "Jump into each marketplace's OWN best-seller ranking to research proven demand — by "
            "category, or type a KEYWORD to search that term sorted by demand. "
            "Temu has native 7 / 14 / 30-day toggles (default the window to 7-day = fastest movers); "
            "AliExpress sorts by orders; 1688 by transactions. "
            "These are RESEARCH sources (demand + a COGS basis), never the supplier."
        ),
    }


# Amazon retired the standalone "Movers & Shakers" nav; the two live demand boards are
# Best Sellers (top-100 per department, proven established demand) and New Releases (fresh
# fast-rising entrants). Both are browsed by DEPARTMENT (Amazon's native model — a department
# slug appended to the base path), not by free-text. A keyword fallback sorts a search by
# popularity. These are RESEARCH sources only (proven demand + a retail price benchmark) —
# the private sourcing agent fulfils chosen SKUs. URL templates live here (single source of
# truth); the client composes the final URL from base_url + slug (or the search template).
_AMAZON_BROWSE_VIEWS = [
    {
        "id": "bestsellers",
        "name": "Best Sellers",
        "color": "var(--state-live)",
        "base_url": "https://www.amazon.com/gp/bestsellers/",
        "what": "Top-100 per department — highest-ranked = proven, established demand.",
    },
    {
        "id": "new-releases",
        "name": "New Releases",
        "color": "var(--state-testing)",
        "base_url": "https://www.amazon.com/gp/new-releases/",
        "what": "Newest fast-rising products — early / trending demand before it saturates.",
    },
]

# Amazon department slugs are stable (same slug works for /bestsellers/ and /new-releases/).
_AMAZON_BROWSE_CATEGORIES = [
    {"label": "All", "slug": ""},
    {"label": "Pet", "slug": "pet-supplies"},
    {"label": "Home & Kitchen", "slug": "kitchen"},
    {"label": "Health & Household", "slug": "hpc"},
    {"label": "Beauty", "slug": "beauty"},
    {"label": "Sports & Outdoors", "slug": "sporting-goods"},
    {"label": "Tech / Electronics", "slug": "electronics"},
    {"label": "Tools & Home Improvement", "slug": "hi"},
    {"label": "Patio, Lawn & Garden", "slug": "lawn-garden"},
    {"label": "Baby", "slug": "baby-products"},
    {"label": "Automotive", "slug": "automotive"},
    {"label": "Office", "slug": "office-products"},
    {"label": "Toys & Games", "slug": "toys-and-games"},
]


def amazon_browse() -> dict:
    """Static launcher spec for Amazon's native demand boards (Best Sellers + New Releases)."""
    return {
        "views": _AMAZON_BROWSE_VIEWS,
        "categories": _AMAZON_BROWSE_CATEGORIES,
        # Free-text fallback: sort a keyword search by popularity (≈ best-seller signal).
        "search_tpl": "https://www.amazon.com/s?k={kw}&s=exact-aware-popularity-rank",
        "note": (
            "Amazon retired the 'Movers & Shakers' page — the two live demand boards are Best "
            "Sellers (proven, established demand) and New Releases (fresh fast-rising entrants). "
            "Browse by department, or search a keyword sorted by popularity. RESEARCH sources "
            "(proven demand + a retail price benchmark), never the supplier."
        ),
    }


# ---------------------------------------------------------------- keyword/trend → product
# The cross-feed: a validated KEYWORD / TREND / EVENT is a demand signal that still needs a
# PRODUCT found for it. Every research path (keyword, trend, world-event) funnels into this ONE
# handoff, with the project-wide Google-LED source order (matches sku_plan source_weights, where
# google=55 is primary):
#   • Google competitor (PRIMARY)    — the spy-roster catalog scan + direct non-branded Google
#                                       Shopping. This is where products come from + the retail
#                                       price basis.
#   • marketplace search (SECONDARY) — Temu / AliExpress / 1688, most-sold = proven; the COGS
#                                       basis + supplementary finds. NOT the price anchor unless
#                                       the product was found directly on the marketplace.
# A catalog dedup check runs BETWEEN finding products (don't re-surface what we already sell /
# already found). Final identity validation is a Gemini-vision check phase (read the real
# product across backgrounds / angles) — NOT wired yet, surfaced here as a pending step.
# Volume tiers (project-wide, operator 2026-07-04) — the winner-probability bands. Kept inline
# here (sku_plan.py imports readers, so readers can't import sku_plan back). Same thresholds.
_VOLUME_TIERS = {"prime": 200_000, "strong": 100_000, "solid": 30_000, "entry": 10_000}
_VOLUME_TIER_LABELS = {
    "prime": "Prime (≥200k)", "strong": "Strong (100–200k)", "solid": "Solid (30–100k)",
    "entry": "Entry (10–30k)", "below": "Below floor (<10k)",
}


def _volume_tier_name(sv) -> dict:
    """Map an SV to {name,label} — 'below' means under the 10k gate."""
    name = "below"
    if sv is not None:
        v = float(sv)
        if v >= _VOLUME_TIERS["prime"]:
            name = "prime"
        elif v >= _VOLUME_TIERS["strong"]:
            name = "strong"
        elif v >= _VOLUME_TIERS["solid"]:
            name = "solid"
        elif v >= _VOLUME_TIERS["entry"]:
            name = "entry"
    return {"name": name, "label": _VOLUME_TIER_LABELS[name]}


def find_products_for_keyword(keyword: str, sv: int | None = None,
                              source: str = "keyword") -> dict:
    kw = (keyword or "").strip()
    enc = urllib.parse.quote_plus(kw)

    marketplace: list[dict] = []
    for p in _MARKETPLACE_BROWSE_PLATFORMS:
        tpl = p.get("search_tpl")
        marketplace.append({
            "id": p["id"],
            "name": p["name"],
            "color": p["color"],
            "url": tpl.format(kw=enc) if tpl else None,
            "sort_note": p.get("sort_note"),
            "timeframes": p.get("timeframes") or [],
        })

    domains = _spy_domains()
    competitor_stores = [
        {"domain": d, "products_url": f"https://{d}/products.json?limit=250"}
        for d in domains
    ]

    # The seed keyword is a STARTING POINT, not a finished list. Whether it arrived from a trend,
    # an event (Oktoberfest, Ferragosto), or a keyword row, we still fan it out through keyword +
    # trend research BEFORE finding products: expand it into the related/higher-volume terms real
    # shoppers type, pull live SV, and volume-tier them. The 10k floor is the GATE; the FOCUS is
    # the biggest terms — a prime (≥200k) term earns the most listings (market share, high winner
    # odds), mid terms fewer. So an event doesn't only list its seed keywords — it drives research.
    seed_tier = _volume_tier_name(sv)
    keyword_research = {
        "what": (
            "Seed keyword → keyword + trend research first (this is a starting point, not the final "
            "list). Expand into the related/higher-volume terms shoppers actually search, pull live "
            "SV, and rank by volume tier — THEN find products, biasing to the biggest terms."
        ),
        "seed": kw,
        "seed_sv": sv,
        "seed_tier": seed_tier,
        "floor_vs_focus": (
            "10k SV is the hard floor (gate). The focus is the highest volume: prefer 100–300k+ "
            "(≥200k = prime) to take market share and list MORE products; still take 30–90k as "
            "supplementary, but bias the batch toward the biggest keywords."
        ),
        "expand_cmd": (
            "01-niche-discovery/scripts/keyword_data.py --seed '" + kw + "' --expand   "
            "# related + broad-match, live DataForSEO SV\n"
            "01-niche-discovery/scripts/trends_dfs.py --keywords <expanded>   "
            "# momentum + rising-related (the timing/feeder signal)\n"
            "06-launch-general-store/scripts/season_classify.py --keywords keyword-data.json "
            "--trends trends-dfs.json   # gate at 10k + volume_tier each term"
        ),
        "then": "Fire find-products on the tier-ranked expanded set (prime/strong first), not just the seed.",
    }

    # REAL product research: actually FIND products (AliExpress + Temu + 1688 + Amazon) for the
    # trend/keyword. INSTANT — returns the cache immediately + warms a cold keyword in the background
    # (the client re-polls while `warming`). Same finder as the SKU-plan drill-in.
    products, warming = find_keyword_products_instant(kw)
    return {
        "keyword": kw,
        "sv": sv,
        "source": source,  # "keyword" | "trend" | "event" — where the seed came from
        "products": products,
        "found": len(products),
        "warming": warming,  # true = cold keyword fetching in the background; client re-polls
        "weighting": {"google_competitor": "product finding", "marketplace": "product finding"},
        "keyword_research": keyword_research,
        "competitor_scan": {
            "weight": "primary",
            "what": (
                "The primary, Google-led discovery lane. Scan the competitors we already track "
                "(spy roster) for this keyword — deeper than the top-30 best-sellers — plus direct "
                "non-branded Google Shopping. This is where products come from AND the retail price basis."
            ),
            "depth": "beyond top-30",
            "n_stores": len(competitor_stores),
            "stores": competitor_stores,
            "scan_cmd": (
                "06-launch-general-store/scripts/bestseller_snapshot.py "
                f"--store <domain> --limit 250  # then grep the snapshot for '{kw}'"
            ),
            "google_shopping": {
                "label": "Direct Google Shopping (non-branded)",
                "what": "Live PLA + category-row results for the keyword — non-branded dropshippable products only.",
                "url": "https://www.google.com/search?tbm=shop&q=" + enc,
            },
        },
        "marketplace": {
            "weight": "product finding",
            "what": (
                "PRODUCT FINDING — search AliExpress + Temu + 1688 for this trend/keyword to find "
                "products to list, ranked by demand (sold count / orders) where the source has it. "
                "First-class discovery sources (what to list), never the supplier."
            ),
            "platforms": marketplace,
        },
        "dedup": {
            "what": "Run the catalog check between finding products so we don't re-source what the store already sells or what was already found.",
            "scan_cmd": (
                "05-launch-niche-store/china-source-match/scripts/catalog_scan.py "
                "index --store <store>  →  check --in <found>.json --judge openrouter"
            ),
        },
        "vision_check": {
            "status": "pending",
            "what": "Gemini-vision identity validation — confirm a found product is REALLY the same item across different backgrounds / angles before committing.",
            "note": "Check phase not wired yet — placeholder seam; runs after find + dedup.",
        },
        "pricing": pricing.rules(),
    }


# ---------------------------------------------------------------- niche launches
def list_niche_launches() -> list[dict]:
    base = config.niche_launches_dir()
    if not base.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(base.iterdir()):
        if not p.is_dir():
            continue
        out.append({
            "slug": p.name,
            "is_internal": p.name.startswith("_"),
        })
    return out


# ---------------------------------------------------------------- global overview
def overview() -> dict:
    """Top-level numbers for the dashboard landing page."""
    stores = list_stores()
    store_summaries = [store_summary(s) for s in stores]
    totals = {s: 0 for s in SKU_STATES}
    skus_total = 0
    cats_total = 0
    for ss in store_summaries:
        cats_total += ss["categories"]
        skus_total += ss["skus_total"]
        for st, n in ss["skus_by_state"].items():
            totals[st] += n
    dossiers = list_dossiers()
    launches = list_niche_launches()
    # Research-discovery rollups — what the funnel has FOUND (keywords + trends), so the
    # landing page leads with discovery signal rather than empty niche-legacy counters.
    kw = keyword_discovery().get("totals", {})
    tr = trends_overview().get("totals", {})
    return {
        "stores": store_summaries,
        "totals": {
            "stores": len(stores),
            "categories": cats_total,
            "skus": skus_total,
            "skus_by_state": totals,
            "dossiers": sum(1 for d in dossiers if not d["is_pool"]),
            "niche_launches": sum(1 for l in launches if not l["is_internal"]),
            "found_keywords": kw.get("candidates", 0),
            "keywords_gated_pass": kw.get("gated_pass", 0),
            "keyword_segments": kw.get("segments", 0),
            "found_trends": tr.get("trends", 0),
            "trends_rising": tr.get("rising", 0),
            "trends_breakout": tr.get("breakout", 0),
        },
    }
