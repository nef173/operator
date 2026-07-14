#!/usr/bin/env python3
"""classify_advertiser.py — split Meta-ad advertisers into BRAND vs DROPSHIPPER.

Reach / live-ad-count / total-reach do NOT separate them (a dropshipper can have
50M reach and 768 live ads). The separator is SHOP STRUCTURE: brands run a deep
coherent catalog off /collections; dropshippers hide a few sourceable SKUs behind
a /pages/<funnel> landing on a store that is weeks-to-months old.

Input  = a TrendTrack search_ads JSON dump (the saved tool-result file).
Output = a table + a JSON of DROPSHIPPER-only advertisers (the sourceable targets),
         each with the advertised landing URL so the product can be matched on
         AliExpress/Temu for sourcing.

Usage:
  python3 classify_advertiser.py --from-search-ads dump.json [--save dropshippers.json]
  python3 classify_advertiser.py --domains a.com,b.com   # ad-hoc probe only
"""
import argparse, json, re, sys, urllib.request, datetime, concurrent.futures as cf

FUNNEL_RE = re.compile(r"/pages/|quiz|lp-?\d|adv-?\d|offer|special|_en\b|-draft|landing|advertorial", re.I)
GENERIC_HANDLE_RE = re.compile(r"all-in-one|body-coverage|perfector|special|offer|lazy|insider|blog", re.I)
DROPSHIP_TLD_RE = re.compile(r"\.(shop|store|online|co)$", re.I)


def probe_products_json(domain, timeout=12):
    """Return (count|None, oldest_iso|None). None count = locked/non-Shopify."""
    url = f"https://{domain}/products.json?limit=250"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            prods = json.load(r).get("products", [])
        if not prods:
            return None, None
        dates = sorted(p.get("created_at", "") for p in prods if p.get("created_at"))
        return len(prods), (dates[0][:10] if dates else None)
    except Exception:
        return None, None


def months_since(iso):
    if not iso:
        return None
    try:
        d = datetime.date.fromisoformat(iso[:10])
        return (datetime.date.today() - d).days / 30.0
    except Exception:
        return None


def score(adv):
    """Composite dropship score (higher = more dropship). Returns (score, reasons)."""
    s, why = 0, []
    landing = adv.get("landing_url") or ""
    dom = adv.get("domain") or ""
    cnt = adv.get("products_count")
    age_m = months_since(adv.get("oldest_product"))

    if FUNNEL_RE.search(landing):
        s += 3; why.append("funnel-landing(/pages)")
    if GENERIC_HANDLE_RE.search(landing):
        s += 1; why.append("generic-handle")
    if age_m is not None and age_m <= 18:
        s += 2; why.append(f"young-store({age_m:.0f}mo)")
    if cnt is not None:
        if cnt <= 8:
            s += 2; why.append(f"few-products({cnt})")
        elif cnt >= 150:
            s += 1; why.append(f"general-dropship-catalog({cnt})")
    if DROPSHIP_TLD_RE.search(dom) and "shop." not in dom:
        s += 1; why.append("dropship-TLD")
    # Brand override: locked products.json + /collections catalog + no funnel + old/unknown
    if cnt is None and "/collections" in landing and not FUNNEL_RE.search(landing):
        s -= 4; why.append("BRAND:locked+collections-catalog")
    if landing.endswith(".php") or "/blog/" in landing:
        # established skincare brands often run .php advertorials off a real catalog
        why.append("non-shopify/blog-LP")
    return s, why


def classify_one(adv):
    cnt, oldest = probe_products_json(adv["domain"])
    adv["products_count"], adv["oldest_product"] = cnt, oldest
    sc, why = score(adv)
    adv["dropship_score"] = sc
    adv["verdict"] = "DROPSHIP" if sc >= 4 else ("BRAND" if sc <= 0 else "REVIEW")
    adv["reasons"] = why
    return adv


def from_search_ads(path):
    raw = open(path).read()
    m = re.search(r"(\{.*\}|\[.*\])", raw, re.S)
    data = json.loads(m.group(1))
    ads = data.get("data", data)
    seen, out = set(), []
    for a in ads:
        adv = a.get("advertiser", {}) or {}
        c = a.get("content", {}) or {}
        mt = a.get("metrics", {}) or {}
        dom = c.get("landingPageDomain")
        if not dom or dom in seen:
            continue
        seen.add(dom)
        out.append({
            "name": adv.get("name"), "domain": dom,
            "landing_url": c.get("landingPageUrl"),
            "reach": mt.get("reach"), "live_ads": adv.get("liveAdsCount"),
            "total_reach": adv.get("totalReach"),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-search-ads")
    ap.add_argument("--domains")
    ap.add_argument("--save")
    a = ap.parse_args()
    if a.from_search_ads:
        advs = from_search_ads(a.from_search_ads)
    elif a.domains:
        advs = [{"name": d, "domain": d, "landing_url": ""} for d in a.domains.split(",")]
    else:
        ap.error("need --from-search-ads or --domains")

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        advs = list(ex.map(classify_one, advs))
    advs.sort(key=lambda x: x["dropship_score"], reverse=True)

    print(f"{'VERDICT':9} {'SCORE':5} {'PRODUCTS':8} {'NAME':22} {'DOMAIN':22} REASONS")
    for x in advs:
        print(f"{x['verdict']:9} {x['dropship_score']:<5} "
              f"{str(x['products_count']):8} {str(x['name'])[:22]:22} "
              f"{x['domain'][:22]:22} {','.join(x['reasons'])}")
    drops = [x for x in advs if x["verdict"] == "DROPSHIP"]
    print(f"\nDROPSHIPPERS (sourceable): {len(drops)}/{len(advs)}")
    if a.save:
        json.dump(drops, open(a.save, "w"), indent=2)
        print(f"saved -> {a.save}")


if __name__ == "__main__":
    main()
