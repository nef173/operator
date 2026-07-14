#!/usr/bin/env python3
"""
china_image_search.py — reverse-IMAGE search a product on Chinese supplier sites
(Alibaba.com international, or 1688 domestic) via a REAL fingerprinted Chrome on a
residential IP (AdsPower), the same approach that makes 01c's paid_shopping_scan
work where headless scrapers get CAPTCHA'd.

Step 1 of the china-source-match pipeline. Step 2 = match_china.py (VLM judge).

WHY a real browser: 1688/Taobao image search ("拍立淘") only serves results to a
logged-in account on a CN/HK residential IP and is guarded by Alibaba's x5sec
anti-bot. Web Unlocker returns empty and BD's Scraping Browser gets x5sec-walled
(both verified). A warmed AdsPower profile on a Bright Data residential CN/HK
proxy is the reliable path. Alibaba.com (international) is far softer — a normal
profile, English, no login — so it's the default; 1688 is the deeper tier.

  --site alibaba   alibaba.com image search   (normal/US profile; testable now)
  --site 1688      s.1688.com image search    (profile on BD residential country-cn
                                                + a logged-in 1688/Taobao account)

PIPELINE per input product:
  download our query image locally -> AdsPower Chrome -> site image-search entry
  -> upload the image into the page's <input type=file> -> wait for results
  -> [DOM] best-effort extract candidate cards (offer_id/title/price/supplier/img)
  -> ALWAYS screenshot the results so the Claude orchestrator can vision-extract
     as the reliable fallback (the proven paid_shopping_scan pattern).

INPUT  products.json: [{"name","image" (url or local path) | "images":[...],
                        "source","slug"}, ...]
OUTPUT search_results.json: each product + {"site","candidates":[...]} ready for
       match_china.py, plus screenshots/ for agent vision extraction.

USAGE
  python china_image_search.py --in products.json --site alibaba --out ./china-scan
  python china_image_search.py --in products.json --site 1688 \
      --profile-id <cn_profile> --out ./china-scan
  # attach to an already-open AdsPower browser (skips the start api-key requirement):
  python china_image_search.py --in products.json --site 1688 --debug-port 9222 --out ./china-scan
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import requests

# Load env (ADSPOWER_*, optional OPENROUTER_API_KEY) from a nearby .env.
try:
    from dotenv import load_dotenv
    _here = Path(__file__).resolve().parent
    for _candidate in [_here, *_here.parents]:
        if (_candidate / ".env").exists():
            load_dotenv(_candidate / ".env")
            break
except ImportError:
    pass

ADSPOWER_BASE = os.environ.get("ADSPOWER_BASE", "http://local.adspower.net:50325")
DEFAULT_PROFILE = os.environ.get("ADSPOWER_PROFILE_ID", "k1cleycc")  # "warmup us"

# Per-site image-search config. entry = page that exposes the upload input.
# file_input_selectors / camera_selectors are tried in order; they are best-effort
# and meant to be tuned on the first live run (the screenshot fallback covers gaps).
SITES = {
    "alibaba": {
        "entry": "https://www.alibaba.com/",
        "camera_selectors": [
            "div[class*='search-bar-camera']", "a[class*='camera']",
            "span[class*='camera']", "[data-spm*='imagesearch']", "i[class*='camera']",
        ],
        "file_input_selectors": ["input[type=file]"],
        "results_url_marker": "picturesearch",
        # DOM extractor runs in the results page; returns [{offer_id,title,price,...}]
        "extractor": r"""() => {
          const out = [];
          const cards = document.querySelectorAll(
            "[class*='organic-list'] [class*='card'], .list-no-v2-outter, .J-offer-wrapper, [data-content*='offer']");
          cards.forEach(c => {
            const a = c.querySelector("a[href*='/product-detail/'], a[href*='offer']");
            const img = c.querySelector("img");
            const priceEl = c.querySelector("[class*='price']");
            const t = (c.querySelector("[class*='title'], h2, a[title]") || {}).innerText
                       || (a && a.getAttribute('title')) || '';
            const href = a ? a.href : '';
            const m = href.match(/(\d{6,})/);
            if (href || t) out.push({
              offer_id: m ? m[1] : null,
              title: (t || '').trim().slice(0,160),
              price_text: priceEl ? priceEl.innerText.trim() : '',
              supplier: '',
              url: href,
              image: img ? (img.src || img.getAttribute('data-src') || '') : ''
            });
          });
          return out.slice(0, 30);
        }""",
    },
    "1688": {
        "entry": "https://s.1688.com/",
        "camera_selectors": [
            "div[class*='camera']", "i[class*='camera']", "[class*='img-search']",
            "[class*='photo-search']", "span[class*='camera']",
        ],
        "file_input_selectors": ["input[type=file]"],
        "results_url_marker": "imageId",
        "extractor": r"""() => {
          const out = [];
          const cards = document.querySelectorAll(
            ".offer-list-row-offer, .sm-offer-item, [class*='offer'] [class*='item'], .J_offerCard");
          cards.forEach(c => {
            const a = c.querySelector("a[href*='detail.1688.com'], a[href*='offer']");
            const img = c.querySelector("img");
            const priceEl = c.querySelector("[class*='price']");
            const t = (c.querySelector("[class*='title']") || {}).innerText
                       || (a && a.getAttribute('title')) || '';
            const href = a ? a.href : '';
            const m = href.match(/offer\/(\d+)/) || href.match(/(\d{8,})/);
            if (href || t) out.push({
              offer_id: m ? m[1] : null,
              title: (t || '').trim().slice(0,160),
              price_text: priceEl ? priceEl.innerText.trim() : '',
              supplier: '',
              url: href,
              image: img ? (img.src || img.getAttribute('data-src') || '') : ''
            });
          });
          return out.slice(0, 30);
        }""",
    },
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def _price_num(s: str) -> float | None:
    if not s:
        return None
    m = re.search(r"[\d,]+\.?\d*", s.replace(",", ""))
    return float(m.group(0)) if m else None


def load_products(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, list) else data.get("products", [])


def ensure_local_image(prod: dict, cache: Path) -> str | None:
    """Image search uploads a FILE, so resolve the product's first image to a local
    path (download it if it's a URL). Returns the local path or None."""
    imgs = prod.get("images") or ([prod["image"]] if prod.get("image") else [])
    if not imgs:
        return None
    src = imgs[0]
    if not src:
        return None
    if not src.startswith(("http://", "https://", "//")):
        return src if Path(src).exists() else None
    if src.startswith("//"):
        src = "https:" + src
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{slugify(prod.get('name') or 'q')}-{abs(hash(src)) % 10**8}.jpg"
    try:
        urllib.request.urlretrieve(src, dest)
        return str(dest)
    except Exception as e:
        print(f"  (could not download query image {src}: {e})", file=sys.stderr)
        return None


# ---- AdsPower control (same contract as paid_shopping_scan.py) ----------------
def adspower_start(profile_id: str) -> dict[str, Any]:
    params = {"user_id": profile_id, "headless": "0"}
    api_key = os.environ.get("ADSPOWER_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    url = f"{ADSPOWER_BASE}/api/v1/browser/start?{urllib.parse.urlencode(params)}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(
            f"AdsPower start failed: {d.get('msg')}. Set ADSPOWER_API_KEY in .env "
            "(AdsPower -> Settings -> API), or open the profile manually and pass "
            "--debug-port <port>.")
    return d["data"]


def adspower_stop(profile_id: str) -> None:
    try:
        requests.get(f"{ADSPOWER_BASE}/api/v1/browser/stop?user_id={profile_id}", timeout=30)
    except Exception:
        pass


def upload_and_search(page, site_cfg: dict, image_path: str, settle: float) -> bool:
    """Open the site's image-search upload and feed it our query image. Returns True
    if a file input was successfully populated. Strategy: reveal the camera control
    (some sites only mount the <input type=file> after it's clicked), then
    set_input_files on the first file input found."""
    # Try to click a camera/image-search trigger to mount the hidden file input.
    for sel in site_cfg.get("camera_selectors", []):
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                time.sleep(1.0)
                break
        except Exception:
            continue
    # Now find a file input (may have been there all along, or just mounted).
    for sel in site_cfg.get("file_input_selectors", ["input[type=file]"]):
        try:
            fi = page.query_selector(sel)
            if fi:
                fi.set_input_files(image_path)
                time.sleep(settle)
                return True
        except Exception:
            continue
    return False


def run(products: list[dict], site: str, out_dir: Path, profile_id: str,
        settle: float, debug_port: str | None, keep_open: bool) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: .venv/bin/pip install playwright "
              "&& .venv/bin/playwright install chromium", file=sys.stderr)
        sys.exit(1)

    cfg = SITES[site]
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / "_query_imgs"

    if debug_port:
        cdp = f"http://127.0.0.1:{debug_port}"
    else:
        data = adspower_start(profile_id)
        cdp = f"http://127.0.0.1:{data['debug_port']}"

    results: list[dict] = []
    import base64
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp_sess = ctx.new_cdp_session(page)

        for i, prod in enumerate(products, 1):
            name = prod.get("name") or f"product-{i}"
            slug = prod.get("slug") or slugify(name)
            rec: dict[str, Any] = {**prod, "slug": slug, "site": site,
                                   "images": prod.get("images")
                                   or ([prod["image"]] if prod.get("image") else []),
                                   "candidates": [], "screenshot": None}
            img = ensure_local_image(prod, cache)
            if not img:
                rec["error"] = "no query image"
                results.append(rec)
                print(f"  [{i}/{len(products)}] {name[:40]}  SKIP (no image)", file=sys.stderr)
                continue
            try:
                page.goto(cfg["entry"], wait_until="domcontentloaded", timeout=45000)
                time.sleep(settle)
                page.bring_to_front()
                if "/_____tmd_____/punish" in page.url or "x5sec" in (page.url or ""):
                    rec["error"] = "x5sec anti-bot wall (need warmed CN/HK residential + login)"
                    results.append(rec)
                    print(f"  [{i}/{len(products)}] {name[:40]}  x5sec BLOCKED", file=sys.stderr)
                    continue

                ok = upload_and_search(page, cfg, img, settle + 2)
                if not ok:
                    rec["error"] = "could not find/upload to file input (tune selectors)"
                # screenshot results regardless (agent vision fallback)
                page.bring_to_front()
                shot = cdp_sess.send("Page.captureScreenshot", {"format": "png"})
                p = shots_dir / f"{slug}__{site}.png"
                p.write_bytes(base64.b64decode(shot["data"]))
                rec["screenshot"] = str(p)
                rec["results_url"] = page.url

                # best-effort DOM extraction
                try:
                    cards = page.evaluate(cfg["extractor"])
                except Exception as e:
                    cards = []
                    rec["extractor_error"] = str(e)
                for c in cards:
                    c["price"] = _price_num(c.get("price_text", ""))
                    c["currency"] = "CNY" if site == "1688" else "USD"
                rec["candidates"] = cards
                results.append(rec)
                print(f"  [{i}/{len(products)}] {name[:40]}  -> {len(cards)} candidate(s)"
                      f"  upload_ok={ok}", file=sys.stderr)
            except Exception as e:
                rec["error"] = str(e)
                results.append(rec)
                print(f"  [{i}/{len(products)}] {name[:40]}  ERROR: {e}", file=sys.stderr)

    if not keep_open and not debug_port:
        adspower_stop(profile_id)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--site", choices=list(SITES), default="alibaba")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--profile-id", default=DEFAULT_PROFILE,
                    help=f"AdsPower profile id (default {DEFAULT_PROFILE}). For --site "
                         "1688 use a profile on a BD residential country-cn proxy + a "
                         "logged-in 1688 account.")
    ap.add_argument("--debug-port",
                    help="Attach to an already-open AdsPower browser at this CDP port "
                         "(skips the start api-key requirement).")
    ap.add_argument("--settle", type=float, default=4.0,
                    help="Seconds to wait for page/results render (default 4)")
    ap.add_argument("--keep-open", action="store_true",
                    help="Leave the AdsPower browser open after the run")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    products = load_products(args.infile)
    print(f"Reverse-image searching {len(products)} product(s) on {args.site} "
          f"(profile={args.profile_id})", file=sys.stderr)

    results = run(products, args.site, out_dir, args.profile_id, args.settle,
                  args.debug_port, args.keep_open)

    out_file = out_dir / "search_results.json"
    out_file.write_text(json.dumps(results, indent=2))
    n_cands = sum(len(r.get("candidates", [])) for r in results)
    print(f"\nWrote {out_file}  ({n_cands} candidates across {len(results)} products)",
          file=sys.stderr)
    print(f"Next: python match_china.py --in {out_file} --judge openrouter --out matched.json",
          file=sys.stderr)
    print(f"If DOM extraction returned 0, have the Claude orchestrator vision-read the "
          f"PNGs in {out_dir/'screenshots'} and fill candidates per product.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
