#!/usr/bin/env python3
"""
catalog_scan.py — "is it already on the store?" dedup gate, run BEFORE 1688 sourcing.

Step 0 of the china-source-match pipeline. After Product Research picks a product but
BEFORE we spend a sourcing call on Alibaba/1688, this scans the OWN store's live catalog
and decides — per researched product — whether the store already sells it. Skipping a
re-source of a product we already list saves the sourcing budget and prevents duplicate
SKUs in the catalog.

Identity is judged the SAME background-invariant way match_china.py judges a 1688 match:
the VLM compares the FULL galleries (researched product vs catalog product) and is told
to "ignore background/lighting/angle/branding-sticker differences" — because the same
physical product is often shot on a white background by the supplier and in a lifestyle
scene on our store. Text/title similarity is only a cheap PRE-FILTER to shortlist which
catalog products are even worth a pixel comparison; the verdict is made on the images.

Two subcommands:

  index  — paginate the store's Admin GraphQL catalog into a cached index (handle, title,
           price, hero + gallery image URLs). Run this once per store (refresh when the
           catalog changes).
             python catalog_scan.py index --store nosura --out catalog-index-nosura.json

  check  — for each researched product (title + image url[s]), text-prefilter the index to
           the closest catalog products, then VLM-confirm identity on the images. Emits a
           verdict per product: ALREADY_LISTED | NEW | UNCERTAIN + the matched store handle
           and the store's current price.
             python catalog_scan.py check --store nosura --in researched.json \
                 --index catalog-index-nosura.json --judge openrouter \
                 --min-conf 0.82 --out catalog-match-nosura.json

INPUT for `check` (researched.json — a list, the shape Product Research / the operator hands off):
  [ {"name":"...", "images":["https://.../hero.jpg", ...]   # or "image":"https://..."
     , "url":"https://...", "source":"aliexpress|temu|meta|amazon|google", "slug":"..."}, ... ]

OUTPUT (catalog-match-<store>.json):
  { "store","generated_at","index_count",
    "results":[ {"subject","verdict","confidence","matched_handle","matched_title",
                 "store_price","store_currency","store_url","n_checked",
                 "candidates":[ ...per-catalog-product verdicts... ]} ],
    "totals":{"checked","already_listed","new","uncertain"} }

JUDGE MODES (same convention as match_china.py):
  --judge openrouter  auto, needs a vision API key (Gemini direct / OpenRouter fallback)
  --judge agent       writes packet dirs for the Claude Code agent to eyeball (no key)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

# This script lives next to match_china.py; reuse its background-invariant JUDGE_PROMPT,
# image downloader, and shared Gemini/vision transport (single source of truth for the
# "ignore background" rule). store_config (the multi-store registry resolver) lives in
# 05-launch-niche-store/scripts.
_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "scripts"))
import match_china as mc  # noqa: E402  (brings in gemini_client as mc.gem, JUDGE_PROMPT, _dl, slugify)

try:
    import store_config  # noqa: E402
except Exception:  # pragma: no cover - resolver optional if env vars are supplied directly
    store_config = None

# Catalog dedup reuses match_china's strict gallery-vs-gallery identity judge verbatim, so
# the "same physical product, ignore background/lighting/angle" rule has ONE definition.
# We only remap its verdict vocabulary to the catalog question:
#   IDENTICAL  -> ALREADY_LISTED   (the store already sells this product)
#   DIFFERENT  -> NEW              (not in the catalog; safe to source)
#   UNCERTAIN  -> UNCERTAIN        (needs a human glance)
_VERDICT_MAP = {"IDENTICAL": "ALREADY_LISTED", "DIFFERENT": "NEW", "UNCERTAIN": "UNCERTAIN"}
_OUT_VERDICTS = ("ALREADY_LISTED", "NEW", "UNCERTAIN")

_STOP = {
    "the", "a", "an", "and", "or", "for", "with", "of", "to", "in", "on", "by", "from",
    "set", "pack", "pcs", "pc", "new", "hot", "sale", "best", "premium", "quality", "free",
    "size", "color", "style", "kit", "pro", "plus", "mini", "large", "small", "big",
}


def _tokens(s: str) -> set[str]:
    """Lowercase alnum tokens, stopwords + very short tokens dropped — for the title prefilter."""
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2 and t not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ------------------------------------------------------------------ Admin GraphQL catalog index

class Shop:
    """Minimal Admin GraphQL client (same pattern as subject_guard.py / verify_listing.py)."""

    def __init__(self, store: str, api_version: str = "2025-01"):
        if store_config is None:
            raise SystemExit("store_config resolver not importable; cannot resolve store token")
        cfg = store_config.resolve(store)
        if not cfg.get("token"):
            raise SystemExit(
                f"store '{store}' has no admin token in its env_file — cannot read the catalog"
            )
        self.myshopify = cfg["myshopify"]
        self.api_version = cfg.get("api_version", api_version)
        self.url = f"https://{self.myshopify}/admin/api/{self.api_version}/graphql.json"
        self.token = cfg["token"]

    def gql(self, q: str, v: dict) -> dict:
        body = json.dumps({"query": q, "variables": v}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("X-Shopify-Access-Token", self.token)
        req.add_header("Content-Type", "application/json")
        for a in range(1, 6):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    out = json.loads(r.read())
                if out.get("errors") and any("THROTTLED" in str(e) for e in out["errors"]) and a < 5:
                    time.sleep(2 ** a)
                    continue
                return out
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and a < 5:
                    time.sleep(2 ** a)
                    continue
                raise
        return {}


_CATALOG_Q = """
query($cursor:String){
  products(first:200, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    edges{ node{
      id title handle status
      featuredImage{ url }
      priceRangeV2{ minVariantPrice{ amount currencyCode } }
      media(first:6){ nodes{ ... on MediaImage{ image{ url } } } } } } } }
"""


def build_index(store: str) -> dict:
    """Paginate the whole store catalog into {handle,title,status,price,hero,images[]} rows."""
    shop = Shop(store)
    products: list[dict] = []
    cursor = None
    while True:
        data = shop.gql(_CATALOG_Q, {"cursor": cursor})
        block = (data.get("data") or {}).get("products") or {}
        for edge in block.get("edges", []):
            n = edge["node"]
            price_obj = ((n.get("priceRangeV2") or {}).get("minVariantPrice") or {})
            hero = (n.get("featuredImage") or {}).get("url")
            gallery = [
                m["image"]["url"]
                for m in ((n.get("media") or {}).get("nodes") or [])
                if m and m.get("image", {}).get("url")
            ]
            # hero first, then the rest of the gallery (deduped, capped at 6)
            imgs: list[str] = []
            for u in ([hero] + gallery):
                if u and u not in imgs:
                    imgs.append(u)
                if len(imgs) >= 6:
                    break
            products.append({
                "id": n["id"],
                "handle": n.get("handle"),
                "title": n.get("title"),
                "status": (n.get("status") or "").lower(),
                "price": price_obj.get("amount"),
                "currency": price_obj.get("currencyCode"),
                "hero": hero,
                "images": imgs,
                "url": f"https://{shop.myshopify}/products/{n.get('handle')}",
            })
        page = block.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
            continue
        break
    return {
        "store": store,
        "myshopify": shop.myshopify,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(products),
        "products": products,
    }


# ------------------------------------------------------------------ check (text prefilter + VLM)

def _researched_images(prod: dict) -> list[str]:
    imgs = prod.get("images") or ([prod["image"]] if prod.get("image") else [])
    return [u for u in imgs if u][:4]


def _judge_pair(query_imgs: list[str], cat_imgs: list[str], cat: dict,
                packdir: pathlib.Path, model: str | None) -> dict:
    """Download both galleries and run the background-invariant VLM identity judge.

    Returns a remapped verdict dict, or None if no vision key is resolvable (agent mode)."""
    qpaths, cpaths = [], []
    qdir = packdir / "query"
    cdir = packdir / f"cat_{cat.get('handle', 'x')}"
    qdir.mkdir(parents=True, exist_ok=True)
    cdir.mkdir(parents=True, exist_ok=True)
    for i, u in enumerate(query_imgs):
        if mc._dl(u, qdir / f"q{i + 1}.jpg"):
            qpaths.append(str(qdir / f"q{i + 1}.jpg"))
    for i, u in enumerate(cat_imgs):
        if mc._dl(u, cdir / f"c{i + 1}.jpg"):
            cpaths.append(str(cdir / f"c{i + 1}.jpg"))
    if not qpaths or not cpaths:
        return {"verdict": "UNCERTAIN", "confidence": 0, "differences": ["missing images"]}

    ctx = (
        f"CATALOG PRODUCT already on the store — handle={cat.get('handle')} "
        f"title={cat.get('title', '')}\n"
        "Decide if the FIRST gallery (a product we just researched and might add) is the "
        "IDENTICAL physical product as this SECOND gallery (a product the store already "
        "sells). Same form factor / parts / materials = IDENTICAL.\nGALLERY:"
    )
    parts = [("text", mc.JUDGE_PROMPT), ("text", "PRODUCT WE WANT TO SOURCE:")]
    parts += [("image", p) for p in qpaths]
    parts += [("text", ctx)]
    parts += [("image", p) for p in cpaths]
    try:
        txt = mc.gem.vision(parts, model=model)
        if txt is None:
            return None  # no key -> caller uses agent mode
        v = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception as e:  # noqa: BLE001
        v = {"verdict": "UNCERTAIN", "confidence": 0, "error": str(e)}
    raw = str(v.get("verdict", "UNCERTAIN")).upper()
    return {
        "handle": cat.get("handle"),
        "title": cat.get("title"),
        "store_price": cat.get("price"),
        "store_currency": cat.get("currency"),
        "store_url": cat.get("url"),
        "verdict": _VERDICT_MAP.get(raw, "UNCERTAIN"),
        "confidence": v.get("confidence", 0),
        "differences": v.get("differences", []),
    }


def _recommend(verdict: str, n_already: int, cap: int) -> str:
    """Fork an ALREADY_LISTED verdict into an action, honoring the 2-3x same-product cap.

    The catalog gate is not a flat skip: the same physical product may live in the catalog
    up to `cap` times as a DIFFERENTIATED A/B variant (different image / price tier / intent
    title). The deciding factor is how many copies already exist:
      NEW          -> SOURCE                  (not in catalog, source it)
      UNCERTAIN    -> REVIEW                   (human eyeball)
      ALREADY x<cap-> ADD_VARIANT_OR_OPTIMIZE  (room for one differentiated copy, OR sharpen the live one)
      ALREADY x>=cap-> OPTIMIZE_EXISTING       (cap reached — don't clone; cut price / better hero+title)
    """
    if verdict == "NEW":
        return "SOURCE"
    if verdict == "UNCERTAIN":
        return "REVIEW"
    return "OPTIMIZE_EXISTING" if n_already >= cap else "ADD_VARIANT_OR_OPTIMIZE"


def check(store: str, researched: list[dict], index: dict, *, judge: str, model: str | None,
          top_k: int, min_conf: float, min_text: float, cap: int, packdir: pathlib.Path) -> dict:
    cat_products = index.get("products", [])
    cat_tokens = [(_tokens(c.get("title", "")), c) for c in cat_products]
    results: list[dict] = []
    no_key = False

    for prod in researched:
        name = prod.get("name") or prod.get("title") or prod.get("slug") or "product"
        slug = prod.get("slug") or mc.slugify(name)
        qtok = _tokens(name)
        query_imgs = _researched_images(prod)

        # 1) cheap title prefilter -> shortlist the catalog products worth a pixel comparison.
        scored = sorted(
            ((_jaccard(qtok, ct), c) for ct, c in cat_tokens),
            key=lambda x: x[0], reverse=True,
        )
        shortlist = [c for s, c in scored if s >= min_text][:top_k]
        # always keep at least the single best-text match, so a low-overlap title (different
        # wording for the same product) still gets ONE image check.
        if not shortlist and scored:
            shortlist = [scored[0][1]]

        cand_verdicts: list[dict] = []
        if judge == "openrouter":
            pdir = packdir / slug
            for c in shortlist:
                v = _judge_pair(query_imgs, c.get("images", []), c, pdir, model)
                if v is None:
                    no_key = True
                    break
                cand_verdicts.append(v)

        # roll the per-catalog-product verdicts up into ONE answer for this researched product.
        idents = [v for v in cand_verdicts if v["verdict"] == "ALREADY_LISTED" and (v.get("confidence") or 0) >= min_conf]
        uncert = [v for v in cand_verdicts if v["verdict"] == "UNCERTAIN" or (v["verdict"] == "ALREADY_LISTED" and (v.get("confidence") or 0) < min_conf)]
        if idents:
            best = max(idents, key=lambda v: v.get("confidence") or 0)
            verdict = "ALREADY_LISTED"
        elif uncert:
            best = max(uncert, key=lambda v: v.get("confidence") or 0)
            verdict = "UNCERTAIN"
        elif judge == "agent" or no_key:
            best = {}
            verdict = "UNCERTAIN"  # not auto-judged; agent/operator must eyeball the packet
        else:
            best = {}
            verdict = "NEW"

        n_already = len(idents)  # how many DIFFERENT catalog listings are this same product
        results.append({
            "subject": name,
            "slug": slug,
            "source": prod.get("source"),
            "url": prod.get("url"),
            "verdict": verdict,
            "confidence": best.get("confidence"),
            "matched_handle": best.get("handle"),
            "matched_title": best.get("title"),
            "store_price": best.get("store_price"),
            "store_currency": best.get("store_currency"),
            "store_url": best.get("store_url"),
            "n_checked": len(cand_verdicts),
            "n_already_listed": n_already,
            "cap": cap,
            "recommended_action": _recommend(verdict, n_already, cap),
            "already_listed_matches": [
                {"handle": v.get("handle"), "title": v.get("title"),
                 "store_price": v.get("store_price"), "store_currency": v.get("store_currency"),
                 "store_url": v.get("store_url"), "confidence": v.get("confidence")}
                for v in idents
            ],
            "shortlist": [c.get("handle") for c in shortlist],
            "candidates": cand_verdicts,
        })

    totals = {v: sum(1 for r in results if r["verdict"] == v) for v in _OUT_VERDICTS}
    out = {
        "store": store,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "index_count": len(cat_products),
        "judge": judge,
        "no_vision_key": no_key,
        "results": results,
        "totals": {"checked": len(results), "already_listed": totals["ALREADY_LISTED"],
                   "new": totals["NEW"], "uncertain": totals["UNCERTAIN"]},
    }
    return out


# ------------------------------------------------------------------ cli

def _cmd_index(args) -> None:
    idx = build_index(args.store)
    pathlib.Path(args.out).write_text(json.dumps(idx, indent=2))
    print(f"indexed {idx['count']} products from {idx['myshopify']} -> {args.out}")


def _cmd_check(args) -> None:
    index = json.loads(pathlib.Path(args.index).read_text())
    researched = json.loads(pathlib.Path(args.infile).read_text())
    if isinstance(researched, dict):
        researched = researched.get("results") or researched.get("products") or [researched]
    out = check(
        args.store, researched, index,
        judge=args.judge, model=args.model, top_k=args.top_k,
        min_conf=args.min_conf, min_text=args.min_text, cap=args.cap,
        packdir=pathlib.Path(args.packdir),
    )
    pathlib.Path(args.out).write_text(json.dumps(out, indent=2))
    t = out["totals"]
    print(f"checked {t['checked']}: {t['already_listed']} already-listed, "
          f"{t['new']} new, {t['uncertain']} uncertain -> {args.out}")
    if out["no_vision_key"]:
        print("  (no vision key resolved — ran in shortlist-only mode; use --judge agent "
              "or set a Gemini/OpenRouter key for image confirmation)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Catalog dedup gate — is the researched product already on the store?")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="cache the store's full catalog (handle/title/price/images)")
    pi.add_argument("--store", required=True, help="store key in the registry (e.g. nosura)")
    pi.add_argument("--out", default="catalog-index.json")
    pi.set_defaults(func=_cmd_index)

    pc = sub.add_parser("check", help="dedup researched products against the catalog index")
    pc.add_argument("--store", required=True)
    pc.add_argument("--in", dest="infile", required=True, help="researched-products json (list)")
    pc.add_argument("--index", required=True, help="catalog-index json from `index`")
    pc.add_argument("--judge", choices=["agent", "openrouter"], default="openrouter")
    pc.add_argument("--model", default=None)
    pc.add_argument("--top-k", type=int, default=5, help="catalog products to image-check per item")
    pc.add_argument("--min-conf", type=float, default=0.82, help="confidence to call ALREADY_LISTED")
    pc.add_argument("--cap", type=int, default=2,
                    help="max copies of the same physical product allowed in the catalog "
                         "(>=cap -> OPTIMIZE_EXISTING; <cap -> ADD_VARIANT_OR_OPTIMIZE)")
    pc.add_argument("--min-text", type=float, default=0.18, help="title-Jaccard floor for the prefilter")
    pc.add_argument("--packdir", default="./_catalog_packets")
    pc.add_argument("--out", default="catalog-match.json")
    pc.set_defaults(func=_cmd_check)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
