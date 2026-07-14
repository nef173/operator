#!/usr/bin/env python3
"""verify_listings.py — final pre-go-live audit for the dog-cooling-mat batch.
Checks every SKU in the spec: status DRAFT, variant count + sizes + prices match
spec, descriptionHtml present (300+ chars), seo title/description set, media count,
and that every variant has a featured image. Reports PASS/WARN/FAIL per SKU.
Read-only.

USAGE: python verify_listings.py --spec <spec.json> --env <admin .env>
"""
from __future__ import annotations
import argparse, json, re, time, urllib.error, urllib.request
from pathlib import Path


def load_env(p: Path) -> dict:
    cfg = {}
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1); cfg[k.strip()] = v.strip()
    return cfg


class Shop:
    def __init__(self, env):
        self.url = f"https://{env['SHOPIFY_STORE']}/admin/api/{env.get('SHOPIFY_API_VERSION','2025-01')}/graphql.json"
        self.token = env["SHOPIFY_ADMIN_TOKEN"]

    def gql(self, q, v):
        body = json.dumps({"query": q, "variables": v}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("X-Shopify-Access-Token", self.token)
        req.add_header("Content-Type", "application/json")
        for a in range(1, 5):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    out = json.loads(r.read())
                if out.get("errors") and any("THROTTLED" in str(e) for e in out["errors"]) and a < 4:
                    time.sleep(2 ** a); continue
                return out
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and a < 4:
                    time.sleep(2 ** a); continue
                raise

    def product(self, handle):
        q = """query($h:String!){ products(first:1, query:$h){ edges{ node{
              id title handle status descriptionHtml
              category{ fullName }
              seo{ title description }
              media(first:50){ nodes{ ... on MediaImage{ id } } }
              variants(first:50){ nodes{ id title price taxable inventoryPolicy
                inventoryItem{ tracked }
                image{ id } } } } } } }"""
        e = self.gql(q, {"h": f"handle:{handle}"})["data"]["products"]["edges"]
        return e[0]["node"] if e else None


BRAND_RE = re.compile(r"nosura", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--env", required=True)
    args = ap.parse_args()
    spec = json.loads(Path(args.spec).read_text())
    shop = Shop(load_env(Path(args.env)))
    prefix = spec["handle_prefix"]
    overall_ok = True
    for sku in spec["skus"]:
        slug = sku["slug"]; handle = f"{prefix}-{slug}"
        exp_sizes = [v["size"] for v in sku["variants"]]
        exp_prices = {v["size"]: v["price"] for v in sku["variants"]}
        node = shop.product(handle)
        issues = []
        if not node:
            print(f"  FAIL {handle}: not found"); overall_ok = False; continue
        if node["status"] != "DRAFT":
            issues.append(f"status={node['status']} (want DRAFT)")
        vs = node["variants"]["nodes"]
        got_sizes = [v["title"] for v in vs]
        if sorted(got_sizes) != sorted(exp_sizes):
            issues.append(f"variants {got_sizes} != spec {exp_sizes}")
        for v in vs:
            ep = exp_prices.get(v["title"])
            if ep and v["price"] != ep:
                issues.append(f"{v['title']} price {v['price']}!={ep}")
            if v["taxable"]:
                issues.append(f"{v['title']} taxable=true")
            if v["inventoryPolicy"] != "CONTINUE":
                issues.append(f"{v['title']} invPolicy={v['inventoryPolicy']}")
            if not v["image"]:
                issues.append(f"{v['title']} NO featured image")
        desc = node["descriptionHtml"] or ""
        if len(desc) < 300:
            issues.append(f"descriptionHtml {len(desc)} chars (<300)")
        if BRAND_RE.search(desc):
            issues.append("BRAND name in descriptionHtml")
        if not ((node.get("category") or {}).get("fullName") or "").strip():
            issues.append("category NULL (no google_product_category to the feed)")
        seo = node.get("seo") or {}
        if not (seo.get("title") or "").strip():
            issues.append("seo.title empty")
        if not (seo.get("description") or "").strip():
            issues.append("seo.description empty")
        mcount = len(node["media"]["nodes"])
        if mcount < 6:
            issues.append(f"media={mcount} (<6)")
        status = "PASS" if not issues else "FAIL"
        if issues:
            overall_ok = False
        print(f"  {status} {handle}: {len(vs)}var, {mcount}img, desc {len(desc)}c"
              + ("" if not issues else "\n      - " + "\n      - ".join(issues)))
    print("\n" + ("ALL PASS — batch ready, awaiting operator go-live" if overall_ok else "ISSUES ABOVE — fix before go-live"))


if __name__ == "__main__":
    main()
