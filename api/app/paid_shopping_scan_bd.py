#!/usr/bin/env python3
"""Server-side Google Sponsored-PLA capture via the Bright Data Scraping Browser (CDP).

The Google "Sponsored products" carousel — the PAYING dropship competitors, with advertiser domains
(lunirel.com, Picadex, Zenido UK…) — is NOT served to proxy/datacenter contexts: DFS returns no ads
block, and the BD Web Unlocker strips the "Sponsored" labels. But the BD SCRAPING BROWSER (a real
residential Chrome, per-country) DOES get it (verified 2026-07 on `air cooler` GB — full carousel, no
CAPTCHA). This drives it over CDP — server-automatable, no local AdsPower.

Reads the CDP wss endpoint from BRIGHTDATA_BROWSER_CDP (exported via connections.as_env; assembled by
brightdata.provision() / brightdata.browser_cdp_endpoint()). Writes {sponsored_products,
sponsored_results, advertisers} — the active paid competitors for a keyword in a country.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

_PRICE = re.compile(r"^[£$€]\s?[\d,]+(?:\.\d{2})?")
# geo -> (gl, hl). UK uses gl=uk (Google's own alias for GB) + en-GB, which is what served the ads.
_GEO_HL = {"US": ("us", "en"), "GB": ("uk", "en-GB"), "UK": ("uk", "en-GB"), "CA": ("ca", "en"),
           "AU": ("au", "en"), "DE": ("de", "de"), "FR": ("fr", "fr"), "NL": ("nl", "nl")}
_SKIP = {"SALE", "Sponsored products", "+ more", "View more from:"}
_GEO_CC = {"US": "us", "GB": "gb", "UK": "gb", "CA": "ca", "AU": "au", "DE": "de", "FR": "fr", "NL": "nl"}


def _with_country(cdp: str, geo: str) -> str:
    """Route the BD Scraping-Browser session through a residential IP in the TARGET country by adding
    `-country-<cc>` to the zone in the wss auth — the SERP's ACTUAL IP (not just gl=) is what decides
    whether Google serves that country's Sponsored PLA ads (the whole point of this capture)."""
    if "-country-" in (cdp or ""):
        return cdp
    cc = _GEO_CC.get((geo or "US").upper(), "us")
    return re.sub(r"(-zone-[A-Za-z0-9_]+)", rf"\1-country-{cc}", cdp, count=1)


def _domain(s: object) -> str | None:
    m = re.search(r"\b([a-z0-9][a-z0-9\-]*\.[a-z]{2,}(?:\.[a-z]{2,})?)\b", str(s or "").lower())
    return m.group(1) if m else None


def _section(lines: list[str], start_label: str, end_labels: tuple[str, ...]) -> list[str]:
    if start_label not in lines:
        return []
    s = lines.index(start_label) + 1
    e = len(lines)
    tail = lines[s:]
    for lbl in end_labels:
        if lbl in tail:
            e = min(e, s + tail.index(lbl))
    return lines[s:e]


def parse_serp_text(text: str) -> dict:
    """Extract the Sponsored-products carousel + Sponsored-results text ads from the SERP innerText."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    sec = _section(lines, "Sponsored products",
                   ("View more from:", "Sponsored results", "Web results", "People also ask",
                    "Find related products", "These searches help"))
    products: list[dict] = []
    seen: set[tuple] = set()
    for i, ln in enumerate(sec):
        if i == 0 or not _PRICE.match(ln):
            continue
        title = sec[i - 1]
        if title in _SKIP or _PRICE.match(title):
            continue
        adv = sec[i + 1] if i + 1 < len(sec) else None
        if adv and _PRICE.match(adv):  # a compare-at price sat between price + advertiser
            adv = sec[i + 2] if i + 2 < len(sec) else None
        network = None
        for j in range(i + 1, min(i + 9, len(sec))):
            if sec[j].startswith("By "):
                network = sec[j][3:].strip()
                break
        key = (title.lower(), ln)
        if key in seen:
            continue
        seen.add(key)
        products.append({"title": title, "price": ln, "advertiser": adv,
                         "domain": _domain(adv), "network": network})
    # Text ads below the carousel ("Sponsored results").
    res = _section(lines, "Sponsored results", ("Hide sponsored results", "Web results", "People also"))
    results: list[dict] = []
    rseen: set[str] = set()
    for i, ln in enumerate(res):
        d = _domain(ln)
        if d and d not in rseen and "google" not in d:
            rseen.add(d)
            results.append({"title": res[i - 1] if i > 0 else None, "domain": d})
    _noise = {"+ more", "more", "sale", ""}
    advertisers = sorted({a for a in (
        (p.get("domain") or p.get("advertiser") or "").strip().removeprefix("www.").rstrip(".")
        for p in products) if a.lower() not in _noise and len(a) > 1})
    return {"sponsored_products": products, "sponsored_results": results, "advertisers": advertisers}


def capture(keyword: str, geo: str, cdp_endpoint: str, timeout_ms: int = 45000) -> dict:
    """Drive the BD Scraping Browser: navigate the Google SERP for the keyword+country and parse the
    Sponsored carousel. Requires playwright (connect_over_cdp to the remote BD browser — no local
    Chromium needed)."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 — heavy import, lazy

    gl, hl = _GEO_HL.get((geo or "US").upper(), ("us", "en"))
    url = f"https://www.google.com/search?q={quote_plus(keyword)}&gl={gl}&hl={hl}&gws_rd=cr"
    consent = False
    endpoint = _with_country(cdp_endpoint, geo)  # route through a residential IP in the target country
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            # Pre-seed Google's consent cookie so a fresh EU/UK session skips the "Before you
            # continue" interstitial that otherwise hides the SERP + the Sponsored ads.
            try:
                ctx.add_cookies([{"name": "SOCS", "value": "CAISNQgQEitib3E",
                                  "domain": ".google.com", "path": "/"}])
            except Exception:  # noqa: BLE001
                pass
            page = ctx.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            # If the consent wall still appears, click through it (Reject/Accept), then wait.
            if "consent.google" in page.url or "Before you continue" in (page.inner_text("body") or ""):
                consent = True
                for sel in ('button:has-text("Reject all")', 'button:has-text("Accept all")',
                            'form[action*="consent"] button', '#L2AGLb', '#W0wltc'):
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click(timeout=4000)
                            page.wait_for_timeout(2500)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                if "search?" not in page.url:
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)  # let the PLA carousel render
            text = page.inner_text("body")
        finally:
            browser.close()
    rec = parse_serp_text(text)
    rec["keyword"] = keyword
    rec["geo"] = geo
    rec["_debug"] = {"chars": len(text or ""), "consent_hit": consent,
                     "has_sponsored_label": "Sponsored products" in (text or ""),
                     "head": (text or "")[:300]}
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="Google Sponsored-PLA capture via BD Scraping Browser.")
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--geo", default="US")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    keyword = (args.keyword or "").strip()
    if not keyword:
        print(json.dumps({"ok": False, "error": "no keyword"}))
        return 1
    cdp = (os.environ.get("BRIGHTDATA_BROWSER_CDP") or "").strip()
    if not cdp:
        print(json.dumps({"ok": False, "error": "no BRIGHTDATA_BROWSER_CDP — run BD provision to "
                          "create the Scraping-Browser zone (needs BRIGHTDATA_CUSTOMER_ID set)"}))
        return 1
    try:
        rec = capture(keyword, (args.geo or "US").upper(), cdp)
    except Exception as e:  # noqa: BLE001 — surface any transport/CDP/timeout error cleanly
        print(json.dumps({"ok": False, "error": f"BD browser capture failed: {type(e).__name__}: {e}"}))
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sponsored-plas.json").write_text(json.dumps(rec, indent=2))
    print(json.dumps({"ok": True, "keyword": keyword, "geo": rec["geo"],
                      "sponsored_products": len(rec["sponsored_products"]),
                      "sponsored_results": len(rec["sponsored_results"]),
                      "advertisers": rec["advertisers"][:25]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
