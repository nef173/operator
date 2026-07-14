#!/usr/bin/env python3
"""fetch_refs.py — parallel supplier-reference fetch + manifest scaffold for a batch.

THE FIND-PHASE BOTTLENECK this removes: after the agent deep-pulls the chosen
candidates (each returns a gallery of image URLs + metadata), the old manual step
was, per SKU, "download ~8 gallery photos one at a time, then hand-write a
_manifest.txt." Across 25-30 SKUs that serial download + authoring dominated the
find phase. This driver takes ONE sourcing sheet and does it for the whole batch:

  * downloads every SKU's reference photos IN PARALLEL (across SKUs and across the
    images within each SKU) into  <category_dir>/sku-<slug>/_supplier-refs/supplier-N.<ext>
  * scaffolds a _manifest.txt that already parses cleanly with gen_gallery.py
    (SKU: / FORM FACTOR / one-line Sizes: / Colors: / honest constraints / subject)

It is the bridge: parse_ae_listing.py|parse_temu.py -> candidate_queue.py (pick
winners) -> agent deep-pulls gallery URLs -> THIS -> gen_gallery.py.

SOURCING MODEL: AliExpress/Temu/Amazon are DISCOVERY references only (form-factor +
COGS basis from the struck-through compare-at, never the flash price). The real
supplier is the operator's private agent. The scaffold records the item_id as a
research REFERENCE and leaves COGS to be confirmed.

Image "url" may be an http(s) URL OR a local path / file:// (so an agent that has
already saved photos locally can point straight at them).

INPUT (sourcing sheet JSON):
  {
    "category_dir": "general-stores/nosura/dog-cooling-mat",   # or pass --category-dir
    "subject": "dog",                                          # SUBJECT LOCK
    "skus": [
      {
        "slug": "gel", "name": "Gel Cooling Mat",
        "item_id": "1005006...", "source": "aliexpress",
        "price": 6.57, "compare_at": 12.99, "sold": 5000,
        "sizes": "Small 24x18in / Medium 30x20in / Large 36x27in",  # ONE line
        "colors": "Blue",
        "image_urls": ["https://...1.jpg", "/local/photo.jpg", "..."],
        "claims": ["Self-cooling, no water or electricity", "Washable"],
        "constraints": ["do NOT advertise gel as a 'freezes' claim"]
      }
    ]
  }

USAGE:
  python fetch_refs.py sourcing-sheet.json [--category-dir DIR] [--max-images 8]
      [--workers 4] [--img-workers 4] [--force] [--force-manifest] [--dry-run]
"""
from __future__ import annotations
import argparse, json, mimetypes, shutil, sys, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen, Request

_PRINT = threading.Lock()


def log(m: str):
    with _PRINT:
        print(m, flush=True)


def _ext_for(url: str, data_head: bytes | None) -> str:
    # prefer the URL's own extension; fall back to sniffing the magic bytes
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return ".jpg" if ext == ".jpeg" else ext
    if data_head:
        if data_head[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if data_head[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data_head[:4] == b"RIFF" and data_head[8:12] == b"WEBP":
            return ".webp"
    return ".jpg"


def fetch_one(url: str, dest_noext: Path, force: bool) -> tuple[bool, str]:
    # already present (any ext)?
    if not force:
        for e in (".jpg", ".png", ".webp"):
            if dest_noext.with_suffix(e).exists():
                return (True, "exists")
    try:
        if url.startswith(("http://", "https://")):
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 fetch_refs"})
            with urlopen(req, timeout=60) as r:
                data = r.read()
            ext = _ext_for(url, data[:16])
            dest = dest_noext.with_suffix(ext)
            dest.write_bytes(data)
        else:  # local path or file://
            src = Path(url[7:] if url.startswith("file://") else url).expanduser()
            if not src.exists():
                return (False, f"local file not found: {src}")
            ext = src.suffix.lower() or ".jpg"
            ext = ".jpg" if ext == ".jpeg" else ext
            dest = dest_noext.with_suffix(ext)
            shutil.copyfile(src, dest)
        return (True, f"ok {dest.stat().st_size // 1024}KB")
    except Exception as e:
        return (False, str(e)[:120])


def scaffold_manifest(sku: dict, subject: str) -> str:
    name = sku.get("name") or sku.get("slug", "").replace("-", " ").title()
    slug = sku["slug"]
    src = sku.get("source", "research-ref")
    item_id = sku.get("item_id", "TBD")
    price = sku.get("price"); compare = sku.get("compare_at"); sold = sku.get("sold")
    sizes = " / ".join(sku["sizes"]) if isinstance(sku.get("sizes"), list) else (sku.get("sizes") or "")
    sizes = " ".join(sizes.split())  # collapse to ONE physical line (the wrap-bug rule)
    colors = sku.get("colors", "")
    claims = sku.get("claims") or []
    cons = sku.get("constraints") or []
    cogs = f"{compare} (struck-through compare-at = COGS basis; NOT the flash price)" if compare else "confirm via private agent"
    lines = [
        f"SKU: {name} (sku-{slug})",
        f"Sourced via DISCOVERY reference ({src}) — research reference only; real supplier = operator private agent.",
        f"Research ref item_id : {item_id}",
        f"Bought/sold (ref)    : {sold if sold is not None else 'n/a'}",
        "",
        "FORM FACTOR : ★ TODO — describe the TRUE form factor faithfully (shape, material, "
        "surface, what makes it distinct). gen_gallery preserves this; leaving it vague lets the "
        "render drift. Fill from the deep-pull photos before generating.",
        "",
        "CLAIMS (true / defensible — only what the locked product actually does):",
    ]
    lines += [f"  - {c}" for c in claims] or ["  - ★ TODO add 3-5 honest, supported claims"]
    lines += [
        "  Do NOT advertise any feature the supplier does not ship (dropship-fraud / "
        "marketing-claim-without-backing).",
    ]
    lines += [f"  - {c if c.lower().startswith('do not') else 'do NOT ' + c}" for c in cons]
    lines += [
        "SUBJECT LOCK / CLEANUP (mandatory on every generated image):",
        f"  - {subject.upper()}-ONLY. Never depict or label any other animal/subject.",
        "  - BRAND-FREE. Omit any supplier trademark/badge art in gen.",
        "  - NO PRICE anywhere (Shopify is the price source of truth).",
        "PRICING (COGS basis = research ref, NOT flash)",
        f"  COGS basis            : {cogs}",
        f"  Flash/sale price (ref): {price if price is not None else 'n/a'} (deal price; use the compare-at above as COGS basis instead)",
        "  Retail = price-to-margin .99 (DRAFT — operator confirms)",
        "VARIANTS",
        f"  Sizes  : {sizes or '★ TODO one physical line, e.g. Small .. / Medium .. / Large ..'}",
        f"  Colors : {colors or 'single colorway shown'}",
        "IMAGES (downloaded supplier-1..N from the deep-pull gallery, research ref)",
    ]
    return "\n".join(lines) + "\n"


def run_sku(sku: dict, cat: Path, subject: str, max_images: int, img_workers: int,
            force: bool, force_manifest: bool, dry: bool) -> dict:
    slug = sku["slug"]
    refs_dir = cat / f"sku-{slug}" / "_supplier-refs"
    urls = (sku.get("image_urls") or [])[:max_images]
    res = {"slug": slug, "downloaded": 0, "skipped": 0, "failed": [], "manifest": ""}
    if not urls:
        res["failed"].append("no image_urls")
    if dry:
        res["manifest"] = "dry"
        log(f"  [dry] sku-{slug}: would fetch {len(urls)} refs -> {refs_dir} + scaffold manifest")
        return res
    refs_dir.mkdir(parents=True, exist_ok=True)
    # parallel image downloads within the SKU
    with ThreadPoolExecutor(max_workers=img_workers) as ex:
        futs = {ex.submit(fetch_one, u, refs_dir / f"supplier-{i}", force): (i, u)
                for i, u in enumerate(urls, 1)}
        for f in as_completed(futs):
            ok, msg = f.result()
            i, u = futs[f]
            if ok and msg == "exists":
                res["skipped"] += 1
            elif ok:
                res["downloaded"] += 1
            else:
                res["failed"].append(f"img{i}: {msg}")
    # manifest scaffold (never clobber a hand-authored one unless --force-manifest)
    mf = refs_dir / "_manifest.txt"
    if mf.exists() and not force_manifest:
        res["manifest"] = "kept"
    else:
        mf.write_text(scaffold_manifest(sku, subject))
        res["manifest"] = "wrote"
    tag = f"dl={res['downloaded']} skip={res['skipped']} manifest={res['manifest']}"
    log(f"  {'✓' if not res['failed'] else '!'} sku-{slug}: {tag}"
        + (f"  FAILED {res['failed']}" if res["failed"] else ""))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sheet")
    ap.add_argument("--category-dir", default="")
    ap.add_argument("--subject", default="")
    ap.add_argument("--max-images", type=int, default=8)
    ap.add_argument("--workers", type=int, default=4, help="parallel SKUs")
    ap.add_argument("--img-workers", type=int, default=4, help="parallel images per SKU")
    ap.add_argument("--only", default="", help="bare slugs")
    ap.add_argument("--force", action="store_true", help="re-download existing images")
    ap.add_argument("--force-manifest", action="store_true", help="overwrite an existing _manifest.txt")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sheet = json.loads(Path(args.sheet).read_text())
    cat = Path(args.category_dir or sheet.get("category_dir") or "").resolve()
    if not cat:
        sys.exit("ERROR need category_dir (in the sheet or via --category-dir)")
    subject = args.subject or sheet.get("subject") or "dog"
    skus = sheet.get("skus") or []
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        skus = [s for s in skus if s["slug"] in only]
    if not skus:
        sys.exit("no SKUs to process")

    log(f"=== fetch_refs: category={cat.name}  subject={subject}  skus={len(skus)}  "
        f"max_images={args.max_images}  workers={args.workers}x{args.img_workers} ===")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pex:
        futs = [pex.submit(run_sku, s, cat, subject, args.max_images, args.img_workers,
                           args.force, args.force_manifest, args.dry_run) for s in skus]
        for f in as_completed(futs):
            results.append(f.result())

    tot_dl = sum(r["downloaded"] for r in results)
    bad = [(r["slug"], r["failed"]) for r in results if r["failed"]]
    log(f"\nDONE  {len(results)} skus  downloaded={tot_dl}  "
        f"manifests={sum(1 for r in results if r['manifest']=='wrote')} written  "
        f"({sum(1 for r in results if r['manifest']=='kept')} kept)")
    if bad:
        log("FAILURES:")
        for slug, fails in bad:
            log(f"  sku-{slug}: {fails}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
