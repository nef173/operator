#!/usr/bin/env python3
"""
candidate_queue.py — the DISCOVERY-side aggregator + scorer for the 6-lane funnel (WORKFLOW §4).

This is the layer that sits BETWEEN the research lanes and the listing state machine:

    Stage 0 seed --> Lane 1 GATE (season_classify) --> candidate-queue.json (THIS) --> listing-queue.json
                     lanes 2-6 attach validation signals ----^                         (listing_queue.py)

It owns  general-stores/<store>/candidate-queue.json  — a scored, deduped backlog of gate-cleared
KEYWORDS (the listing unit per D10) with the validation-lane signals stacked on each. It is distinct
from listing-queue.json (listing_queue.py), which is the per-SKU CREATE/cull state machine. A
gate-cleared, well-scored candidate is PROMOTED from here into the listing queue as a category.

WHY a separate file: §4 mixes raw scored candidates with the SKU state machine conceptually, but in
practice the discovery backlog (many speculative keywords, re-scored on every run) and the build
state machine (a few committed categories + their SKUs) have different lifecycles. Keeping them apart
lets you re-run discovery/scoring nightly without churning the build artifact. `promote` is the bridge.

GATE INVARIANT (§4): a candidate cannot SCORE until it clears the Lane-1 keyword gate (>=10k SV).
Validation-lane convergence only adds CONFIDENCE on top of a gate-cleared keyword — it never
substitutes for search demand (a Meta/Amazon winner with no Google SV is the wrong channel).

LANES (signal sources attached per candidate):
  keyword        Lane 1 (GATE) — sv, capture_bucket, momentum, gate. Ingested from season_classify.json.
  shopping_scan  Lane 2 — sub-segment rows / PLA price band / advertiser concentration (recon).
  bestseller_spy Lane 3 — rank-delta / %gain on competitor Shopify; in_store cross-ref.
  amazon         Lane 4 — Movers&Shakers %rank-gain + review-count moat.
  marketplace    Lane 5 — AliExpress orders / Temu sold-count + COGS basis.
  meta           Lane 6 — ad longevity / duplicate-creative count (supplementary feeder).

SCORING (transparent, numeric-only — no qualitative inputs, §4 / D1):
  score = 0                                   if gate != PASS*
        = sv_score + corr_score + bucket_score + momentum_score + not_in_store_score + lane_extras
    sv_score      = min(sv/10000, 10)         1 pt / 10k SV, capped 10
    corr_score    = corroboration * 3         each VALIDATION lane present (not the keyword lane) = +3
    bucket_score  = BREAKOUT 6 / LIST-NOW 5 / BUILD-AHEAD 2 / EVERGREEN 0.5 / SKIP 0
    momentum_score= clamp((momentum-1)*5, -3, 5)
    not_in_store  = +3 if Lane-3 says not-in-store (the auto-prioritized backlog, §3 Lane 3)
    lane_extras   = capped contributions from amazon %gain, spy rank-delta, meta ad-longevity-days

USAGE
-----
  PY=06-launch-general-store/scripts/candidate_queue.py
  python $PY <store> init
  python $PY <store> ingest-keywords --season season.json        # Lane-1 gate -> candidates
  python $PY <store> add-signal "dog cooling mat" --lane bestseller_spy --rank-delta 12 \
        --not-in-store --store-name belroshop --image https://... --price 24.99
  python $PY <store> add-signal "dog cooling mat" --lane amazon --pct-gain 320 --reviews 2067
  python $PY <store> score                                        # recompute all scores
  python $PY <store> show [--bucket LIST-NOW] [--min-score 8] [--gate PASS]
  python $PY <store> promote "dog cooling mat"                    # prints the listing_queue add-category cmd
  python $PY <store> promote "dog cooling mat" --apply            # actually runs listing_queue add-category

Pure stdlib. Base dir overridable via $GENERAL_STORES_DIR (default: general-stores/).
"""
import argparse
import datetime as _dt
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

VALIDATION_LANES = ["shopping_scan", "bestseller_spy", "amazon", "marketplace", "meta"]
ALL_LANES = ["keyword"] + VALIDATION_LANES
BUCKET_SCORE = {"BREAKOUT": 6, "LIST-NOW": 5, "BUILD-AHEAD": 2, "EVERGREEN": 0.5, "SKIP": 0}


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _slug(kw: str) -> str:
    return "-".join("".join(c if c.isalnum() or c == " " else " " for c in kw.lower()).split())[:40]


def _base_dir() -> Path:
    return Path(os.environ.get("GENERAL_STORES_DIR", "general-stores"))


def _path(store: str) -> Path:
    return _base_dir() / store / "candidate-queue.json"


def _load(store: str) -> dict:
    p = _path(store)
    if not p.exists():
        return {"store": store, "created": _now(), "updated": _now(), "candidates": {}}
    return json.loads(p.read_text())


def _save(store: str, data: dict) -> Path:
    data["updated"] = _now()
    p = _path(store)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return p


def _get(data: dict, kw: str) -> dict | None:
    slug = _slug(kw)
    return data["candidates"].get(slug)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# Last-line product-intent guard at INGEST — junk can reach the queue from ANY source (a Trends
# co-search, a keyword-planner related term, a re-used season file), not just the expansion step,
# so filter here too: a candidate must plausibly name a PRODUCT, not a service/info/local/nav query
# or a bare number ("10", "ac repair", "near me", "how to…"). Mirrors research_dfs._is_product_keyword.
_JUNK_PHRASES = ("near me", "for sale", "for rent", "how to", "what is", "second hand")
_JUNK_WORDS = {
    "repair", "repairs", "installation", "install", "installed", "fitting", "service", "servicing",
    "services", "company", "companies", "contractor", "contractors", "rental", "rentals", "hire",
    "quote", "quotes", "job", "jobs", "salary", "how", "what", "why", "when", "who", "vs", "versus",
    "review", "reviews", "rating", "meaning", "definition", "wiki", "reddit", "youtube", "login",
    "download", "holiday", "holidays", "flight", "flights", "hotel", "hotels", "insurance",
    "weather", "near", "map", "directions",
}


def _is_product_keyword(term) -> bool:
    t = (term or "").strip().lower()
    if len(t) < 3 or re.fullmatch(r"[0-9]+", t):
        return False
    if any(p in t for p in _JUNK_PHRASES):
        return False
    return not (set(re.findall(r"[a-z0-9]+", t)) & _JUNK_WORDS)


def _score_one(c: dict) -> float:
    gate = str(c.get("gate", ""))
    if not gate.startswith("PASS"):
        return 0.0
    sv = c.get("sv") or 0
    lanes = c.get("lanes", {})
    corroboration = sum(1 for ln in VALIDATION_LANES if ln in lanes)
    momentum = c.get("momentum")
    score = 0.0
    # Log-scale search volume so BIGGER keywords actually outrank smaller ones, instead of every
    # term ≥100k flat-capping at 10 (which made prime/strong/solid keywords all tie at ~20.0).
    # 10k→3.3, 30k→4.9, 100k→6.6, 200k→7.6, 450k→8.8, 1M→9.9, capped at 12.
    score += _clamp(math.log10(max(sv, 1) / 1_000) * 3.3, 0, 12) if sv else 0.0
    score += corroboration * 3
    score += BUCKET_SCORE.get(c.get("capture_bucket"), 0)
    if momentum is not None:
        score += _clamp((momentum - 1) * 5, -3, 5)
    if c.get("in_store") is False:
        score += 3
    # lane extras (capped)
    amz = lanes.get("amazon", {})
    if amz.get("pct_gain") is not None:
        score += _clamp(amz["pct_gain"] / 100, 0, 4)
    spy = lanes.get("bestseller_spy", {})
    if spy.get("rank_delta") is not None:
        score += _clamp(spy["rank_delta"] / 10, 0, 4)
    meta = lanes.get("meta", {})
    if meta.get("ad_longevity_days") is not None:
        score += _clamp(meta["ad_longevity_days"] / 30, 0, 4)
    mkt = lanes.get("marketplace", {})
    if mkt.get("orders") is not None:
        score += _clamp(mkt["orders"] / 5000, 0, 3)
    return round(score, 2)


# ---------------------------------------------------------------- commands
def cmd_init(args):
    print(f"✓ candidate queue ready at {_save(args.store, _load(args.store))}")


def cmd_ingest_keywords(args):
    """Pull gate-cleared keywords from a season_classify.json into the candidate queue (Lane 1)."""
    data = _load(args.store)
    try:
        doc = json.loads(Path(args.season).read_text())
    except FileNotFoundError:
        sys.exit(f"❌ season file not found: {args.season}")
    except json.JSONDecodeError as e:
        sys.exit(f"❌ invalid JSON in {args.season}: {e}")
    rows = doc.get("results", doc) if isinstance(doc, dict) else doc
    added = updated = skipped = 0
    for r in rows:
        gate = str(r.get("gate", ""))
        if not gate.startswith("PASS"):
            if not args.include_failed:
                skipped += 1
                continue
        # Drop non-product junk regardless of which upstream source produced it.
        if not _is_product_keyword(r.get("keyword", "")):
            skipped += 1
            continue
        slug = r.get("slug") or _slug(r.get("keyword", ""))
        c = data["candidates"].setdefault(slug, {"created": _now(), "lanes": {}})
        is_new = "keyword" not in c.get("lanes", {})
        c["keyword"] = r.get("keyword")
        c["sv"] = r.get("sv")
        c["gate"] = r.get("gate")
        c["capture_bucket"] = r.get("capture_bucket")
        mw = r.get("momentum_weekly")
        c["momentum"] = mw if mw is not None else r.get("momentum_monthly")
        c["list_date"] = r.get("list_date")
        c.setdefault("lanes", {})["keyword"] = {
            "sv": r.get("sv"), "capture_bucket": r.get("capture_bucket"),
            "momentum_source": r.get("momentum_source"), "cpc": r.get("cpc"),
            "competition_index": r.get("competition_index"),
            "verify_breakout": r.get("verify_breakout"), "reason": r.get("reason"),
        }
        c["score"] = _score_one(c)
        c["updated"] = _now()
        added += is_new
        updated += (not is_new)
    p = _save(args.store, data)
    print(f"✓ ingested keywords: {added} new, {updated} updated, {skipped} below-gate skipped -> {p}")


def cmd_add_signal(args):
    """Attach a validation-lane signal to an existing candidate (must already be gate-cleared)."""
    data = _load(args.store)
    c = _get(data, args.keyword)
    if c is None:
        if not args.create:
            sys.exit(f"❌ '{args.keyword}' not in queue. Ingest it via Lane-1 first, or pass --create "
                     f"(it will sit gate=UNKNOWN, score 0, until a season ingest sets the gate).")
        c = data["candidates"].setdefault(_slug(args.keyword), {
            "keyword": args.keyword, "created": _now(), "gate": "UNKNOWN", "lanes": {},
        })
    if args.lane not in VALIDATION_LANES:
        sys.exit(f"❌ --lane must be one of {VALIDATION_LANES} (the keyword/Lane-1 gate is set via ingest-keywords)")
    sig = c.setdefault("lanes", {}).setdefault(args.lane, {})
    for k, v in [
        ("rank_delta", args.rank_delta), ("pct_gain", args.pct_gain), ("reviews", args.reviews),
        ("orders", args.orders), ("sold_count", args.sold_count), ("ad_longevity_days", args.ad_longevity_days),
        ("dup_creatives", args.dup_creatives), ("price", args.price), ("image", args.image),
        ("url", args.url), ("store_name", args.store_name), ("cogs", args.cogs), ("note", args.note),
    ]:
        if v is not None:
            sig[k] = v
    sig["seen"] = _now()
    if args.image and not c.get("image"):
        c["image"] = args.image
    if args.price is not None and not c.get("price"):
        c["price"] = args.price
    if args.cogs is not None:
        c["cogs"] = args.cogs
    if args.not_in_store:
        c["in_store"] = False
    if args.in_store:
        c["in_store"] = True
    c["corroboration"] = sum(1 for ln in VALIDATION_LANES if ln in c.get("lanes", {}))
    c["score"] = _score_one(c)
    c["updated"] = _now()
    p = _save(args.store, data)
    print(f"✓ {args.keyword} += {args.lane} signal  (corroboration={c['corroboration']}, score={c['score']}) -> {p}")


def cmd_score(args):
    data = _load(args.store)
    for c in data["candidates"].values():
        c["corroboration"] = sum(1 for ln in VALIDATION_LANES if ln in c.get("lanes", {}))
        c["score"] = _score_one(c)
    _save(args.store, data)
    n = len(data["candidates"])
    print(f"✓ re-scored {n} candidate(s)")
    cmd_show(args)


def cmd_show(args):
    data = _load(args.store)
    cands = list(data["candidates"].values())
    if args.bucket:
        cands = [c for c in cands if c.get("capture_bucket") == args.bucket]
    if args.gate:
        cands = [c for c in cands if str(c.get("gate", "")).startswith(args.gate)]
    if args.min_score is not None:
        cands = [c for c in cands if (c.get("score") or 0) >= args.min_score]
    if not cands:
        print("(no candidates match)")
        return
    cands.sort(key=lambda c: -(c.get("score") or 0))
    icons = {"BREAKOUT": "⚡", "LIST-NOW": "🟢", "BUILD-AHEAD": "🟡", "EVERGREEN": "⚪", "SKIP": "❌"}
    print(f"{'SCORE':>6}  {'BUCKET':<12} {'KEYWORD':<28} {'SV':>8}  CORROB  LANES")
    for c in cands:
        lanes = ",".join(ln for ln in ALL_LANES if ln in c.get("lanes", {}))
        bk = c.get("capture_bucket") or "-"
        insto = "" if c.get("in_store") is None else (" [not-in-store]" if c["in_store"] is False else " [in-store]")
        vb = " ⚡vb" if (c.get("lanes", {}).get("keyword", {}).get("verify_breakout")) else ""
        print(f"{(c.get('score') or 0):>6}  {icons.get(bk,' ')}{bk:<11} {(c.get('keyword') or '')[:28]:<28} "
              f"{(c.get('sv') or 0):>8,}  {c.get('corroboration',0):>5}   {lanes}{insto}{vb}")


def cmd_promote(args):
    """Bridge a scored candidate into the listing-queue as a category (the build hand-off)."""
    data = _load(args.store)
    c = _get(data, args.keyword)
    if c is None:
        sys.exit(f"❌ '{args.keyword}' not in candidate queue")
    if not str(c.get("gate", "")).startswith("PASS"):
        sys.exit(f"❌ refuse to promote — '{args.keyword}' has not cleared the Lane-1 gate (gate={c.get('gate')})")
    slug = _slug(args.keyword)
    lq = Path(__file__).with_name("listing_queue.py")
    cmd = [sys.executable, str(lq), args.store, "add-category", slug,
           "--keyword", c["keyword"], "--capture", c.get("capture_bucket") or "LIST-NOW",
           "--state", "keyword-clustered"]
    if c.get("sv") is not None:
        cmd += ["--sv", str(int(c["sv"]))]
    if args.apply:
        print(f"  running: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            sys.exit(f"❌ promote failed — listing_queue.py add-category returned {e.returncode}")
        c["promoted"] = _now()
        _save(args.store, data)
    else:
        print("# dry-run — re-run with --apply to execute, or run this yourself:")
        print(" ".join(f'"{x}"' if " " in x else x for x in cmd))


def main():
    ap = argparse.ArgumentParser(description="general-store discovery candidate queue (aggregator/scorer)")
    ap.add_argument("store", help="store key (e.g. nosura) — the general-stores/<store>/ dir")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    a = sub.add_parser("ingest-keywords", help="Lane-1: pull gate-cleared keywords from a season_classify.json")
    a.add_argument("--season", required=True)
    a.add_argument("--include-failed", action="store_true", help="also ingest below-gate keywords (score 0)")
    a.set_defaults(func=cmd_ingest_keywords)

    a = sub.add_parser("add-signal", help="attach a validation-lane signal (lanes 2-6)")
    a.add_argument("keyword")
    a.add_argument("--lane", required=True, help=f"one of {VALIDATION_LANES}")
    a.add_argument("--rank-delta", type=float)
    a.add_argument("--pct-gain", type=float)
    a.add_argument("--reviews", type=int)
    a.add_argument("--orders", type=int)
    a.add_argument("--sold-count", type=int)
    a.add_argument("--ad-longevity-days", type=int)
    a.add_argument("--dup-creatives", type=int)
    a.add_argument("--price", type=float)
    a.add_argument("--cogs", type=float)
    a.add_argument("--image")
    a.add_argument("--url")
    a.add_argument("--store-name")
    a.add_argument("--note")
    a.add_argument("--not-in-store", action="store_true")
    a.add_argument("--in-store", action="store_true")
    a.add_argument("--create", action="store_true", help="create candidate if missing (gate=UNKNOWN until ingest)")
    a.set_defaults(func=cmd_add_signal)

    sub.add_parser("score").set_defaults(func=cmd_score, bucket=None, gate=None, min_score=None)

    a = sub.add_parser("show")
    a.add_argument("--bucket")
    a.add_argument("--gate", help="filter by gate prefix, e.g. PASS")
    a.add_argument("--min-score", type=float)
    a.set_defaults(func=cmd_show)

    a = sub.add_parser("promote", help="hand a gate-cleared candidate to listing_queue.py as a category")
    a.add_argument("keyword")
    a.add_argument("--apply", action="store_true")
    a.set_defaults(func=cmd_promote)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
