"""VisionScan image-QA gate — the post-import, pre-go-live image quality + IP/overlay pass.

This is a STEP IN THE LISTING PATH, run OPERATOR-REVIEW-FIRST: the engine looks at every
imported/generated image with a cheap vision model and PROPOSES a per-image verdict
(PASS / FIX / REJECT); the operator reviews + changes those verdicts on the category page,
then APPLIES them. Nothing is committed until the operator approves. Once the proposals are
trusted, the same scan→apply can be promoted to the fully-automated worker path (the apply
action is already a decision-inbox payload the worker can run).

Locked decisions (see memory project_operator_visionscan_image_qa_gate + feedback_gemini_is_the_vision_model):
  1. Vision model = Gemini, ALWAYS. The automated analyse/validate pass runs on a CHEAP
     Gemini tier (Flash) via the in-app LiteLLM proxy when a vision key is present; otherwise
     the engine emits the exact Gemini command for the operator's own Claude Code (free fallback).
  2. FIX = AUTO-CLEAN-OVER-BLOCK. A removable overlay (text / logo / watermark / competitor-or-
     store brand on the BACKGROUND) on the hero/variant does NOT block go-live — it is stripped
     for free + automatably (Canva remove-bg/text, magic-eraser) → re-scan → list. Gallery foreign
     text → cheap language-rewrite (warn-only). Off-subject/wrong-product hero → regen from supplier ref.
  3. HARD REJECT / re-source ONLY for the unfixable class: a 3rd-party trademark printed ON the
     ACTUAL product (counterfeit / dropship-fraud — an image edit can't fix a sourcing problem),
     and an off-subject / wrong-product hero (can't ship the wrong image). Everything removable
     prefers FIX over block.

The vision JUDGEMENT comes from the model; the VERDICT MAPPING (what blocks go-live, what gets
auto-cleaned vs re-sourced) is derived server-side here from the model's structured observations
+ the image tier, so the policy stays authoritative regardless of model phrasing.

LLM transport reuses the same stdlib OpenAI-compatible LiteLLM proxy seam as assistant.py
(ASSISTANT_LLM_BASE_URL + ASSISTANT_LLM_API_KEY); the vision model id is the cheap Gemini tier.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import mimetypes
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path

from . import config, connections, costs, readers

# ------------------------------------------------------------------ policy constants
VERDICTS = ("PASS", "FIX", "REJECT")
# How a FIX is carried out (all reuse EXISTING tooling — nothing new to build here):
FIX_KINDS = (
    "auto-clean",        # FREE automatable cleaner (Canva remove-bg/text, magic-eraser) — removable overlay
    "upscale",           # FREE local LANCZOS resize (image_upscale.py) — clean-but-small image below the feed floor
    "language-rewrite",  # cheap path (Canva / cheap model) — gallery text in the wrong language
    "regen",             # nano_banana_pro + gen_gallery.py with the MANDATORY supplier photo ref — off-subject/wrong-product
    "resource",          # NOT an image fix — re-source the product (3rd-party trademark printed ON the product)
)
# Roles whose images are EXPECTED to carry text (gallery infographics) — text there is allowed
# as long as it's in the store's native language and clean of competitor/store branding.
_TEXT_ROLES = {
    "benefits", "construction", "size guide", "use anywhere", "comparison",
    "scratch resistant", "how to", "stat", "chart",
}
# Fallback store language for the gallery-language check when a store hasn't pulled its Shopify
# profile yet. The REAL value is per-store: `connections.store_language(store)` (pulled from Shopify
# in Settings → Connections). `_lang(store)` resolves the live value, falling back to this default.
_STORE_LANGUAGE = os.environ.get("IMAGE_QA_STORE_LANGUAGE", "English")


def _lang(store: str | None) -> str:
    """The store's native language for the gallery-language check. Reads the per-store Shopify-pulled
    profile via the connections single-source; falls back to the global default if not yet pulled."""
    if store:
        try:
            v = connections.store_language(store)
            if v:
                return v
        except Exception:  # noqa: BLE001 — never let profile resolution break a scan
            pass
    return _STORE_LANGUAGE
# Hard cap on images scanned per category so a runaway catalog can't burn vision budget.
_MAX_IMAGES = int(os.environ.get("IMAGE_QA_MAX_IMAGES", "80"))
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# Google-Shopping recommends >=800x800 for the feed image; below this the feed/hero is
# "low resolution" and looks weak / can be penalized. Measured deterministically from the
# file header (no vision model + no Pillow needed) so it's caught even with no vision key.
_MIN_FEED_PX = int(os.environ.get("IMAGE_QA_MIN_PX", "800"))
# Operator-review-FIRST phase (2026-06-23): the FREE local fixes (auto-clean / upscale) now
# ADOPT in place — they overwrite the real on-disk file (the source the later push uploads).
# So, like the scan, the FIX is gated behind an inbox DECISION instead of auto-running on apply.
# Approving the "Run image fixes" decision is what actually fires the jobs. Flip this to "1" to
# promote back to auto-run once the gate is trusted.
_AUTO_RUN_FIXES = os.environ.get("IMAGE_QA_AUTO_RUN_FIXES", "0") not in ("0", "", "false", "False")


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ deterministic pixel metrics
def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """Read (width, height) from the file header using STDLIB ONLY — the api venv has no
    Pillow. Supports PNG / GIF / BMP / WEBP / JPEG. Returns None if the size can't be read.
    Resolution is measurable without decoding pixels, so the low-res gate needs no model."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if len(head) < 24:
                return None
            if head[:8] == b"\x89PNG\r\n\x1a\n":                       # PNG: IHDR w,h at 16..24
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):                     # GIF: LE u16 at 6..10
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:2] == b"BM":                                      # BMP: LE i32 at 18..26
                w, h = struct.unpack("<ii", head[18:26])
                return abs(int(w)), abs(int(h))
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":          # WEBP (3 sub-formats)
                fmt = head[12:16]
                f.seek(20)
                c = f.read(10)
                if fmt == b"VP8 " and len(c) >= 10:                    # lossy
                    w = struct.unpack("<H", c[6:8])[0] & 0x3FFF
                    h = struct.unpack("<H", c[8:10])[0] & 0x3FFF
                    return int(w), int(h)
                if fmt == b"VP8L" and len(c) >= 5:                     # lossless
                    bits = int.from_bytes(c[1:5], "little")
                    return int((bits & 0x3FFF) + 1), int(((bits >> 14) & 0x3FFF) + 1)
                if fmt == b"VP8X" and len(c) >= 10:                    # extended (24-bit dims-1)
                    w = (c[4] | (c[5] << 8) | (c[6] << 16)) + 1
                    h = (c[7] | (c[8] << 8) | (c[9] << 16)) + 1
                    return int(w), int(h)
                return None
            if head[:2] == b"\xff\xd8":                                # JPEG: scan to an SOF marker
                f.seek(2)
                while True:
                    byte = f.read(1)
                    if not byte:
                        return None
                    if byte != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        return None
                    m = marker[0]
                    if m in (0xD8, 0xD9, 0x01) or 0xD0 <= m <= 0xD7:   # standalone, no length
                        continue
                    seg = f.read(2)
                    if len(seg) < 2:
                        return None
                    seglen = struct.unpack(">H", seg)[0]
                    if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):  # SOF0..15 (not DHT/JPG/DAC)
                        data = f.read(5)
                        if len(data) < 5:
                            return None
                        h = struct.unpack(">H", data[1:3])[0]
                        w = struct.unpack(">H", data[3:5])[0]
                        return int(w), int(h)
                    f.seek(seglen - 2, 1)
    except (OSError, struct.error, ValueError):
        return None
    return None


def _pixel_metrics(path: Path) -> dict:
    """Deterministic, model-free observations measured from the file header. Currently just
    resolution (the GMC >=800px floor). Merges INTO the vision observations so a model that
    misses a small/upscaled image is still backstopped, and so low-res is caught even when no
    vision model is configured. Blur stays a model judgement (needs pixel decode)."""
    dims = _image_dimensions(path)
    if not dims:
        return {}
    w, h = dims
    return {"width": w, "height": h, "low_resolution": min(w, h) < _MIN_FEED_PX}


# ------------------------------------------------------------------ vision transport (stdlib)
def _configured() -> bool:
    """A vision key is present when the shared LiteLLM proxy is configured (same seam as the
    assistant). The model routed behind it is the cheap Gemini tier — that's the locked choice."""
    return bool(connections.runtime_get("ASSISTANT_LLM_BASE_URL") and connections.runtime_get("ASSISTANT_LLM_API_KEY"))


def _vision_model() -> str:
    """The cheap Gemini tier (Flash) is the default automated validator. Overridable, but the
    model FAMILY is locked to Gemini — never re-litigated (feedback_gemini_is_the_vision_model).
    The exact model *id* is provider-aware: through OpenRouter it must be the OpenRouter-namespaced
    id (`google/gemini-2.5-flash`), while the Gemini-direct OpenAI surface wants the bare id. We
    read the resolved gateway base to pick the right spelling so vision works whichever key is set."""
    explicit = os.environ.get("IMAGE_QA_VISION_MODEL")
    if explicit:
        return explicit
    base = (connections.runtime_get("ASSISTANT_LLM_BASE_URL") or "").lower()
    if "openrouter" in base:
        return "google/gemini-2.5-flash"
    return "gemini-2.5-flash"


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    raw = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


_VISION_SYSTEM = (
    "You are a strict Google-Shopping image-compliance reviewer for a dropshipping catalog. "
    "You look at ONE product image and report structured observations only. Be conservative: "
    "Google Shopping disapproves overlay/watermark/promo-text/brand-text on the feed image and "
    "SUSPENDS for trademark/counterfeit. Report ONLY what you actually see in the pixels. "
    "Reply with a SINGLE JSON object and nothing else."
)


def _vision_prompt(expected_product: str, tier: str, language: str) -> str:
    return (
        f"This image should depict: \"{expected_product}\". It is a {tier.upper()} image "
        f"({'the Google Shopping feed / hero image — strictest rules' if tier == 'hero' else 'a gallery image — looser rules, infographic text allowed'}).\n"
        f"The store's native language is {language}.\n\n"
        "Report these fields as JSON (booleans unless noted):\n"
        '  "wrong_product"        : the image does NOT clearly show the expected product, or shows a different/off-subject item\n'
        '  "removable_overlay"    : there is floating text / a logo / a watermark / a sale-badge / a competitor-or-store brand name laid OVER the photo background (NOT printed on the physical product) — i.e. something a background eraser could remove\n'
        '  "on_product_trademark" : the PHYSICAL product reproduces a protected mark — set TRUE ONLY for (a) a real brand LOGO/wordmark on the item (Nike swoosh, Adidas trefoil, a team crest), or (b) a copyrighted CHARACTER LIKENESS / sculpted character face / signature full-costume design (a Spider-Man suit with web pattern + spider logo, a molded Deadpool/superhero mask face, a Disney-character molded toy). Set FALSE for a costume that merely EVOKES a show through GENERIC elements that nobody owns — a plain colored jumpsuit/tracksuit, a printed NUMBER (e.g. "230"), a color scheme, basic geometric shapes (circle/triangle/square), or a genre look (Kpop-stage, ninja, racer). Generic-evocative costumes are LISTABLE; only a real logo or a reproduced character likeness is true here\n'
        '  "foreign_text"         : the image contains readable text that is NOT in the store native language\n'
        '  "blurry"               : the product is out of focus / soft / motion-blurred / has heavy JPEG or upscaling artifacts — not crisp and sharp\n'
        '  "low_resolution"       : the image looks pixelated / upscaled-from-small / lacks fine detail (an enlarged low-res source), regardless of the stored pixel dimensions\n'
        '  "low_quality"          : the product is tiny in frame, badly cropped, poorly lit, or not the clear main subject\n'
        '  "overlay_bbox"         : when removable_overlay is true, the NORMALIZED [x,y,w,h] (each 0..1, origin top-left) tightly bounding the overlay so it can be erased; else null\n'
        '  "notes"                : a short string (max 18 words) describing what you see\n\n'
        "JSON only, e.g.: "
        '{"wrong_product":false,"removable_overlay":true,"on_product_trademark":false,'
        '"foreign_text":false,"blurry":false,"low_resolution":false,"low_quality":false,'
        '"overlay_bbox":[0.0,0.0,0.4,0.16],"notes":"floating \'SALE\' badge top-left"}'
    )


def _call_vision(path: Path, expected_product: str, tier: str, language: str) -> dict:
    """One cheap-Gemini vision call → structured observations dict. Raises on transport/parse
    failure so the caller can degrade to the operator-handoff baseline."""
    base = (connections.runtime_get("ASSISTANT_LLM_BASE_URL") or "").rstrip("/")
    key = connections.runtime_get("ASSISTANT_LLM_API_KEY") or ""
    payload = {
        "model": _vision_model(),
        "messages": [
            {"role": "system", "content": _VISION_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": _vision_prompt(expected_product, tier, language)},
                {"type": "image_url", "image_url": {"url": _data_url(path)}},
            ]},
        ],
        "max_tokens": 300,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (trusted proxy)
        body = json.loads(resp.read().decode("utf-8"))
    text = body["choices"][0]["message"]["content"] or ""
    return _parse_obs(text)


def _parse_obs(text: str) -> dict:
    """Extract the JSON observation object from the model reply (tolerates fenced blocks)."""
    s = (text or "").strip()
    if "```" in s:
        for chunk in s.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{"):
                s = c
                break
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in vision reply")
    return json.loads(s[start : end + 1])


# ------------------------------------------------------------------ verdict mapping (authoritative)
def _tier_for(order: int, lowest_order: int) -> str:
    """The lowest-order image is the hero / Google-Shopping feed image (strictest); the rest
    are gallery (looser). Variant featured images mirror the hero, so hero rules cover the feed."""
    return "hero" if order == lowest_order else "gallery"


def _map_verdict(obs: dict, tier: str, language: str = _STORE_LANGUAGE) -> dict:
    """Derive the AUTHORITATIVE verdict from the model's observations + the image tier, applying
    the locked auto-clean-over-block policy. Precedence (highest first):
      on_product_trademark → REJECT/resource (counterfeit; an image edit can't fix sourcing)
      wrong_product(hero)  → REJECT/regen   (can't ship the wrong feed image; regen from supplier ref)
      removable_overlay    → FIX/auto-clean (strip free → list; NEVER blocks)
      foreign_text(gallery)→ FIX/language-rewrite (warn-only)
      wrong_product(gallery)→ FIX/regen
      else                 → PASS
    Only the REJECT class blocks go-live."""
    b = lambda k: bool(obs.get(k))  # noqa: E731
    reasons: list[str] = []
    notes = str(obs.get("notes") or "").strip()

    if b("on_product_trademark"):
        reasons.append("third-party trademark printed on the actual product — counterfeit/dropship-fraud risk; re-source (an image edit can't fix a sourcing problem)")
        return _verdict("REJECT", "resource", reasons, blocks=True, notes=notes)

    if tier == "hero" and b("wrong_product"):
        reasons.append("hero/feed image is off-subject or shows the wrong product — regenerate from the supplier reference photo")
        return _verdict("REJECT", "regen", reasons, blocks=True, notes=notes)

    if b("removable_overlay"):
        where = "hero/feed" if tier == "hero" else "gallery"
        reasons.append(f"removable overlay (text/logo/watermark/brand on the background) on the {where} image — auto-clean it for free, then list (does NOT block go-live)")
        return _verdict("FIX", "auto-clean", reasons, blocks=False, notes=notes)

    if tier == "gallery" and b("foreign_text"):
        reasons.append(f"gallery text is not in the store language ({language}) — cheap language-rewrite (warn-only)")
        return _verdict("FIX", "language-rewrite", reasons, blocks=False, notes=notes)

    if b("wrong_product"):  # gallery off-subject
        reasons.append("gallery image looks off-subject — regenerate from the supplier reference photo")
        return _verdict("FIX", "regen", reasons, blocks=False, notes=notes)

    if b("low_resolution"):
        where = "hero/feed" if tier == "hero" else "gallery"
        wh = f'{obs.get("width", "?")}×{obs.get("height", "?")}px'
        reasons.append(
            f"low-resolution {where} image ({wh}, below the {_MIN_FEED_PX}px Google-Shopping floor) — "
            "FREE local upscale to 1024² (escalates to AI-upscale / supplier-ref regen if still soft after re-scan)")
        return _verdict("FIX", "upscale", reasons, blocks=False, notes=notes)

    if b("blurry") or b("low_quality"):
        reasons.append("weak image (blurry / soft / small / poorly framed) — AI-improve (reframe, clean bg, sharpen, upscale)")
        return _verdict("FIX", "regen", reasons, blocks=False, notes=notes)

    reasons.append("clean — product is the clear subject, no overlay/IP/foreign-text issues")
    return _verdict("PASS", None, reasons, blocks=False, notes=notes)


def _verdict(verdict: str, fix_kind: str | None, reasons: list[str], blocks: bool, notes: str) -> dict:
    return {
        "verdict": verdict,
        "fix_kind": fix_kind,
        "blocks_go_live": blocks,
        "reasons": reasons,
        "notes": notes,
    }


# ------------------------------------------------------------------ scan (operator-review-first)
def policy(model_available: bool | None = None, store: str | None = None) -> dict:
    """Read-only description of what the gate does — for the UI's explainer panel."""
    if model_available is None:
        model_available = _configured()
    language = _lang(store)
    return {
        "verdicts": list(VERDICTS),
        "fix_kinds": list(FIX_KINDS),
        "tiers": {
            "hero": "Hero + variant images = the Google Shopping feed image (strictest): product is the "
                    f"clear, well-framed, SHARP main subject at >={_MIN_FEED_PX}px (not blurry / low-res / "
                    "upscaled); ZERO overlay/text/badge/watermark/competitor-or-store brand; no third-party "
                    "trademark/IP on the product; truthful to title.",
            "gallery": "Gallery images (looser): infographic text allowed but MUST be in the store's native "
                       f"language ({language}); still clean of competitor logo / store name / 3rd-party "
                       "branding; and not blurry / low-resolution.",
        },
        "gate": "AUTO-CLEAN-OVER-BLOCK: a removable overlay never blocks go-live — it is stripped for free "
                "(Canva remove-bg/text, magic-eraser) then listed. HARD REJECT/re-source ONLY for a 3rd-party "
                "trademark printed ON the actual product, or an off-subject/wrong-product hero (regen).",
        "vision_model": "Gemini (cheap Flash tier) via the in-app LiteLLM proxy when keyed; else the exact "
                        "Gemini command is emitted for the operator's own Claude Code (free fallback).",
        "store_language": language,
        "model_available": model_available,
        "review_first": True,
    }


def _expected_product(slug: str, sku: dict) -> str:
    title = (sku.get("spec") or {}).get("title") if isinstance(sku.get("spec"), dict) else None
    return (title or slug.replace("-", " ")).strip()


def _baseline_command(store: str, slug: str, images: list[dict], language: str) -> str:
    """The free fallback: the exact instruction the operator pastes into their OWN Claude Code
    (which holds a Gemini vision key) when the in-app proxy isn't configured. Gemini is the model."""
    paths = "\n".join(f"  - {i['path']}" for i in images[:_MAX_IMAGES])
    return (
        f"# VisionScan image-QA — run with GEMINI vision in your Claude Code (store={store}, category={slug})\n"
        f"For EACH image below, judge against the gate policy and return PASS / FIX(auto-clean|language-rewrite|regen) / "
        f"REJECT(resource):\n"
        f"  HERO/feed image (strictest): clean, well-framed, SHARP and >={_MIN_FEED_PX}px (not blurry/low-res/upscaled), no overlay/text/badge/watermark/brand, no 3rd-party IP, truthful.\n"
        f"  GALLERY: infographic text OK but in {language}, no competitor/store branding; not blurry/low-resolution.\n"
        f"  Blurry / low-resolution / upscaled -> FIX regen (regenerate at 1024² from the supplier ref, or AI-upscale).\n"
        f"  AUTO-CLEAN-OVER-BLOCK: a removable background overlay -> FIX auto-clean (never blocks). "
        f"3rd-party trademark printed ON the product (real brand LOGO, or a copyrighted CHARACTER LIKENESS/sculpt e.g. Spider-Man/Deadpool) -> REJECT re-source. "
        f"A costume that only EVOKES a show via generic elements (a color, a NUMBER like '230', a plain jumpsuit/tracksuit, basic shapes, a genre look) is LISTABLE — NOT a reject. "
        f"Off-subject/wrong-product hero -> REJECT regen.\n"
        f"Images (relative to general-stores/{store}/):\n{paths}\n"
        f"Then POST the per-image verdicts back to /api/stores/{store}/categories/{slug}/image-qa/apply."
    )


def scan_category(store: str, slug: str) -> dict:
    """Scan every image in a category and PROPOSE a per-image verdict. Read-only — commits nothing.

    When the in-app cheap-Gemini proxy is configured, each image gets a real pixel verdict. When
    it isn't, every image is proposed PASS+needs_vision (we never auto-flag what we can't see) and
    the report carries the exact Gemini command for the operator's Claude Code (the free fallback)."""
    detail = readers.category_detail(store, slug)
    if detail is None:
        return None  # caller raises 404

    model_ok = _configured()
    language = _lang(store)  # per-store native language (Shopify-pulled) for the gallery-text check
    skus_out: list[dict] = []
    all_images: list[dict] = []
    counts = {"total": 0, "PASS": 0, "FIX": 0, "REJECT": 0}
    by_fix: dict[str, int] = {}
    blocking: list[dict] = []
    auto_clean_queue: list[dict] = []
    upscale_queue: list[dict] = []
    scanned = 0

    for sku in detail.get("skus") or []:
        images = sku.get("images") or []
        if not images:
            skus_out.append({"id": sku.get("id"), "images": []})
            continue
        lowest = min((img.get("order", 999) for img in images), default=999)
        expected = _expected_product(slug, sku)
        rows: list[dict] = []
        for img in images:
            counts["total"] += 1
            tier = _tier_for(img.get("order", 999), lowest)
            row = {
                "sku": sku.get("id"),
                "file": img.get("file"),
                "path": img.get("path"),
                "role": img.get("role"),
                "order": img.get("order"),
                "tier": tier,
                "expected_product": expected,
                "needs_vision": not model_ok,
                "operator_override": None,
            }
            # Deterministic, model-free pixel metrics (resolution) run for EVERY image — they
            # backstop the vision model AND let us catch low-res with no vision key configured.
            target = readers.resolve_store_file(store, img.get("path") or "")
            metrics = _pixel_metrics(target) if target is not None else {}
            if metrics:
                row["width"], row["height"] = metrics.get("width"), metrics.get("height")

            if model_ok and scanned < _MAX_IMAGES:
                try:
                    if target is None:
                        raise FileNotFoundError(img.get("path"))
                    obs = _call_vision(target, expected, tier, language)
                    # Merge deterministic metrics over the model reply: a measured low-res image
                    # is low-res even if the model said otherwise (OR-semantics on low_resolution).
                    obs = {**obs, **metrics}
                    if metrics.get("low_resolution") or obs.get("low_resolution"):
                        obs["low_resolution"] = True
                    row.update(_map_verdict(obs, tier, language))
                    row["observations"] = obs
                    scanned += 1
                except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                        KeyError, ValueError, TimeoutError) as e:
                    # Vision failed, but we still have the deterministic resolution check.
                    if metrics.get("low_resolution"):
                        row.update(_map_verdict(metrics, tier, language))
                        row["observations"] = metrics
                    else:
                        row.update(_verdict("PASS", None,
                                            [f"vision scan failed ({type(e).__name__}); operator must verify the pixels"],
                                            blocks=False, notes=""))
                    row["needs_vision"] = True
            elif metrics.get("low_resolution"):
                # No vision model — but resolution is measurable, so still flag the low-res FIX.
                row.update(_map_verdict(metrics, tier, language))
                row["observations"] = metrics
                row["needs_vision"] = True  # content checks (overlay/IP/subject) still unseen
            else:
                row.update(_verdict("PASS", None,
                                    ["no in-app vision model configured — operator must visually verify (or run the Gemini command below)"],
                                    blocks=False, notes=""))
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
            if row.get("fix_kind"):
                by_fix[row["fix_kind"]] = by_fix.get(row["fix_kind"], 0) + 1
            if row.get("blocks_go_live"):
                blocking.append({"sku": row["sku"], "file": row["file"], "fix_kind": row["fix_kind"]})
            if row.get("fix_kind") == "auto-clean":
                auto_clean_queue.append({"sku": row["sku"], "file": row["file"], "path": row["path"]})
            if row.get("fix_kind") == "upscale":
                upscale_queue.append({"sku": row["sku"], "file": row["file"], "path": row["path"]})
            rows.append(row)
            all_images.append(row)
        skus_out.append({"id": sku.get("id"), "images": rows})

    return {
        "store": store,
        "slug": slug,
        "scanned_at": _now(),
        "model_available": model_ok,
        "policy": policy(model_ok, store),
        "skus": skus_out,
        "summary": {**counts, "by_fix_kind": by_fix, "scanned": scanned},
        "go_live": {
            "blocks": bool(blocking),
            "blocking_images": blocking,
            "auto_clean_queue": auto_clean_queue,
            "upscale_queue": upscale_queue,
            "verdict": "BLOCKED" if blocking else "CLEAR",
        },
        "command": None if model_ok else _baseline_command(store, slug, all_images, language),
    }


# ------------------------------------------------------------------ apply (operator-approved commit)
_REPORT_FILE = "_image-qa-report.json"


def _report_path(store: str, slug: str) -> Path | None:
    base = config.general_stores_dir().resolve()
    cand = (base / store / slug).resolve()
    if base not in cand.parents or not cand.is_dir():
        return None
    return cand / _REPORT_FILE


def stored_report(store: str, slug: str) -> dict | None:
    """The last APPLIED verdict report (the go-live gate of record), if one exists on disk."""
    p = _report_path(store, slug)
    if p is None or not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def apply(store: str, slug: str, verdicts: list[dict]) -> dict:
    """Commit the operator-reviewed verdicts: the FINAL verdict per image is the operator's
    override when present, else the AI proposal. We persist the report (the go-live gate of
    record) and return the handoff queues the listing path acts on — auto-clean (free strip),
    language-rewrite / regen (cheap / nano_banana_pro+supplier-ref), re-source (REJECT class).

    The go-live gate is AUTO-CLEAN-OVER-BLOCK: only a REJECT (3rd-party-IP-on-product or
    wrong-product hero) blocks; every removable overlay is queued for a free clean, not a block.

    Each non-PASS image also spawns the matching fix JOB under the hybrid model: a removable
    overlay → `image-autoclean` (FREE local inpaint, no AI); language → `image-relang`; off-
    subject → `image-regen` (supplier-ref); REJECT → `image-resource` (re-source handoff)."""
    from . import jobs, runlog  # lazy — jobs has no dep on image_qa, so no import cycle

    final: list[dict] = []
    counts = {"total": 0, "PASS": 0, "FIX": 0, "REJECT": 0}
    auto_clean: list[dict] = []
    upscale: list[dict] = []
    regen: list[dict] = []
    language_rewrite: list[dict] = []
    resource: list[dict] = []
    blocking: list[dict] = []
    # FREE local fixes that OVERWRITE the real on-disk file (adopt-in-place). In the operator-
    # review-FIRST phase these are NOT run on apply — they are collected and dropped as ONE inbox
    # decision the operator approves before any file is mutated (see _AUTO_RUN_FIXES).
    pending_fixes: list[dict] = []

    for v in verdicts or []:
        if not isinstance(v, dict):
            continue
        override = v.get("operator_override")
        decided = (override or {}).get("verdict") if isinstance(override, dict) else override
        verdict = decided if decided in VERDICTS else v.get("verdict")
        if verdict not in VERDICTS:
            verdict = "PASS"
        fix_kind = (override or {}).get("fix_kind") if isinstance(override, dict) else None
        fix_kind = fix_kind or v.get("fix_kind")
        # Re-derive whether this blocks: only the REJECT class blocks go-live.
        blocks = verdict == "REJECT"
        row = {
            "sku": v.get("sku"), "file": v.get("file"), "path": v.get("path"),
            "tier": v.get("tier"), "role": v.get("role"),
            "verdict": verdict, "fix_kind": fix_kind if verdict != "PASS" else None,
            "blocks_go_live": blocks, "source": "operator" if decided else "ai",
            "reasons": v.get("reasons") or [],
        }
        counts["total"] += 1
        counts[verdict] = counts.get(verdict, 0) + 1
        sku_id = row["sku"] or ""
        sku_slug = sku_id[len("sku-"):] if sku_id.startswith("sku-") else sku_id
        overlay_bbox = (v.get("observations") or {}).get("overlay_bbox")
        bucket = {"sku": row["sku"], "file": row["file"], "path": row["path"], "tier": row["tier"]}
        job_args = {"slug": slug, "sku": row["sku"], "sku_slug": sku_slug,
                    "file": row["file"], "path": row["path"]}
        job = None
        if verdict == "FIX" and fix_kind == "auto-clean":
            if _AUTO_RUN_FIXES:
                job = jobs.create("image-autoclean", store, {**job_args, "overlay_bbox": overlay_bbox})
                auto_clean.append({**bucket, "job_id": job.get("id"), "overlay_bbox": overlay_bbox})
            else:
                pending_fixes.append({"spec": "image-autoclean", "fix_kind": fix_kind,
                                      "args": {**job_args, "overlay_bbox": overlay_bbox}, **bucket})
                auto_clean.append({**bucket, "job_id": None, "overlay_bbox": overlay_bbox,
                                   "pending_decision": True})
        elif verdict == "FIX" and fix_kind == "upscale":
            if _AUTO_RUN_FIXES:
                job = jobs.create("image-upscale", store, job_args)
                upscale.append({**bucket, "job_id": job.get("id")})
            else:
                pending_fixes.append({"spec": "image-upscale", "fix_kind": fix_kind,
                                      "args": job_args, **bucket})
                upscale.append({**bucket, "job_id": None, "pending_decision": True})
        elif verdict == "FIX" and fix_kind == "language-rewrite":
            job = jobs.create("image-relang", store, job_args)
            language_rewrite.append({**bucket, "job_id": job.get("id")})
        elif verdict == "FIX" and fix_kind == "regen":
            job = jobs.create("image-regen", store, job_args)
            regen.append({**bucket, "job_id": job.get("id")})
        elif verdict == "REJECT":
            job = jobs.create("image-resource", store, job_args)
            resource.append({**bucket, "fix_kind": fix_kind or "resource", "job_id": job.get("id")})
            blocking.append(bucket)
        if job is not None:
            row["fix_job_id"] = job.get("id")
        final.append(row)

    report = {
        "store": store, "slug": slug, "applied_at": _now(),
        "summary": counts,
        "go_live": {
            "blocks": bool(blocking),
            "verdict": "BLOCKED" if blocking else "CLEAR",
            "blocking_images": blocking,
        },
        "handoffs": {
            "auto_clean": auto_clean,            # FREE strip → re-scan → list (never blocks)
            "upscale": upscale,                  # FREE local LANCZOS lift → re-scan → list (never blocks)
            "language_rewrite": language_rewrite,  # cheap gallery-language rewrite
            "regen": regen,                      # nano_banana_pro + supplier-ref
            "resource": resource,                # REJECT: re-source the product (IP on product / wrong product)
        },
        "images": final,
    }
    # Operator-review-FIRST: the FREE file-overwriting fixes wait behind ONE inbox decision.
    if pending_fixes and not _AUTO_RUN_FIXES:
        n = len(pending_fixes)
        report["fix_decision_id"] = runlog.decision_create(
            store, kind="image-fix",
            title=f"Run image fixes — {slug}",
            summary=f"{n} free local fix{'es' if n != 1 else ''} (auto-clean / upscale) will OVERWRITE "
                    f"the on-disk image{'s' if n != 1 else ''} in place (a .orig backup is kept). "
                    f"Approve to run; the originals are not touched until you do.",
            payload={"action": "image-fix-run", "store": store, "slug": slug, "fixes": pending_fixes},
            source="image-qa-apply",
        )
        report["fixes_pending"] = n

    p = _report_path(store, slug)
    if p is not None:
        try:
            p.write_text(json.dumps(report, indent=2))
            report["persisted"] = True
        except OSError:
            report["persisted"] = False
    else:
        report["persisted"] = False
    return report
