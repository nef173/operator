#!/usr/bin/env python3
"""Competitor BEST-SELLER ranking via the BD Scraping Browser.

A Shopify store's OWN best-selling order (`/collections/all?sort_by=best-selling`) is the only sales
signal it exposes — but the BD Web Unlocker (immediate mode) is robots-blocked on `/collections/` for
many stores, and the plain fetch is bot-blocked (403/503). A REAL browser (the BD Scraping Browser)
doesn't check robots.txt and renders fine. This drives it over CDP, reads the product cards in
best-selling order across the top N pages, and prints the ranked products as JSON to stdout.

Reads the CDP wss from BRIGHTDATA_BROWSER_CDP (connections.as_env; assembled by BD provision).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_GEO_CC = {"US": "us", "GB": "gb", "UK": "gb", "CA": "ca", "AU": "au", "DE": "de", "FR": "fr", "NL": "nl"}
_SKIP = {"shipping-protection"}


def _with_country(cdp: str, geo: str) -> str:
    if "-country-" in (cdp or ""):
        return cdp
    cc = _GEO_CC.get((geo or "US").upper(), "us")
    return re.sub(r"(-zone-[A-Za-z0-9_]+)", rf"\1-country-{cc}", cdp, count=1)


def _price(text: str) -> str | None:
    m = (re.search(r"Sale price\s*(?:from\s*)?\$?([\d,]+\.?\d*)", text)
         or re.search(r"\$([\d,]+\.?\d*)", text))
    return m.group(1).replace(",", "") if m else None


def _title(text: str) -> str | None:
    for ln in (text or "").splitlines():
        s = ln.strip()
        if (len(s) > 6 and "$" not in s
                and s.lower() not in ("quick view", "sale", "regular price", "add to cart", "sold out")):
            return s
    return None


def _read_cards(page, url: str, timeout_ms: int) -> list[dict]:
    """Load one best-seller page and return its product cards in DOM (= best-seller) order. Scrolls to
    trigger lazy-loaded grids. Raises on nav failure so the caller can retry."""
    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    # nudge lazy-load grids (many themes only render cards on scroll into view).
    try:
        for _ in range(3):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001 — scroll is best-effort
        pass
    try:
        page.wait_for_selector('a[href*="/products/"]', timeout=6000)
    except Exception:  # noqa: BLE001 — fall through; eval returns [] if truly none
        pass
    return page.eval_on_selector_all(
        'a[href*="/products/"]',
        "els => els.map(e => ({href: e.getAttribute('href')||'', "
        "text: ((e.closest('li,.grid__item,.card-wrapper,.product-card,article,.card,.product-item')||e)"
        ".innerText||'').slice(0,300)}))")


def capture(domain: str, pages: int, cdp: str, geo: str = "US", timeout_ms: int = 45000) -> list[dict]:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 — heavy, lazy

    endpoint = _with_country(cdp, geo)
    out: list[dict] = []
    seen: set[str] = set()
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            for pg in range(1, pages + 1):
                url = f"https://{domain}/collections/all?sort_by=best-selling&page={pg}"
                cards = []
                # Retry each page — the remote browser cold-starts / flakes on the first hit for a
                # store (the belroshop '0 scanned' symptom). Retry the FIRST page harder.
                for attempt in range((3 if pg == 1 else 2)):
                    try:
                        cards = _read_cards(page, url, timeout_ms)
                        if cards:
                            break
                    except Exception:  # noqa: BLE001 — transient; retry
                        cards = []
                    page.wait_for_timeout(1500)
                page_added = 0
                for c in cards:
                    m = re.search(r"/products/([a-z0-9][a-z0-9\-]*)", c.get("href") or "")
                    if not m:
                        continue
                    h = m.group(1)
                    if h in seen or h in _SKIP:
                        continue
                    seen.add(h)
                    out.append({"rank": len(out) + 1, "handle": h,
                                "title": _title(c.get("text") or "") or h.replace("-", " ").title(),
                                "price": _price(c.get("text") or ""),
                                "url": f"https://{domain}/products/{h}"})
                    page_added += 1
                if page_added == 0:  # empty page after retries = past the end (or dead store)
                    break
        finally:
            browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Competitor best-seller ranking via BD Scraping Browser.")
    ap.add_argument("--domain", required=True)
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--geo", default="US")
    args = ap.parse_args()

    cdp = (os.environ.get("BRIGHTDATA_BROWSER_CDP") or "").strip()
    if not cdp:
        print(json.dumps({"ok": False, "error": "no BRIGHTDATA_BROWSER_CDP (run BD provision)"}))
        return 1
    dom = re.sub(r"^https?://|/.*$|^www\.", "", (args.domain or "").strip().lower())
    if not dom:
        print(json.dumps({"ok": False, "error": "no domain"}))
        return 1
    try:
        prods = capture(dom, max(1, min(int(args.pages or 5), 10)), cdp, geo=(args.geo or "US").upper())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}))
        return 1
    print(json.dumps({"ok": True, "domain": dom, "products": prods}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
