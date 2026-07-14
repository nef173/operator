#!/usr/bin/env python3
"""validate_store.py — the canonical roster-admission funnel for the Competitor
Best-Seller Spy (06 general store). Runs ordered gates and emits a single verdict:

  REJECT(<reason>)  — fails a hard gate; do NOT add
  REVIEW(<reason>)  — passes hard gates but needs an operator VISION check
                      (vendor names / homepage can't be fully machine-read)
  PASS:GENERAL      — clean broad generic-dropship general (arvilaro-like)
  PASS:NICHE        — clean single-vertical dropship store (in-scope)

Gate order (cheapest + most decisive first; any REJECT stops the funnel):
  0 live Shopify         products.json returns products
  1 channel  (HARD)      activeAds >= --max-ads  -> Meta-driven  REJECT
  2 market   (soft)      US share  <  --min-us   -> not-US       REVIEW
  3 momentum (soft)      MoM       <  --max-drop -> declining     REVIEW
  4 product-class (HARD) licensed/trademarked IP in titles/vendors REJECT (non-dropshippable)
  5 brand/reseller       many distinct brand vendors (!=domain)   REVIEW (vision)
  6 in-scope niche       apparel/fashion-dominated catalog         REVIEW (out-of-scope)
  7 label                classify_store breadth -> GENERAL|NICHE

The 3 TrendTrack-only numbers (ads / US / MoM) are NOT in products.json. Supply them:
  --traffic store_traffic.json   (pull from the roster record, for existing members)
  --ads N --us 0.99 --mom 24     (ad-hoc, e.g. from a find_similar/search_shops dump)
If neither is given, gates 1-3 are SKIPPED-with-warning (verify them by hand).

Usage:
  validate_store.py eptchn.com --ads 62 --us 0.99 --mom 24
  validate_store.py --stores stores.txt --traffic store_traffic.json
  validate_store.py newcandidate.com --traffic store_traffic.json --json out.json
"""
import argparse, json, os, re, sys
import requests
import classify_store as cs   # reuse the breadth/dominance/coverage logic

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# --- Gate 4: licensed / trademarked / copyrighted IP -> non-dropshippable ----
# A title or vendor hit here means authorized-retailer-only goods we can't source.
IP_TOKENS = re.compile(
    r"\b(funko|pop! ?vinyl|pokemon|pokémon|squishmallow|disney|marvel|star ?wars|"
    r"hello ?kitty|sanrio|nintendo|lego|barbie|hot ?wheels|nba|nfl|mlb|nhl|fifa|"
    r"anime|naruto|dragon ?ball|one ?piece|sailor ?moon|harry ?potter|"
    r"dvd|blu-?ray|vinyl record|trading ?card|topps|panini)\b", re.I)
IP_SHARE = 0.12   # IP must be pervasive (>= this share of titles) to REJECT

# --- Gate 8: Google-Shopping feed app (soft CONFIRMING signal) ----------------
# A pure-Google store almost always runs a Google-Shopping product-feed app to push
# its catalog into Merchant Center (Simprosys Multi Feeds etc.). Presence CONFIRMS
# the Google channel; ABSENCE is NOT a reject — TrendTrack's installed-app list does
# not always surface the feed app (operator: "may not always show, but most of time
# must"). So this only downgrades to REVIEW when we affirmatively HAVE the app list
# AND it shows no feed app; with no app data it's recorded "unknown" and never gates.
FEED_APP_TOKENS = re.compile(
    r"\b(simprosys|multiple? ?google ?shopping ?feeds?|google ?shopping ?feed|"
    r"multi ?feeds?|feed ?for ?google ?shopping|datafeedwatch|data ?feed ?watch|"
    r"cedcommerce|adnabu|nabu|sales ?& ?orders|koongo|feedarmy|socialshop|"
    r"clever ?ads|productfeeds?|shopping ?feed|merchant ?center ?feed|"
    r"google ?& ?youtube|google ?channel)\b", re.I)

# HTML storefront markers a few feed apps / the Google channel leave behind (most
# feed apps run admin-side and leave NO storefront trace, so this is a weak fallback
# to the authoritative TrendTrack app list, never the primary source).
FEED_HTML_MARKERS = re.compile(
    r"(simprosys|google-site-verification|content-api|shopping-feed|"
    r"cdn\.shopify\.com/.*?google-shopping)", re.I)


def detect_feed_app(installed_apps, html):
    """Return (matched_app_or_None, source) — source in {'app-list','html',None}.
    installed_apps: list[str] from TrendTrack (authoritative). html: storefront (weak)."""
    for a in installed_apps or []:
        m = FEED_APP_TOKENS.search(a or "")
        if m:
            return a.strip(), "app-list"
    if html:
        m = FEED_HTML_MARKERS.search(html)
        if m:
            return m.group(1), "html"
    return None, None


# --- Gate 6: fashion / apparel = out-of-scope niche ---------------------------
APPAREL_TOKENS = re.compile(
    r"\b(dress|gown|jumpsuit|romper|blouse|skirt|shirt|t-?shirt|tee|hoodie|"
    r"sweater|jacket|coat|pants|jeans|leggings|shorts|swimwear|swimsuit|bikini|"
    r"lingerie|bra|underwear|shoe|sneaker|boot|heel|sandal|loafer)\b", re.I)

# Vendors that are obviously NOT a brand name (generic / platform default).
GENERIC_VENDOR = re.compile(r"^(mysite|my store|default|shopify|admin|n/?a|)$", re.I)


def domain_root(domain):
    return re.sub(r"\.(com|co|shop|store|online|net|us|io)$", "",
                  domain.split("//")[-1].split("/")[0]).replace("-", "").lower()


def fetch_products(session, domain, limit=250):
    for pref in ("", "/en", "/en-us"):
        try:
            r = session.get(f"https://{domain}{pref}/products.json?limit={limit}", timeout=20)
            if r.status_code == 200:
                prods = r.json().get("products", [])
                if prods:
                    return prods
        except (requests.RequestException, ValueError):
            continue
    return None


def looks_like_brand(vendor, root):
    """A vendor that is TitleCase / multiword / not generic and != the domain
    reads like a real national brand (reseller signal)."""
    v = (vendor or "").strip()
    if not v or GENERIC_VENDOR.match(v):
        return False
    vnorm = re.sub(r"[^a-z0-9]", "", v.lower())
    if vnorm == root or root in vnorm or vnorm in root:
        return False  # house vendor == domain -> NOT a reseller brand
    # heuristic brand-ness: has a capital letter or a space and >2 chars
    return len(v) > 2 and (v[0].isupper() or " " in v)


def analyse_catalog(prods, domain):
    root = domain_root(domain)
    vendors, titles = {}, []
    for p in prods:
        v = (p.get("vendor") or "").strip()
        vendors[v] = vendors.get(v, 0) + 1
        titles.append(p.get("title") or "")
    n = len(prods)

    # IP gate is a HARD reject, so it must be HIGH-PRECISION: a single stray
    # "one-piece swimsuit" must NOT reject a general store. Only flag when the IP
    # token is PERVASIVE (>= --ip-share of titles) OR appears as a VENDOR/brand
    # (a licensed-merch store carries the franchise across its catalog).
    ip_title_hits, ip_tok = 0, None
    for t in titles:
        m = IP_TOKENS.search(t)
        if m:
            ip_title_hits += 1; ip_tok = ip_tok or m.group(0)
    ip_vendor = next((v for v in vendors if IP_TOKENS.search(v)), None)
    ip_share = ip_title_hits / n if n else 0

    apparel_hits = sum(1 for t in titles if APPAREL_TOKENS.search(t))
    apparel_share = apparel_hits / n if n else 0

    top = sorted(vendors.items(), key=lambda kv: -kv[1])
    top_vendor, top_n = (top[0] if top else ("", 0))
    house = bool(top_vendor) and (re.sub(r"[^a-z0-9]", "", top_vendor.lower()) == root
                                   or root in re.sub(r"[^a-z0-9]", "", top_vendor.lower()))
    # A "brand vendor" must REPEAT (>=2 products) — a vendor appearing on a single
    # SKU is per-SKU dropship noise (random strings), NOT a stocked national brand.
    brand_vendors = [v for v, c in top if c >= 2 and looks_like_brand(v, root)]
    brand_vendor_share = sum(vendors[v] for v in brand_vendors) / n if n else 0
    return {
        "root": root, "n_sample": n,
        "ip_hit": (ip_tok if (ip_share >= IP_SHARE or ip_vendor) else None),
        "ip_share": round(ip_share, 2),
        "apparel_share": round(apparel_share, 2),
        "top_vendor": top_vendor, "top_vendor_n": top_n,
        "house_vendor": house,
        "distinct_brand_vendors": len(brand_vendors),
        "brand_vendor_share": round(brand_vendor_share, 2),
        "brand_vendor_sample": brand_vendors[:6],
    }


def traffic_for(domain, traffic_path):
    if not traffic_path or not os.path.isfile(traffic_path):
        return None
    rec = json.load(open(traffic_path)).get("stores", {}).get(domain)
    if not rec:
        return None
    h = rec.get("history") or []
    mom = None
    if len(h) >= 2 and h[-2]:
        mom = round((h[-1] - h[-2]) / h[-2] * 100)
    # top_countries (code->share 0-1) is the full split when enriched; us_share is
    # the legacy single field. Build a minimal map from us_share if that's all we have.
    tc = rec.get("top_countries")
    if not tc and rec.get("us_share") is not None:
        tc = {"US": rec["us_share"]}
    # installed_apps: the TrendTrack app list for this domain (list of app names).
    # Accept a few field spellings so a raw dump can be merged in unchanged.
    apps = (rec.get("installed_apps") or rec.get("apps")
            or rec.get("technologies") or None)
    return {"ads": rec.get("active_meta_ads"), "us": rec.get("us_share"),
            "mom": mom, "top_countries": tc, "installed_apps": apps}


def market_gate(top_countries, us, args):
    """Return a REVIEW reason string (or None to pass) for the market gate.
    Configurable via --market (target country, default US), --multi-market
    (pass when the store legitimately splits across several markets), and the
    --min-us / --market-min thresholds."""
    tc = {k.upper(): v for k, v in (top_countries or {}).items()}
    target = args.market.upper()
    share = tc.get(target, us if target == "US" and us is not None else None)
    if args.multi_market:
        # multi-market = OK as long as the target market still holds a real slice
        # AND the store genuinely spreads (>= --markets-min markets at >= --market-min)
        big = [c for c, s in tc.items() if s is not None and s >= args.market_min]
        if share is not None and share < args.market_min and len(big) < args.markets_min:
            return f"market:{target}<{int(args.market_min*100)}% & not-multi-market"
        return None
    # single-market mode
    if share is not None and share < args.min_us:
        return f"market:{target}({round(share*100)}%<{int(args.min_us*100)}%)"
    return None


def fetch_collections(session, domain):
    """Count distinct storefront collections (categories). Dropship generals tend
    to publish many; thin niche/junk stores publish few. Returns (count, sample)."""
    for pref in ("", "/en", "/en-us"):
        try:
            r = session.get(f"https://{domain}{pref}/collections.json?limit=250", timeout=20)
            if r.status_code == 200:
                cols = r.json().get("collections", [])
                titles = [c.get("title", "") for c in cols]
                return len(cols), titles[:12]
        except (requests.RequestException, ValueError):
            continue
    return None, []


def homepage_fingerprint(session, domain):
    """A cheap structural fingerprint of the storefront homepage so the operator
    can spot the same cookie-cutter dropship template reused across many stores.
    Returns (theme_name, fingerprint) — fingerprint is a stable hash of the
    Shopify theme + section ordering, NOT the content."""
    import hashlib
    for pref in ("", "/en", "/en-us"):
        try:
            r = session.get(f"https://{domain}{pref}/", timeout=20)
            if r.status_code != 200 or not r.text:
                continue
            html = r.text
            theme = None
            m = re.search(r'Shopify\.theme\s*=\s*\{[^}]*"name":"([^"]+)"', html)
            if m:
                theme = m.group(1)
            # ordered section schema ids = the structural skeleton of the page
            sections = re.findall(r'id="shopify-section-(?:template--\d+__)?([a-z0-9_-]+)"', html, re.I)
            skel = "|".join(sections[:40])
            fp = hashlib.sha1(skel.encode()).hexdigest()[:12] if skel else None
            return theme, fp, html
        except requests.RequestException:
            continue
    return None, None, None


# Map a dominant department to a short niche tag the operator uses (e.g. "dog niche").
NICHE_TAG = {
    "Pets & Animals": "pet", "Apparel": "apparel", "Home & Garden": "home",
    "Beauty & Fitness": "beauty", "Computers & Electronics": "electronics",
    "Business & Industrial": "industrial", "Gifts & Special Event Items": "gifts",
    "Toys & Hobbies": "toys", "Sporting Goods": "sports", "Vehicles & Parts": "auto",
    "Baby & Toddler": "baby", "Food & Beverage": "food", "Arts & Entertainment": "arts",
    "Health": "health", "Jewelry & Watches": "jewelry",
}


def niche_tag(label, dominant_dept):
    """Return the roster tag: 'general' for broad stores, else a niche slug like
    'pet niche' / 'beauty niche' derived from the dominant department."""
    if label == "GENERAL":
        return "general"
    base = NICHE_TAG.get(dominant_dept, (dominant_dept or "niche").split("&")[0].strip().lower())
    return f"{base} niche"


def validate(session, domain, ads, us, mom, top_countries, args, installed_apps=None):
    out = {"store": domain, "ads": ads, "us": us, "mom": mom}

    # Gate 0 — live Shopify
    prods = fetch_products(session, domain)
    if not prods:
        out["verdict"] = "REJECT"; out["reason"] = "not-live-Shopify"; return out

    # Gate 1 — channel (HARD): pure-Google means ~0 Meta ads
    if ads is not None and ads >= args.max_ads:
        out["verdict"] = "REJECT"; out["reason"] = f"meta-driven({ads} ads>=  {args.max_ads})"; return out

    # Gate 2 — market (soft REVIEW): configurable target market / multi-market mode
    review = []
    mkt = market_gate(top_countries, us, args)
    if mkt:
        review.append(mkt)
    # Gate 3 — momentum (soft REVIEW; bowlift-style keep-exceptions live in stores.txt comments)
    if mom is not None and mom < -args.max_drop:
        review.append(f"declining({mom:+d}% MoM)")

    cat = analyse_catalog(prods, domain)
    out.update({k: cat[k] for k in ("ip_hit", "apparel_share", "top_vendor",
                                    "house_vendor", "distinct_brand_vendors",
                                    "brand_vendor_share", "brand_vendor_sample")})

    # Gate 4 — product class (HARD): licensed/trademarked IP = non-dropshippable
    if cat["ip_hit"]:
        out["verdict"] = "REJECT"; out["reason"] = f"licensed-IP({cat['ip_hit']})"; return out

    # Gate 5 — brand / reseller (REVIEW: needs vision; can't perfectly read vendor names)
    top_share = cat["top_vendor_n"] / cat["n_sample"] if cat["n_sample"] else 0
    if not cat["house_vendor"] and cat["distinct_brand_vendors"] >= 3 and cat["brand_vendor_share"] >= 0.4:
        review.append(f"multi-brand-reseller?({cat['distinct_brand_vendors']} brand-vendors, "
                      f"{int(cat['brand_vendor_share']*100)}% share)")
    elif (not cat["house_vendor"] and top_share >= 0.6
          and looks_like_brand(cat["top_vendor"], cat["root"])):
        review.append(f"single-brand?({cat['top_vendor']} = {int(top_share*100)}% of catalog)")

    # Gate 6 — in-scope niche. Mode-aware (STORE_MODE env, set per store by the operator app):
    #   general (default) → fashion/apparel is out of scope, REVIEW an apparel-heavy store.
    #   fashion           → apparel IS the scope: the gate inverts — REVIEW a store with
    #                       almost no apparel (it can't teach a fashion catalog anything).
    #   both              → apparel AND general are both in scope: the gate is a no-op
    #                       (nothing is excluded on apparel share either way).
    store_mode = (os.environ.get("STORE_MODE") or "general").strip().lower()
    if store_mode == "both":
        pass  # both paths in scope — no apparel-share gate
    elif store_mode == "fashion":
        if cat["apparel_share"] < 0.2:
            review.append(f"not-apparel({int(cat['apparel_share']*100)}% — fashion-mode store)")
    elif cat["apparel_share"] >= 0.5:
        review.append(f"apparel-niche({int(cat['apparel_share']*100)}%)")

    # Category scan — how many storefront collections the store publishes.
    n_cols, col_sample = fetch_collections(session, domain)
    out["collections"] = n_cols
    out["collection_sample"] = col_sample
    if n_cols is not None and n_cols < args.min_collections:
        review.append(f"thin-catalog({n_cols} collections<{args.min_collections})")

    # Homepage / store-design fingerprint — lets the operator catch the same
    # cookie-cutter dropship template reused across many "different" stores.
    theme, fp, html = homepage_fingerprint(session, domain)
    out["theme"] = theme; out["homepage_fp"] = fp

    # Gate 8 — Google-Shopping feed app (soft CONFIRMING signal; tolerant of absence)
    feed_app, feed_src = detect_feed_app(installed_apps, html)
    if feed_app:
        out["feed_app"] = feed_app; out["feed_app_src"] = feed_src
    elif installed_apps:
        # We HAVE the TrendTrack app list and it shows no Google feed app -> soft flag.
        out["feed_app"] = None
        review.append("no-google-feed-app?(app-list shows none)")
    else:
        # No app data supplied -> unknown; never gate on it (may not show on TrendTrack).
        out["feed_app"] = "unknown"

    # Gate 7 — GENERAL vs NICHE label (breadth/dominance/coverage)
    cl = cs.classify(session, domain, args.min_depts, args.max_dominance, args.min_coverage)
    label = cl.get("verdict", "NICHE")
    out["departments"] = cl.get("distinct_departments")
    out["dominant"] = f"{cl.get('dominant_dept')} {int((cl.get('dominant_share') or 0)*100)}%"
    out["coverage"] = cl.get("coverage")
    out["tag"] = niche_tag(label, cl.get("dominant_dept"))
    # broad house-vendor general tripping ONLY the dominance line -> operator-override REVIEW
    if (label == "NICHE" and cat["house_vendor"]
            and (cl.get("distinct_departments") or 0) >= 4
            and 0.45 < (cl.get("dominant_share") or 1) <= 0.6):
        review.append(f"override?(house-vendor, {cl.get('distinct_departments')} depts, "
                      f"dominance {int((cl.get('dominant_share') or 0)*100)}%)")
        out["tag"] = "general?"  # operator-override candidate -> likely general

    if review:
        out["verdict"] = "REVIEW"; out["reason"] = "; ".join(review)
        out["label"] = label
    else:
        out["verdict"] = "PASS"; out["label"] = label
        out["reason"] = "house-vendor general" if cat["house_vendor"] else "clean"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("domains", nargs="*")
    ap.add_argument("--stores", help="file of domains (one per line, # ignored)")
    ap.add_argument("--traffic", help="store_traffic.json to pull ads/US/MoM per domain")
    ap.add_argument("--ads", type=int, help="active Meta ads (ad-hoc, single domain)")
    ap.add_argument("--us", type=float, help="US traffic share 0-1 (ad-hoc)")
    ap.add_argument("--mom", type=int, help="MoM %% (ad-hoc)")
    ap.add_argument("--apps", nargs="*", help="installed app names from TrendTrack "
                    "(ad-hoc, single domain) — used for the Google-feed-app signal")
    ap.add_argument("--max-ads", type=int, default=10, help="REJECT at >= this many Meta ads (default 10)")
    # --- market selector ---
    ap.add_argument("--market", default="US", help="target main market country code (default US)")
    ap.add_argument("--multi-market", action="store_true",
                    help="accept stores split across several markets (each >= --market-min)")
    ap.add_argument("--min-us", type=float, default=0.50,
                    help="single-market mode: REVIEW below this target-market share (default 0.50)")
    ap.add_argument("--market-min", type=float, default=0.05,
                    help="multi-market mode: a market 'counts' at >= this share (default 0.05 = 5%%)")
    ap.add_argument("--markets-min", type=int, default=3,
                    help="multi-market mode: need >= this many qualifying markets (default 3)")
    ap.add_argument("--max-drop", type=int, default=30, help="REVIEW below -this%% MoM (default 30)")
    ap.add_argument("--min-collections", type=int, default=4,
                    help="REVIEW below this many storefront collections (default 4)")
    ap.add_argument("--min-depts", type=int, default=5)
    ap.add_argument("--max-dominance", type=float, default=0.45)
    ap.add_argument("--min-coverage", type=float, default=0.35)
    ap.add_argument("--json")
    a = ap.parse_args()

    domains = list(a.domains)
    if a.stores and os.path.isfile(a.stores):
        domains += [ln.strip() for ln in open(a.stores)
                    if ln.strip() and not ln.startswith("#")]
    domains = [d.replace("https://", "").replace("http://", "").rstrip("/") for d in domains]
    if not domains:
        print("give domains or --stores", file=sys.stderr); return 1

    session = requests.Session(); session.headers.update({"User-Agent": UA})
    results = []
    print(f"{'STORE':<26} {'VERDICT':<8} {'TAG':<14} {'COL':>3} {'DEPT':>4} {'DOM%':>5}  WHY")
    print("-" * 110)
    for d in domains:
        if a.traffic:
            t = traffic_for(d, a.traffic) or {}
            ads, us, mom, tc = t.get("ads"), t.get("us"), t.get("mom"), t.get("top_countries")
            apps = t.get("installed_apps")
        else:
            ads, us, mom, tc = a.ads, a.us, a.mom, None
            apps = a.apps
        if a.apps:  # ad-hoc --apps overrides / supplements the traffic record
            apps = a.apps
        r = validate(session, d, ads, us, mom, tc, a, installed_apps=apps)
        results.append(r)
        icon = {"PASS": "✅", "REVIEW": "🔎", "REJECT": "⛔"}.get(r["verdict"], "?")
        dom = (r.get("dominant") or "").split()[-1] if r.get("dominant") else "—"
        fa = r.get("feed_app")
        feed = "feed✗" if fa is None else ("feed?" if fa == "unknown" else "feed✓")
        print(f"{icon}{r['store']:<24} {r['verdict']:<8} {str(r.get('tag','—')):<14} "
              f"{str(r.get('collections','—')):>3} {str(r.get('departments','—')):>4} "
              f"{dom:>5} {feed:<6} {r.get('reason','')}")
    if a.json:
        json.dump(results, open(a.json, "w"), indent=2)
        print(f"\nwrote {a.json}", file=sys.stderr)
    n = {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    for r in results:
        n[r["verdict"]] = n.get(r["verdict"], 0) + 1
    print(f"\n{n.get('PASS',0)} PASS · {n.get('REVIEW',0)} REVIEW · {n.get('REJECT',0)} REJECT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
