#!/usr/bin/env python3
"""upload_gallery.py — upload a SKU's 6-image gallery to its Shopify product and
set the LIFESTYLE hero as every variant's featured image.

Gallery-order rule (binding): canonical media order =
  lifestyle -> benefits -> construction -> size-guide -> use-anywhere -> product-white (LAST).
The variant featured image = the LIFESTYLE hero, because that is the image Shopify
sends to the Google Shopping feed. So we upload the NN-prefixed files in order, then
append the hero media to every variant (first appended media = the variant's featured).

Resolves the product by handle (<handle_prefix>-<slug>). Parallel across SKUs.

USAGE:
  python upload_gallery.py <category-dir> --spec <spec.json> --env <admin .env> [--only a,b] [--dry-run]
"""
from __future__ import annotations
import argparse, io, json, mimetypes, os, sys, threading, time, urllib.error, urllib.request, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_PL = threading.Lock()
def log(m):
    with _PL: print(m, flush=True)


def load_env(p: Path) -> dict:
    cfg = {}
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1); cfg[k.strip()] = v.strip()
    return cfg


class Shop:
    def __init__(self, env: dict):
        self.store = env["SHOPIFY_STORE"]; self.token = env["SHOPIFY_ADMIN_TOKEN"]
        self.ver = env.get("SHOPIFY_API_VERSION", "2025-01")
        self.url = f"https://{self.store}/admin/api/{self.ver}/graphql.json"

    def gql(self, q: str, v: dict) -> dict:
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
                t = e.read().decode(); log(f"  HTTP {e.code}: {t[:200]}")
                if e.code in (429, 502, 503, 504) and a < 4:
                    time.sleep(2 ** a); continue
                raise

    def product_by_handle(self, handle: str) -> dict | None:
        q = """query($h:String!){ products(first:1, query:$h){ edges{ node{
              id title handle media(first:30){ nodes{ ... on MediaImage{ id } } }
              variants(first:30){ nodes{ id title } } } } } }"""
        r = self.gql(q, {"h": f"handle:{handle}"})
        edges = r["data"]["products"]["edges"]
        return edges[0]["node"] if edges else None

    def staged_upload(self, path: Path) -> str:
        fn = path.name; sz = str(path.stat().st_size)
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        q = """mutation($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){
              stagedTargets{url resourceUrl parameters{name value}} userErrors{field message}}}"""
        r = self.gql(q, {"input": [{"filename": fn, "mimeType": mime, "httpMethod": "POST",
                                    "resource": "IMAGE", "fileSize": sz}]})
        tgt = r["data"]["stagedUploadsCreate"]["stagedTargets"][0]
        boundary = "----nosura" + uuid.uuid4().hex
        body = io.BytesIO()
        def w(s): body.write(s if isinstance(s, bytes) else s.encode())
        for p in tgt["parameters"]:
            w(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{p['name']}\"\r\n\r\n{p['value']}\r\n")
        w(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fn}\"\r\n")
        w(f"Content-Type: {mime}\r\n\r\n"); w(path.read_bytes()); w(f"\r\n--{boundary}--\r\n")
        req = urllib.request.Request(tgt["url"], data=body.getvalue(), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=180) as resp:
            assert resp.status in (200, 201, 204), resp.status
        return tgt["resourceUrl"]

    def create_media(self, pid: str, source: str, alt: str) -> str | None:
        q = """mutation($p:ID!,$m:[CreateMediaInput!]!){ productCreateMedia(productId:$p,media:$m){
              media{... on MediaImage{ id }} mediaUserErrors{field message} } }"""
        r = self.gql(q, {"p": pid, "m": [{"originalSource": source, "mediaContentType": "IMAGE", "alt": alt}]})
        d = r["data"]["productCreateMedia"]
        if d["mediaUserErrors"]:
            log(f"    media err: {d['mediaUserErrors']}"); return None
        return d["media"][0]["id"] if d["media"] else None

    def append_hero_to_variants(self, pid: str, variant_ids: list[str], hero_media: str):
        q = """mutation($p:ID!,$vm:[ProductVariantAppendMediaInput!]!){
              productVariantAppendMedia(productId:$p, variantMedia:$vm){
                userErrors{field message} } }"""
        vm = [{"variantId": vid, "mediaIds": [hero_media]} for vid in variant_ids]
        r = self.gql(q, {"p": pid, "vm": vm})
        return r["data"]["productVariantAppendMedia"]["userErrors"]


def gallery_files(sku_dir: Path) -> list[Path]:
    return sorted((sku_dir / "images").glob("*.png")) + sorted((sku_dir / "images").glob("*.jpg"))


def do_sku(shop: Shop, cat: Path, prefix: str, slug: str, dry: bool,
           img_workers: int = 4) -> tuple[str, bool, str]:
    handle = f"{prefix}-{slug}"
    sku_dir = cat / f"sku-{slug}"
    files = gallery_files(sku_dir)
    files = [f for f in files if "-lifestyle" in f.name] + [f for f in files if "-lifestyle" not in f.name]
    # ^ ensure lifestyle (hero) is uploaded FIRST regardless of glob; rest keep NN order
    files = sorted(set(gallery_files(sku_dir)), key=lambda p: (0 if "-lifestyle" in p.name else 1, p.name))
    if len(files) < 6:
        return (handle, False, f"only {len(files)} gallery images")
    if dry:
        return (handle, True, f"dry: would upload {len(files)} -> {handle}, hero={files[0].name}")
    node = shop.product_by_handle(handle)
    if not node:
        return (handle, False, "product not found by handle")
    pid = node["id"]
    if node["media"]["nodes"]:
        return (handle, True, f"already has {len(node['media']['nodes'])} media — skipped")
    # Phase 1 — STAGE every file in PARALLEL. staged_upload's heavy part is the
    # S3-style multipart PUT to the bucket, which is NOT Admin-rate-limited, so the
    # slow per-file uploads overlap instead of running back-to-back. Results land in
    # an index-keyed list so gallery ORDER is preserved for phase 2.
    sources: list[str | None] = [None] * len(files)
    with ThreadPoolExecutor(max_workers=min(img_workers, len(files))) as ex:
        futs = {ex.submit(shop.staged_upload, f): i for i, f in enumerate(files)}
        for fu in as_completed(futs):
            sources[futs[fu]] = fu.result()
    # Phase 2 — REGISTER media SEQUENTIALLY in gallery order. productCreateMedia is
    # Admin-API rate-limited AND order-sensitive (this defines the gallery sequence),
    # so it stays serial; the staged sources are already uploaded, so this is just
    # the cheap registration calls.
    hero_media = None
    for i, src in enumerate(sources):
        mid = shop.create_media(pid, src, node["title"])
        if i == 0:
            hero_media = mid
        time.sleep(0.3)
    vids = [v["id"] for v in node["variants"]["nodes"]]
    if hero_media and vids:
        # media must be registered; brief settle then append hero to each variant
        time.sleep(1.5)
        errs = shop.append_hero_to_variants(pid, vids, hero_media)
        if errs:
            return (handle, True, f"images up; variant-hero warn: {errs}")
    return (handle, True, f"uploaded {len(files)} imgs; hero->{len(vids)} variants")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("category_dir")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=3, help="parallelism ACROSS SKUs")
    ap.add_argument("--img-workers", type=int, default=4,
                    help="parallel staged-uploads WITHIN one SKU (S3 PUT, not Admin-rate-limited)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    shop = Shop(load_env(Path(args.env)))
    cat = Path(args.category_dir).resolve()
    prefix = spec["handle_prefix"]
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    slugs = [s["slug"] for s in spec["skus"] if not only or s["slug"] in only]
    log(f"uploading galleries for {len(slugs)} SKUs "
        f"(workers={args.workers} x img-workers={args.img_workers})")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(do_sku, shop, cat, prefix, s, args.dry_run, args.img_workers): s
                for s in slugs}
        for f in as_completed(futs):
            h, ok, msg = f.result()
            log(f"  {'✓' if ok else '✗'} {h}: {msg}")
            results.append((h, ok))
    bad = [h for h, ok in results if not ok]
    log(f"\nDONE {sum(1 for _,ok in results if ok)}/{len(results)} ok" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
