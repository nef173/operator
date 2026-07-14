#!/usr/bin/env python3
"""
mtop_1688.py — 1688-DIRECT bulk research via the mobile H5 JSON API (the x5sec bypass).

WHY this exists: 1688's HTML search pages (s.1688.com/selloffer/...) are walled by
x5sec/AntBot (the /_____tmd_____/punish?x5secdata= challenge), which is why every
Bright Data layer (Web-Unlocker, Scraping Browser, Scraper-Studio AI-gen) fails on
1688. But 1688's *mobile* API host `h5api.m.1688.com` is NOT gated by the JS
challenge — it is gated only by a cheap CLIENT-SIDE MD5 `sign`. Compute that sign
locally and the JSON comes straight back. This is the path every open-source 1688
scraper uses (Zhui-CN/1688_image_search_crawler, netkaruma/search1688api,
tiam-bloom/1688-scraper).

THE SIGN ALGORITHM (the whole trick):
    token = cookie `_m_h5_tk` value, split on "_", take [0]
    t     = current epoch milliseconds (string)
    data  = the request body as a COMPACT json string (no spaces)
    sign  = md5( token + "&" + t + "&" + appKey + "&" + data )   # hex
  appKey for the 1688 H5 web app = 12574478

THE TOKEN BOOTSTRAP: the very first call has no `_m_h5_tk` cookie, so it returns
`FAIL_SYS_TOKEN_EMPTY` / `FAIL_SYS_TOKEN_EXPIRED` and Set-Cookie's a fresh token.
We keep a requests.Session, make a priming call, read the token from the cookie,
then re-sign and re-send the real call. Token is reusable for ~? minutes and is
IP-bound, so reuse the same Session (+ same proxy session) across a batch.

SCALE / PARALLEL: each request is just an MD5 + an HTTP GET — no browser, no
captcha — so this fans out cleanly across a ThreadPoolExecutor. For VOLUME a
CN-residential / mobile IP is strongly recommended (datacenter IPs get x5sec-walled
even on the H5 host once you push rate). Point --proxy at a BD CN-residential
sticky session. We do NOT have a residential zone provisioned yet (account has only
web_unlocker / browser zones) — until then this runs direct/low-volume for probing,
and the paid managed APIs (TMAPI / OTAPI / Onebound / Apify zen-studio) are the
drop-in fallback that bundle the CN IP + x5sec solver for you.

ENGLISH: keyword search takes whatever string you pass (1688 will match latin text
weakly; for best recall translate the keyword to zh first). IMAGE search needs no
language at all — it is the recommended path for English-sourced products.

USAGE
  # keyword search (one or many; batch fans out in parallel)
  python mtop_1688.py keyword --keywords "summer jacket, knife sharpener" --out kw.json
  python mtop_1688.py keyword --keywords-file kws.txt --proxy "$BD_CN_PROXY" --out kw.json

  # image search (captcha-free): upload an image, get offers
  python mtop_1688.py image --image ./hero.jpg --out img.json
  python mtop_1688.py image --image https://.../hero.jpg --proxy "$BD_CN_PROXY"

OUTPUT shape matches search_results.json (what match_china.py consumes):
  [{"name","slug","source":"1688","site":"1688",
    "candidates":[{"offer_id","title","price","currency","moq","supplier","url","image"}]}]

The api-name + data payload for each endpoint are EXPOSED as constants below because
mtop endpoint names drift; if a probe returns FAIL_SYS_ILLEGAL_ACCESS or an empty
ret, adjust API_KEYWORD / API_IMAGE_PUT / the data builders after a live probe
(run with --debug to dump the raw envelope).

=== PROVEN LIVE 2026-06-18 (from our own US datacenter IP, NO proxy) ===
  ✅ The MD5 sign + _m_h5_tk token bootstrap WORKS — server accepts our signature
     and processes the call (we get business-level rets, never FAIL_SYS_TOKEN_*).
  ✅ `mtop.1688.imageService.putImage` (POST) returns a real `imageId` for any
     uploaded product image — captcha-free, no CN IP needed. This is the upload
     half of image search and it is fully working on our own infra.
  ❌ The RESULT half is gated to a CN IP:
       - the recommend RESULT feed `mtop.relationrecommend.WirelessRecommend.recommend`
         returns `FAIL_BIZ_PARAM_ERR::request is forbidden` for EVERY valid scene
         appId (24723/27936/30540/45902/20593...) from a foreign datacenter IP.
       - the non-mtop result view `search.1688.com/service/imageSearchOfferResult
         ViewService?imageId=` is x5sec-walled (returns the /_____tmd_____/punish page).
  ⇒ FINISH PATH for a FREE self-hosted 1688-direct image search: route this whole
    client through a Bright Data **CN-residential sticky session** (set --proxy /
    BD_CN_PROXY). We do NOT have a residential zone provisioned yet — that one zone
    is the only missing piece. Once added, putImage + the recommend result feed both
    run from a CN IP and the offer list comes back as clean JSON, bulk + parallel.
  ⇒ NO-PROVISION ALTERNATIVE (works today, costs $): a managed 1688 API that bundles
    the CN IP + x5sec solver + English image search — Onebound/万邦 (cheapest),
    TMAPI (easiest English image search), OTAPI (free test tier). See onebound_1688.py.
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import pathlib
import re
import time
import urllib.parse

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

APP_KEY = "12574478"                       # 1688 H5 web app appKey
H5_BASE = "https://h5api.m.1688.com/h5"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

# --- endpoint config (drift-prone; adjust after a live --debug probe) ---------
# Keyword offer search. Several mtop apis serve offer lists; this is the most
# commonly working H5 one. If it 401s, try mtop.1688.offerService.queryOfferList
# or mtop.alibaba.cbu.search.offer.
API_KEYWORD = ("mtop.relationrecommend.WirelessRecommend.recommend", "2.0")
# Image search is a two-step flow: putImage -> imageSearch result view.
API_IMAGE_PUT = ("mtop.1688.imageService.putImage", "1.0")
IMAGE_SEARCH_VIEW = "https://search.1688.com/service/imageSearchOfferResultViewService"


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"([\d.]+)", str(v).replace(",", ""))
    return float(m.group(1)) if m else None


class MtopClient:
    """Holds a Session + the _m_h5_tk token; signs and sends mtop GET calls."""

    def __init__(self, proxy: str | None = None, timeout: int = 30, debug: bool = False):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://m.1688.com/"})
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
            self.s.verify = False
        self.timeout = timeout
        self.debug = debug

    # ---- token --------------------------------------------------------------
    def _token(self) -> str:
        tk = self.s.cookies.get("_m_h5_tk", "")
        return tk.split("_")[0] if tk else ""

    def _prime(self, api: str, version: str, data: str) -> None:
        """First (unsigned/garbage-signed) call just to receive the _m_h5_tk cookie."""
        if self._token():
            return
        self._send(api, version, data, _priming=True)

    # ---- core signed send ---------------------------------------------------
    def _send(self, api: str, version: str, data: str, _priming: bool = False,
              post: bool = False) -> dict:
        t = str(int(time.time() * 1000))
        token = self._token()
        sign = md5_hex(f"{token}&{t}&{APP_KEY}&{data}")
        params = {
            "jsv": "2.7.0", "appKey": APP_KEY, "t": t, "sign": sign,
            "api": api, "v": version, "type": "originaljson",
            "dataType": "json", "valueType": "original", "timeout": "20000",
        }
        url = f"{H5_BASE}/{api}/{version}/"
        # Large payloads (image base64) MUST go in the POST body — a GET query
        # string truncates past ~8 KB and the server returns an empty envelope.
        if post or len(data) > 4000:
            r = self.s.post(url, params=params, data={"data": data},
                            timeout=self.timeout)
        else:
            params["data"] = data
            r = self.s.get(url, params=params, timeout=self.timeout)
        try:
            env = r.json()
        except Exception:  # noqa: BLE001
            env = {"_raw": r.text[:2000]}
        if self.debug and not _priming:
            print(f"[mtop] {api} ret={env.get('ret')} keys={list(env.get('data', {}) if isinstance(env.get('data'), dict) else {})}")
        return env

    def call(self, api: str, version: str, payload: dict) -> dict:
        """Sign+send with one automatic token-refresh retry (handles TOKEN_EXPIRED)."""
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self._prime(api, version, data)
        env = self._send(api, version, data)
        ret = " ".join(env.get("ret", []) if isinstance(env.get("ret"), list) else [])
        if "TOKEN" in ret.upper() or "FAIL_SYS_TOKEN" in ret.upper():
            # cookie just got (re)set by that response; retry once signed
            env = self._send(api, version, data)
        return env

    # ---- searches -----------------------------------------------------------
    def keyword_search(self, keyword: str, page: int = 1, page_size: int = 60) -> dict:
        payload = {"appName": "na4045", "appKey": APP_KEY,
                   "keywords": keyword, "keyword": keyword,
                   "beginPage": page, "pageSize": page_size,
                   "pageNum": page, "n": page_size}
        return self.call(*API_KEYWORD, payload)

    def image_put(self, image_b64_or_url: str) -> dict:
        payload = {"appName": "na4045", "appKey": APP_KEY,
                   "imageBase64": image_b64_or_url}
        return self.call(*API_IMAGE_PUT, payload)


# --- response normalisation (defensive: mtop offer shapes vary) --------------
def _walk_offers(env: dict):
    """Yield offer-like dicts from anywhere in an mtop envelope."""
    data = env.get("data", env) if isinstance(env, dict) else {}
    stack = [data]
    seen_lists = 0
    while stack and seen_lists < 20:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    # heuristically an offer list if items have id/title-ish keys
                    if any(any(t in (kk.lower()) for t in ("offer", "title", "subject", "price"))
                           for kk in v[0].keys()):
                        seen_lists += 1
                        for it in v:
                            yield it
                    else:
                        stack.extend(x for x in v if isinstance(x, dict))
                elif isinstance(v, dict):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(x for x in node if isinstance(x, (dict, list)))


def normalize(env: dict, keyword: str) -> list[dict]:
    cands, seen = [], set()
    for o in _walk_offers(env):
        oid = (o.get("offerId") or o.get("id") or o.get("offer_id")
               or o.get("itemId") or "")
        oid = str(oid)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        title = (o.get("subject") or o.get("title") or o.get("name")
                 or o.get("simpleSubject") or "")
        price = (o.get("price") or o.get("priceInfo") or o.get("sellQuantityPrice")
                 or (o.get("priceRange") or {}))
        if isinstance(price, dict):
            price = price.get("price") or price.get("min") or price.get("value")
        url = (o.get("detailUrl") or o.get("offerUrl")
               or (f"https://detail.1688.com/offer/{oid}.html" if oid.isdigit() else ""))
        img = (o.get("imageUrl") or o.get("image") or o.get("picUrl") or "")
        if isinstance(img, dict):
            img = img.get("url") or img.get("fullPathImageURI") or ""
        cands.append({
            "offer_id": oid,
            "title": str(title)[:200],
            "price": _num(price), "currency": "CNY",
            "moq": _num(o.get("quantityBegin") or o.get("minOrderQuantity") or o.get("moq")),
            "supplier": o.get("companyName") or o.get("sellerNick") or o.get("memberId"),
            "sold": _num(o.get("saleQuantity") or o.get("monthSold") or o.get("soldQuantity")),
            "url": url,
            "image": img if isinstance(img, str) else "",
        })
    return cands


# --- CLI ---------------------------------------------------------------------
def _read_keywords(args) -> list[str]:
    kws = []
    if args.keywords:
        kws += [k.strip() for k in args.keywords.split(",")]
    if args.keywords_file:
        kws += [l.strip() for l in pathlib.Path(args.keywords_file).read_text().splitlines()
                if l.strip() and not l.startswith("#")]
    seen, out = set(), []
    for k in kws:
        if k and k.lower() not in seen:
            seen.add(k.lower()); out.append(k)
    return out


def _proxy_from_env(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return os.environ.get("BD_CN_PROXY") or None


def cmd_keyword(args) -> None:
    keywords = _read_keywords(args)
    if not keywords:
        raise SystemExit("Give --keywords or --keywords-file.")
    proxy = _proxy_from_env(args.proxy)

    def one(kw: str) -> dict:
        cli = MtopClient(proxy=proxy, debug=args.debug)   # own session/token per kw
        env = cli.keyword_search(kw, page=1, page_size=args.page_size)
        cands = normalize(env, kw)
        ret = env.get("ret")
        print(f"{len(cands):3d} offers  {kw[:40]:<40} ret={ret}")
        return {"name": kw, "slug": slugify(kw), "source": "1688", "site": "1688",
                "candidates": cands, "_ret": ret}

    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(one, keywords):
            results.append(r)
    pathlib.Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    tot = sum(len(r["candidates"]) for r in results)
    print(f"\n{tot} candidates across {len(keywords)} keywords -> {args.out}")


def cmd_image(args) -> None:
    import base64
    proxy = _proxy_from_env(args.proxy)
    img = args.image
    if not img.startswith(("http://", "https://")):
        b = pathlib.Path(img).read_bytes()
        img = base64.b64encode(b).decode()
    cli = MtopClient(proxy=proxy, debug=args.debug)
    env = cli.image_put(img)
    pathlib.Path(args.out).write_text(json.dumps(env, indent=2, ensure_ascii=False))
    print(f"putImage ret={env.get('ret')} -> {args.out}")
    print("If ret==SUCCESS, pull imageId from data and GET", IMAGE_SEARCH_VIEW,
          "?imageId=<id> for the offer list (see --debug envelope).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keyword", help="keyword offer search (bulk/parallel)")
    k.add_argument("--keywords")
    k.add_argument("--keywords-file")
    k.add_argument("--page-size", type=int, default=60)
    k.add_argument("--workers", type=int, default=6)
    k.add_argument("--proxy", help="CN-residential proxy URL (or set BD_CN_PROXY)")
    k.add_argument("--out", default="search_1688.json")
    k.add_argument("--debug", action="store_true")
    k.set_defaults(func=cmd_keyword)

    i = sub.add_parser("image", help="image search (captcha-free)")
    i.add_argument("--image", required=True, help="local path or URL")
    i.add_argument("--proxy")
    i.add_argument("--out", default="search_1688_image.json")
    i.add_argument("--debug", action="store_true")
    i.set_defaults(func=cmd_image)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
