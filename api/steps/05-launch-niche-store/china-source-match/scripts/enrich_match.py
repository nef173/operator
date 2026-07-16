#!/usr/bin/env python3
"""enrich_match.py — populate matched.json rows with full 1688 SKU detail.

The sourcing-match modal shows variants / specs / gallery for each match's ``best``
record. A freshly-judged (or seeded) match carries only the search thumbnail — no
variants, no specs. This drives the EXISTING ``tmapi_1688.enrich()`` over the ``best``
record(s) in a matched.json (one item_detail TMAPI call per offer) and writes the
enriched variants / specs / gallery / sold back IN PLACE, in the exact shape the app
reader already consumes (``best.variants`` as [{name, values}], ``best.specs`` as a
dict). No scraper logic is reimplemented here — it only applies enrich() to records
already on disk.

Auth: TMAPI_TOKEN (env or this folder's .env) — same source as tmapi_1688.py.

Usage:
  enrich_match.py --matched nosura-neck-fan.matched.json --offer 675158757029
  enrich_match.py --matched nosura-neck-fan.matched.json --all
"""
import argparse
import json
import pathlib
import sys

from tmapi_1688 import enrich, tmapi_token


def _rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    return []


def _resolve(matched: str) -> pathlib.Path:
    """Accept a full path OR a bare filename resolved within this scripts dir."""
    p = pathlib.Path(matched)
    if p.is_file():
        return p
    here = pathlib.Path(__file__).resolve().parent
    direct = here / matched
    if direct.is_file():
        return direct
    hits = list(here.rglob(matched if matched.endswith(".json") else f"*{matched}*"))
    if hits:
        return hits[0]
    raise SystemExit(f"matched file not found: {matched}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matched", required=True,
                    help="matched.json path or bare filename in this scripts dir")
    ap.add_argument("--offer", default=None,
                    help="enrich only the row whose best.offer_id == this id")
    ap.add_argument("--all", action="store_true",
                    help="enrich every row's best record")
    args = ap.parse_args()

    if not args.offer and not args.all:
        raise SystemExit("pass --offer <id> or --all")

    path = _resolve(args.matched)
    data = json.loads(path.read_text())
    rows = _rows(data)
    if not rows:
        raise SystemExit(f"no rows in {path}")

    token = tmapi_token()
    touched = 0
    for r in rows:
        # match_china.py writes the chosen offer under "matched" (confident) or "closest"
        # (near-miss fallback) — NOT "best". Reading "best" made this a silent no-op.
        best = r.get("matched") or r.get("closest")
        if not isinstance(best, dict) or not best.get("offer_id"):
            continue
        if args.offer and str(best.get("offer_id")) != str(args.offer):
            continue
        enrich(best, token)  # in-place: adds variants/specs/images/sold/stock/price_*
        if best.get("variants") or best.get("specs"):
            touched += 1
        print(f"enriched offer {best.get('offer_id')}: "
              f"{len(best.get('variants') or [])} variant groups, "
              f"{len(best.get('specs') or {})} specs", file=sys.stderr)

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"{touched} row(s) enriched -> {path.name}")
    return 0 if touched else 1


if __name__ == "__main__":
    sys.exit(main())
