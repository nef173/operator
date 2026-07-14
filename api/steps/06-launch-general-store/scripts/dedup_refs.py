#!/usr/bin/env python3
"""dedup_refs.py — perceptual-hash (dHash) PRE-PASS that narrows the manual
vision-dedup of a SKU batch's supplier reference photos.

THE FIND-PHASE BOTTLENECK this attacks: D11 caps "same physical product" at <=2-3
SKUs (re-source beyond that — two identical ice-silk SKUs is wasted auction tickets,
not breadth). The authoritative check is a human eyeballing every SKU's supplier
photos, which is ~O(N^2) by eye across 25-50 SKUs. This pre-pass computes a cheap
perceptual hash of every `_supplier-refs/` image, clusters SKUs whose photos are
near-identical, and hands the human a SHORT list of clusters to confirm instead of
the whole matrix. It does NOT delete or auto-merge anything — the vision pass stays
authoritative (text/silhouette baked inside an image is invisible to a hash); this
only tells you WHERE to look.

It sits between fetch_refs.py (downloads the refs) and gen_gallery.py (renders the
batch): parse -> candidate_queue -> deep-pull -> fetch_refs -> THIS -> gen_gallery.

METHOD: dHash (difference hash). Each image -> 9x8 grayscale -> compare adjacent
pixels -> 64-bit fingerprint. Two images are "near-duplicate" when the Hamming
distance between their hashes is <= --threshold (default 8 of 64 bits; lower =
stricter). Two SKUs are linked when ANY cross-SKU image pair is near-duplicate (one
reused product photo is the signal). Linked SKUs are unioned into clusters; a cluster
larger than --max-per-product is a D11 VIOLATION (exit 1 so a pipeline can gate on it).

USAGE:
  python dedup_refs.py <category_dir> [--threshold 8] [--max-per-product 3]
      [--workers 8] [--only slug1,slug2] [--json out.json] [--quiet]

  <category_dir> holds sku-<slug>/_supplier-refs/*.{jpg,png,webp}
  Default --json is <category_dir>/_dedup-report.json
"""
from __future__ import annotations
import argparse, json, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("ERROR: Pillow is required (pip install Pillow).")

EXTS = (".jpg", ".jpeg", ".png", ".webp")


def dhash(path: Path, size: int = 8) -> int | None:
    """64-bit horizontal difference hash. None if the image can't be read."""
    try:
        with Image.open(path) as im:
            im = im.convert("L").resize((size + 1, size), Image.LANCZOS)
            px = list(im.tobytes())  # mode "L" -> one byte per pixel, row-major
    except Exception:
        return None
    w = size + 1
    bits = 0
    for row in range(size):
        base = row * w
        for col in range(size):
            bits = (bits << 1) | int(px[base + col] < px[base + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class Union:
    def __init__(self, items):
        self.p = {x: x for x in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def collect(cat: Path, only: set[str]) -> dict[str, list[Path]]:
    """slug -> [image paths] for every sku-*/_supplier-refs/ in the category."""
    out: dict[str, list[Path]] = {}
    for refs in sorted(cat.glob("sku-*/_supplier-refs")):
        slug = refs.parent.name[len("sku-"):]
        if only and slug not in only:
            continue
        imgs = sorted(p for p in refs.iterdir() if p.suffix.lower() in EXTS)
        if imgs:
            out[slug] = imgs
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("category_dir")
    ap.add_argument("--threshold", type=int, default=8,
                    help="max Hamming distance (of 64) to call two images near-dup")
    ap.add_argument("--max-per-product", type=int, default=3,
                    help="D11 cap: a cluster bigger than this is a VIOLATION (exit 1)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", default="", help="bare slugs to restrict to")
    ap.add_argument("--json", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cat = Path(args.category_dir).resolve()
    if not cat.is_dir():
        sys.exit(f"ERROR: not a directory: {cat}")
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    by_slug = collect(cat, only)
    if not by_slug:
        sys.exit(f"no sku-*/_supplier-refs images under {cat}")

    # hash every image in parallel (decode-bound)
    flat = [(slug, p) for slug, ps in by_slug.items() for p in ps]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        hashes = list(ex.map(lambda sp: dhash(sp[1]), flat))
    recs = [(slug, p, h) for (slug, p), h in zip(flat, hashes) if h is not None]
    unreadable = [(slug, p) for (slug, p), h in zip(flat, hashes) if h is None]
    n_img = len(recs)

    # cross-SKU near-dup edges (skip same-SKU pairs; one reused photo links two SKUs)
    uf = Union(list(by_slug.keys()))
    edges = []  # (slugA, imgA, slugB, imgB, dist)
    for i in range(n_img):
        sa, pa, ha = recs[i]
        for j in range(i + 1, n_img):
            sb, pb, hb = recs[j]
            if sa == sb:
                continue
            d = hamming(ha, hb)
            if d <= args.threshold:
                uf.union(sa, sb)
                edges.append((sa, pa.name, sb, pb.name, d))

    # build clusters (slug -> root); only size>=2 are interesting
    groups: dict[str, list[str]] = {}
    for slug in by_slug:
        groups.setdefault(uf.find(slug), []).append(slug)
    clusters = sorted((sorted(g) for g in groups.values() if len(g) > 1),
                      key=len, reverse=True)
    violations = [c for c in clusters if len(c) > args.max_per_product]

    report = {
        "category": cat.name,
        "skus_scanned": len(by_slug),
        "images_hashed": n_img,
        "unreadable": [f"sku-{s}/{p.name}" for s, p in unreadable],
        "threshold": args.threshold,
        "max_per_product": args.max_per_product,
        "near_dup_clusters": [
            {
                "skus": c,
                "size": len(c),
                "violation": len(c) > args.max_per_product,
                "matches": [
                    {"a": f"sku-{sa}/{ia}", "b": f"sku-{sb}/{ib}", "dist": d}
                    for (sa, ia, sb, ib, d) in edges
                    if sa in c and sb in c
                ],
            }
            for c in clusters
        ],
    }
    out_path = Path(args.json) if args.json else cat / "_dedup-report.json"
    out_path.write_text(json.dumps(report, indent=2))

    if not args.quiet:
        print(f"=== dedup_refs: {cat.name}  skus={len(by_slug)}  images={n_img}  "
              f"threshold={args.threshold}  cap={args.max_per_product} ===")
        if unreadable:
            print(f"  ! {len(unreadable)} unreadable image(s) skipped")
        if not clusters:
            print("  ✓ no cross-SKU near-duplicate supplier photos — every SKU looks "
                  "like a distinct product. Vision pass can spot-check only.")
        else:
            print(f"  Found {len(clusters)} near-duplicate cluster(s) "
                  f"({len(violations)} exceed the cap) — CONFIRM by eye:")
            for c in clusters:
                flag = "  ✗ VIOLATION (> cap)" if len(c) > args.max_per_product else ""
                print(f"    [{len(c)}] {', '.join('sku-' + s for s in c)}{flag}")
                shown = [(sa, ia, sb, ib, d) for (sa, ia, sb, ib, d) in edges
                         if sa in c and sb in c]
                for sa, ia, sb, ib, d in sorted(shown, key=lambda e: e[4])[:4]:
                    print(f"        sku-{sa}/{ia}  ≈  sku-{sb}/{ib}   (dist {d})")
        print(f"  report -> {out_path}")
        if violations:
            print(f"\n  ACTION: {len(violations)} cluster(s) exceed the D11 cap of "
                  f"{args.max_per_product}. Re-source or drop the extra SKUs so each "
                  f"physical product appears <= {args.max_per_product}x.")

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
