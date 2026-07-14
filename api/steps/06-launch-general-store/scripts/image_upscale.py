#!/usr/bin/env python3
"""image_upscale.py — FREE, no-AI resolution lift for the VisionScan image-QA gate.

The gate's deterministic resolution check flags any imported image whose SHORT side is below the
Google-Shopping feed floor (default 800px; our generation standard is 1024px square). When the
image is otherwise CLEAN (good subject, no overlay, just small), the locked policy is the same
auto-clean-over-block spirit as overlay removal: lift it for FREE and list, instead of paying an
AI model to regenerate the whole thing. Only a genuinely PIXELATED / soft source (a tiny image
already blown up, which a resize cannot un-blur) escalates to a paid AI-upscale / regen.

This script does the lift for FREE, locally, with NO AI generation:

  TIER A (best, optional dep)   cv2 with INTER_LANCZOS4 — slightly sharper edge interpolation.
  TIER B (free, Pillow-only)    Image.resize(..., LANCZOS) — high-quality resampling, zero paid
                                deps. Enough to clear the 800px feed floor on a clean small shot.
  TIER C (no engine)            no local engine → exit 3 with the free manual route (AI-upscale /
                                regen from the supplier ref), so the caller hands off instead.

Geometry: by default we enlarge so the SHORT side reaches --target (1024), preserving aspect — a
resize never invents detail, it only clears the pixel floor. Pass --square to also center-crop to
an exact target×target 1:1 (the GMC feed geometry), matching gen_gallery._square_1k.

Output: writes an upscaled copy (default: <name>.upscaled<ext>) and prints a JSON result line.
This is a repair utility on our OWN imported images — it only resamples pixels we already hold;
it never fabricates product detail.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False

try:
    import numpy as np  # noqa: F401  (used by the cv2 path)
    import cv2
    _HAVE_CV2 = True
except ImportError:
    _HAVE_CV2 = False


def _upscale_pillow(image: str, out: str, target: int, square: bool) -> dict:
    im = Image.open(image).convert("RGB")
    w, h = im.size
    short = min(w, h)
    if short < target:
        scale = target / short
        nw, nh = round(w * scale), round(h * scale)
        im = im.resize((nw, nh), Image.LANCZOS)
        w, h = nw, nh
        scaled = True
    else:
        scaled = False
    if square:
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        if side != target:
            im = im.resize((target, target), Image.LANCZOS)
        w, h = target, target
    im.save(out)
    return {"ok": True, "method": "pillow.lanczos", "scaled": scaled,
            "out_w": w, "out_h": h, "square": square}


def _upscale_cv2(image: str, out: str, target: int, square: bool) -> dict:
    img = cv2.imread(image)
    h, w = img.shape[:2]
    short = min(w, h)
    scaled = False
    if short < target:
        scale = target / short
        nw, nh = round(w * scale), round(h * scale)
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LANCZOS4)
        w, h = nw, nh
        scaled = True
    if square:
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img[top:top + side, left:left + side]
        if side != target:
            img = cv2.resize(img, (target, target), interpolation=cv2.INTER_LANCZOS4)
        w, h = target, target
    cv2.imwrite(out, img)
    return {"ok": True, "method": "cv2.lanczos4", "scaled": scaled,
            "out_w": w, "out_h": h, "square": square}


def main() -> int:
    ap = argparse.ArgumentParser(description="Free, no-AI resolution lift for the image-QA gate.")
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", default=None, help="output path (default: <name>.upscaled<ext>)")
    ap.add_argument("--target", type=int, default=1024,
                    help="short side is lifted to at least this many px (default 1024 = feed standard)")
    ap.add_argument("--square", action="store_true",
                    help="also center-crop to an exact target×target 1:1 (GMC feed geometry)")
    ap.add_argument("--engine", choices=["auto", "cv2", "pillow"], default="auto")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        print(json.dumps({"ok": False, "error": f"image not found: {args.image}"}))
        return 2
    out = args.out or str(src.with_suffix("").as_posix() + ".upscaled" + src.suffix)

    if not _HAVE_PIL and not _HAVE_CV2:
        print(json.dumps({
            "ok": False, "engine": None,
            "error": "no local image engine (Pillow/cv2) — use the AI-upscale / regen-from-supplier "
                     "route, then re-scan.",
        }))
        return 3

    use_cv2 = (args.engine == "cv2") or (args.engine == "auto" and _HAVE_CV2)
    try:
        if use_cv2 and _HAVE_CV2:
            res = _upscale_cv2(args.image, out, args.target, args.square)
        else:
            res = _upscale_pillow(args.image, out, args.target, args.square)
    except Exception as e:  # noqa: BLE001 — report any engine failure as a clean JSON line
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 5

    res.update({"in": str(src), "out": out, "target": args.target})
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
