"""Server-side general-store DISCOVERY — the always-on path for background automation.

The premium discovery source (TrendTrack `search_shops`) is MCP-only: only an agent with the
MCP can call it, never a background worker on a cloud host. So this is the PRODUCTION discovery
path that runs on ANY host (the Railway worker included) with just the DataForSEO creds already
configured in Connections:

    seed keywords  ->  DFS Google-Shopping recon (the real geolocated US grid)  ->  harvest the
    seller domains advertising there  ->  classify_store GENERAL gate (live-Shopify reachability
    + catalog breadth/balance)  ->  add the passing general stores to the Best-Seller Spy roster.

No browser, no MCP, no image-gen, no keys beyond DataForSEO. It mirrors `shopping_scan_dfs`'s
"always-on" design so the *Discover general stores* job COMPLETES with real results inside the
app instead of handing the operator a copy-paste command. When TrendTrack IS reachable (an agent
session), that richer source still wins via the job's manual command; this is the worker's path.

Stdlib only (urllib for DFS via the reused scan module; subprocess for the harvest+classify gate,
which runs in the general-store venv where `requests` lives).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile

# Reuse the DFS plumbing verbatim (same package dir) — creds loader + Merchant grid fetch.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shopping_scan_dfs as scan  # noqa: E402

# Broad seeds that surface a wide spread of US general-store advertisers on the Shopping grid.
# Tunable with --keywords; these are deliberately generic so the catalog-breadth gate (not the
# seed) decides what counts as a general store.
_DEFAULT_SEEDS = [
    "kitchen gadgets",
    "home decor",
    "pet supplies",
    "car accessories",
    "garden tools",
    "office desk accessories",
]

# Fashion-mode seeds — used when the store this discovery runs FOR is a fashion store
# (STORE_MODE=fashion in the env, set per store by the operator app's job runner). Same
# breadth idea, but the spread covers the apparel verticals so the harvest surfaces
# fashion advertisers instead of general-store ones.
_FASHION_SEEDS = [
    "women dresses",
    "men jackets",
    "sneakers women",
    "handbags",
    "jewelry for women",
    "swimwear",
]

# Marketplaces / retail chains that show up on every Shopping grid but are never dropshippable
# general-store candidates — drop before the gate to save classify calls.
_SKIP_DOMAINS = {
    "amazon.com", "walmart.com", "target.com", "ebay.com", "etsy.com", "wayfair.com",
    "homedepot.com", "lowes.com", "bestbuy.com", "costco.com", "aliexpress.com", "temu.com",
    "macys.com", "kohls.com", "overstock.com", "google.com", "shop.app", "tiktok.com",
}


def _norm(domain: str | None) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).rstrip("/")
    d = d.split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def harvest_candidates(seeds: list[str], geo: str, auth: str, per_seed_cap: int = 40):
    """Run a DFS Merchant recon per seed keyword and collect candidate stores. The Merchant grid
    gives a `seller` NAME (and occasionally a `domain`); we return both. Name → domain resolution
    is delegated to harvest_general_stores.py (its `--from-scan` path), so the guessing logic lives
    in one place and the classifier's live-Shopify check is the safety net for wrong guesses."""
    location = scan._GEO_LOCATION.get((geo or "US").upper(), "United States")
    sellers: dict[str, int] = {}   # seller name -> times seen across seeds
    domains: dict[str, int] = {}   # explicit domain -> times seen
    errors: list[str] = []
    for kw in seeds:
        kw = kw.strip()
        if not kw:
            continue
        try:
            merch = scan.fetch_merchant(kw, location, auth)
        except Exception as e:  # noqa: BLE001 — one bad seed must not sink the whole run
            errors.append(f"{kw}: {e}")
            continue
        extracted = scan.extract_merchant(merch.get("items") or [])
        for p in extracted["products"][:per_seed_cap]:
            d = _norm(p.get("domain"))
            if d and "." in d and d not in _SKIP_DOMAINS:
                domains[d] = domains.get(d, 0) + 1
            s = (p.get("seller") or "").strip()
            if s:
                sellers[s] = sellers.get(s, 0) + 1
    # Seen across MORE seeds first — a store appearing under several categories is a stronger
    # general-store signal than a one-category seller.
    ordered_sellers = [s for s, _ in sorted(sellers.items(), key=lambda kv: (-kv[1], kv[0]))]
    ordered_domains = [d for d, _ in sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))]
    return ordered_sellers, ordered_domains, errors


def trendtrack_domains(limit: int = 40) -> list[str]:
    """The PREMIUM domain source: when a TrendTrack API token is present (injected into the job's
    env via connections.as_env), pull scaling US-market Shopify stores from api.trendtrack.io and
    return their domains, merged into the DFS harvest so the discovery job uses TrendTrack directly.
    Best-effort + graceful: any error / no token → [] so the DFS grid still carries the job. The
    response field names are probed defensively; tighten once verified against a live key."""
    import urllib.error
    import urllib.request

    tok = (os.environ.get("TRENDTRACK_API_TOKEN") or "").strip()
    if not tok:
        return []
    # Verified against the live /v1/shops/query schema: mainMarketCountries (audience market) +
    # dtcRegion=us (US DTC preset) + sortBy=growth30d = fast-growing US-market Shopify stores.
    # Response rows are in data[]; each row's `domain` is the storefront domain (1 credit/shop).
    body = json.dumps({"mainMarketCountries": ["US"], "dtcRegion": "us",
                       "sortBy": "growth30d", "order": "desc",
                       "minMonthlyVisits": 5000, "limit": limit}).encode()
    req = urllib.request.Request("https://api.trendtrack.io/v1/shops/query", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return []
    out: list[str] = []
    for it in (data.get("data") or []):
        if not isinstance(it, dict):
            continue
        dom = str(it.get("domain") or "").replace("https://", "").replace("http://", "").strip("/ ").split("/")[0].lower()
        if dom and "." in dom:
            out.append(_norm(dom) or dom)
    return list(dict.fromkeys(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="Server-side general-store discovery (DFS → gate → add).")
    ap.add_argument("--keywords", "--seeds", dest="keywords", default="",
                    help="comma-separated seed keywords (default: a broad general-store spread)")
    ap.add_argument("--geo", default="US")
    ap.add_argument("--store", default="")
    ap.add_argument("--harvest", required=True, help="path to harvest_general_stores.py")
    ap.add_argument("--harvest-python", required=True, help="python that has requests (general venv)")
    ap.add_argument("--max-domains", type=int, default=40, help="cap candidates sent to the gate")
    ap.add_argument("--per-seed-cap", type=int, default=40)
    ap.add_argument("--min-depts", type=int, default=5)
    ap.add_argument("--max-dominance", type=float, default=0.45)
    ap.add_argument("--dry-run", action="store_true", help="classify only; do NOT add to the roster")
    args = ap.parse_args()

    # Explicit --keywords win; otherwise the seed spread follows the store's catalog path.
    #   fashion → apparel seeds only; both → general + fashion (union, order-preserving dedup);
    #   general (default) → the broad general-store seeds.
    store_mode = (os.environ.get("STORE_MODE") or "general").strip().lower()
    if store_mode == "fashion":
        default_seeds = _FASHION_SEEDS
    elif store_mode == "both":
        default_seeds = list(dict.fromkeys([*_DEFAULT_SEEDS, *_FASHION_SEEDS]))
    else:
        default_seeds = _DEFAULT_SEEDS
    seeds = [s for s in (args.keywords or "").split(",") if s.strip()] or list(default_seeds)

    u, p = scan._load_creds()
    if not (u and p):
        print(json.dumps({"ok": False, "error": "no DataForSEO credentials (set them in "
                          "Connections -> Data, or in the project-root .env)"}))
        return 1
    auth = base64.b64encode(f"{u}:{p}".encode()).decode()

    sellers, domains, errors = harvest_candidates(seeds, args.geo, auth, args.per_seed_cap)
    sellers = sellers[: args.max_domains]
    domains = domains[: args.max_domains]
    # Premium source: merge TrendTrack's scaling-US-shops feed into the domain pool (when a token
    # is set). Additive — dedup vs the DFS harvest; the same GENERAL classifier gate then filters.
    tt_domains = trendtrack_domains(limit=args.max_domains)
    if tt_domains:
        domains = list(dict.fromkeys(domains + tt_domains))
    if not sellers and not domains:
        print(json.dumps({"ok": False, "error": "no candidate stores harvested from the Shopping grid",
                          "seeds": seeds, "seed_errors": errors}))
        return 1

    # Hand seller NAMES to harvest via its --from-scan path (it owns the name→domain guess and
    # skips the big marketplaces); pass any explicit domains directly. The classifier gate then
    # keeps only the live-Shopify GENERAL stores.
    scan_doc = {"sponsored": [{"advertiser": s} for s in sellers]}
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(scan_doc, tmp)
    tmp.close()
    try:
        cmd = [
            args.harvest_python, args.harvest,
            "--from-scan", tmp.name,
            "--min-depts", str(args.min_depts),
            "--max-dominance", str(args.max_dominance),
        ]
        if domains:
            cmd += ["--domains", ",".join(domains)]
        if not args.dry_run:
            cmd.append("--apply")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")

    # Parse the human-readable gate summary for the headline numbers.
    added = re.search(r"PASSED:\s*(.+)", out)
    passed_list = [d.strip() for d in added.group(1).split(",")] if added else []
    m_counts = re.search(r"(\d+)\s+GENERAL\s*/\s*(\d+)\s+rejected", out)
    n_general = int(m_counts.group(1)) if m_counts else len(passed_list)

    print(json.dumps({
        "ok": proc.returncode == 0,
        "seeds": seeds,
        "candidates_evaluated": len(sellers) + len(domains),
        "trendtrack_domains": len(tt_domains),
        "source": "dataforseo+trendtrack" if tt_domains else "dataforseo",
        "general_found": n_general,
        "added": [] if args.dry_run else passed_list,
        "dry_run": args.dry_run,
        "seed_errors": errors,
        "gate_output": out.strip(),
    }, indent=2))
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
