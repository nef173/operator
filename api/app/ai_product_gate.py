"""AI product-domain gate — a SECOND-OPINION classifier that catches non-dropship keywords the
deterministic blocklist (readers._is_generic_product_trend) can't know: NOVEL brands, airlines,
car models, places, people, media titles, services, news/events. It generalizes instead of
requiring an ever-growing hand-maintained wordlist.

Design contract (matches the operator's ask — "if unsure include still, operator can click x"):
  • SECOND pass — runs AFTER the cheap deterministic filter, only on terms that survive it, so the
    LLM sees fewer terms and known junk is already gone for free.
  • INCLUSIVE bias — the model is told to reject ONLY when CONFIDENT it is not a sourceable product;
    anything uncertain returns ok=true (kept). The operator's X remains the final say.
  • FAIL-OPEN — no gateway configured / API error / unparseable reply / term dropped from the batch
    → ok=true (include). The gate can only ADD safety, never block the pipeline or nuke real products.
  • CACHED — every unique term is judged once and persisted (a keyword's product-ness doesn't change),
    so steady-state cost is ~0; only brand-new terms hit the API.
  • CHEAP LOCKED TIER — forces the gateway's default flash model (not the operator's chat selection),
    same cost discipline as VisionScan / translate.
"""
from __future__ import annotations

import json
import urllib.request

from . import config, connections

_BATCH = 40           # terms per LLM call
_MAX_TOKENS = 2000
_TIMEOUT = 45

_SYSTEM = (
    "You classify search keywords for a Google-Shopping DROPSHIPPING business. For each keyword "
    "decide if it names a GENERIC PHYSICAL PRODUCT a dropshipper could source from a supplier "
    "(AliExpress / CJ / a sourcing agent) and resell — e.g. 'cooling blanket', 'portable air "
    "conditioner', 'dog nail grinder', 'car phone holder', 'suv trunk organizer', 'turkey fryer'.\n"
    "Set ok=false ONLY when you are CONFIDENT the keyword is NOT such a product, i.e. it is a:\n"
    "  - brand or company name (Toyota, JetBlue, Dyson, Nike, Home Depot)\n"
    "  - airline / car make or model / specific electronics model (Camry, iPhone 17, PS5)\n"
    "  - place, country or city (Portugal, Bali, Paris)\n"
    "  - a specific PERSON / celebrity / sports TEAM or PLAYER / media or software title (Taylor "
    "Swift, AC Milan, Modric, Sabalenka, Netflix, ChatGPT) — BUT a broad recurring EVENT you could "
    "sell themed products around (World Cup, Olympics, Super Bowl, Black Friday) IS a keeper, keep it\n"
    "  - service, info, news, weather or ticket query (ac repair, flights, weather, bitcoin price, tickets)\n"
    "  - an OVERLY-BROAD bare category naming no specific product (bare 'shoes', 'clothing', "
    "'electronics', 'furniture', 'appliances') or a vague non-product word ('jet', 'new', 'deals')\n"
    "  - PERISHABLE food / groceries / pet food that spoils and can't be dropshipped (dog food, cat "
    "food, fresh produce, meat, snacks) — but shelf-stable supplements/vitamins ARE products, keep those\n"
    "  - the LARGE ITEM ITSELF that is too big/heavy to ship (an actual swimming pool, garden shed, hot "
    "tub, full mattress, vehicle, large furniture). IMPORTANT: small ACCESSORIES for these ARE products "
    "— pool float, pool noodle, pool cover, pool chair, pool pump, mattress topper/pad — KEEP those.\n"
    "When UNSURE, set ok=true — a human reviews everything downstream, so keep it.\n"
    "Return ONLY a JSON array, one object per input keyword, echoing the keyword verbatim in \"k\":\n"
    "[{\"k\":\"<keyword>\",\"ok\":true,\"why\":\"<=6 words\"}]\n"
    "No markdown, no prose, just the JSON array."
)


def _configured() -> bool:
    return bool(connections.runtime_get("ASSISTANT_LLM_BASE_URL")
                and connections.runtime_get("ASSISTANT_LLM_API_KEY"))


def _cheap_model(base: str) -> str:
    """A locked, cheap, RELIABLE text model for this classifier — NOT the operator's chat selector.
    On OpenRouter, Google's Gemini route 403s ('provider Terms Of Service'); Anthropic's Haiku is
    cheap + reliable for a JSON classification, so pin it. On a Gemini-direct gateway, flash is the
    only servable tier."""
    if "openrouter" in base.lower():
        return "anthropic/claude-haiku-4.5"
    return connections.runtime_get("ASSISTANT_LLM_MODEL") or "gemini-2.5-flash"


def _call(system: str, user: str) -> str:
    """One OpenAI-compatible chat call on the LOCKED cheap tier (NOT the operator's chat selector).
    Stdlib urllib; raises on any transport/parse problem (caller fails open)."""
    base = (connections.runtime_get("ASSISTANT_LLM_BASE_URL") or "").rstrip("/")
    key = connections.runtime_get("ASSISTANT_LLM_API_KEY") or ""
    payload = {
        "model": _cheap_model(base),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": _MAX_TOKENS,
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    if "openrouter" in base.lower():  # OpenRouter's recommended attribution headers
        headers["X-Title"] = "Google Stores Operator"
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):  # some gateways return a list of parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


def _parse_json_array(raw: str) -> list | None:
    """Extract a JSON array from the model reply — tolerates ```json fences or leading prose."""
    if not raw:
        return None
    s = raw.strip()
    if "```" in s:
        for chunk in s.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("["):
                try:
                    return json.loads(c)
                except (json.JSONDecodeError, ValueError):
                    continue
    a, b = s.find("["), s.rfind("]")
    if a != -1 and b > a:
        try:
            return json.loads(s[a:b + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _classify_batch(terms: list[str]) -> dict[str, dict]:
    """One LLM call for up to _BATCH terms → {lower_term: {ok, why}}. Fail-OPEN on everything."""
    if not terms:
        return {}
    if not _configured():
        return {t.lower(): {"ok": True, "why": "ai-unavailable"} for t in terms}
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(terms))
    try:
        arr = _parse_json_array(_call(_SYSTEM, f"Classify these {len(terms)} keywords:\n{numbered}"))
    except Exception:  # noqa: BLE001 — any failure → include everything
        return {t.lower(): {"ok": True, "why": "ai-error"} for t in terms}
    out: dict[str, dict] = {}
    for obj in arr or []:
        if not isinstance(obj, dict):
            continue
        k = str(obj.get("k", "")).strip().lower()
        if k:
            out[k] = {"ok": bool(obj.get("ok", True)), "why": str(obj.get("why", ""))[:60]}
    for t in terms:  # fail-open for any term the model dropped/renamed
        out.setdefault(t.lower(), {"ok": True, "why": "no-verdict-include"})
    return out


# ------------------------------------------------------------------ seller (competitor) classifier
# For "Find products" we only want INDEPENDENT Google-Shopping dropship STORES — never marketplaces
# (Amazon/Walmart/eBay) or manufacturer brands (BedJet/Costway/Tacool). The deterministic list
# catches the well-known ones; this generalizes to NOVEL brands by NAME PATTERN (a coined single-word
# product name reads as a brand even when unrecognized).
_SELLER_SYSTEM = (
    "You classify e-commerce SELLERS for dropshipping competitor research. We ONLY want small "
    "INDEPENDENT online stores (niche Shopify-style shops with their own store name/domain). We must "
    "EXCLUDE:\n"
    "  - marketplaces & big-box retailers (Amazon, Walmart, eBay, Etsy, Target, Best Buy, Home "
    "Depot, Lowe's, Costco, Wayfair, Ace Hardware, AliExpress, Temu, and any 'Walmart - X' seller)\n"
    "  - recognizable manufacturer / product BRANDS (Dyson, Shark, BedJet, Costway, Dreo, LG, "
    "Samsung), and sellers whose name is clearly a single product model/brand token (ALL-CAPS or a "
    "model code like 'WAP1-08C').\n"
    "Set indie=true for anything that could be a small independent online store. Independent stores "
    "OFTEN have short coined brand names too, so do NOT reject just because a name is unfamiliar or "
    "invented — only reject when you actually RECOGNIZE it as a big retailer/marketplace/brand or it "
    "is obviously a bare product model. When in any doubt, indie=true (a human reviews).\n"
    "Return ONLY a JSON array, one object per seller, echoing it in \"s\":\n"
    "[{\"s\":\"<seller>\",\"indie\":true,\"why\":\"<=5 words\"}]  No prose."
)


def _seller_cache_path():
    return config.data_root() / "ai-seller-verdicts.json"


def classify_sellers(sellers: list[str], use_cache: bool = True) -> dict[str, dict]:
    """Judge sellers → {lower_seller: {indie: bool, why: str}}. Cached; fail-OPEN to indie=true."""
    norm: dict[str, str] = {}
    for s in sellers:
        k = (s or "").strip()
        if k:
            norm.setdefault(k.lower(), k)
    verdicts: dict[str, dict] = {}
    cache = {}
    if use_cache:
        try:
            cache = json.loads(_seller_cache_path().read_text())
            cache = cache if isinstance(cache, dict) else {}
        except (OSError, ValueError):
            cache = {}
    todo = [orig for low, orig in norm.items() if not (use_cache and low in cache)]
    for low, orig in norm.items():
        if use_cache and low in cache:
            verdicts[low] = cache[low]
    dirty = False
    for i in range(0, len(todo), _BATCH):
        batch = todo[i:i + _BATCH]
        res = _classify_seller_batch(batch)
        for low, v in res.items():
            verdicts[low] = v
            if use_cache:
                cache[low] = v
                dirty = True
    if use_cache and dirty:
        try:
            p = _seller_cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cache))
        except OSError:
            pass
    return verdicts


def _classify_seller_batch(sellers: list[str]) -> dict[str, dict]:
    if not sellers:
        return {}
    if not _configured():
        return {s.lower(): {"indie": True, "why": "ai-unavailable"} for s in sellers}
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sellers))
    try:
        arr = _parse_json_array(_call(_SELLER_SYSTEM, f"Classify these {len(sellers)} sellers:\n{numbered}"))
    except Exception:  # noqa: BLE001 — fail open to indie=true
        return {s.lower(): {"indie": True, "why": "ai-error"} for s in sellers}
    out: dict[str, dict] = {}
    for obj in arr or []:
        if isinstance(obj, dict) and str(obj.get("s", "")).strip():
            out[str(obj["s"]).strip().lower()] = {"indie": bool(obj.get("indie", True)),
                                                   "why": str(obj.get("why", ""))[:60]}
    for s in sellers:
        out.setdefault(s.lower(), {"indie": True, "why": "no-verdict-include"})
    return out


def is_independent_store(seller: str) -> bool:
    """True = keep (independent store or unsure); False = confident marketplace/brand. Fail-open."""
    v = classify_sellers([seller]).get((seller or "").strip().lower())
    return bool(v.get("indie", True)) if v else True


# ------------------------------------------------------------------ catalog title matcher
# Token matching can't bridge synonyms/abbreviations ('portable air conditioner' vs a store that
# titles it 'Mark 2 AC'), and must exclude accessories ('Hose Cover For Mark 2 AC'). The AI reads a
# store's product titles and returns which ones ARE the searched product.
_MATCH_SYSTEM = (
    "You match products to a shopping search. Given a SEARCH phrase and a numbered list of product "
    "titles from ONE store, return the numbers of the titles that are the SAME product type as the "
    "search. Treat abbreviations/synonyms as matches (e.g. 'AC' = 'air conditioner', 'mount' = "
    "'holder', 'tumbler' = 'cup'). EXCLUDE accessories / parts / spares / add-ons (hose, cover, "
    "filter, replacement, mount kit, solar panel, battery) UNLESS the search itself is for that part. "
    "Return ONLY a JSON array of the matching numbers (1-based), e.g. [1,4,7]. [] if none."
)


def match_catalog_titles(keyword: str, titles: list[str]) -> set[int] | None:
    """Which of `titles` are the product `keyword` is searching for (indices, 0-based). Handles
    synonyms/abbreviations + drops accessories. Returns None on no-gateway/error/unparseable so the
    caller falls back to token matching (fail-safe — never silently drops everything)."""
    clean = [(i, str(t or "").strip()) for i, t in enumerate(titles) if str(t or "").strip()]
    if not clean or not _configured():
        return None
    hits: set[int] = set()
    for start in range(0, len(clean), 60):
        chunk = clean[start:start + 60]
        numbered = "\n".join(f"{j + 1}. {t}" for j, (_, t) in enumerate(chunk))
        try:
            arr = _parse_json_array(_call(_MATCH_SYSTEM, f"SEARCH: {keyword}\nTITLES:\n{numbered}"))
        except Exception:  # noqa: BLE001
            return None
        if arr is None:
            return None
        for n in arr:
            try:
                idx = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(chunk):
                hits.add(chunk[idx][0])
    return hits


# ------------------------------------------------------------------ store-type classifier
# The Google-Shopping lane must keep only real DROPSHIPPERS, not manufacturer BRANDS. Deterministic
# signals fail here — a brand (zerobreeze) and a dropshipper (belroshop) BOTH house-brand their
# products under their own domain name. The separator is catalog coherence + brand recognition,
# which the LLM judges: one proprietary product line = brand; varied commodity products = dropshipper.
_STORE_TYPE_SYSTEM = (
    "Classify an online store as exactly one of: dropshipper, brand, marketplace.\n"
    "- dropshipper: resells GENERIC, supplier-sourced commodity products (AliExpress/Temu-style) — a "
    "general store with many UNRELATED categories, or a young store pushing a few trending no-name "
    "products under a house label. Anyone could source the same items.\n"
    "- brand: a MANUFACTURER selling its OWN proprietary/patented product line under a real brand — a "
    "coherent catalog built around ONE product family. Examples: zerobreeze.com (Zero Breeze portable "
    "AC), sylvansport.com (SylvanSport campers), geertop.com (GeerTop outdoor gear), dyson.com. Even "
    "if the brand is unfamiliar, a catalog that is ONE branded product line = brand, NOT a dropshipper.\n"
    "- marketplace: Amazon/Walmart/eBay-style multi-seller retailer.\n"
    "We ONLY want dropshippers. Given the domain + sample product titles, return ONLY "
    '{"type":"dropshipper"|"brand"|"marketplace","why":"<=8 words"}.'
)


def _parse_json_object(raw: str) -> dict | None:
    if not raw:
        return None
    s = raw.strip()
    if "```" in s:
        for chunk in s.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{"):
                try:
                    return json.loads(c)
                except (json.JSONDecodeError, ValueError):
                    continue
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(s[a:b + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def classify_store_type(domain: str, sample_titles: list[str]) -> str | None:
    """'dropshipper' | 'brand' | 'marketplace' for a store, from its domain + sample product titles.
    None on no-gateway/error (caller falls back to a deterministic heuristic)."""
    titles = [str(t).strip() for t in sample_titles if str(t or "").strip()][:25]
    if not titles or not _configured():
        return None
    listed = "\n".join(f"- {t}" for t in titles)
    try:
        obj = _parse_json_object(_call(_STORE_TYPE_SYSTEM, f"STORE: {domain}\nSAMPLE PRODUCTS:\n{listed}"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    t = str(obj.get("type", "")).strip().lower()
    return t if t in ("dropshipper", "brand", "marketplace") else None


def _cache_path():
    return config.data_root() / "ai-keyword-verdicts.json"


def _load_cache() -> dict:
    try:
        d = json.loads(_cache_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache))
    except OSError:
        pass


def classify(terms: list[str], use_cache: bool = True, refresh: bool = False) -> dict[str, dict]:
    """Judge a list of terms → {lower_term: {ok: bool, why: str}}. Cached per unique term.
    refresh=True re-classifies even cached terms (and rewrites the cache) — use after a prompt change."""
    norm: dict[str, str] = {}
    for t in terms:
        k = (t or "").strip()
        if k:
            norm.setdefault(k.lower(), k)
    verdicts: dict[str, dict] = {}
    cache = _load_cache() if use_cache else {}
    todo: list[str] = []
    for low, orig in norm.items():
        if use_cache and not refresh and low in cache:
            verdicts[low] = cache[low]
        else:
            todo.append(orig)
    dirty = False
    for i in range(0, len(todo), _BATCH):
        batch = todo[i:i + _BATCH]
        res = _classify_batch(batch)
        for low, v in res.items():
            verdicts[low] = v
            if use_cache:
                cache[low] = v
                dirty = True
    if use_cache and dirty:
        _save_cache(cache)
    return verdicts


def is_dropshippable(term: str) -> bool:
    """Single-term gate. True = keep (product or unsure); False = confident non-product. Fail-open."""
    v = classify([term]).get((term or "").strip().lower())
    return bool(v.get("ok", True)) if v else True


# ------------------------------------------------------------------ eval harness (prove it works)
# Labeled test set: real dropshippable products (expect KEEP) + non-products (expect REJECT). The
# critical metric is FALSE REJECTS (real product wrongly rejected) — that must be ~0. Missed junk is
# tolerable (inclusive bias + operator X backstop). Deliberately includes the exact junk from the
# incident (jet blue / toyota / portugal / breeze) AND ambiguous keepers (suv/turkey/china/air).
_EVAL_KEEP = [
    "cooling blanket", "portable air conditioner", "neck fan", "dog cooling mat", "knife sharpener",
    "bidet attachment", "memory foam pillow", "standing desk converter", "car phone holder",
    "suv trunk organizer", "turkey fryer", "china cabinet", "air fryer", "posture corrector",
    "resistance bands", "cold plunge tub", "electric spin scrubber", "led strip lights",
    "cordless vacuum", "weighted blanket", "cat water fountain", "sunset lamp", "back massager",
    "3 in 1 charging station", "reusable food wraps", "adjustable dumbbells", "car trunk organizer",
    "wireless meat thermometer", "collapsible laundry basket", "silicone stretch lids",
]
_EVAL_REJECT = [
    "jet blue", "jetblue airways", "toyota", "toyota camry", "toyota rav4", "portugal", "bali",
    "delta airlines", "breeze airways", "british airways", "taylor swift", "nfl scores",
    "bitcoin price", "netflix", "iphone 17 pro max", "home depot", "chatgpt", "weather today",
    "flights to paris", "ac repair near me", "honda civic", "tesla stock", "super bowl tickets",
    "united airlines", "car insurance quotes",
]


def evaluate(use_cache: bool = False) -> dict:
    """Run the classifier over the labeled set and score it. Returns accuracy + the two error
    lists that matter: false_rejects (real product killed — DANGER, want empty) and missed_junk
    (junk kept — tolerable, X backstop)."""
    labeled = {t: True for t in _EVAL_KEEP}
    labeled.update({t: False for t in _EVAL_REJECT})
    verdicts = classify(list(labeled), use_cache=use_cache)
    rows, false_rejects, missed_junk, correct = [], [], [], 0
    for term, expect_keep in labeled.items():
        ok = bool((verdicts.get(term.lower()) or {}).get("ok", True))
        why = (verdicts.get(term.lower()) or {}).get("why", "")
        hit = (ok == expect_keep)
        correct += hit
        if expect_keep and not ok:
            false_rejects.append(term)
        elif not expect_keep and ok:
            missed_junk.append(term)
        rows.append({"term": term, "expected": "keep" if expect_keep else "reject",
                     "got": "keep" if ok else "reject", "why": why, "correct": hit})
    n = len(labeled)
    kept_real = sum(1 for t in _EVAL_KEEP if bool((verdicts.get(t.lower()) or {}).get("ok", True)))
    caught_junk = sum(1 for t in _EVAL_REJECT if not bool((verdicts.get(t.lower()) or {}).get("ok", True)))
    return {
        "configured": _configured(),
        "n": n,
        "accuracy": round(correct / n, 3) if n else None,
        "real_products_kept": f"{kept_real}/{len(_EVAL_KEEP)}",   # want ALL — false rejects are harmful
        "junk_rejected": f"{caught_junk}/{len(_EVAL_REJECT)}",    # want high — catches novel junk
        "false_rejects": false_rejects,                            # DANGER list — must be empty
        "missed_junk": missed_junk,                                # tolerable (inclusive + X backstop)
        "rows": rows,
    }
