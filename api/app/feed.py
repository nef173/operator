"""Product Feed & Optimization — feed-readiness derivation.

This module computes a Google-Merchant (GMC) feed-readiness report for a store's catalog
from the SAME on-disk artifacts the listing pipeline already writes — `listing-queue.json`
plus each category's `_expansion-spec.json` (variants / seo / body_html) and the generated
gallery on disk. It introduces NO new disk format and reimplements none of the pipeline.

Honesty posture (binding project rule): every check here is deterministic and explained;
nothing is fabricated. The LIVE GMC half (account status / disapprovals / impressions)
requires the Google Merchant Content API, which is NOT wired in this app — so it is surfaced
to the UI as an explicit "not connected" block, never as stub numbers.

The GMC title rules encoded below come from the project's binding docs:
  - title head keyword in the FIRST words, brand NEVER in indexed product text (V2/V10);
  - google_product_category set from the Standard Product Taxonomy (category_gid);
  - Shopify silently caps seo.title ~95 chars (assert ≤95);
  - main image carries no text/price/logo overlay (a manual VisionScan gate — flagged as an
    advisory the operator must eyeball, since pixels aren't readable here).
"""
from __future__ import annotations

import json
import re

from . import readers


def _category_gid(store: str, slug: str) -> str | None:
    """Read the category's `category_gid` from its expansion/draft spec (the value that
    drives google_product_category). Traversal-safe via readers' file resolver."""
    for name in ("_expansion-spec.json", "_draft-spec.json"):
        target = readers.resolve_store_file(store, f"{slug}/{name}")
        if target is None:
            continue
        try:
            spec = json.loads(target.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(spec, dict) and spec.get("category_gid"):
            return spec.get("category_gid")
    return None

# GMC / Shopify hard limits used by the checks.
TITLE_MAX = 150          # GMC product title max length
SEO_TITLE_CAP = 95       # Shopify silently caps seo.title past ~95 chars (no error)
KEYWORD_HEAD_WINDOW = 70  # head keyword must appear within the first N chars of the title
BODY_MIN_CHARS = 120     # a description thinner than this reads stub-y

# Known store/brand tokens that must NOT appear in indexed product titles (V2/V10 rule:
# brand lives in logo/footer/vendor/order-emails only, never in the searched title text).
_BRAND_TOKENS: set[str] = set()  # add YOUR brand names here so the feed strips them from indexed titles

_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm(s: str | None) -> str:
    return (s or "").strip()


def _words(s: str) -> list[str]:
    return _WORD_RE.findall(s.lower())


def _suggest_title(title: str, keyword: str | None) -> str | None:
    """Propose a GMC-optimized title: head keyword first, brand tokens stripped, ≤TITLE_MAX.

    Read-only suggestion the operator can copy — never written back (draft-first, no
    auto-publish). Returns None when no improvement is needed/possible.
    """
    title = _norm(title)
    kw = _norm(keyword)
    if not title:
        return None

    # Strip any brand token (whole word, case-insensitive).
    stripped = title
    for tok in _BRAND_TOKENS:
        stripped = re.sub(rf"\b{re.escape(tok)}\b", "", stripped, flags=re.IGNORECASE)
    # Collapse whitespace + dangling separators left by the strip.
    stripped = re.sub(r"\s{2,}", " ", stripped).strip(" -—–|,").strip()

    out = stripped or title
    # Front-load the head keyword if it isn't already near the front.
    if kw:
        head = out[:KEYWORD_HEAD_WINDOW].lower()
        if kw.lower() not in head:
            # Avoid stuttering if the keyword already appears later in the title.
            without_kw = re.sub(rf"\b{re.escape(kw)}\b", "", out, flags=re.IGNORECASE)
            without_kw = re.sub(r"\s{2,}", " ", without_kw).strip(" -—–|,").strip()
            # Title-case the keyword for a natural lead.
            lead = " ".join(w.capitalize() for w in kw.split())
            out = f"{lead} — {without_kw}".strip(" -—–|") if without_kw else lead

    if len(out) > TITLE_MAX:
        out = out[:TITLE_MAX].rstrip()
    out = out.strip()
    return out if out and out != title else None


def _sku_price(spec: dict) -> float | None:
    """Best available price for a SKU: the queue price if set, else the first variant price."""
    p = spec.get("price")
    if isinstance(p, (int, float)) and p > 0:
        return float(p)
    for v in spec.get("variants") or []:
        if isinstance(v, dict):
            try:
                vp = float(v.get("price"))
            except (TypeError, ValueError):
                continue
            if vp > 0:
                return vp
    return None


def _sku_checks(sku: dict, keyword: str | None, has_category_gid: bool) -> dict:
    """Run the deterministic feed checks for one SKU. Returns a UI-ready row."""
    title = _norm(sku.get("title"))
    seo_title = _norm(sku.get("seo_title"))
    body = _norm(sku.get("body_html"))
    price = sku.get("price")
    n_images = int(sku.get("n_images") or 0)

    issues: list[dict] = []

    def fail(check: str, rule: str, detail: str, severity: str = "error") -> None:
        issues.append({"check": check, "rule": rule, "detail": detail, "severity": severity})

    # Title present + length.
    if not title:
        fail("title", "Title must be present", "No title set on this SKU.")
    else:
        if len(title) > TITLE_MAX:
            fail("title_length", f"Title ≤ {TITLE_MAX} chars (GMC max)",
                 f"Title is {len(title)} chars.")
        # Keyword-first.
        if keyword:
            head = title[:KEYWORD_HEAD_WINDOW].lower()
            if keyword.lower() not in head:
                fail("keyword_first",
                     f"Head keyword in first {KEYWORD_HEAD_WINDOW} chars of title",
                     f"'{keyword}' not found near the start of the title.")
        # Brand-in-title.
        brand_hit = next((t for t in _BRAND_TOKENS if t in _words(title)), None)
        if brand_hit:
            fail("brand_in_title", "No brand token in the indexed title (V2/V10)",
                 f"Title contains brand token '{brand_hit}'.")

    # google_product_category (taxonomy node) — set at the category level.
    if not has_category_gid:
        fail("google_product_category",
             "Category mapped to a Standard Product Taxonomy node",
             "Category has no category_gid; GMC will auto-guess google_product_category.")

    # Price.
    if not isinstance(price, (int, float)) or float(price or 0) <= 0:
        fail("price", "Price must be > 0", "No usable price set.")

    # SEO title cap.
    if seo_title and len(seo_title) > SEO_TITLE_CAP:
        fail("seo_title_cap", f"SEO title ≤ {SEO_TITLE_CAP} chars (Shopify silent cap)",
             f"SEO title is {len(seo_title)} chars; Shopify drops it past ~{SEO_TITLE_CAP}.",
             severity="warn")

    # Description.
    if not body:
        fail("description", "Description present", "No body_html on this SKU.")
    elif len(body) < BODY_MIN_CHARS:
        fail("description", f"Description ≥ ~{BODY_MIN_CHARS} chars",
             f"Description is only {len(body)} chars (reads thin).", severity="warn")

    # Image present.
    if n_images < 1:
        fail("image", "At least one product image", "No generated gallery image on disk.")

    errors = [i for i in issues if i["severity"] == "error"]
    status = "pass" if not errors else "needs-work"
    suggestion = None
    if any(i["check"] in {"keyword_first", "brand_in_title", "title_length"} for i in issues):
        suggestion = _suggest_title(title, keyword)

    return {
        "id": sku.get("id"),
        "title": title or None,
        "state": sku.get("state"),
        "price": price,
        "n_images": n_images,
        "status": status,
        "n_issues": len(issues),
        "issues": issues,
        "suggested_title": suggestion,
    }


def feed_report(store: str) -> dict | None:
    """Per-SKU feed-readiness for a store, derived from real pipeline artifacts."""
    if store not in readers.list_stores():
        return None

    categories = readers.store_categories(store)
    cat_rows: list[dict] = []
    totals = {"skus": 0, "pass": 0, "needs_work": 0}
    issue_tally: dict[str, dict] = {}

    for cat in categories:
        slug = cat.get("slug")
        keyword = cat.get("keyword")
        detail = readers.category_detail(store, slug) or {}
        gid = _category_gid(store, slug)
        has_gid = bool(gid)
        # The category drill-down is the spec+disk-grounded source of truth (its SKUs carry
        # title/seo_title/body_html/variants/images). The flat listing-queue SKUs use a
        # different id scheme, so we drive readiness off category_detail directly.
        detail_skus = detail.get("skus") or []

        cat_pass = 0
        cat_needs = 0
        cat_sku_rows: list[dict] = []
        for spec in detail_skus:
            merged = {
                "id": spec.get("id") or spec.get("slug"),
                "title": spec.get("title"),
                "state": spec.get("state"),
                "price": _sku_price(spec),
                "seo_title": spec.get("seo_title"),
                "body_html": spec.get("body_html"),
                "n_images": spec.get("n_images"),
            }
            row = _sku_checks(merged, keyword, has_gid)
            row["category"] = slug
            row["keyword"] = keyword
            cat_sku_rows.append(row)
            totals["skus"] += 1
            if row["status"] == "pass":
                totals["pass"] += 1
                cat_pass += 1
            else:
                totals["needs_work"] += 1
                cat_needs += 1
            for iss in row["issues"]:
                t = issue_tally.setdefault(
                    iss["check"], {"check": iss["check"], "rule": iss["rule"], "count": 0,
                                   "severity": iss["severity"]}
                )
                t["count"] += 1

        cat_rows.append({
            "slug": slug,
            "keyword": keyword,
            "sv": cat.get("sv"),
            "category_fullname": detail.get("category_fullname"),
            "has_category_gid": has_gid,
            "n_skus": len(cat_sku_rows),
            "n_pass": cat_pass,
            "n_needs_work": cat_needs,
            "skus": cat_sku_rows,
        })

    top_issues = sorted(issue_tally.values(), key=lambda x: -x["count"])
    ready_pct = round(100 * totals["pass"] / totals["skus"]) if totals["skus"] else 0

    return {
        "store": store,
        "checks": _CHECK_CATALOG,
        "limits": {
            "title_max": TITLE_MAX,
            "seo_title_cap": SEO_TITLE_CAP,
            "keyword_head_window": KEYWORD_HEAD_WINDOW,
            "body_min_chars": BODY_MIN_CHARS,
        },
        "totals": {**totals, "categories": len(cat_rows), "ready_pct": ready_pct},
        "top_issues": top_issues,
        "categories": cat_rows,
        # The live GMC half is NOT wired — surfaced honestly, never stubbed with fake numbers.
        "gmc": {
            "connected": False,
            "what": "Live GMC account status, product disapprovals, and impressions.",
            "blocker": "Requires the Google Merchant Content API, which is not connected yet.",
            "next_step": "Connect the GMC Content API (the one hard external dependency, "
                         "currently deferred with the Google Ads write path).",
        },
        "note": "Feed-readiness is computed from the on-disk listing pipeline "
                "(listing-queue + per-category specs + generated galleries). The main-image "
                "no-text/no-price overlay rule is a manual VisionScan gate — eyeball each hero.",
    }


# Catalogue of the checks the report runs, for the UI to render a legend.
_CHECK_CATALOG = [
    {"check": "title", "rule": "Title present", "what": "Every SKU needs a title for the feed."},
    {"check": "title_length", "rule": f"Title ≤ {TITLE_MAX} chars",
     "what": "GMC truncates titles past its max length."},
    {"check": "keyword_first", "rule": f"Head keyword in first {KEYWORD_HEAD_WINDOW} chars",
     "what": "GMC ranks on the leading words — lead with the searched keyword."},
    {"check": "brand_in_title", "rule": "No brand token in the indexed title",
     "what": "Brand lives in logo/footer/vendor only (V2/V10); never the searched title."},
    {"check": "google_product_category", "rule": "Mapped to a taxonomy node",
     "what": "A set category_gid drives google_product_category; blank → GMC mis-guesses."},
    {"check": "price", "rule": "Price > 0", "what": "Feed items need a usable price."},
    {"check": "seo_title_cap", "rule": f"SEO title ≤ {SEO_TITLE_CAP} chars",
     "what": "Shopify silently drops seo.title past ~95 chars."},
    {"check": "description", "rule": "Description present",
     "what": "GMC wants a real description, not a stub."},
    {"check": "image", "rule": "≥ 1 product image",
     "what": "The hero is the Google Shopping feed image."},
]
