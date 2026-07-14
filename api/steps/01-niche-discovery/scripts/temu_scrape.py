#!/usr/bin/env python3
"""
Reliable Temu research scraper via the AdsPower trusted US profile.

WHY THIS DESIGN: Temu defeats every hands-off scrape — its internal API is
anti-content-token + 403 walled (curl_cffi Chrome-impersonation returns 403/500),
the Web Unlocker returns empty, and a fresh browser is bounced to login/verification.
The ONLY durable path is a real anti-detect browser (AdsPower) running a US proxy with
an aged, **signed-in** Temu session. Sign-in is a ONE-TIME human step; after that the
session cookies persist for weeks and this script scrapes repeatedly with no CAPTCHA.

PREREQ (one-time, human): open the AdsPower profile and sign into Temu once
(Google/email), ship-to United States. Then this script works until the session ages out.

USAGE:
  cd 01-niche-discovery/scripts
  "<scraper-dashboard>/.venv/bin/python" temu_scrape.py "diamond painting kit" --profile k1cleycc --limit 30
  (any venv with `patchright` or `playwright` + `requests` works; it ATTACHES to the
   AdsPower Chrome over CDP, so no browser download is needed.)

OUTPUT: JSON list [{title, price, sold, url}] sorted by sold-count desc.
If it hits the login wall it exits with a clear SIGN-IN-REQUIRED message (not a crash).
"""
from __future__ import annotations
import argparse, json, re, sys, time, urllib.request

ADS_API = "http://local.adspower.net:50325"   # AdsPower Local API (also 127.0.0.1:50325)

def ads_start(profile_id: str) -> dict:
    # Local API port varies per install; try the common ones.
    bases = [ADS_API, "http://127.0.0.1:50325", "http://local.adspower.net:50325",
             "http://127.0.0.1:50335", "http://127.0.0.1:50300"]
    for base in bases:
        try:
            u = f"{base}/api/v1/browser/start?user_id={profile_id}&open_tabs=0&headless=0"
            d = json.load(urllib.request.urlopen(u, timeout=20))
            if d.get("code") == 0:
                return d["data"]
        except Exception:
            continue
    raise SystemExit(
        "AdsPower Local API auto-start not reachable. Open the profile first "
        "(AdsPower UI → Open, or the AdsPower MCP open-browser) then re-run with "
        "--cdp-port <debug_port shown by open-browser>.")

def connect(cdp_http: str):
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SystemExit("install patchright or playwright in this venv (pip install patchright)")
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_http)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, browser, page

EXTRACT_JS = r"""
() => {
  const out = []; const seen = new Set();
  const cards = document.querySelectorAll('a[href*="goods.html"], a[href*="goods_id"], a[href*="-g-"]');
  cards.forEach(a => {
    const href = a.href || '';
    const m = href.match(/goods_id=(\d+)/) || href.match(/-g-(\d+)/);
    const id = m ? m[1] : href;
    if (seen.has(id)) return; seen.add(id);
    const txt = (a.innerText || a.textContent || '').replace(/\s+/g,' ').trim();
    const price = (txt.match(/\$[\d,.]+/) || [''])[0];
    const sold = (txt.match(/([\d.,]+[KkMm]?\+?)\s*sold/) || [,''])[1];
    let title = '';
    a.querySelectorAll('*').forEach(el => { if(!el.children.length){const t=(el.textContent||'').trim(); if(t.length>title.length && !t.startsWith('$') && t.length<200) title=t;} });
    if (title || price) out.push({id, title, price, sold, url: href.split('?')[0]});
  });
  return out;
}
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword")
    ap.add_argument("--profile", default="k1cleycc", help="AdsPower profile_id (default = warmup us)")
    ap.add_argument("--cdp-port", default="", help="attach to an already-open profile's debug_port (skips auto-start)")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    if args.cdp_port:
        cdp_http = f"http://127.0.0.1:{args.cdp_port}"
    else:
        data = ads_start(args.profile)
        cdp_http = f"http://127.0.0.1:{data['debug_port']}"
    print(f"AdsPower profile {args.profile} — CDP {cdp_http}", file=sys.stderr)
    pw, browser, page = connect(cdp_http)
    try:
        url = f"https://www.temu.com/search_result.html?search_key={urllib.parse.quote(args.keyword)}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(7):
            page.mouse.wheel(0, 1600); time.sleep(1.0)
        cur = page.url
        if "login" in cur or "verification" in cur:
            print(json.dumps({"error": "SIGN_IN_REQUIRED",
                              "message": f"Temu redirected to {cur.split('?')[0]}. "
                              f"One-time fix: in AdsPower profile {args.profile}, sign into Temu (ship-to US) once; "
                              f"then re-run — the session persists.", "results": []}, indent=2))
            return
        items = page.evaluate(EXTRACT_JS)
        def sold_num(s):
            s=(s or "").lower().replace("+","").replace(",","")
            mult=1000 if "k" in s else (1000000 if "m" in s else 1)
            try: return float(re.sub(r"[km]","",s))*mult
            except: return 0
        items.sort(key=lambda x: sold_num(x.get("sold")), reverse=True)
        items = items[:args.limit]
        print(f"=== {len(items)} Temu products for {args.keyword!r} (US trusted session) ===", file=sys.stderr)
        for it in items:
            print(f"  {(it['title'] or '')[:50]:50}  {it['price'] or '?':>9}  sold {it['sold'] or '?'}")
        if args.save:
            import pathlib; pathlib.Path(args.save).write_text(json.dumps(items, indent=2))
            print(f"saved -> {args.save}", file=sys.stderr)
    finally:
        try: browser.close()
        except Exception: pass
        try: pw.stop()
        except Exception: pass

if __name__ == "__main__":
    import urllib.parse
    main()
