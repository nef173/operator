#!/usr/bin/env python3
"""
match_china.py — VLM identity judge for China-source candidates.

Step 2 of the china-source-match pipeline. Takes the candidate offers found by
`china_image_search.py` (Alibaba.com / 1688 reverse-image search) and decides,
per candidate, whether it is the IDENTICAL physical product as the researched
item — never trusting image-search ranking or title similarity alone.

This is the same strict gallery-vs-gallery judge used by cj-sourcing/match.py,
adapted to be standalone (no cj_client dependency) and fed by image search
instead of keyword search.

INPUT  (search_results.json — written by china_image_search.py):
  [
    {
      "name": "...", "slug": "...", "source": "aliexpress|temu|meta|amazon|google",
      "images": ["/path/or/url/to/our/product/photo", ...],   # the query image(s)
      "site": "alibaba|1688",
      "candidates": [
        {"offer_id":"...", "title":"...", "price": 12.3, "currency":"CNY|USD",
         "supplier":"...", "url":"https://...", "image":"https://..."}, ...
      ]
    }, ...
  ]

OUTPUT (matched.json):
  per product: best IDENTICAL candidate (offer_id + price + url) or route=no-match,
  plus the full per-candidate verdict list.

JUDGE MODES (same as match.py):
  --judge openrouter   auto, needs OPENROUTER_API_KEY (vision model)
  --judge agent        writes packet.json dirs for the Claude Code agent to verify

USAGE
  python match_china.py --in search_results.json --judge openrouter \
      --model google/gemini-3-pro-image-preview --min-conf 0.85 --out matched.json
  python match_china.py --in search_results.json --judge agent --packdir ./_packets
"""
from __future__ import annotations
import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

# Shared Gemini/vision transport lives in 05-launch-niche-store/scripts (stdlib-only,
# so it imports cleanly into this step's separate venv). Direct Gemini by default,
# OpenRouter fallback — resolved from ~/.launch-niche/settings.toml [image_gen].
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import gemini_client as gem  # noqa: E402

JUDGE_PROMPT = (
    "You are verifying a 1:1 product match for dropship sourcing. The FIRST image "
    "set is the product we want to source (found on AliExpress/Temu/Amazon/a "
    "competitor/an ad). The SECOND set is the FULL gallery of a candidate offer from "
    "a Chinese supplier site (Alibaba.com / 1688). The candidate may be a "
    "MULTI-VARIANT listing (several colors/sizes/styles under one offer); its "
    "available variants and key specs are given as text. Decide whether the candidate "
    "listing carries the IDENTICAL physical product a customer would receive: same "
    "form factor, parts/ports/controls, materials. CRUCIAL: the matching color/size "
    "must be an AVAILABLE VARIANT of the listing — judge variants from the WHOLE "
    "gallery + the variant text, NOT just the first/hero image (the hero often shows "
    "a different colorway than the variant we want). "
    "PIXELS OVER TEXT — CRITICAL: variant/spec TEXT only proves COLOR/SIZE availability. "
    "It does NOT prove that a physical BUNDLE ACCESSORY or COMPONENT is included. Any "
    "included item the source product shows (e.g. a toy gun, a holster, a mask, a bag, a "
    "remote, a cable, mounting hardware) must be VISUALLY CONFIRMED in the candidate's "
    "gallery PHOTOS before you count it as present — never infer it from an ambiguous "
    "variant string (e.g. a Chinese term like 背带/套装/全套 does NOT by itself prove a "
    "specific accessory ships; the term may mean a plain strap/harness, not the holster+gun "
    "in the source photo). If the source bundle contains an accessory you cannot SEE in any "
    "candidate gallery image, the listing is NOT identical (verdict DIFFERENT or UNCERTAIN) "
    "and you MUST name the missing accessory in differences[]. "
    "FINE DETAIL — INSPECT CLOSELY, DO NOT SKIM: zoom into the product and compare the SMALL "
    "distinguishing details, not just the overall silhouette. Two products can share a shape but "
    "be different items. Systematically check, on BOTH galleries: (1) the COUNT and PLACEMENT of "
    "buttons / ports / controls / vents / holes / nozzles / slots; (2) connector / plug / cable / "
    "valve / fitting type; (3) hinge / clasp / buckle / latch / zipper / strap mechanism and how it "
    "attaches; (4) texture & finish — matte vs gloss, woven vs molded, ribbed vs smooth, "
    "transparent vs opaque, metal vs plastic; (5) seams, stitching, weld lines, edge & corner shape, "
    "bevels; (6) any TEXT / NUMBERS / ICONS / EMBOSSING / engraving / molded logo physically ON the "
    "product (read it — a different model number or printed label = a different product, distinct "
    "from a removable branding STICKER which you ignore); (7) decorative motif / pattern / print; "
    "(8) PROPORTIONS — relative size of parts to each other, thickness, aspect ratio, segment count "
    "(e.g. a 3-bar vs 4-bar grille, a 5- vs 6-blade fan). A mismatch in ANY load-bearing small "
    "detail (not merely color/angle/sticker) means it is NOT the same physical product → verdict "
    "DIFFERENT or UNCERTAIN, and you MUST name the specific detail in differences[]. List the "
    "concrete small details you actually compared in details_checked[]. "
    "Ignore background/lighting/angle/removable-branding-sticker differences. Reply as strict JSON: "
    '{"verdict":"IDENTICAL|UNCERTAIN|DIFFERENT","confidence":0-1,'
    '"matching_variant":"<the variant that matches our product, or empty>",'
    '"details_checked":["<concrete small details you compared, e.g. \'4 side vents both\', '
    '\'USB-C port same position\', \'molded logo on base differs\'>"],'
    '"accessories_seen":["<bundle items you actually SEE in the candidate photos>"],'
    '"accessories_missing":["<source-bundle items NOT visible in any candidate photo>"],'
    '"differences":["..."]}'
)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


# alicdn (1688/Alibaba image CDN) throttles bare urllib fetches with HTTP 420
# ("Enhance Your Calm") after the first hit — so a plain urlretrieve grabs the query
# image then fails every candidate, starving the judge. A browser UA + Referer +
# backoff on the throttle codes fixes it.
_IMG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://detail.1688.com/",
}


def _dl(url: str, dest: pathlib.Path, tries: int = 4) -> bool:
    """Fetch a remote image, or copy a local path, into dest. Returns success.
    Retries with backoff on alicdn throttle codes (420/429/503)."""
    try:
        if url.startswith(("http://", "https://", "//")):
            if url.startswith("//"):
                url = "https:" + url
            for k in range(tries):
                try:
                    req = urllib.request.Request(url, headers=_IMG_HEADERS)
                    with urllib.request.urlopen(req, timeout=25) as r:
                        data = r.read()
                    if not data:
                        return False
                    dest.write_bytes(data)
                    return True
                except urllib.error.HTTPError as e:
                    if e.code in (420, 429, 503) and k < tries - 1:
                        time.sleep(0.8 * (k + 1))
                        continue
                    return False
        else:  # already a local file
            src = pathlib.Path(url)
            if not src.exists():
                return False
            dest.write_bytes(src.read_bytes())
            return True
    except Exception:
        return False
    return False


def make_packet(prod: dict, packdir: str) -> dict:
    """Download our query images + each candidate's image into a packet dir so the
    judge (model or agent) compares galleries side by side."""
    slug = prod.get("slug") or slugify(prod.get("name", "product"))
    d = pathlib.Path(packdir) / slug
    (d / "query").mkdir(parents=True, exist_ok=True)

    query_imgs = prod.get("images") or ([prod["image"]] if prod.get("image") else [])
    query_paths = []
    for i, u in enumerate(query_imgs[:4]):
        if u and _dl(u, d / "query" / f"q{i + 1}.jpg"):
            query_paths.append(str(d / "query" / f"q{i + 1}.jpg"))

    cand_meta = []
    for j, c in enumerate(prod.get("candidates", [])):
        cd = d / f"cand{j + 1}_{c.get('offer_id', j)}"
        cd.mkdir(exist_ok=True)
        cps = []
        # Prefer the enriched full gallery (images[]); fall back to the lone hero.
        # Up to 6 so a multi-variant listing's matching colorway is actually seen.
        gallery = (c.get("images") or []) + [c.get("image")]
        seen = set()
        for u in gallery:
            if not u or u in seen:
                continue
            seen.add(u)
            if _dl(u, cd / f"c{len(cps) + 1}.jpg"):
                cps.append(str(cd / f"c{len(cps) + 1}.jpg"))
            if len(cps) >= 6:
                break
        cand_meta.append({**c, "image_paths": cps})

    packet = {
        "slug": slug,
        "site": prod.get("site"),
        "query": {"name": prod.get("name"), "source": prod.get("source"),
                  "images": query_paths},
        "candidates": cand_meta,
    }
    (d / "packet.json").write_text(json.dumps(packet, indent=2))
    return packet


def load_calibration(path: pathlib.Path) -> str | None:
    """Close the learning loop: read the operator-feedback store (written by the operator
    app when a human rates a match good/bad) and distil it into a calibration preamble that
    biases the judge away from its past mistakes. Overturned IDENTICAL calls are the gold
    signal — they teach the model to be more skeptical when a bundle accessory or exact
    variant can't be SEEN in the candidate gallery."""
    try:
        fb = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(fb, dict) or not fb:
        return None
    confirmed = [v for v in fb.values() if isinstance(v, dict) and v.get("verdict") == "good"]
    overturned = [v for v in fb.values() if isinstance(v, dict) and v.get("verdict") == "bad"]
    lines = []
    for v in overturned[:10]:
        subj = v.get("subject") or "?"
        seg = (f"- '{subj}': you judged {v.get('ai_verdict') or '?'} "
               f"(offer {v.get('ai_offer_id') or '?'}), but the operator OVERTURNED it")
        if v.get("correct_offer_id"):
            seg += f"; the correct offer was {v['correct_offer_id']}"
        if v.get("note"):
            seg += f" — {v['note']}"
        lines.append(seg)
    if not lines and not confirmed:
        return None
    head = (f"CALIBRATION FROM {len(fb)} PAST OPERATOR REVIEWS "
            f"({len(confirmed)} of your matches were confirmed correct, "
            f"{len(overturned)} were overturned). ")
    if lines:
        head += ("Learn from these overturned calls — when in doubt, prefer UNCERTAIN over "
                 "IDENTICAL, and never count a bundle accessory you cannot SEE:\n" + "\n".join(lines))
    else:
        head += "Your recent matches held up; keep applying the same strict gallery test."
    return head


def judge_openrouter(packet: dict, model: str | None, calibration: str | None = None) -> list[dict] | None:
    """Vision-judge each candidate via the shared Gemini client (DIRECT Gemini by
    default, OpenRouter fallback). Returns None if no API key is resolvable (caller
    falls back to agent mode). `calibration` (if present) is a learning-loop preamble
    distilled from past operator corrections — injected so the judge improves over time."""
    out = []
    for c in packet["candidates"]:
        if not c["image_paths"] or not packet["query"]["images"]:
            out.append({"offer_id": c.get("offer_id"), "verdict": "UNCERTAIN",
                        "confidence": 0, "differences": ["missing images"]})
            continue
        # Ordered parts: prompt, our query gallery, then the candidate gallery +
        # its variant/spec text (so the judge knows which colorways/sizes exist).
        ctx = f"CHINA CANDIDATE offer_id={c.get('offer_id')} title={c.get('title', '')}\n"
        if c.get("variants"):
            ctx += "AVAILABLE VARIANTS: " + " | ".join(
                f"{v.get('name')}: {', '.join(str(x) for x in v.get('values', []))}"
                for v in c["variants"]) + "\n"
        if c.get("specs"):
            ctx += "SPECS: " + "; ".join(
                f"{k}={v}" for k, v in list(c["specs"].items())[:12]) + "\n"
        parts = [("text", JUDGE_PROMPT)]
        if calibration:
            parts.append(("text", calibration))
        parts.append(("text", "PRODUCT WE WANT TO SOURCE:"))
        parts += [("image", p) for p in packet["query"]["images"]]
        parts += [("text", ctx + "GALLERY:")]
        parts += [("image", p) for p in c["image_paths"]]
        try:
            txt = gem.vision(parts, model=model)
            if txt is None:
                return None  # no key resolvable -> caller uses agent mode
            v = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
        except Exception as e:  # noqa: BLE001
            v = {"verdict": "UNCERTAIN", "confidence": 0, "error": str(e)}
        out.append({"offer_id": c.get("offer_id"), "price": c.get("price"),
                    "currency": c.get("currency"), "url": c.get("url"),
                    # keep the candidate hero image + title so the operator-app review UI
                    # ("open the N judged") can show each candidate as image + url, and so
                    # operator feedback (good/bad find) has a thumbnail to attach to.
                    "image": c.get("image") or (c.get("images") or [None])[0],
                    "title": c.get("title"),
                    "supplier": c.get("supplier"), "sold": c.get("sold"),
                    "stock": c.get("stock"), "price_min": c.get("price_min"),
                    "price_max": c.get("price_max"), **v})
    return out


def pick_score(j: dict) -> tuple:
    """Rank IDENTICAL candidates so we pick the SMARTER offer, not just the cheapest.

    Image search surfaces both the real, complete product listing AND cheap
    accessory/partial SKUs (e.g. a ¥3.90 'gloves only' variant of an otherwise ¥30
    full costume) — the lowest price is usually a trap that ships less than the photo
    shows. A proven listing with real sales is the safer source. Order of preference:
      1. has real sales volume (sold) — proven, complete product a buyer actually got
      2. higher stock — supplier can fulfil
      3. higher judge confidence
    Price is deliberately NOT a ranking input (lowest price = the accessory trap)."""
    sold = j.get("sold") or 0
    stock = j.get("stock") or 0
    return (1 if sold else 0, sold, stock, j.get("confidence", 0))


# Verdict tiers for the "closest near-match" fallback (most-similar first).
_NEAR_TIER = {"IDENTICAL": 2, "UNCERTAIN": 1, "DIFFERENT": 0}


def near_score(j: dict) -> tuple:
    """Rank ALL judged candidates by HOW SIMILAR they are, for the fallback when no exact
    1:1 IDENTICAL match exists — so the scan surfaces the closest option instead of nothing
    (operator: "if it doesn't find 1:1 it can find similar, as high similar as possible").

    Confidence means different things per verdict: a confident IDENTICAL/UNCERTAIN is MORE
    similar, but a confident DIFFERENT is LESS similar (the model is sure it's a different
    product). So within the DIFFERENT tier we invert confidence — a 'barely different' (low
    confidence DIFFERENT) ranks above a 'definitely different'. Sales/stock break ties toward
    a proven, fulfillable supplier."""
    verdict = j.get("verdict", "UNCERTAIN")
    conf = j.get("confidence", 0) or 0
    sim = conf if verdict != "DIFFERENT" else (1 - conf)
    sold = j.get("sold") or 0
    stock = j.get("stock") or 0
    return (_NEAR_TIER.get(verdict, 1), sim, 1 if sold else 0, sold, stock)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--packdir", default="./_packets")
    ap.add_argument("--judge", choices=["agent", "openrouter"], default="agent",
                    help="openrouter = auto vision judge via shared client "
                         "(DIRECT Gemini by default per settings.toml; OpenRouter fallback)")
    ap.add_argument("--model", default=None,
                    help="override the vision model (default resolves per provider: "
                         "gemini-3-pro-preview for google)")
    ap.add_argument("--min-conf", type=float, default=0.85)
    ap.add_argument("--out", default="matched.json")
    ap.add_argument("--feedback", default="match-feedback.json",
                    help="operator review log; distilled into a calibration preamble "
                         "so the judge learns from past good/bad calls")
    args = ap.parse_args()

    calib = load_calibration(pathlib.Path(args.feedback))
    products = json.loads(pathlib.Path(args.infile).read_text())
    results = []
    for p in products:
        packet = make_packet(p, args.packdir)
        decision = {"name": p.get("name"), "slug": packet["slug"], "site": p.get("site"),
                    "source": p.get("source"), "n_candidates": len(packet["candidates"]),
                    "matched": None, "route": "no-match",
                    # Provenance stamp — records HOW this verdict was produced so a downstream
                    # consumer can tell a real (vision/agent) judgement from a hand-typed stub.
                    # openrouter = automated VLM judge; agent = Claude Code packet verification.
                    "provenance": {"tool": "match_china.py", "judge": args.judge,
                                   "model": args.model, "vision": args.judge == "openrouter",
                                   "calibrated": bool(calib),
                                   "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
        if args.judge == "openrouter":
            judged = judge_openrouter(packet, args.model, calib)
            if judged is None:
                decision["note"] = "OPENROUTER_API_KEY not set -> use --judge agent"
            else:
                decision["judged"] = judged
                identical = [j for j in judged
                             if j.get("verdict") == "IDENTICAL"
                             and j.get("confidence", 0) >= args.min_conf]
                best = max(identical, key=pick_score, default=None)
                _near_keys = ("offer_id", "price", "currency", "url", "supplier", "verdict",
                              "confidence", "matching_variant", "sold", "stock",
                              "price_min", "price_max", "details_checked",
                              "accessories_seen", "accessories_missing", "differences")
                if best:
                    decision["matched"] = {k: best.get(k) for k in _near_keys}
                    decision["route"] = "matched"
                elif judged:
                    # No exact 1:1 — surface the CLOSEST candidate so the scan never returns
                    # nothing. It is NOT auto-adopted as the source (matched stays None to keep
                    # the dropship-fraud guard): the operator sees the best near-match WITH its
                    # differences/missing accessories and decides. (operator 2026-06-23)
                    closest = max(judged, key=near_score)
                    decision["closest"] = {k: closest.get(k) for k in _near_keys}
                    decision["route"] = "near-match"
        else:
            decision["note"] = (f"AGENT VERIFY: open {args.packdir}/{packet['slug']}/packet.json, "
                                "compare query vs each candidate gallery, set matched offer if IDENTICAL.")
        results.append(decision)
        tag = decision["route"]
        if decision["matched"]:
            m = decision["matched"]
            sold = f" sold={m['sold']}" if m.get("sold") else ""
            var = f" [{m['matching_variant']}]" if m.get("matching_variant") else ""
            miss = m.get("accessories_missing") or []
            misstr = f"  ⚠ MISSING: {', '.join(str(x) for x in miss)}" if miss else ""
            extra = f"  -> {m['offer_id']} @ {m['price']}{sold}{var}{misstr}"
        elif decision.get("closest"):
            c = decision["closest"]
            diff = c.get("differences") or c.get("accessories_missing") or []
            diffstr = f"  ~differs: {', '.join(str(x) for x in diff)}" if diff else ""
            extra = (f"  ~closest {c.get('offer_id')} @ {c.get('price')} "
                     f"({c.get('verdict')} conf={c.get('confidence')}){diffstr}")
        else:
            extra = ""
        print(f"{tag:10} {(p.get('name') or '')[:45]:45}  cands={decision['n_candidates']}{extra}")

    pathlib.Path(args.out).write_text(json.dumps(results, indent=2))
    n = sum(1 for r in results if r["route"] == "matched")
    print(f"\n{n}/{len(results)} matched to a China supplier -> {args.out}")


if __name__ == "__main__":
    main()
