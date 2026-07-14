#!/usr/bin/env python3
"""
harvest_general_stores.py — the discovery LOOP: take candidate stores from ANY
source, run them through the general-store classifier GATE, and auto-add the ones
that pass to the Best-Seller Spy's stores.txt.

The reframe (2026-06-17): the discovery SOURCE doesn't need to be clean — the
classifier is the universal gate. So this runner is source-agnostic:

  candidates  →  resolve to domains  →  classify_store GATE  →  add GENERAL ones

Sources (combine freely):
  --from-scan paid-scan.json     AdsPower /competitor-shopping-scan manifest
                                 (reads sponsored[].advertiser → domain guess)
  --from-trendtrack dump.json    TrendTrack search_shops dump (reads data[].domain)
  --domains a.com,b.com          explicit list
  --from-file list.txt           one domain (or advertiser name) per line

Gate (delegates to classify_store.py):
  --min-depts 5 --max-dominance 0.55   GENERAL = breadth AND balance

By default this is a DRY RUN (report only). Pass --apply to actually add passing
stores via manage_stores.py (which re-validates live-Shopify + can snapshot).

Usage:
  harvest_general_stores.py --from-trendtrack dump.json
  harvest_general_stores.py --domains directtoolsoutlet.com,shopzaza.com
  harvest_general_stores.py --from-scan paid-scan.json --apply --snapshot-now
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import requests  # noqa: E402
from classify_store import classify, UA  # reuse the gate logic verbatim  # noqa: E402

STORES = os.path.join(HERE, "stores.txt")


def norm(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).rstrip("/")
    return d.split("/")[0]


def name_to_domain_guess(name):
    """Best-effort advertiser-name → domain. Unreliable on its own, but the
    Shopify-reachability check in classify() is the safety net (a wrong guess
    just fails to resolve and gets dropped)."""
    n = (name or "").strip().lower()
    if not n or n in ("?", "google", "walmart", "amazon.com", "amazon", "target",
                       "ebay", "etsy", "best buy", "walmart.com"):
        return None
    if "." in n and " " not in n:        # already a domain
        return norm(n)
    slug = re.sub(r"[^a-z0-9]", "", n)    # "Spark Paws" -> "sparkpaws"
    return f"{slug}.com" if slug else None


def from_scan(path):
    with open(path) as f:
        doc = json.load(f)
    rows = doc.get("sponsored") or doc.get("listings") or []
    if isinstance(doc, dict) and "keywords" in doc:        # multi-kw manifest
        rows = []
        for k in doc.get("keywords", []):
            rows += k.get("sponsored", []) if isinstance(k, dict) else []
    out = []
    for r in rows:
        g = name_to_domain_guess(r.get("advertiser") or r.get("source"))
        if g:
            out.append(g)
    return out


def from_trendtrack(path):
    with open(path) as f:
        doc = json.load(f)
    rows = doc.get("data") or doc.get("shops") or (doc if isinstance(doc, list) else [])
    return [norm(s.get("domain")) for s in rows if s.get("domain")]


def tracked_domains():
    s = set()
    if os.path.isfile(STORES):
        for ln in open(STORES):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                s.add(norm(ln))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-scan", action="append", default=[])
    ap.add_argument("--from-trendtrack", action="append", default=[])
    ap.add_argument("--from-file", action="append", default=[])
    ap.add_argument("--domains", default=None)
    ap.add_argument("--min-depts", type=int, default=5)
    ap.add_argument("--max-dominance", type=float, default=0.45)
    ap.add_argument("--apply", action="store_true", help="actually add passing stores")
    ap.add_argument("--snapshot-now", action="store_true",
                    help="baseline added stores immediately (with --apply)")
    args = ap.parse_args()

    cands = []
    for p in args.from_scan:
        cands += from_scan(p)
    for p in args.from_trendtrack:
        cands += from_trendtrack(p)
    for p in args.from_file:
        cands += [name_to_domain_guess(ln) for ln in open(p)
                  if ln.strip() and not ln.startswith("#")]
    if args.domains:
        cands += [norm(d) for d in args.domains.split(",")]
    cands = [c for c in cands if c]

    tracked = tracked_domains()
    seen, queue = set(), []
    for c in cands:
        if c in seen or c in tracked:
            continue
        seen.add(c)
        queue.append(c)

    if not queue:
        print("no new candidates to evaluate (all empty / already tracked).")
        return 0

    print(f"Evaluating {len(queue)} candidate domain(s) through the GENERAL gate "
          f"(min_depts={args.min_depts}, max_dominance={args.max_dominance})\n")
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    passed, failed = [], []
    print(f"{'STORE':<28} {'VERDICT':<8} {'DEPTS':>5} {'TOP%':>16}  WHY")
    print("-" * 90)
    for d in queue:
        r = classify(session, d, args.min_depts, args.max_dominance)
        if r.get("error"):
            print(f"{d:<28} {'ERROR':<8}     —  {r['error']}")
            failed.append(d)
            continue
        dom = (f"{r['dominant_dept']} {int((r['dominant_share'] or 0)*100)}%"
               if r['dominant_dept'] else "—")
        flag = "✅" if r["verdict"] == "GENERAL" else "▫️"
        print(f"{flag}{r['store']:<26} {r['verdict']:<8} {r['distinct_departments']:>5} "
              f"{dom:>16}  {r['reason']}")
        (passed if r["verdict"] == "GENERAL" else failed).append(d)

    print(f"\n{len(passed)} GENERAL / {len(failed)} rejected.")
    if not passed:
        return 0
    print("PASSED:", ", ".join(passed))

    if not args.apply:
        print("\n(dry run — re-run with --apply to add these to stores.txt)")
        return 0

    cmd = [os.path.join(HERE, ".venv", "bin", "python"),
           os.path.join(HERE, "manage_stores.py"), "--add", *passed,
           "--note", "google-shopping discovery (classifier-gated)"]
    if args.snapshot_now:
        cmd.append("--snapshot-now")
    print("\nadding via manage_stores.py …\n")
    subprocess.run(cmd, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
