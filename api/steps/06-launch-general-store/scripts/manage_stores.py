#!/usr/bin/env python3
"""
manage_stores.py — add / remove / list tracked competitor stores for the
Competitor Best-Seller Spy. The tracked list lives in stores.txt.

Adds are VALIDATED as live Shopify stores (must expose /products.json) before
being written, so you never track a dead or non-Shopify domain.

Usage:
  manage_stores.py --list
  manage_stores.py --add zenzuri.com viralaluna.com            # validate + append
  manage_stores.py --add wniny.com --note "TrendTrack 2026-06-16"
  manage_stores.py --remove libiyi.com
  manage_stores.py --add x-all.com --snapshot-now              # add then baseline it
"""
import argparse
import datetime as dt
import os
import re
import subprocess
import sys

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
STORES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores.txt")


def norm(domain):
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d).rstrip("/")
    d = d.split("/")[0]
    return d


def read_lines():
    if not os.path.isfile(STORES):
        return []
    with open(STORES) as f:
        return f.read().splitlines()


def listed(lines):
    return [norm(ln) for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def is_shopify(domain, timeout=15):
    """Live Shopify check: /products.json returns 200 + a 'products' array."""
    try:
        r = requests.get(f"https://{domain}/products.json?limit=1",
                         headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if "products" not in data:
            return False, "no products key (not Shopify?)"
        # also confirm best-seller sort page is reachable
        c = requests.get(f"https://{domain}/collections/all?sort_by=best-selling",
                        headers={"User-Agent": UA}, timeout=timeout)
        if c.status_code != 200:
            return False, f"collections/all HTTP {c.status_code}"
        return True, "ok"
    except requests.RequestException as e:
        return False, type(e).__name__
    except ValueError:
        return False, "non-JSON products.json"


def cmd_list(lines):
    items = listed(lines)
    print(f"{len(items)} tracked stores:")
    for d in items:
        print(f"  {d}")


def cmd_add(lines, domains, note):
    existing = set(listed(lines))
    added, skipped = [], []
    for raw in domains:
        d = norm(raw)
        if d in existing:
            print(f"  = {d} (already tracked)")
            skipped.append(d)
            continue
        ok, why = is_shopify(d)
        if not ok:
            print(f"  ✗ {d} — NOT added ({why})")
            skipped.append(d)
            continue
        added.append(d)
        existing.add(d)
        print(f"  ✓ {d} — validated, adding")
    if added:
        with open(STORES, "a") as f:
            header = f"\n# --- added {dt.date.today().isoformat()}"
            header += f" · {note}" if note else ""
            header += " ---\n"
            f.write(header + "\n".join(added) + "\n")
        print(f"\nadded {len(added)} store(s) to stores.txt")
    return added


def cmd_remove(lines, domains):
    targets = {norm(d) for d in domains}
    kept, removed = [], []
    for ln in lines:
        if ln.strip() and not ln.strip().startswith("#") and norm(ln) in targets:
            removed.append(norm(ln))
            continue
        kept.append(ln)
    with open(STORES, "w") as f:
        f.write("\n".join(kept).rstrip("\n") + "\n")
    for d in removed:
        print(f"  − removed {d}")
    if not removed:
        print("  (no matching stores found)")
    return removed


def snapshot_now(domains, depth=30):
    if not domains:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    py = os.path.join(here, ".venv", "bin", "python")
    print(f"\nbaselining {len(domains)} new store(s)…")
    subprocess.run([py, os.path.join(here, "bestseller_snapshot.py"),
                    "--stores", ",".join(domains), "--depth", str(depth),
                    "--out", os.path.join(here, "snapshots")], check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--add", nargs="+", default=[])
    ap.add_argument("--remove", nargs="+", default=[])
    ap.add_argument("--note", default=None, help="label for the added batch")
    ap.add_argument("--snapshot-now", action="store_true",
                    help="baseline newly-added stores immediately")
    args = ap.parse_args()

    lines = read_lines()
    if args.list or (not args.add and not args.remove):
        cmd_list(lines)
        return 0
    added = []
    if args.add:
        added = cmd_add(lines, args.add, args.note)
        lines = read_lines()
    if args.remove:
        cmd_remove(lines, args.remove)
    if args.snapshot_now and added:
        snapshot_now(added)
    return 0


if __name__ == "__main__":
    sys.exit(main())
