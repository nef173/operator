#!/usr/bin/env python3
"""build_batch.py — ONE phased, parallel-safe driver for a whole general-store batch.

Chains the four build phases with the canonical args baked in so there is no
arg-shape rediscovery and no missed step (the friction the bottleneck analysis
flagged). Each phase is the existing, separately-testable script — this only
sequences them and fails fast with a clear per-phase summary.

    gen     -> gen_gallery.py   (low concurrency + retry + spec-authoritative sizes)
    create  -> create_drafts.py (idempotent; data-driven copy+SEO with guards)
    upload  -> upload_gallery.py (parallel across SKUs; skips SKUs already uploaded)
    verify  -> verify_listings.py (whole-spec gate)

Everything stays DRAFT — go-live is a separate, explicit operator step.

DESIGN NOTES (why it's reliable at scale):
  * Phases run in order; a non-zero phase STOPS the run (no silently-half-built batch).
  * SPEED: gen + create run CONCURRENTLY (create uses no images → fully independent of
    gen), so the whole serial Shopify create hides under the long image-gen wall time;
    upload waits for both. Other phases stay serial stages (dedup first; upload/verify last).
  * `gen` and `create` are each independently idempotent, so a stopped run is safe to
    re-run from the top — finished images are skipped, existing drafts are updated.
  * Subject for the SUBJECT-LOCK comes from the spec; sizes for the size-guide come
    from the spec variants (authoritative), not the free-text manifest.
  * --only takes BARE slugs (e.g. pcm,blanket); they're mapped to sku-<slug> for gen.

USAGE:
  python build_batch.py <category-dir> --spec <spec.json> --env <admin.env> \
      [--only pcm,blanket] [--phases gen,create,upload,verify] [--skip-gen] \
      [--dedup [--dedup-threshold 8] [--dedup-cap 3] [--dedup-strict]] \
      [--max-concurrent 3] [--workers 3] [--force] [--dry-run]

  --dedup prepends the find-phase dedup_refs guard BEFORE gen: it perceptual-hashes
  every sku-*/_supplier-refs/ photo and FLAGS any physical product reused across more
  than --dedup-cap SKUs (the D11 "duplicate SKU" trap). It is ADVISORY by default —
  it prints the flagged clusters and continues, because a perceptual hash can
  false-positive (the vision pass is authoritative). Add --dedup-strict to make a
  violation actually STOP the build.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ALL_PHASES = ["gen", "create", "upload", "verify"]
# 'dedup' is an OPT-IN pre-gen guard (find-phase), not part of the default build
# sequence — refs may not exist yet for every batch. Enable with --dedup.
OPTIONAL_PHASES = ["dedup"]


def run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("category_dir")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--only", default="", help="bare slugs, e.g. pcm,blanket")
    ap.add_argument("--phases", default=",".join(ALL_PHASES))
    ap.add_argument("--skip-gen", action="store_true", help="shorthand for dropping the gen phase")
    ap.add_argument("--dedup", action="store_true",
                    help="run the dedup_refs pre-gen guard first (D11 same-product cap)")
    ap.add_argument("--dedup-threshold", type=int, default=8,
                    help="dedup: max Hamming distance to call two supplier photos near-dup")
    ap.add_argument("--dedup-cap", type=int, default=3,
                    help="dedup: a cluster bigger than this is a D11 violation")
    ap.add_argument("--dedup-strict", action="store_true",
                    help="dedup: STOP the build on a violation (default = warn + continue, "
                         "since a perceptual hash can false-positive; vision pass is authoritative)")
    ap.add_argument("--max-concurrent", type=int, default=3, help="gen image concurrency (keep low)")
    ap.add_argument("--workers", type=int, default=3, help="upload parallelism across SKUs")
    ap.add_argument("--img-workers", type=int, default=4,
                    help="upload: parallel staged-uploads WITHIN one SKU")
    ap.add_argument("--force", action="store_true", help="gen: regenerate existing images")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    py = sys.executable  # the venv python that's running this orchestrator
    spec_path = Path(args.spec).resolve()
    spec = json.loads(spec_path.read_text())
    subject = spec.get("subject", "dog")
    spec_slugs = [s["slug"] for s in spec["skus"]]
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    bad = [s for s in only if s not in spec_slugs]
    if bad:
        sys.exit(f"ERROR --only has slugs not in spec: {bad}\n  spec slugs: {spec_slugs}")
    slugs = only or spec_slugs

    recognized = ALL_PHASES + OPTIONAL_PHASES
    phases = [p.strip() for p in args.phases.split(",") if p.strip() in recognized]
    if args.skip_gen and "gen" in phases:
        phases.remove("gen")
    if args.dedup and "dedup" not in phases:
        phases.insert(0, "dedup")  # guard runs BEFORE gen

    cat = Path(args.category_dir).resolve()
    sku_folders = ",".join(f"sku-{s}" for s in slugs)
    only_csv = ",".join(slugs)

    print(f"=== build_batch: category={cat.name}  spec={spec_path.name}  "
          f"subject={subject}  skus={len(slugs)} ({only_csv})  phases={phases} ===")

    def build_cmd(ph: str) -> list[str]:
        if ph == "dedup":
            return [py, str(SCRIPTS / "dedup_refs.py"), str(cat),
                    "--threshold", str(args.dedup_threshold),
                    "--max-per-product", str(args.dedup_cap), "--only", only_csv]
        if ph == "gen":
            c = [py, str(SCRIPTS / "gen_gallery.py"), str(cat),
                 "--skus", sku_folders, "--spec", str(spec_path),
                 "--subject", subject, "--max-concurrent", str(args.max_concurrent)]
            if args.force: c.append("--force")
            if args.dry_run: c.append("--dry-run")
            return c
        if ph == "create":
            c = [py, str(SCRIPTS / "create_drafts.py"), str(spec_path),
                 "--env", args.env, "--only", only_csv]
            if args.dry_run: c.append("--dry-run")
            return c
        if ph == "upload":
            c = [py, str(SCRIPTS / "upload_gallery.py"), str(cat),
                 "--spec", str(spec_path), "--env", args.env,
                 "--only", only_csv, "--workers", str(args.workers),
                 "--img-workers", str(args.img_workers)]
            if args.dry_run: c.append("--dry-run")
            return c
        return [py, str(SCRIPTS / "verify_listings.py"),
                "--spec", str(spec_path), "--env", args.env]

    # Group contiguous gen+create into ONE parallel stage: create uses NO images, so
    # it is fully independent of gen; running them concurrently hides the whole serial
    # Shopify create under the long image-gen wall time. upload waits for both. Every
    # other phase stays its own serial stage (dedup guard first; upload/verify last).
    PARALLEL = {"gen", "create"}
    stages: list[list[str]] = []
    buf: list[str] = []
    for ph in phases:
        if ph in PARALLEL:
            buf.append(ph)
        else:
            if buf: stages.append(buf); buf = []
            stages.append([ph])
    if buf: stages.append(buf)

    t0 = time.time()
    summary = []
    stop = False
    for stage in stages:
        if len(stage) == 1:
            results = [(stage[0], run(build_cmd(stage[0])))]
        else:
            print(f"\n— running {stage} concurrently (independent phases) —")
            with ThreadPoolExecutor(max_workers=len(stage)) as ex:
                futs = {ex.submit(run, build_cmd(ph)): ph for ph in stage}
                results = [(futs[f], f.result()) for f in as_completed(futs)]
            results.sort(key=lambda pr: stage.index(pr[0]))
        for ph, rc in results:
            summary.append((ph, rc))
            if rc != 0:
                if ph == "dedup" and not args.dedup_strict:
                    print(f"\n⚠ dedup flagged possible duplicate-supplier SKUs (rc={rc}) — "
                          f"continuing (advisory; confirm by eye, or pass --dedup-strict to gate).")
                    continue
                stop_hint = ("resolve the duplicate SKUs or drop --dedup-strict"
                             if ph == "dedup" else
                             "re-run is safe; phases are idempotent")
                print(f"\n✗ phase '{ph}' returned {rc} — STOPPING ({stop_hint}).")
                stop = True
        if stop:
            break

    dt = int(time.time() - t0)
    print(f"\n=== build_batch summary ({dt}s) ===")
    for ph, rc in summary:
        print(f"  {'✓' if rc == 0 else '✗'} {ph}: rc={rc}")
    done = [p for p, _ in summary]
    skipped = [p for p in phases if p not in done]
    if skipped:
        print(f"  (not run: {skipped})")
    # an advisory (non-strict) dedup nonzero is informational, not a build failure
    failures = [(p, rc) for p, rc in summary
                if rc != 0 and not (p == "dedup" and not args.dedup_strict)]
    return 0 if not failures and not skipped else 1


if __name__ == "__main__":
    sys.exit(main())
