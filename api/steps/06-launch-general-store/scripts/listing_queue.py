#!/usr/bin/env python3
"""
listing_queue.py — the general-store candidate/listing state machine.

The living artifact lives at  general-stores/<store>/listing-queue.json  and tracks every
keyword-category + its SKUs through the WORKFLOW §4 state machine:

    candidate -> keyword-clustered -> drafted -> live -> testing -> winner | killed

This is the ONLY supported way to read/mutate the queue (the /general-store-listing skill calls it).
Pure stdlib, no deps — matches the repo-native backbone (WORKFLOW D3).

Usage
-----
  PY=06-launch-general-store/scripts/listing_queue.py
  python $PY <store> init
  python $PY <store> add-category <keyword-slug> --keyword "dog cooling mat" --sv 49500 \
        --capture LIST-NOW --state keyword-clustered
  python $PY <store> add-sku <keyword-slug> <sku> --title "..." --cogs 5.40 --price 19.99
  python $PY <store> set <keyword-slug> <sku> --state drafted --product-id gid://... --price 19.99
  python $PY <store> show [<keyword-slug>]
  python $PY <store> list --state testing          # flat view of SKUs in a state (cull loop)

The queue path resolves relative to the repo root: general-stores/<store>/listing-queue.json
(override the base dir with $GENERAL_STORES_DIR).
"""
import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

VALID_SKU_STATES = [
    "candidate", "keyword-clustered", "drafted", "live", "testing", "winner", "killed",
]
VALID_CAPTURE = ["BREAKOUT", "LIST-NOW", "BUILD-AHEAD", "EVERGREEN", "SKIP"]


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _base_dir() -> Path:
    return Path(os.environ.get("GENERAL_STORES_DIR", "general-stores"))


def _queue_path(store: str) -> Path:
    return _base_dir() / store / "listing-queue.json"


def _load(store: str) -> dict:
    p = _queue_path(store)
    if not p.exists():
        return {"store": store, "created": _now(), "updated": _now(), "categories": {}}
    with p.open() as fh:
        return json.load(fh)


def _save(store: str, data: dict) -> Path:
    data["updated"] = _now()
    p = _queue_path(store)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return p


def _get_category(data: dict, slug: str) -> dict:
    cat = data["categories"].get(slug)
    if cat is None:
        sys.exit(f"❌ category '{slug}' not in queue. Add it with: add-category {slug} --keyword ...")
    return cat


# ---------------------------------------------------------------- commands
def cmd_init(args):
    data = _load(args.store)
    p = _save(args.store, data)
    print(f"✓ queue ready at {p}")


def cmd_add_category(args):
    data = _load(args.store)
    if args.capture and args.capture not in VALID_CAPTURE:
        sys.exit(f"❌ --capture must be one of {VALID_CAPTURE}")
    if args.state and args.state not in VALID_SKU_STATES:
        sys.exit(f"❌ --state must be one of {VALID_SKU_STATES}")
    cat = data["categories"].setdefault(args.slug, {})
    cat.update({
        "keyword": args.keyword or cat.get("keyword", args.slug.replace("-", " ")),
        "sv": args.sv if args.sv is not None else cat.get("sv"),
        "capture_bucket": args.capture or cat.get("capture_bucket"),
        "state": args.state or cat.get("state", "keyword-clustered"),
        "recon": args.recon or cat.get("recon", f"general-stores/{args.store}/{args.slug}/recon.md"),
        "skus": cat.get("skus", {}),
    })
    cat.setdefault("created", _now())
    p = _save(args.store, data)
    print(f"✓ category '{args.slug}' (SV {cat.get('sv')}, {cat.get('capture_bucket')}) -> {p}")


def cmd_add_sku(args):
    data = _load(args.store)
    cat = _get_category(data, args.slug)
    sku = cat["skus"].setdefault(args.sku, {})
    sku.update({
        "title": args.title or sku.get("title"),
        "cogs": args.cogs if args.cogs is not None else sku.get("cogs"),
        "price": args.price if args.price is not None else sku.get("price"),
        "state": sku.get("state", "candidate"),
    })
    # Research REFERENCE from the found product (AliExpress / Temu / 1688 / Amazon) so the listing
    # step has the supplier ref + image + demand. `price` here is the marketplace reference price;
    # the FINAL listing price + landed COGS come from the research source later (not this number).
    for _k in ("url", "image", "source", "sold"):
        _v = getattr(args, _k, None)
        if _v is not None:
            sku[_k] = _v
    sku.setdefault("created", _now())
    p = _save(args.store, data)
    print(f"✓ sku '{args.slug}/{args.sku}' state={sku['state']} -> {p}")


def cmd_set(args):
    data = _load(args.store)
    cat = _get_category(data, args.slug)
    sku = cat["skus"].get(args.sku)
    if sku is None:
        sku = cat["skus"].setdefault(args.sku, {"created": _now(), "state": "candidate"})
    if args.state:
        if args.state not in VALID_SKU_STATES:
            sys.exit(f"❌ --state must be one of {VALID_SKU_STATES}")
        sku["state"] = args.state
    if args.product_id:
        sku["product_id"] = args.product_id
    if args.price is not None:
        sku["price"] = args.price
    if args.sv is not None:
        cat["sv"] = args.sv
    if args.note:
        sku.setdefault("notes", []).append(f"{_now()}: {args.note}")
    sku["updated"] = _now()
    p = _save(args.store, data)
    print(f"✓ {args.slug}/{args.sku} -> state={sku.get('state')} product={sku.get('product_id','-')} ({p})")


def cmd_remove_category(args):
    """Drop a keyword-category (and its SKUs) from the queue — undoes a mis-added candidate.
    Removes only the queue entry; any on-disk build directory is left untouched."""
    data = _load(args.store)
    if args.slug not in data.get("categories", {}):
        sys.exit(f"❌ category '{args.slug}' not in queue")
    n = len(data["categories"][args.slug].get("skus", {}))
    data["categories"].pop(args.slug, None)
    p = _save(args.store, data)
    print(f"✓ removed category '{args.slug}' ({n} sku(s)) -> {p}")


def cmd_remove_sku(args):
    """Drop a single SKU from a category — undoes a mis-added found product."""
    data = _load(args.store)
    cat = _get_category(data, args.slug)
    if args.sku not in cat.get("skus", {}):
        sys.exit(f"❌ sku '{args.slug}/{args.sku}' not in queue")
    cat["skus"].pop(args.sku, None)
    p = _save(args.store, data)
    print(f"✓ removed sku '{args.slug}/{args.sku}' -> {p}")


def cmd_show(args):
    data = _load(args.store)
    cats = data["categories"]
    if not cats:
        print(f"(queue empty for store '{args.store}')")
        return
    slugs = [args.slug] if args.slug else sorted(cats)
    for slug in slugs:
        cat = cats.get(slug)
        if cat is None:
            print(f"❌ no category '{slug}'")
            continue
        skus = cat.get("skus", {})
        counts = {}
        for s in skus.values():
            counts[s.get("state", "candidate")] = counts.get(s.get("state", "candidate"), 0) + 1
        summary = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "no skus"
        print(f"\n■ {slug}  SV={cat.get('sv')}  {cat.get('capture_bucket')}  cat-state={cat.get('state')}")
        print(f"  {len(skus)} skus  [{summary}]")
        for sk, s in sorted(skus.items()):
            pid = s.get("product_id", "-")
            price = s.get("price") or "?"
            print(f"    - {sk:<28} {s.get('state','candidate'):<16} ${str(price):<8} {pid}")


def cmd_list(args):
    """Flat view of SKUs filtered by state — drives the cull loop / go-live batches."""
    data = _load(args.store)
    rows = []
    for slug, cat in data["categories"].items():
        for sk, s in cat.get("skus", {}).items():
            if args.state and s.get("state") != args.state:
                continue
            rows.append((slug, sk, s.get("state", "candidate"), s.get("price") or "?",
                         s.get("product_id", "-")))
    if not rows:
        print(f"(no SKUs{' in state ' + args.state if args.state else ''})")
        return
    for slug, sk, st, price, pid in sorted(rows):
        print(f"{slug}/{sk}\t{st}\t${price}\t{pid}")


def main():
    ap = argparse.ArgumentParser(description="general-store listing queue state machine")
    ap.add_argument("store", help="store key (e.g. nosura) — also the general-stores/<store>/ dir")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    a = sub.add_parser("add-category")
    a.add_argument("slug")
    a.add_argument("--keyword")
    a.add_argument("--sv", type=int)
    a.add_argument("--capture", help=f"one of {VALID_CAPTURE}")
    a.add_argument("--state", help=f"one of {VALID_SKU_STATES}")
    a.add_argument("--recon")
    a.set_defaults(func=cmd_add_category)

    a = sub.add_parser("add-sku")
    a.add_argument("slug")
    a.add_argument("sku")
    a.add_argument("--title")
    a.add_argument("--cogs", type=float)
    a.add_argument("--price", type=float)
    a.add_argument("--url", help="research-ref product URL (AliExpress/Temu/1688/Amazon)")
    a.add_argument("--image", help="research-ref product image URL")
    a.add_argument("--source", help="which finder: aliexpress / temu / 1688 / amazon")
    a.add_argument("--sold", type=int, help="research-ref demand signal (sold/bought count)")
    a.set_defaults(func=cmd_add_sku)

    a = sub.add_parser("set")
    a.add_argument("slug")
    a.add_argument("sku")
    a.add_argument("--state", help=f"one of {VALID_SKU_STATES}")
    a.add_argument("--product-id")
    a.add_argument("--price", type=float)
    a.add_argument("--sv", type=int)
    a.add_argument("--note")
    a.set_defaults(func=cmd_set)

    a = sub.add_parser("remove-category")
    a.add_argument("slug")
    a.set_defaults(func=cmd_remove_category)

    a = sub.add_parser("remove-sku")
    a.add_argument("slug")
    a.add_argument("sku")
    a.set_defaults(func=cmd_remove_sku)

    a = sub.add_parser("show")
    a.add_argument("slug", nargs="?")
    a.set_defaults(func=cmd_show)

    a = sub.add_parser("list")
    a.add_argument("--state", help=f"filter: one of {VALID_SKU_STATES}")
    a.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
