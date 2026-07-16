#!/usr/bin/env python3
"""create_drafts.py — batch-create general-store DRAFT products from a spec JSON.

Mirrors the LIVE Nosura dog-cooling-mat schema exactly, with every CREATE default
baked in so there are ZERO re-do loops (the "half the speed" rule):
  - status DRAFT                         (publish gate: never auto-publish)
  - templateSuffix from spec ('generic') (one generic template + metafield content)
  - productType + vendor from spec
  - single 'Size' option
  - EVERY variant: taxable=false, inventoryPolicy=CONTINUE, tracked=true,
    inventoryQuantities = stock_per_variant at the spec location  (buffer >=1000)
  - .99 prices straight from spec (price-to-margin already applied)
  - plain-keyword tag(s) from spec
  - idempotent on handle via productSet(synchronous:true)

DATA-DRIVEN COPY + SEO (folds in what the per-SKU _finalize_<slug>.py scripts used
to do — those don't scale to 25-30 SKUs). If a spec sku carries `body_html`,
`seo_title`, `seo_description`, this sends them ATOMICALLY in the same productSet
call, with the silent-failure guards that those scripts were hitting one at a time:
  - PRE-FLIGHT assert len(seo_title) <= 95   (Shopify silently saves a longer
    seo.title as null with NO userError — measured: 93/96 save, 100 -> null)
  - PRE-FLIGHT assert no '$' / price token in any copy (price lives in Shopify only)
  - seo is sent as the FULL {title, description} object every time — ProductInput.seo
    is REPLACE-not-merge, so a title-only update would wipe seo.description to null
  - POST-CREATE assert the returned seo.title is non-null when one was sent

Channels are intentionally NOT set here: a DRAFT holds 0 channels and publish-on-draft
is a silent no-op; the store-default 4 channels auto-apply on flip -> ACTIVE. So we
verify channels at GO-LIVE, never at draft-create.

USAGE:
  python create_drafts.py <spec.json> --env <path-to-admin .env> [--only slug1,slug2] [--dry-run]
"""
from __future__ import annotations
import argparse, json, re, sys, time, urllib.request, urllib.error
from pathlib import Path

# Generous sanity bound only. EMPIRICAL: live seo.title saves fine up to 111 chars
# (measured on Nosura). An earlier one-off null at 100 chars was NOT reproducible by
# length, so length is NOT the real silent-null trigger — the authoritative guard is
# the POST-CREATE non-null check below (it catches a dropped title whatever the cause).
# This cap only rejects pathologically long input.
SEO_TITLE_MAX = 150


def load_env(p: Path) -> dict:
    cfg = {}
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def gql(url: str, token: str, q: str, v: dict) -> dict:
    body = json.dumps({"query": q, "variables": v}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("X-Shopify-Access-Token", token)
    req.add_header("Content-Type", "application/json")
    for a in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read())
            if out.get("errors") and any("THROTTLED" in str(e) for e in out["errors"]) and a < 4:
                time.sleep(2 ** a); continue
            return out
        except urllib.error.HTTPError as e:
            t = e.read().decode()
            print(f"  HTTP {e.code}: {t[:300]}")
            if e.code in (429, 502, 503, 504) and a < 4:
                time.sleep(2 ** a); continue
            raise


PBYHANDLE = "query($h:String!){ productByHandle(handle:$h){ id } }"

PSET = """
mutation Set($input: ProductSetInput!){
 productSet(synchronous:true, input:$input){
  product{ id handle title status templateSuffix vendor productType
   category{ fullName }
   totalInventory tags
   seo{ title description } descriptionHtml
   variants(first:20){ nodes{ title price taxable inventoryPolicy inventoryQuantity } } }
  userErrors{ field message code } } }"""


def copy_preflight(sku: dict, subject: str) -> tuple[str, str, str]:
    """Validate + return (body_html, seo_title, seo_description) for a sku, or
    ('','','') if the spec carries no copy. Raises ValueError on a guard breach so a
    bad listing is caught BEFORE the API call instead of as a silent null afterward."""
    body = (sku.get("body_html") or "").strip()
    st = (sku.get("seo_title") or "").strip()
    sd = (sku.get("seo_description") or "").strip()
    if not (body or st or sd):
        return ("", "", "")
    if st and len(st) > SEO_TITLE_MAX:
        raise ValueError(f"seo_title is {len(st)} chars (> {SEO_TITLE_MAX} sanity bound); "
                         f"that's implausibly long for a title — check the spec.")
    if (st or sd) and not (st and sd):
        raise ValueError("send BOTH seo_title and seo_description or neither — ProductInput.seo "
                         "is replace-not-merge, so a one-sided update wipes the other field.")
    blob = f"{st} {sd} {body}".lower()
    if "$" in blob or re.search(r"\b\d+(?:\.\d{2})?\s*(?:usd|dollars?)\b", blob) or "% off" in blob:
        raise ValueError("price/discount token found in copy — price lives in Shopify only.")
    if subject:
        # naive cross-subject token guard; subject_guard.py does the authoritative pass
        bad = {"cat": r"\bcats?\b", "dog": r"\bdogs?\b"}.get(
            "cat" if subject == "dog" else ("dog" if subject == "cat" else ""), "")
        if bad and re.search(bad, blob):
            raise ValueError(f"off-subject token for a {subject} product found in copy.")
    return (body, st, sd)


def build_variants(sku: dict, location_gid: str, stock: int) -> list[dict]:
    out = []
    for v in sku["variants"]:
        size = v["size"]
        # SKU = <product-slug>-<size>. The slug is unique per product, so this is
        # unique and category-correct. (Was hardcoded "DCM-" = dog-cooling-mat, which
        # mislabeled every other category and risked cross-category SKU collisions.)
        sku_code = f"{sku['slug'].upper()}-{size.split('(')[0].strip().replace(' ', '').replace('-', '')[:10]}"
        out.append({
            "optionValues": [{"optionName": "Size", "name": size}],
            "price": v["price"],
            "taxable": False,
            "inventoryPolicy": "CONTINUE",
            "inventoryItem": {
                "tracked": True,
                "sku": sku_code,
                "measurement": {"weight": {"value": 1.0, "unit": "POUNDS"}},
            },
            "inventoryQuantities": [
                {"locationId": location_gid, "name": "available", "quantity": stock}
            ],
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--env", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    cfg = load_env(Path(args.env))
    store = cfg["SHOPIFY_STORE"]; token = cfg["SHOPIFY_ADMIN_TOKEN"]
    ver = cfg.get("SHOPIFY_API_VERSION", "2025-01")
    url = f"https://{store}/admin/api/{ver}/graphql.json"

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    skus = [s for s in spec["skus"] if not only or s["slug"] in only]
    loc = spec["location_gid"]; stock = spec.get("stock_per_variant", 1000)

    # Shopify Standard Product Taxonomy node (drives the google_product_category
    # sent to the Google & YouTube feed + tax + cross-channel metafields). The
    # "category never null" rule is enforced here: a batch CANNOT be created
    # without it. Find a node GID with:
    #   { taxonomy { categories(first:8, search:"<term>"){ nodes{ id fullName } } } }
    category = spec.get("category_gid")
    if not category:
        print("ERROR: spec is missing 'category_gid' (Shopify taxonomy node GID).\n"
              "  Category drives the Google feed's google_product_category and must never be null.\n"
              "  Resolve one, e.g. dog cooling mat -> gid://shopify/TaxonomyCategory/ap-2-9-4 (Pet Beds > Cooling Beds).")
        return 1

    print(f"store={store}  template={spec['template_suffix']}  type={spec['product_type']}  "
          f"vendor={spec['vendor']}  skus={len(skus)}  stock/var={stock}  status={spec['status']}")

    subject = spec.get("subject", "")
    created = []
    failed = []
    for sku in skus:
        handle = f"{spec['handle_prefix']}-{sku['slug']}"
        try:
            body, st, sd = copy_preflight(sku, subject)
        except ValueError as e:
            print(f"  ✗ {handle}: COPY PREFLIGHT — {e}")
            failed.append(handle); continue
        inp = {
            "handle": handle,
            "title": sku["title"],
            "vendor": spec["vendor"],
            "productType": spec["product_type"],
            "category": category,
            "templateSuffix": spec["template_suffix"],
            "status": spec["status"],
            "tags": sku["tags"],
            "productOptions": [{"name": "Size", "values": [{"name": v["size"]} for v in sku["variants"]]}],
            "variants": build_variants(sku, loc, stock),
        }
        if body:
            inp["descriptionHtml"] = body
        if st and sd:  # always the FULL seo object (replace-not-merge safe)
            inp["seo"] = {"title": st, "description": sd}
        # True idempotency: productSet CREATES unless given an id, so a re-run on an
        # existing handle would fail HANDLE_NOT_UNIQUE. Look up the id first and pass
        # it through -> the same command safely create-OR-updates every time.
        if not args.dry_run:
            ex = gql(url, token, PBYHANDLE, {"h": handle})
            exist = (ex.get("data") or {}).get("productByHandle")
            if exist:
                inp["id"] = exist["id"]
        if args.dry_run:
            copytag = f"  copy={'Y' if body else '-'} seo={'Y' if st else '-'}"
            print(f"  [dry] {handle}  '{sku['title'][:55]}...'  "
                  f"variants={[ (v['size'], v['price']) for v in sku['variants'] ]}{copytag}")
            continue
        r = gql(url, token, PSET, {"input": inp})
        errs = (r.get("data", {}).get("productSet") or {}).get("userErrors") or r.get("errors")
        if errs:
            print(f"  ✗ {handle}: {errs}")
            failed.append(handle); continue
        pr = r["data"]["productSet"]["product"]
        ntax = sum(1 for v in pr["variants"]["nodes"] if v["taxable"] is False)
        cat = (pr.get("category") or {}).get("fullName", "∅")
        # POST-CREATE silent-null guard: if we sent a seo.title, it MUST come back non-null
        seo_ret = (pr.get("seo") or {}).get("title")
        if st and not seo_ret:
            print(f"  ✗ {handle}: seo.title came back NULL despite len={len(st)} — Shopify dropped it.")
            failed.append(handle); continue
        seo_tag = f"  seo.title={len(seo_ret or '')}c" if st else ""
        print(f"  ✓ {pr['handle']}  id={pr['id'].split('/')[-1]}  status={pr['status']}  "
              f"suffix={pr['templateSuffix']}  inv={pr['totalInventory']}  vars={len(pr['variants']['nodes'])}  "
              f"taxoff={ntax}/{len(pr['variants']['nodes'])}  cat={cat.split(' > ')[-1]}{seo_tag}")
        created.append(pr)
        time.sleep(0.6)  # gentle on the product-create rate limit

    print(f"\n=== created/updated {len(created)}/{len(skus)} DRAFTs"
          + (f"  ({len(failed)} FAILED: {', '.join(failed)})" if failed else "") + " ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
