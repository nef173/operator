#!/usr/bin/env python3
"""gen_gallery.py — FAST parallel product-gallery generator for general-store SKUs.

WHY: the 05-niche `generate_product_images.py` needs the full launch-folder layout
(catalog/products.csv, brand/color-tokens.yml, ~/.launch-niche playbook image-prompts).
General-store SKUs live in a flat per-SKU folder:

    general-stores/<store>/<category>/sku-<slug>/
        _supplier-refs/supplier-1.jpg ... supplier-N.jpg   (DISCOVERY-reference photos)
        _supplier-refs/_manifest.txt                        (form-factor + honest constraints)
        images/                                             (output gallery, NN-sku-<slug>-<role>.png)

This driver reuses the PROVEN OpenRouter multimodal contract from the 05 script
(call /v1/chat/completions, modalities=["image","text"], reference images FIRST as
image_url blocks, then a subject-preservation text prompt) but reads the flat layout
and parallelizes across SKUs AND across the 6 gallery roles.

V10-11 HARD GATE: every image-gen call attaches the actual discovery-reference photos
(`_supplier-refs/supplier-*.jpg`) as `image_url` blocks. Never text-to-image alone —
that hallucinates form factor (dropship-fraud risk). The manifest's honest constraints
("do NOT advertise...", "DROP the ... claim", single-size notes) are injected into every
prompt so the render never invents features the locked supplier doesn't ship.

NOTE: AliExpress/Temu are DISCOVERY/product-finding sources, NOT the supplier. The real
fulfillment supplier is CJ/agent. The ref photos are "discovery references," used purely
to ground the render on the true product shape.

Gallery roles (canonical order — variant featured image = the LIFESTYLE hero, plain-white LAST):
    1 lifestyle      hero; pet on the mat in a real scene; NO text; 1:1
    2 benefits       infographic feature callouts
    3 construction   material / layers / cutaway closeup
    4 size-guide     dimensions chart
    5 use-anywhere   multi-scene (crate / car / floor / bed)
    6 product-white  clean white studio shot (LAST)

USAGE:
  python gen_gallery.py <category-dir> --skus sku-gel,sku-mesh [--force] [--dry-run]
  python gen_gallery.py <category-dir> --all-ready            # every sku-* with refs and empty images/
  python gen_gallery.py <category-dir> --all-ready --max-concurrent 9 --product-workers 4 --image-workers 3
"""
from __future__ import annotations
import argparse, base64, json, mimetypes, os, re, sys, threading, time, tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError as e:
    sys.exit(f"ERROR missing dep ({e}); use the 06 venv: 06-launch-general-store/scripts/.venv")

HOME = Path(os.environ.get("LAUNCH_NICHE_HOME", str(Path.home() / ".launch-niche")))

ROLES = ["lifestyle", "benefits", "construction", "size-guide", "use-anywhere", "product-white"]

# Feed-critical image generated in a FIRST wave so an infographic timeout can never
# block a usable listing. The LIFESTYLE hero is the ONE that matters: it is the
# variant featured image Shopify sends to the Google Shopping feed. The plain
# product-white packshot is low-value (operator 2026-06-18: "generating the white
# product isn't good anyway — we'd rather lead with lifestyle/good images"), so it is
# NOT in the priority wave; it's still produced last in the normal gallery order.
FEED_CRITICAL = ["lifestyle"]

# Global concurrency cap across ALL in-flight OpenRouter calls (set in main()).
# DEFAULT 3 (NOT high): the OpenRouter image endpoint read-times-out under load.
# Measured: --max-concurrent 6 -> 50% timeouts (forced a full 2nd pass);
# --max-concurrent 2 -> 100% success. LOW concurrency is FASTER end-to-end
# because it removes the re-run loop. See feedback_batch_build_bottlenecks memory.
_SEM: threading.Semaphore = threading.Semaphore(3)
_PRINT_LOCK = threading.Lock()


def log(msg: str):
    with _PRINT_LOCK:
        print(msg, flush=True)


def load_settings() -> dict:
    p = HOME / "settings.toml"
    if not p.exists():
        sys.exit(f"ERROR settings.toml not found at {p}")
    with open(p, "rb") as f:
        s = tomllib.load(f)
    ig = s.get("image_gen", {})
    # Require the key for the ACTIVE provider only: google needs google_api_key,
    # OpenRouter (the fallback) needs api_key. Don't demand the OpenRouter key when
    # running DIRECT Gemini — that would falsely abort a google-only config.
    provider = ig.get("provider", "openrouter")
    if provider == "google":
        if not (ig.get("google_api_key") or ig.get("api_key")):
            sys.exit("ERROR [image_gen].google_api_key missing in settings.toml (provider=google)")
    elif not ig.get("api_key"):
        sys.exit("ERROR [image_gen].api_key missing in settings.toml")
    return ig


# ---------------------------------------------------------------------------
# Manifest parsing — pull display name, form-factor, honest constraints, variants
# ---------------------------------------------------------------------------

def parse_manifest(mf: Path) -> dict:
    txt = mf.read_text() if mf.exists() else ""
    name = ""
    m = re.search(r"^SKU:\s*(.+?)(?:\s*\(sku|$)", txt, re.M)
    if m:
        name = m.group(1).strip()
    # FORM FACTOR block: from "FORM FACTOR" line up to the next ALLCAPS section header
    ff = ""
    fm = re.search(r"FORM FACTOR\s*:?(.*?)(?:\nPRICING|\nVARIANTS|\nIMAGES|\Z)", txt, re.S)
    if fm:
        ff = re.sub(r"\s+", " ", fm.group(1)).strip()
    # Honest constraints — any line warning against a claim
    constraints = []
    for line in txt.splitlines():
        if re.search(r"do NOT|DROP THE|DROP that|MISMATCH|single (live )?size|Only ~?\d|dropship-fraud|don't lead|DON'T print|verify .* before", line, re.I):
            constraints.append(line.strip(" *-"))
    sizes = ""
    sm = re.search(r"Sizes\s*:\s*(.+)", txt)
    if sm:
        sizes = sm.group(1).strip()
    colors = ""
    cm = re.search(r"Colors\s*:\s*(.+)", txt)
    if cm:
        colors = cm.group(1).strip()
    return {"name": name, "form_factor": ff, "constraints": constraints,
            "sizes": sizes, "colors": colors}


# Binding readability rule for EVERY text-bearing (infographic) tile: type must be
# large, bold and high-contrast so it stays legible at Google-Shopping thumbnail size.
# Applied to benefits / construction / size-guide / use-anywhere (lifestyle + product-white
# carry no text). Operator rule 2026-06-18.
TYPE_RULE = (
    "\n\nTYPOGRAPHY (mandatory): make ALL text LARGE, BOLD and high-contrast — "
    "headline/callout labels should be big and easy to read even at a small thumbnail "
    "size; never use small, thin, or low-contrast type. Favor a few short, large labels "
    "over many tiny ones. Keep generous spacing so words never crowd or overlap."
    "\n\nNO PRICE (mandatory): never show any price, dollar amount, '$', 'list price', "
    "discount, or percentage-off anywhere in the image — even if a reference photo contains "
    "one. Our store price is set in Shopify, not baked into the image.")

ROLES_WITH_TEXT = {"benefits", "construction", "size-guide", "use-anywhere"}

# Binding composition rule for the LIFESTYLE hero (and any lifestyle-style frame).
# The PRODUCT leads every frame: centered/foreground, large and dominant (~60-75% of
# the frame), the clear focal point. A human model is SECONDARY and small when present,
# and is OMITTED entirely when a model is not logical for the product. Operator rule
# 2026-07-01 (caught on the stove-gap-cover + bird-feeder heroes where the woman filled
# ~60% of the frame and the product was small and pushed to the side).
LIFESTYLE_COMPOSITION = (
    "\n\nCOMPOSITION (mandatory — this is the #1 requirement): shoot the PRODUCT CLOSE and "
    "LARGE like a macro product-hero. The product must FILL roughly two-thirds of the image "
    "(about 60-75% of the frame area), sitting in the FOREGROUND, centered/center-weighted, "
    "in sharp focus, with its edges coming close to the image borders. Do NOT render a wide "
    "or distant scene where the product looks small; do NOT leave large empty background. "
    "The product is unmistakably the biggest, closest, sharpest thing in the frame. A human "
    "model must NEVER dominate or be larger than the product."
)
# When a person genuinely helps sell the use (a body part the product acts on — a foot
# file on a foot, a hair curler on hair, hands demonstrating a tool in real use), the
# model stays SECONDARY and smaller than the product, in a supporting role, with the
# product still in front.
MODEL_SECONDARY = (
    " If a person or hands are shown, they are in a supporting role demonstrating use, "
    "smaller than the product and never the compositional subject; the product stays in "
    "front and dominant."
)
# When a model is NOT logical for the product (a tool, sprayer, strainer, camera, feeder,
# etc.), OMIT the human entirely and show the product in its real use context.
NO_MODEL = (
    " Do NOT include any human, model, hands, or person in this image — a model is not "
    "logical for this product. Show the product in its real use context (on the workbench, "
    "on the stovetop, in the sink, mounted on the vehicle, in the garden, etc.) with NO "
    "person present."
)


def model_is_logical(subject: str, noun: str, name: str) -> bool:
    """A human model is only logical when a body part the product acts ON genuinely sells
    the use (foot file -> foot, hair curler -> hair, callus remover -> foot). For tools,
    sprayers, strainers, cameras, feeders, repellers, etc. a model is NOT logical and is
    omitted. Operator rule 2026-07-01."""
    text = f"{noun} {name}".lower()
    BODY_PART_CUES = ("foot", "feet", "callus", "hair", "curl", "curler", "nail",
                      "skin", "face", "facial", "lip", "hand cream", "scalp", "body")
    return any(cue in text for cue in BODY_PART_CUES)


PET_SUBJECTS = {"dog", "cat", "puppy", "kitten", "pet"}


def is_pet(subject: str) -> bool:
    return (subject or "").strip().lower() in PET_SUBJECTS


def subject_rule(subject: str) -> str:
    """Hard guard: the render must depict/label ONLY the declared subject — never an
    adjacent animal/subject. Prevents the 'cat on a dog product' dropship-fraud class
    (operator 2026-06-18, project-wide). Applied to EVERY role."""
    if is_pet(subject):
        return (f"\n\nSUBJECT LOCK (mandatory): the ONLY animal/subject shown or named anywhere "
                f"in this image is a {subject}. Never depict, draw a silhouette of, or label any "
                f"other animal or subject (no cat, no other species). Any 'suitable for' / size "
                f"rows must reference {subject} breeds/sizes only.")
    return (f"\n\nSUBJECT LOCK (mandatory): if a living subject is shown, it is ONLY a {subject} "
            f"(a human being) — never a dog, cat, or any animal. Do not depict, draw a silhouette "
            f"of, or label any animal. Any 'suitable for' / size rows reference {subject} use only.")


def role_prompt(role: str, info: dict) -> str:
    name = info["name"] or "product"
    ff = info["form_factor"]
    sizes = info["sizes"]
    colors = info["colors"]
    subject = info.get("subject") or "dog"
    noun = info.get("product_noun") or "product"
    pet = is_pet(subject)
    cons = ""
    if info["constraints"]:
        cons = ("\n\nHONEST-PRODUCT CONSTRAINTS (do not violate — the render must match the "
                "real product the supplier ships):\n- " + "\n- ".join(info["constraints"][:6]))
    if pet:
        base = (f"Product: {name} — a {subject} {noun} for summer.\n"
                f"True form factor (preserve faithfully): {ff}")
    else:
        base = (f"Product: {name} — a {noun} for a {subject}.\n"
                f"True form factor (preserve faithfully): {ff}")
    if role == "lifestyle":
        if pet:
            body = (f"{base}\n\nCREATE a clean, photo-real LIFESTYLE hero LED BY THE PRODUCT: this "
                    f"exact {noun} large and dominant in the foreground, with a calm, happy {subject} "
                    f"using it in a bright, real home or shaded patio setting. "
                    "Soft natural daylight, shallow depth of field, premium e-commerce look. "
                    "1:1 square. ABSOLUTELY NO text, no logos, no graphic overlays — this is the "
                    "Google-Shopping feed image and the variant featured image."
                    + LIFESTYLE_COMPOSITION)
        else:
            if model_is_logical(subject, noun, name):
                body = (f"{base}\n\nCREATE a clean, photo-real LIFESTYLE hero LED BY THE PRODUCT: "
                        f"this exact {noun} large and dominant in the foreground, shown in genuine "
                        f"use on the relevant body part of a {subject}, in a bright, believable "
                        "everyday setting that matches the product's real use. "
                        "Soft natural daylight, shallow depth of field, premium e-commerce look. "
                        "1:1 square. ABSOLUTELY NO text, no logos, no graphic overlays — this is the "
                        "Google-Shopping feed image and the variant featured image."
                        + LIFESTYLE_COMPOSITION + MODEL_SECONDARY)
            else:
                body = (f"{base}\n\nCREATE a clean, photo-real LIFESTYLE hero LED BY THE PRODUCT: "
                        f"this exact {noun} large and dominant in the foreground, shown in its real "
                        "use context in a bright, believable everyday setting that matches the "
                        "product's real use — with NO person present. "
                        "Soft natural daylight, shallow depth of field, premium e-commerce look. "
                        "1:1 square. ABSOLUTELY NO text, no logos, no graphic overlays — this is the "
                        "Google-Shopping feed image and the variant featured image."
                        + LIFESTYLE_COMPOSITION + NO_MODEL)
    elif role == "benefits":
        body = (f"{base}\n\nCREATE an Amazon-style BENEFITS infographic: the product centered on "
                "a clean light background with 3-4 short feature callout labels and thin pointer "
                "lines that name ONLY benefits the true form factor above genuinely supports. "
                "Only claim benefits the true form factor supports — never invent a feature. "
                "Crisp legible sans-serif. 1:1 square.")
    elif role == "construction":
        body = (f"{base}\n\nCREATE a CONSTRUCTION / material close-up: a macro shot or simple "
                f"cutaway showing the {noun}'s surface texture and material layers, with 2-3 small "
                "labels naming the real materials only. 1:1 square.")
    elif role == "size-guide":
        scale = (f"Include a {subject}-silhouette scale next to each size. "
                 if pet else
                 "Show the item clearly with its real proportions for scale. ")
        body = (f"{base}\n\nCREATE a SIZE-GUIDE graphic: the {noun} shown flat with clean dimension "
                "lines and a small size table. Use ONLY these real sizes: "
                f"{sizes or 'a single size as the supplier ships'}. {scale}"
                "Do NOT invent sizes beyond the list. 1:1 square.")
    elif role == "use-anywhere":
        if pet:
            scenes = ("a dog crate/kennel, a car back seat, a living-room floor, and a pet bed")
        else:
            scenes = ("four believable everyday settings where a person would really use this "
                      f"{noun} (matching its true purpose)")
        body = (f"{base}\n\nCREATE a USE-ANYWHERE multi-scene composite (2x2 grid) showing the "
                f"same {noun} used in: {scenes}. Consistent product across all four. Small "
                "location captions allowed. 1:1 square.")
    else:  # product-white
        body = (f"{base}\n\nCREATE a clean studio PACKSHOT: the {noun} alone on a pure white "
                "background, slight angle showing its surface, soft shadow. "
                "NO text. 1:1 square.")
    if colors:
        body += f"\n\nColorway shown by supplier: {colors} — keep the product's true color."
    if role in ROLES_WITH_TEXT:
        body += TYPE_RULE
    body += subject_rule(subject)
    # Prepend the role-aware reference-grounding contract here (where the role is
    # known) instead of at the transport layer, so product-white isolates the
    # product on white rather than preserving the supplier scene.
    return ref_prefix(role) + body + cons


# ---------------------------------------------------------------------------
# Image generation — two transports, identical contract (prompt, key, model,
# refs) -> PNG bytes. Provider is chosen in main() from settings.toml:
#   provider = "google"     -> call_gemini_image    (DIRECT Gemini API)
#   provider = "openrouter" -> call_openrouter_image (OpenRouter, the fallback)
# Both attach the supplier reference photos FIRST (V10-11 gate) then the same
# subject-preservation prefix, so the only thing that changes is the wire shape.
# ---------------------------------------------------------------------------

# V10-11 subject-preservation prefix + gate message — shared by both transports
# so the contract (and the hard no-text-to-image gate) is byte-identical.
REF_PREFIX = (
    "Generate a NEW image featuring the EXACT product shown in the reference image(s) above. "
    "Preserve the product's shape, materials, proportions, and color faithfully. "
    "Vary ONLY the surrounding scene, lighting, props, camera angle, and composition. "
    "Do NOT alter the product itself or add features it does not have. "
    "BRAND-SWAP (mandatory): remove ONLY brand names, store names, logos, wordmarks, monograms, "
    "and badges that appear on the product, its tag, or its packaging in the reference. KEEP all "
    "other label content — descriptive product-name text (e.g. 'Keratin Protein Cream'), size/volume, "
    "usage lines, and the label's layout, colors, and design. If removing a brand wordmark leaves an "
    "obvious empty panel, fill it with the descriptive product name in a matching neutral typeface. "
    "Treat any coined, trademark-style name (a distinctive capitalized word that is not a plain "
    "English product noun, or anything marked ™/®) as a BRAND to remove — when unsure, replace it "
    "with the plain product name. Never reproduce or invent any brand mark. "
    "Render the product EXACTLY ONCE: never add a second, floating, duplicated, mirrored, "
    "or ghosted copy of the product, and never composite the reference photo's background "
    "behind a separate product cut-out — unless the prompt explicitly asks for a multi-panel "
    "grid.\n\n"
)
GATE_MSG = "V10-11 GATE: no discovery-reference photos found — refusing text-to-image"


def ref_prefix(role: str) -> str:
    """Role-aware version of the V10-11 reference-grounding contract.

    The packshot needs the OPPOSITE of "keep the reference scene". Using the
    generic REF_PREFIX ("vary ONLY the surrounding scene, do not alter the
    product") for product-white directly contradicts its body ("the mat alone on
    a pure white background"): the model resolved the conflict by keeping the
    supplier's deck-scene-with-dog AND adding a second floating mat — a duplicated
    product on the wrong background. The packshot must instead DROP the reference
    scene and isolate exactly one unit on white."""
    if role == "product-white":
        return (
            "Use the reference image(s) above ONLY to identify the EXACT product "
            "(its shape, materials, proportions, and color). Generate a NEW studio "
            "packshot of that product. Show EXACTLY ONE unit of the product, fully "
            "isolated on a pure white seamless background. REMOVE everything else "
            "from the reference: no animals, no people, no plants, no furniture, no "
            "floor, no second copy of the product. Exactly one unit, soft contact "
            "shadow. BRAND-SWAP: remove ONLY brand/store names, logos, wordmarks, and badges "
            "from the product or packaging; KEEP descriptive label text (product name, size/volume, "
            "usage lines) and the label's layout, colors, and design — fill any wordmark gap with "
            "the descriptive product name in a matching neutral typeface; "
            "do NOT reproduce or invent any brand mark. "
            "Do NOT alter the product or add features it lacks.\n\n"
        )
    return REF_PREFIX


def _to_png(data: bytes) -> bytes:
    """Gemini returns JPEG bytes; the gallery writes `.png` files and upload_gallery
    guesses the Shopify mime from the extension — so a JPEG-in-.png would mislabel
    the upload. Transcode to real PNG so the extension matches the content. (The
    OpenRouter path already returns PNG, so this is a no-op there.)"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(data))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# Locked GMC / Google-Shopping feed geometry: a true 1:1 square at the 1k setting.
# Memory: feedback_nano_banana_flash_cheap_tier_model.md ("ALWAYS 1080p / 1k").
GMC_SQUARE_PX = 1024


def _square_1k(data: bytes) -> bytes:
    """DETERMINISTIC GMC-feed geometry gate — runs on EVERY generated tile.

    The prompt *asks* for "1:1 square", but prompt text is not a guarantee: if the
    model drifts to a non-square output, Google Shopping fits it into its square
    product card and pads the leftover with white bars (the "decors letterbox"
    look — wasted card space, cheaper appearance). This forces every tile to an
    EXACT 1024x1024 square by center-cropping to the shorter side (no white bars,
    no stretch) then resizing to 1024. Squareness no longer depends on the model
    behaving, and it holds for BOTH transports (Gemini direct + OpenRouter)."""
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(data))
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    if w != h:
        s = min(w, h)
        left = (w - s) // 2
        top = (h - s) // 2
        im = im.crop((left, top, left + s, top + s))
    if im.size != (GMC_SQUARE_PX, GMC_SQUARE_PX):
        im = im.resize((GMC_SQUARE_PX, GMC_SQUARE_PX), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def generate_image(provider: str, prompt: str, api_key: str, model: str,
                   refs: list[Path]) -> bytes:
    raw = (call_gemini_image(prompt, api_key, model, refs) if provider == "google"
           else call_openrouter_image(prompt, api_key, model, refs))
    # Final, deterministic 1:1 square @ 1k — guarantees the GMC feed image and every
    # gallery tile are exactly square regardless of what the model returned.
    return _square_1k(raw)


def call_gemini_image(prompt: str, api_key: str, model: str, refs: list[Path]) -> bytes:
    """DIRECT Gemini API (generativelanguage.googleapis.com). No OpenRouter hop:
    cheaper per image and one fewer point of failure. Same reference-grounded,
    subject-locked contract as the OpenRouter path."""
    m = model.split("/")[-1]  # "google/gemini-3-pro-image-preview" -> bare model id
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
    parts: list[dict] = []
    used = 0
    for rp in refs:
        if not rp.exists():
            continue
        mime, _ = mimetypes.guess_type(str(rp))
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        with open(rp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        used += 1
    if used == 0:
        raise RuntimeError(GATE_MSG)
    parts.append({"text": prompt})
    # NOTE: do NOT send generationConfig.imageConfig.aspectRatio — on the direct
    # generativelanguage.googleapis.com path it causes the generateContent call to
    # hang indefinitely (>300s timeout) for gemini-2.5-flash-image / gemini-3-pro-image.
    # Empirically, dropping it returns an image in ~10-15s. _square_1k() below is the
    # hard 1:1/1k guarantee, so the API-level aspect hint was redundant anyway.
    payload = {
        "contents": [{"parts": parts}],
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    with _SEM:
        resp = requests.post(url, headers=headers, json=payload, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    cands = body.get("candidates") or []
    if not cands:
        raise RuntimeError(f"no candidates: {str(body)[:300]}")
    for p in cands[0].get("content", {}).get("parts", []):
        inline = p.get("inlineData") or p.get("inline_data")
        if inline and inline.get("data"):
            return _to_png(base64.b64decode(inline["data"]))
    raise RuntimeError(f"could not extract image; snippet={str(body)[:200]}")


def call_openrouter_image(prompt: str, api_key: str, model: str, refs: list[Path]) -> bytes:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/google-stores/general-store",
        "X-Title": "general-store gallery gen",
    }
    content: list[dict] = []
    used = 0
    for rp in refs:
        if not rp.exists():
            continue
        mime, _ = mimetypes.guess_type(str(rp))
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        with open(rp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        used += 1
    if used == 0:
        raise RuntimeError(GATE_MSG)
    content.append({"type": "text", "text": prompt})
    payload = {"model": model, "messages": [{"role": "user", "content": content}],
               "modalities": ["image", "text"]}

    with _SEM:
        resp = requests.post(url, headers=headers, json=payload, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"no choices: {str(body)[:300]}")
    msg = choices[0].get("message", {})
    mc = msg.get("content")
    if isinstance(mc, list):
        for b in mc:
            if isinstance(b, dict) and b.get("type") == "image_url":
                u = b.get("image_url", {}).get("url", "")
                if u.startswith("data:image"):
                    return base64.b64decode(u.split(",", 1)[1])
                if u.startswith("http"):
                    r = requests.get(u, timeout=90); r.raise_for_status(); return r.content
    for imgs in (msg.get("images"), choices[0].get("images")):
        if imgs:
            first = imgs[0]
            if isinstance(first, dict):
                iu = first.get("image_url")
                if isinstance(iu, dict):
                    u = iu.get("url", "")
                    if u.startswith("data:image"):
                        return base64.b64decode(u.split(",", 1)[1])
                    if u.startswith("http"):
                        r = requests.get(u, timeout=90); r.raise_for_status(); return r.content
                data = first.get("b64_json") or first.get("data")
                if data:
                    return base64.b64decode(data)
                if first.get("url"):
                    r = requests.get(first["url"], timeout=90); r.raise_for_status(); return r.content
            if isinstance(first, str) and first.startswith("data:image"):
                return base64.b64decode(first.split(",", 1)[1])
    if isinstance(mc, str) and mc.startswith("data:image"):
        return base64.b64decode(mc.split(",", 1)[1])
    raise RuntimeError(f"could not extract image; msg keys={list(msg.keys())} snippet={str(body)[:200]}")


# ---------------------------------------------------------------------------
# Per-SKU / per-role orchestration
# ---------------------------------------------------------------------------

def spec_index(spec_path: str) -> dict:
    """Map slug -> {sizes: 'S, M, L', n_sizes: int, name: str} from the spec's
    variants. The spec is the AUTHORITATIVE size source (it's what create_drafts.py
    actually ships); the free-text manifest's `Sizes:` line is only a hint and has
    silently dropped sizes when it wrapped. Using the spec removes that whole class
    of 'size-guide shows fewer sizes than the product sells' bug."""
    if not spec_path:
        return {}
    spec = json.loads(Path(spec_path).read_text())
    subject = spec.get("subject", "")
    idx = {}
    for sku in spec.get("skus", []):
        sizes = [v["size"] for v in sku.get("variants", [])]
        idx[sku["slug"]] = {
            "sizes": ", ".join(sizes),
            "n_sizes": len(sizes),
            "name": sku.get("title", ""),
            "subject": subject,
        }
    return idx


def sku_refs(sku_dir: Path) -> list[Path]:
    rd = sku_dir / "_supplier-refs"
    refs = sorted(rd.glob("supplier-*.jpg")) + sorted(rd.glob("supplier-*.png")) + sorted(rd.glob("supplier-*.jpeg"))
    return refs[:4]  # first 4 are enough to ground shape; keeps payload small


def gen_one(sku_dir: Path, role: str, info: dict, refs: list[Path],
            provider: str, api_key: str, model: str, force: bool, dry: bool,
            retries: int = 3) -> tuple[str, str, bool, str]:
    slug = sku_dir.name.replace("sku-", "")
    idx = ROLES.index(role) + 1
    out = sku_dir / "images" / f"{idx:02d}-sku-{slug}-{role}.png"
    if out.exists() and not force:
        return (sku_dir.name, role, True, "exists")
    prompt = role_prompt(role, info)
    if dry:
        log(f"  [dry] {sku_dir.name}/{role}: {prompt[:90]}...")
        return (sku_dir.name, role, True, "dry")
    # In-process retry-with-backoff. The endpoint's failure mode is a read-timeout
    # under load; retrying the SAME call after a short backoff almost always
    # succeeds, which removes the manual "re-run the dropped roles" pass that used
    # to dominate build time. Old behavior DROPPED a failure on the first error.
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            img = generate_image(provider, prompt, api_key, model, refs)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(img)
            tag = "ok" if attempt == 1 else f"ok@try{attempt}"
            log(f"  ✓ {sku_dir.name}/{role}  ({len(img)//1024}KB) {tag if attempt>1 else ''}".rstrip())
            return (sku_dir.name, role, True, tag)
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                back = 3 * attempt  # 3s, 6s — short, since timeouts are transient
                log(f"  … {sku_dir.name}/{role} try{attempt} failed ({last_err[:70]}); retry in {back}s")
                time.sleep(back)
            else:
                log(f"  ✗ {sku_dir.name}/{role}  gave up after {retries} tries: {last_err[:100]}")
    return (sku_dir.name, role, False, last_err[:120])


def discover_skus(cat: Path, arg_skus: str, all_ready: bool) -> list[Path]:
    if arg_skus:
        return [cat / s.strip() for s in arg_skus.split(",") if s.strip()]
    skus = []
    for d in sorted(cat.glob("sku-*")):
        if not (d / "_supplier-refs").is_dir():
            continue
        if not sku_refs(d):
            continue
        if all_ready:
            imgs = list((d / "images").glob("*")) if (d / "images").is_dir() else []
            if imgs:
                continue  # already has a gallery
        skus.append(d)
    return skus


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("category_dir")
    ap.add_argument("--skus", default="")
    ap.add_argument("--all-ready", action="store_true", help="every sku-* with refs and empty images/")
    ap.add_argument("--roles", default=",".join(ROLES))
    ap.add_argument("--spec", default="",
                    help="expansion/draft spec JSON; AUTHORITATIVE source for per-SKU sizes "
                         "+ subject (overrides the free-text manifest's Sizes: line)")
    ap.add_argument("--subject", default="dog",
                    help="fallback SUBJECT LOCK if --spec has no 'subject' key")
    ap.add_argument("--max-concurrent", type=int, default=3,
                    help="global cap on in-flight image calls. Keep LOW (2-3): the endpoint "
                         "times out under load and high concurrency forces slower re-run passes.")
    ap.add_argument("--product-workers", type=int, default=4)
    ap.add_argument("--image-workers", type=int, default=3)
    ap.add_argument("--retries", type=int, default=3, help="in-process retry-with-backoff per image")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    global _SEM
    _SEM = threading.Semaphore(args.max_concurrent)

    cat = Path(args.category_dir).resolve()
    if not cat.is_dir():
        sys.exit(f"ERROR category dir not found: {cat}")
    ig = load_settings()
    provider = ig.get("provider", "openrouter")
    if provider == "google":
        # DIRECT Gemini API — own key (falls back to the OpenRouter key field only
        # if google_api_key is unset, which won't authenticate, so it's set below).
        api_key = ig.get("google_api_key") or ig["api_key"]
        model = ig.get("model", "gemini-3-pro-image-preview")
    else:
        api_key, model = ig["api_key"], ig.get("model", "openai/gpt-5.4-image-2")
    roles = [r.strip() for r in args.roles.split(",") if r.strip() in ROLES]
    spec_idx = spec_index(args.spec)
    spec_subject = ""
    if spec_idx:
        spec_subject = next(iter(spec_idx.values())).get("subject", "")

    skus = discover_skus(cat, args.skus, args.all_ready)
    if not skus:
        sys.exit("no SKUs matched (need _supplier-refs/supplier-*.jpg; --all-ready skips ones with images)")
    log(f"category={cat.name}  skus={len(skus)}  roles={len(roles)}  "
        f"provider={provider}  model={model}  max_concurrent={args.max_concurrent}  retries={args.retries}  "
        f"(product_workers={args.product_workers} x image_workers={args.image_workers})  "
        f"spec={'yes' if spec_idx else 'no'}")
    for s in skus:
        log(f"  - {s.name}  refs={len(sku_refs(s))}")

    results = []

    def run_sku(sku_dir: Path):
        info = parse_manifest(sku_dir / "_supplier-refs" / "_manifest.txt")
        info["subject"] = spec_subject or args.subject
        # Category dir name is the product noun ("cooling-towel" -> "cooling towel").
        # Generalizes the prompts off the old dog-cooling-mat hardcode.
        info["product_noun"] = cat.name.replace("-", " ")
        # AUTHORITATIVE sizes from the spec, not the free-text manifest. Also assert
        # the manifest didn't silently drop sizes (the wrapped-`Sizes:` bug).
        slug = sku_dir.name.replace("sku-", "")
        si = spec_idx.get(slug)
        if si:
            mf_sizes = [x for x in re.split(r"\s*/\s*|\s*,\s*", info.get("sizes", "")) if x.strip()]
            if mf_sizes and len(mf_sizes) != si["n_sizes"]:
                log(f"  ! {sku_dir.name}: manifest lists {len(mf_sizes)} sizes but spec ships "
                    f"{si['n_sizes']} — using SPEC (authoritative). Check the manifest Sizes: line.")
            info["sizes"] = si["sizes"]
            if si["name"]:
                info["name"] = si["name"]
        refs = sku_refs(sku_dir)
        # Feed-critical wave FIRST (lifestyle hero + packshot) so a later infographic
        # timeout can never leave the listing without its variant featured image.
        wave1 = [r for r in roles if r in FEED_CRITICAL]
        wave2 = [r for r in roles if r not in FEED_CRITICAL]
        res = []
        for wave in (wave1, wave2):
            if not wave:
                continue
            with ThreadPoolExecutor(max_workers=args.image_workers) as ex:
                futs = [ex.submit(gen_one, sku_dir, r, info, refs, provider, api_key, model,
                                  args.force, args.dry_run, args.retries) for r in wave]
                res.extend(f.result() for f in as_completed(futs))
        return res

    with ThreadPoolExecutor(max_workers=args.product_workers) as pex:
        futs = {pex.submit(run_sku, s): s for s in skus}
        for f in as_completed(futs):
            results.extend(f.result())

    ok = sum(1 for _, _, good, _ in results if good)
    bad = [(s, r, m) for s, r, good, m in results if not good]
    log(f"\nDONE  {ok}/{len(results)} images ok")
    if bad:
        log("FAILURES:")
        for s, r, m in bad:
            log(f"  {s}/{r}: {m}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
