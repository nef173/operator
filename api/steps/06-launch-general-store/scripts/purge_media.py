#!/usr/bin/env python3
"""purge_media.py — delete ALL media on the given product handles so upload_gallery.py
(which skips products that already have media) can re-upload the corrected gallery in
canonical order. DRAFT products only; does not touch variants/copy/status.

USAGE: python purge_media.py --env <admin .env> --handles dog-cooling-mat-gel,...
"""
from __future__ import annotations
import argparse, json, time, urllib.error, urllib.request
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
              id status media(first:50){ nodes{ ... on MediaImage{ id } } } } } } }"""
        e = self.gql(q, {"h": f"handle:{handle}"})["data"]["products"]["edges"]
        return e[0]["node"] if e else None

    def delete_media(self, pid, media_ids):
        q = """mutation($p:ID!,$ids:[ID!]!){ productDeleteMedia(productId:$p, mediaIds:$ids){
              deletedMediaIds mediaUserErrors{field message} } }"""
        r = self.gql(q, {"p": pid, "ids": media_ids})
        return r["data"]["productDeleteMedia"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--handles", required=True)
    args = ap.parse_args()
    shop = Shop(load_env(Path(args.env)))
    for h in [x.strip() for x in args.handles.split(",") if x.strip()]:
        node = shop.product(h)
        if not node:
            print(f"  ✗ {h}: not found"); continue
        ids = [m["id"] for m in node["media"]["nodes"]]
        if not ids:
            print(f"  - {h}: already 0 media (status {node['status']})"); continue
        res = shop.delete_media(node["id"], ids)
        errs = res["mediaUserErrors"]
        print(f"  ✓ {h}: deleted {len(res['deletedMediaIds'])}/{len(ids)} media (status {node['status']})"
              + (f"  ERR {errs}" if errs else ""))


if __name__ == "__main__":
    main()
