#!/usr/bin/env python3
"""
discover_seedlist.py — source-agnostic trending-list ingester (nice-to-have)
============================================================================
Pulls a ranked "what's trending" candidate list from one or more EXTERNAL
sources and emits a clean keyword list for the radar pipeline (sv_batch.py →
trend_report.py). Deliberately lightweight — discovery breadth is a nice-to-have,
the real value is OUR detection engine that runs on whatever candidates arrive.

DESIGN PRINCIPLE (locked 2026-06-15): no single source is load-bearing. ET's
free blog list is ONE optional feeder. If it goes stale or vanishes (it's an
evergreen URL they refresh on their own schedule — there are NO dated monthly
archives, May/April don't exist as separate posts), the pipeline still runs on
the other sources you control (TrendTrack, DFS Labs). Every pull is date-stamped
so a stale source is obvious.

Sources:
  • et    — Exploding Topics free /blog/trending-topics (scrape, ~100 ranked items)
            Requires a scraper that bypasses bot detection — we call it via the
            BrightData MCP at the agent layer and pass the markdown to --from-file.
            (This script PARSES the scraped markdown; it does not fetch directly,
            keeping it dependency-free + testable.)
  • file  — any pre-saved markdown/text list (Sell The Trend, a manual paste, etc.)

Output: a JSON keyword list ready for `sv_batch.py --keywords <file>`, plus a
metadata sidecar (source, date, raw rank + growth%) so nothing is lost.

Usage:
  # parse a scraped ET markdown dump into a keyword list:
  python discover_seedlist.py --source et --from-file /tmp/et_scrape.md \
      --out /tmp/et_candidates.json

  # then pipe into the radar:
  python sv_batch.py --keywords /tmp/et_candidates.json --geo US --out /tmp/sv.json
  python trend_report.py --sv-json /tmp/sv.json --title "ET-fed Trending" --out report.md
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

# Non-ecommerce rows ET/others include that our sellability gate also rejects —
# pre-filter here so we don't even pay the SV call on obvious non-products.
NON_PRODUCT = {
    "ai ethics", "prompt engineering", "ai seo", "programmatic seo", "cloud-native",
    "algorithmic bias", "open-source intelligence", "gender affirming care",
    "ai for teachers", "workflow automation platform", "food robotics",
    "agricultural marketplace", "booktok", "dupr",
}
# substrings that signal a brand/startup/app/non-product (skip)
NON_PRODUCT_SUBSTR = ("gpt", "beehiiv", " ai ", "fastmoss", "partiful", "tuta",
                      "penpot", "curipod", "workwhile", "tiphaus", "docuclipper",
                      "carsized", "petfolk", "viwoods", "plaud", "owala", "momcozy")


def parse_et_markdown(md: str) -> list[dict]:
    """Parse ET's trending-topics markdown table. Rows look like:
        1
        Cold Plunge Tub
        3,900%
    (rank / topic / growth on separate lines in the scraped markdown).
    Robust to the exact whitespace by scanning line triples.
    """
    lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
    out = []
    i = 0
    while i < len(lines) - 2:
        rank, topic, growth = lines[i], lines[i + 1], lines[i + 2]
        # rank = pure int, growth = "N%" or "99x+" / "NNx+"
        if re.fullmatch(r"\d{1,3}", rank) and re.search(r"(%|x\+)$", growth):
            kw = topic.lower().strip()
            if kw and len(kw) > 2 and not re.search(r"\d", rank) is None:
                out.append({"rank": int(rank), "keyword": topic.strip(),
                            "growth_raw": growth})
            i += 3
        else:
            i += 1
    return out


def is_product(kw: str) -> bool:
    k = kw.lower().strip()
    if k in NON_PRODUCT:
        return False
    if any(s in f" {k} " for s in NON_PRODUCT_SUBSTR):
        return False
    if len(k.split()) > 6:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["et", "file"], default="et",
                    help="et = parse ET trending-topics markdown; file = generic line list")
    ap.add_argument("--from-file", required=True, help="Path to scraped markdown / text")
    ap.add_argument("--out", required=True, help="Output JSON keyword list (for sv_batch)")
    ap.add_argument("--keep-non-product", action="store_true",
                    help="Don't pre-filter non-product rows (sellability gate will catch them anyway)")
    args = ap.parse_args()

    raw = Path(args.from_file).read_text()
    if args.source == "et":
        items = parse_et_markdown(raw)
    else:
        items = [{"rank": i + 1, "keyword": ln.strip(), "growth_raw": None}
                 for i, ln in enumerate(raw.splitlines()) if ln.strip()]

    if not args.keep_non_product:
        kept = [it for it in items if is_product(it["keyword"])]
    else:
        kept = items
    dropped = len(items) - len(kept)

    # keyword list for sv_batch
    keywords = [it["keyword"] for it in kept]
    Path(args.out).write_text(json.dumps(keywords, indent=2))
    # metadata sidecar (dated — so a stale source is obvious)
    meta = {"source": args.source, "pulled": date.today().isoformat(),
            "total_parsed": len(items), "kept": len(kept), "dropped_non_product": dropped,
            "items": kept}
    Path(args.out.replace(".json", "-meta.json")).write_text(json.dumps(meta, indent=2))

    print(f"parsed {len(items)} rows from {args.source}; kept {len(kept)} products, "
          f"dropped {dropped} non-product → {args.out}")
    print("next: sv_batch.py --keywords %s --geo US --out /tmp/sv.json "
          "&& trend_report.py --sv-json /tmp/sv.json --out report.md" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
