#!/usr/bin/env python3
"""
classify_store.py — decide whether a Shopify store is a GENERAL store or a NICHE
store, by measuring CATALOG BREADTH across unrelated retail departments.

Why: traffic + product-count + collection-count CANNOT tell general from niche.
  - belroshop (GENERAL): 27 collections spanning Car / Garden / Fitness / Beauty /
    Kitchen / Kids / Pets — many unrelated departments.
  - shopzaza (NICHE): 96 collections but ALL "14 Grams / 28 Grams / 1 Pound" — one
    department (cannabis). High count, ZERO breadth.
  - viralaluna (NICHE): 16 collections all beauty.
The discriminator is the number of DISTINCT RETAIL DEPARTMENTS the catalog touches,
NOT how many products/collections it has.

Method: pull /collections.json + a /products.json sample, map every collection
title + product_type + tag to a canonical department via a keyword taxonomy, then
count distinct departments. >= --min-depts distinct = GENERAL.

Usage:
  classify_store.py belroshop.com shopzaza.com viralaluna.com
  classify_store.py --stores stores.txt              # audit the tracked list
  classify_store.py --min-depts 5 --json out.json belroshop.com
"""
import argparse
import json
import os
import re
import sys
import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Canonical retail departments -> trigger keywords (matched against collection
# titles, product_types, tags). Deliberately broad; a store touching many of
# these is "general". Keep niche-defining words OUT (e.g. don't let "grams"
# count as a department).
DEPARTMENTS = {
    "home_kitchen":   ["kitchen", "home", "cookware", "dining", "tableware", "utensil",
                       "appliance", "storage", "organizer", "cleaning", "household"],
    "furniture_decor":["furniture", "decor", "lighting", "lamp", "rug", "curtain",
                       "wall art", "candle", "bedding", "mattress", "pillow"],
    "garden_outdoor": ["garden", "outdoor", "patio", "lawn", "plant", "grill", "bbq",
                       "camping", "yard"],
    "auto":           ["car", "auto", "vehicle", "motorcycle", "truck", "rv", "tire"],
    "pets":           ["pet", "dog", "cat", "puppy", "kitten", "aquarium", "fish tank"],
    "beauty":         ["beauty", "makeup", "cosmetic", "skincare", "skin care", "hair",
                       "nail", "fragrance", "perfume", "lash", "brow"],
    "health_wellness":["health", "wellness", "massage", "pain relief", "orthopedic",
                       "posture", "supplement", "vitamin", "fitness", "gym", "workout"],
    "baby_kids":      ["baby", "kids", "child", "toddler", "infant", "nursery", "toy"],
    "electronics":    ["electronic", "gadget", "tech", "audio", "headphone", "speaker",
                       "camera", "phone", "charger", "antenna", "smart home"],
    "tools_diy":      ["tool", "diy", "hardware", "drill", "workshop", "repair", "garage"],
    "office":         ["office", "desk", "stationery", "school supplies"],
    "sports_outdoor": ["sport", "bike", "cycling", "fishing", "hiking", "golf",
                       "hunting", "athletic"],
    "apparel":        ["apparel", "clothing", "shirt", "dress", "shoe", "footwear",
                       "jacket", "fashion", "accessor", "jewelry", "watch", "bag"],
    "travel":         ["travel", "luggage", "backpack", "suitcase"],
    "seasonal_gift":  ["christmas", "halloween", "holiday", "gift", "valentine"],
}
# Departments that are too generic to count as breadth on their own.
WEAK = {"seasonal_gift"}

NOISE = re.compile(r"\b(all|best ?sell|new arrival|sale|featured|shipping|protection|"
                   r"upsell|bundle|gift card|home page|frontpage|catalog|shop all|"
                   r"\d+ ?gram|\d+ ?pound|ounce|oz|off offer|clearance|discount week)\b", re.I)
# Dated drop/batch collections (e.g. "Dress 08-06-2025", "Jewelry2 26-05-2025")
# are merchandising drops, not real departments — they over-count one category.
DATE_DROP = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")


def is_noise(title):
    return bool(NOISE.search(title) or DATE_DROP.search(title))


def fetch_json(session, url):
    try:
        r = session.get(url, timeout=20)
        if r.status_code == 200:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def map_department(text):
    t = (text or "").lower()
    hits = set()
    for dept, kws in DEPARTMENTS.items():
        if any(k in t for k in kws):
            hits.add(dept)
    return hits


# Shopify locale prefixes to try for English titles (many EU dropship general
# stores default to DE/FR/NL but expose an English locale at /en).
EN_LOCALES = ["", "en", "en-us", "en-gb"]


def best_collections(session, store):
    """Fetch collections across locale prefixes; return the title set that maps
    BEST to our (English) department taxonomy. Fixes multilingual stores whose
    default-locale titles (e.g. German 'Bekleidung'/'Beleuchtung') the English
    keyword map can't read -> fake low coverage. Returns (titles, locale)."""
    best_titles, best_score, best_loc = [], -1, ""
    for loc in EN_LOCALES:
        pref = f"/{loc}" if loc else ""
        data = fetch_json(session, f"https://{store}{pref}/collections.json?limit=250")
        if not data:
            continue
        titles = [c.get("title") or "" for c in data.get("collections", [])
                  if c.get("title") and not is_noise(c.get("title") or "")]
        if not titles:
            continue
        score = sum(1 for t in titles if map_department(t) - WEAK)
        if score > best_score:
            best_titles, best_score, best_loc = titles, score, loc
        if loc == "" and score >= max(3, len(titles) * 0.5):
            break  # default locale already maps well, no need to probe others
    return best_titles, best_loc


def classify(session, store, min_depts, max_dominance=0.45, min_coverage=0.35, sample=250):
    titles, locale = best_collections(session, store)
    prod = fetch_json(session, f"https://{store}/products.json?limit={sample}")
    if not titles and prod is None:
        return {"store": store, "error": "unreachable / not Shopify"}

    ptypes, tags = [], set()
    for p in (prod or {}).get("products", []):
        if p.get("product_type"):
            ptypes.append(p["product_type"])
        for tg in (p.get("tags") or []):
            tags.add(tg)
    ptypes, tags = [], set()
    for p in (prod or {}).get("products", []):
        if p.get("product_type"):
            ptypes.append(p["product_type"])
        for tg in (p.get("tags") or []):
            tags.add(tg)

    # COLLECTION TITLES are the unit of analysis — they're how the MERCHANT
    # organizes the store for shoppers, which is exactly where general-vs-niche
    # shows. Product_type/tags are noise on dropship general stores (often blank
    # or unique-per-SKU). feanatic collections (Backyard/Camping/Clothing/Furniture)
    # map across departments; venom collections (110cc Pocket Bikes/250cc Choppers)
    # map to NOTHING; pharma collections all map to one department.
    dept_hits = {}            # department -> # collections touching it
    mapped = 0                # collections that map to >=1 department
    for ti in titles:
        ds = map_department(ti) - WEAK
        if ds:
            mapped += 1
            for d in ds:
                dept_hits[d] = dept_hits.get(d, 0) + 1
    strong = set(dept_hits.keys())

    # DOMINANCE: top department's share of mapped collections (balance check).
    dominant_share = (max(dept_hits.values()) / mapped) if mapped else None
    dominant_dept = (max(dept_hits, key=dept_hits.get) if dept_hits else None)

    # COVERAGE: fraction of (non-noise) collections that map to ANY consumer
    # department. General stores organize into recognizable departments => HIGH
    # coverage. Specialized stores (powersports cc-classes, tractor SKUs, fragrance
    # chemicals) have collections that map to nothing => LOW coverage => NICHE.
    n_titles = len(titles)
    coverage = (mapped / n_titles) if n_titles else 0

    # GENERAL requires breadth (>= min_depts) AND balance (no single dept dominates)
    # AND enough coverage that the breadth is real (not a few stray mapped SKUs).
    breadth_ok = len(strong) >= min_depts
    balance_ok = dominant_share is not None and dominant_share <= max_dominance
    coverage_ok = coverage >= min_coverage
    verdict = "GENERAL" if (breadth_ok and balance_ok and coverage_ok) else "NICHE"
    if verdict == "GENERAL":
        reason = "ok"
    elif not breadth_ok:
        reason = "too few departments"
    elif not coverage_ok:
        reason = f"low catalog coverage ({round(coverage*100)}% map) — specialized"
    elif dominant_share is None:
        reason = "no product-level department signal"
    else:
        reason = f"dominated by {dominant_dept} ({round(dominant_share*100)}%)"
    return {
        "store": store,
        "verdict": verdict,
        "reason": reason,
        "distinct_departments": len(strong),
        "departments": sorted(strong),
        "dominant_dept": dominant_dept,
        "dominant_share": round(dominant_share, 2) if dominant_share is not None else None,
        "coverage": round(coverage, 2),
        "locale": locale or "default",
        "n_collections": n_titles,
        "n_product_types": len(set(ptypes)),
        "min_depts": min_depts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("domains", nargs="*")
    ap.add_argument("--stores", help="file with domains (audit mode)")
    ap.add_argument("--min-depts", type=int, default=5,
                    help="distinct strong departments to qualify as GENERAL (default 5)")
    ap.add_argument("--max-dominance", type=float, default=0.45,
                    help="max product share for the single top department (default 0.55)")
    ap.add_argument("--min-coverage", type=float, default=0.35,
                    help="min fraction of sampled products that map to a department (default 0.35)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    domains = list(args.domains)
    if args.stores and os.path.isfile(args.stores):
        with open(args.stores) as f:
            domains += [ln.strip() for ln in f
                        if ln.strip() and not ln.strip().startswith("#")]
    domains = [d.replace("https://", "").replace("http://", "").rstrip("/")
               for d in domains]
    if not domains:
        print("give domains or --stores", file=sys.stderr)
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    results = []
    print(f"{'STORE':<26} {'VERDICT':<8} {'DEPTS':>5} {'COV':>5} {'TOPDEPT%':>16}  WHY")
    print("-" * 98)
    for d in domains:
        r = classify(session, d, args.min_depts, args.max_dominance, args.min_coverage)
        results.append(r)
        if r.get("error"):
            print(f"{d:<26} {'ERROR':<8}     —  {r['error']}")
            continue
        flag = "✅" if r["verdict"] == "GENERAL" else "▫️"
        dom = f"{r['dominant_dept']} {int((r['dominant_share'] or 0)*100)}%" if r['dominant_dept'] else "—"
        cov = f"{int(r.get('coverage',0)*100)}%"
        print(f"{flag}{r['store']:<24} {r['verdict']:<8} {r['distinct_departments']:>5} {cov:>5} {dom:>16}  {r['reason']}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nwrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
