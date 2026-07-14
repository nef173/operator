"""Photo-duplicate check — the research-time pixel-level vision dedup the SKU plan promises.

The SKU-plan dedup rule caps the SAME physical product at `dedup_cap` different-style
listings. Title matching can't see that two competitor listings with different names are
the same mold from the same supplier — the PHOTOS can. This module runs the found products
of one head keyword through Gemini vision (the locked vision model — see memory
feedback_gemini_is_the_vision_model) and groups images that show the same physical product
across different backgrounds / angles / lighting.

How it stays cheap and bounded:
  * Incremental clustering, ~1 vision call per batch of new images: each call carries one
    REPRESENTATIVE image per existing group + the next batch of new images, and the model
    assigns every new image to an existing group or "new". No N² pairwise calls.
  * Images are downloaded once, size-capped, and sent as data URLs (works on both the
    OpenRouter and Gemini-direct OpenAI surfaces — remote-URL fetching is not portable).
  * A hard per-run image cap; anything beyond it is reported as skipped, never silent.

Transport reuses image_qa's seam (the shared LiteLLM proxy: ASSISTANT_LLM_BASE_URL +
ASSISTANT_LLM_API_KEY via connections; cheap Gemini Flash tier).

Results persist per store in app_settings (key `photo_dedup_results:<store>`, capped to the
most recent keywords — Railway-cost guardrail) and are merged onto /api/sku-plan/found so
the drill-in modal shows which found products are photo-duplicates of each other.
"""
from __future__ import annotations

import base64
import json
import threading
import urllib.request

from . import image_qa, readers, runlog

SETTINGS_PREFIX = "photo_dedup_results:"  # one app_settings key per store

_MAX_IMAGES = 60          # hard per-run cap so a huge find can't burn vision budget
_BATCH = 10               # new images per vision call
_MAX_IMAGE_BYTES = 4_000_000
_KEEP_KEYWORDS = 40       # per-store result history cap (bounds the settings JSON blob)


def configured() -> bool:
    """Same key seam as VisionScan — a vision key present means the check can run."""
    return image_qa._configured()


def vision_status() -> dict:
    """What the SKU-plan banner shows for 'Photo duplicate check'."""
    if configured():
        return {
            "status": "ready",
            "hint": "Runs on a keyword's found products (View found → dup check): matches by product "
                    "TITLE + Gemini vision on the PHOTOS, and flags any already in your store's catalog.",
        }
    return {
        "status": "title-only",
        "hint": "Title + store-catalog matching runs without a key; add the vision/LLM key in "
                "Settings → Connections to also match same products across different photos.",
    }


# ------------------------------------------------------------------ image fetch
def _fetch_data_url(url: str) -> str | None:
    """Download one product image and return it as a data URL, or None on any failure.
    Failures are per-image (reported as skipped) — one dead CDN link never kills the run."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (operator-app photo-dedup)"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — operator-supplied product URLs
            mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            raw = resp.read(_MAX_IMAGE_BYTES + 1)
        if not raw or len(raw) > _MAX_IMAGE_BYTES:
            return None
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        return None


# ------------------------------------------------------------------ vision grouping
_GROUP_SYSTEM = (
    "You compare e-commerce product photos and decide which ones show the SAME physical "
    "product — the same mold / design / shape, even across different backgrounds, angles, "
    "lighting, or watermarks. A different COLOR of the same mold still counts as the same "
    "physical product. A genuinely different design/model is a different product. "
    "Reply with a SINGLE JSON object and nothing else."
)


def _group_prompt(n_reps: int, n_new: int) -> str:
    rep_part = (
        f"The FIRST {n_reps} image(s) are representatives of existing groups, in order: "
        + ", ".join(f"image {i + 1} = group G{i + 1}" for i in range(n_reps))
        + ".\n"
        if n_reps
        else "There are no existing groups yet.\n"
    )
    first_new = n_reps + 1
    return (
        rep_part
        + f"The NEXT {n_new} image(s) (image {first_new}..{n_reps + n_new}) are NEW product photos.\n\n"
        "For EACH new image, decide: does it show the SAME physical product as one of the "
        "existing groups' representatives, or as an EARLIER new image in this same batch?\n"
        'Reply as JSON: {"assignments": [{"image": <image number>, "group": "G<k>" | "same_as_image" | "new", '
        '"same_as": <earlier image number or null>}]}\n'
        'Use "G<k>" to join an existing group, "same_as_image" (+ same_as) to match an earlier new image, '
        'or "new" for a product not seen before. Every new image must appear exactly once.'
    )


def _call_group(rep_urls: list[str], new_urls: list[str]) -> list[dict]:
    """One incremental-clustering vision call. Returns the parsed assignment list; raises on
    transport/parse failure so the caller can degrade to no-op for that batch."""
    from . import connections  # late import — keeps module import light

    base = (connections.runtime_get("ASSISTANT_LLM_BASE_URL") or "").rstrip("/")
    key = connections.runtime_get("ASSISTANT_LLM_API_KEY") or ""
    content: list[dict] = [{"type": "text", "text": _group_prompt(len(rep_urls), len(new_urls))}]
    for u in rep_urls + new_urls:
        content.append({"type": "image_url", "image_url": {"url": u}})
    payload = {
        "model": image_qa._vision_model(),
        "messages": [
            {"role": "system", "content": _GROUP_SYSTEM},
            {"role": "user", "content": content},
        ],
        "max_tokens": 500,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (trusted proxy)
        body = json.loads(resp.read().decode("utf-8"))
    text = body["choices"][0]["message"]["content"] or ""
    obs = image_qa._parse_obs(text)
    assignments = obs.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError("no assignments[] in vision reply")
    return assignments


def _product_key(p: dict) -> str:
    """Same identity seam as found_products_for_head's dedupe: url, else title."""
    return ((p.get("url") or p.get("title") or "").strip().lower())


# ------------------------------------------------------------------ the run
def run(store: str, keyword: str, terms: list[str] | None, dedup_cap: int) -> dict:
    """Group the head keyword's found-product photos by physical product and flag duplicates.

    Returns + persists: {groups, flags, images_checked, images_skipped, ...}. `flags` maps
    product key → {group, order, drop} where `drop` marks members beyond `dedup_cap` in
    their group (the same physical product listed more times than the A/B cap allows)."""
    # Vision is now OPTIONAL: without a key we still run the title-identity + store-catalog
    # layers (no photo grouping). With a key, vision also merges same products across photos.
    has_vision = configured()

    found = readers.found_products_for_head(store, keyword, terms)
    products = [p for p in (found.get("products") or []) if p.get("image") and _product_key(p)]

    # One entry per unique image URL (many lanes can stamp the same listing). Each entry also
    # carries its TITLE identity tokens — the text layer that works even when photos differ or
    # are missing, and the seam the store-catalog existence scan matches on.
    entries: list[dict] = []
    seen_img: set[str] = set()
    for p in products:
        img = str(p["image"])
        if img in seen_img:
            continue
        seen_img.add(img)
        title = p.get("title")
        entries.append({"key": _product_key(p), "image": img, "title": title,
                        "tokens": readers.norm_tokens(title)})

    over_cap = max(0, len(entries) - _MAX_IMAGES)
    entries = entries[:_MAX_IMAGES]

    # ── Store-catalog existence scan ── the "do we already sell this?" pass. Reuses the SAME
    # token matcher the Sourcing-Match store-check uses, so a found product already in the
    # store's catalog (category or SKU title) is flagged regardless of its photo.
    catalog: list[dict] = []
    try:
        catalog = readers.catalog_terms(store)
    except Exception:  # noqa: BLE001 — best-effort; a catalog read must never sink the dedup
        catalog = []
    for e in entries:
        m = readers.match_in_store(e["tokens"], catalog) if catalog else None
        e["in_store"] = bool(m)
        e["in_store_label"] = (m or {}).get("label")

    skipped: list[dict] = []
    fetched: list[dict] = []
    if has_vision:
        for e in entries:
            data_url = _fetch_data_url(e["image"])
            if data_url is None:
                skipped.append(e)
                continue
            fetched.append({**e, "data_url": data_url})
    else:
        # No vision key — skip photo fetching entirely; title + catalog layers do the work.
        skipped = list(entries)

    # groups: list of {members: [entry, ...]} — members in the order they were assigned.
    groups: list[list[dict]] = []
    batch_errors = 0
    for i in range(0, len(fetched), _BATCH):
        batch = fetched[i : i + _BATCH]
        reps = [g[0]["data_url"] for g in groups]
        try:
            assignments = _call_group(reps, [b["data_url"] for b in batch])
        except Exception:
            # Degrade per batch: unjudged images become their own groups (no false dups).
            batch_errors += 1
            for b in batch:
                groups.append([b])
            continue
        n_reps = len(reps)
        # Map "image number" → batch entry; apply in image order so same_as references resolve.
        img_to_group: dict[int, int] = {}
        by_image = {int(a.get("image", -1)): a for a in assignments if isinstance(a, dict)}
        for j, b in enumerate(batch):
            img_no = n_reps + 1 + j
            a = by_image.get(img_no) or {}
            target: int | None = None
            g = str(a.get("group") or "new")
            if g.upper().startswith("G"):
                try:
                    gi = int(g[1:]) - 1
                    if 0 <= gi < n_reps:
                        target = gi
                except ValueError:
                    target = None
            elif g == "same_as_image":
                ref = a.get("same_as")
                if isinstance(ref, int) and ref in img_to_group:
                    target = img_to_group[ref]
            if target is None:
                groups.append([b])
                target = len(groups) - 1
            else:
                groups[target].append(b)
            img_to_group[img_no] = target

    # Images we couldn't fetch still join as singletons so the TITLE layer can catch them —
    # a missing/blocked photo must not hide a same-product duplicate.
    for e in skipped:
        groups.append([e])

    # ── Title-identity merge ── IMAGE is the PRIMARY signal (operator: dedup by the photo, not the
    # title/description). Vision already grouped by photo, so the title layer ONLY bridges products the
    # vision could NOT see (no image / fetch failed) — it must NEVER merge two products the vision
    # actually judged (that's what wrongly grouped a Frigidaire with two LGs when titles were similar).
    # When there's no vision key at all, the title layer is the only signal and merges freely.
    vision_keys = {f["key"] for f in fetched}  # products the vision actually judged by photo
    parent = list(range(len(groups)))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ent_group = [(m, gi) for gi, members in enumerate(groups) for m in members]
    for a in range(len(ent_group)):
        ma, ga = ent_group[a]
        for b in range(a + 1, len(ent_group)):
            mb, gb = ent_group[b]
            # image-primary: if BOTH products were vision-judged, trust the photo grouping — a title
            # match does NOT override it. Title only bridges when ≥1 product had no usable image.
            if has_vision and ma["key"] in vision_keys and mb["key"] in vision_keys:
                continue
            ra, rb = _find(ga), _find(gb)
            if ra != rb and readers.titles_same_product(ma.get("tokens") or set(), mb.get("tokens") or set()):
                parent[max(ra, rb)] = min(ra, rb)
    merged: dict[int, list] = {}
    for gi in range(len(groups)):
        merged.setdefault(_find(gi), []).extend(groups[gi])
    groups = list(merged.values())

    # Flags: every member of a >1 group is a duplicate (same product by photo OR title);
    # members beyond dedup_cap are drops. `in_store` is stamped independently.
    flags: dict[str, dict] = {}
    group_out: list[dict] = []
    for gi, members in enumerate(groups):
        group_out.append({
            "id": gi + 1,
            "size": len(members),
            "members": [{"key": m["key"], "title": m.get("title"), "image": m["image"]} for m in members],
        })
        if len(members) < 2:
            continue
        for order, m in enumerate(members):
            flags[m["key"]] = {
                "group": gi + 1,
                "size": len(members),
                "order": order + 1,
                "drop": order >= max(1, int(dedup_cap)),
            }
    # Store-existence flag per entry (independent of the dup grouping) — merged into flags.
    for e in entries:
        if e.get("in_store"):
            f = flags.setdefault(e["key"], {})
            f["in_store"] = True
            f["in_store_label"] = e.get("in_store_label")
    in_store_count = sum(1 for e in entries if e.get("in_store"))

    result = {
        "store": store,
        "keyword": keyword,
        "checked_at": runlog._now(),
        "model": image_qa._vision_model() if has_vision else "title+catalog (no vision key)",
        "dedup_cap": int(dedup_cap),
        "n_products": len(products),  # image-bearing found products at run time (re-run trigger)
        "images_checked": len(fetched),
        "images_skipped": len(skipped) + over_cap,
        "batch_errors": batch_errors,
        "duplicate_groups": sum(1 for g in groups if len(g) > 1),
        "duplicates": sum(len(g) - 1 for g in groups if len(g) > 1),
        "in_store_count": in_store_count,  # found products the store ALREADY lists
        "groups": [g for g in group_out if g["size"] > 1],  # singletons add noise, keep dups only
        "flags": flags,
    }
    _store_result(store, keyword, result)
    return result


# ------------------------------------------------------------------ persistence + merge
def _settings_key(store: str) -> str:
    return f"{SETTINGS_PREFIX}{store}"


def _store_result(store: str, keyword: str, result: dict) -> None:
    key = _settings_key(store)
    try:
        stored = runlog.setting_get(key) or {}
    except Exception:
        stored = {}
    stored[keyword.strip().lower()] = result
    if len(stored) > _KEEP_KEYWORDS:
        # Keep the most recently checked keywords (bounds the JSON blob).
        newest = sorted(stored.items(), key=lambda kv: kv[1].get("checked_at") or "", reverse=True)
        stored = dict(newest[:_KEEP_KEYWORDS])
    runlog.setting_set(key, stored)


def result_for(store: str, keyword: str) -> dict | None:
    try:
        stored = runlog.setting_get(_settings_key(store)) or {}
    except Exception:
        return None
    return stored.get(keyword.strip().lower())


def dropped_keys(store: str, keyword: str) -> set[str]:
    """Product keys the last dedup run marked as DROP — duplicates BEYOND `dedup_cap` in their photo
    group. The listing handoff skips these so the SAME physical product is never listed more than
    `dedup_cap` times (operator's 'max N of one duplicate product' rule). Empty if never run."""
    r = result_for(store, keyword)
    if not r:
        return set()
    return {k for k, f in (r.get("flags") or {}).items() if isinstance(f, dict) and f.get("drop")}


# ------------------------------------------------------------------ auto-run on find (background)
_DEDUP_WARMING: set[str] = set()
_DEDUP_LOCK = threading.Lock()


def maybe_fire_on_find(store: str, keyword: str, terms: list[str] | None, dedup_cap: int,
                       n_products: int) -> bool:
    """Auto-run the vision dedup in the BACKGROUND when a keyword's finds are viewed — so the duplicate
    check happens ON FIND, not only when the operator clicks it. Fires when it has never run for this
    keyword OR the found-product count has GROWN since the last run (new finds to check). Deduped per
    (store, keyword) so concurrent /found polls don't stack runs. No-op without a vision key or with no
    products. Returns True if a run was fired (so the UI can show 'checking…')."""
    if not configured() or n_products <= 0:
        return False
    prev = result_for(store, keyword)
    # skip if already checked at this count AND that run actually used VISION. Re-run when the count
    # grew by >2 (new finds) OR the last run was title-only (no vision key at the time) — so a stale
    # title-only result self-heals to a real Gemini-vision pass.
    prev_vision = bool(prev and str(prev.get("model") or "").startswith(("gemini", "google/")))
    if (prev and prev_vision and prev.get("n_products") is not None
            and n_products <= int(prev.get("n_products") or 0) + 2):
        return False
    # CAPTURE the vision creds NOW, in the request thread (which has the DB/connections context) — both
    # the PROVIDER key and the derived gateway. A background daemon can't read the DB, so we hand these
    # to it and it sets them in os.environ (runtime_get checks os.environ first) before running — without
    # this the bg run silently used title-only matching and OVER-grouped different products.
    from . import connections
    _cred_keys = ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "ASSISTANT_LLM_BASE_URL",
                  "ASSISTANT_LLM_API_KEY", "ASSISTANT_LLM_MODEL")
    creds = {ck: connections.runtime_get(ck) for ck in _cred_keys if connections.runtime_get(ck)}
    k = f"{store}|{keyword.strip().lower()}"
    with _DEDUP_LOCK:
        if k in _DEDUP_WARMING:
            return True  # a run is already in flight for this keyword
        _DEDUP_WARMING.add(k)

    def _work() -> None:
        import os
        for ck, cv in creds.items():
            os.environ.setdefault(ck, cv)  # give the daemon the vision creds it can't DB-resolve
        try:
            run(store, keyword, terms, dedup_cap)
        except Exception:  # noqa: BLE001 — best-effort; a deploy/vision hiccup never breaks the find
            pass
        finally:
            with _DEDUP_LOCK:
                _DEDUP_WARMING.discard(k)

    threading.Thread(target=_work, name=f"photo-dedup:{k}", daemon=True).start()
    return True


def vision_creds_debug() -> dict:
    """Diagnostic: which vision-related creds resolve in THIS thread (request context) — so a
    'why is the dedup title-only?' is answerable."""
    from . import connections
    return {ck: bool(connections.runtime_get(ck)) for ck in
            ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "ASSISTANT_LLM_BASE_URL", "ASSISTANT_LLM_API_KEY")}


def apply_flags(store: str, keyword: str, products: list[dict]) -> dict | None:
    """Stamp photo_dup / dup_group / photo_dup_drop onto the found-product dicts (mutates in
    place) from the last stored run. Returns a small summary for the API payload, or None if
    the check has never run for this keyword."""
    result = result_for(store, keyword)
    if not result:
        return None
    flags = result.get("flags") or {}
    for p in products:
        f = flags.get(_product_key(p)) or {}
        is_dup = f.get("group") is not None  # part of a >1 duplicate group (photo or title)
        p["photo_dup"] = is_dup
        p["dup_group"] = f.get("group")
        p["dup_group_size"] = f.get("size")
        p["photo_dup_drop"] = bool(f.get("drop"))
        # Already-in-store existence flag (independent of dup grouping).
        p["in_store"] = bool(f.get("in_store"))
        p["in_store_label"] = f.get("in_store_label")
    return {
        "checked_at": result.get("checked_at"),
        "model": result.get("model"),  # e.g. 'google/gemini-2.5-flash' (vision) vs 'title+catalog…'
        "vision": bool(str(result.get("model") or "").startswith(("gemini", "google/"))),
        "images_checked": result.get("images_checked"),
        "images_skipped": result.get("images_skipped"),
        "duplicate_groups": result.get("duplicate_groups"),
        "duplicates": result.get("duplicates"),
        "in_store_count": result.get("in_store_count"),
        "dedup_cap": result.get("dedup_cap"),
    }
