#!/usr/bin/env python3
"""image_autoclean.py — FREE, no-AI overlay remover for the VisionScan image-QA gate.

The gate's #1 FIX case is a *removable overlay*: floating text / a logo / a watermark / a
sale-badge / a competitor-or-store brand name laid OVER the photo background (NOT printed on
the physical product). The locked policy is AUTO-CLEAN-OVER-BLOCK — strip it for free and list,
never regenerate the whole image with a paid AI model just to delete a corner badge.

This script does that strip for FREE, locally, with NO AI generation:

  TIER A (best, optional deps)  cv2.inpaint (Telea / Navier-Stokes) over the overlay mask.
                                Region from --region, or auto-detected text via easyocr /
                                pytesseract when installed. Best on busy backgrounds.
  TIER B (free, Pillow-only)    diffusion inpaint — iteratively fill the masked region from its
                                surrounding pixels (a cheap Navier-Stokes-style blur-fill). On a
                                clean/near-solid background (exactly the Google-feed hero case)
                                the result is indistinguishable from a clean shot. Zero paid deps.
  TIER C (no Pillow)            no local engine → exit 3 with the free manual route (Canva
                                remove-bg / magic-eraser), so the caller hands off instead.

The overlay REGION is supplied by the caller — the cheap-Gemini vision scan returns the overlay
bbox, so auto-clean gets a target for free without its own OCR. Pass it as one or more:
  --region  X,Y,W,H            pixel rect(s) (repeatable)
  --region-norm X,Y,W,H        normalized 0..1 rect(s) (repeatable)  [recommended — model output]
  --ocr                        auto-detect text regions (needs easyocr or pytesseract)

Output: writes a cleaned copy (default: <name>.cleaned<ext>) and prints a JSON result line.
This is a RESEARCH/repair utility on our OWN imported images — it removes overlays we are
allowed to remove (badges/watermarks/foreign brand text on the background); it never fabricates
product detail and is not used to alter anyone's protected work for redistribution.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from PIL import Image, ImageFilter
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False

try:
    import numpy as np  # noqa: F401  (used by the cv2 path)
    import cv2
    _HAVE_CV2 = True
except ImportError:
    _HAVE_CV2 = False


def _parse_rect(s: str) -> tuple[float, float, float, float]:
    parts = [float(x) for x in s.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"rect must be X,Y,W,H — got {s!r}")
    return tuple(parts)  # type: ignore[return-value]


def _px_rects(args, w: int, h: int) -> list[tuple[int, int, int, int]]:
    """Resolve all supplied regions into integer pixel rects, clamped to the image."""
    rects: list[tuple[int, int, int, int]] = []
    for x, y, rw, rh in args.region or []:
        rects.append((int(x), int(y), int(rw), int(rh)))
    for x, y, rw, rh in args.region_norm or []:
        rects.append((int(x * w), int(y * h), int(rw * w), int(rh * h)))
    if args.ocr:
        rects.extend(_ocr_rects(args.image))
    out: list[tuple[int, int, int, int]] = []
    for x, y, rw, rh in rects:
        x = max(0, min(x, w - 1))
        y = max(0, min(y, h - 1))
        rw = max(1, min(rw, w - x))
        rh = max(1, min(rh, h - y))
        out.append((x, y, rw, rh))
    return out


def _ocr_rects(image: str) -> list[tuple[int, int, int, int]]:
    """Auto-detect text bounding boxes (optional — needs easyocr or pytesseract)."""
    try:
        import easyocr  # type: ignore
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        boxes = reader.readtext(image, detail=1)
        out = []
        for poly, _txt, conf in boxes:
            if conf < 0.3:
                continue
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            out.append((int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))))
        return out
    except ImportError:
        pass
    try:
        import pytesseract  # type: ignore
        from PIL import Image as _I
        data = pytesseract.image_to_data(_I.open(image), output_type=pytesseract.Output.DICT)
        out = []
        for i, txt in enumerate(data["text"]):
            if txt.strip() and int(data["conf"][i]) > 30:
                out.append((data["left"][i], data["top"][i], data["width"][i], data["height"][i]))
        return out
    except ImportError:
        return []


def _pad(rects, w, h, pad):
    out = []
    for x, y, rw, rh in rects:
        nx, ny = max(0, x - pad), max(0, y - pad)
        out.append((nx, ny, min(w - nx, rw + 2 * pad), min(h - ny, rh + 2 * pad)))
    return out


def _clean_cv2(image: str, out: str, rects, pad: int) -> dict:
    img = cv2.imread(image)
    h, w = img.shape[:2]
    mask = np.zeros((h, w), dtype="uint8")
    for x, y, rw, rh in _pad(rects, w, h, pad):
        mask[y : y + rh, x : x + rw] = 255
    result = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
    cv2.imwrite(out, result)
    return {"ok": True, "method": "cv2.inpaint(TELEA)", "regions": len(rects)}


def _clean_pillow(image: str, out: str, rects, pad: int, iters: int) -> dict:
    """Free diffusion inpaint: blank the masked region, then repeatedly blur the whole image
    and paste the blurred pixels back ONLY inside the mask. Each pass pulls surrounding colour
    inward; after enough passes the hole is filled from its neighbours. Excellent on the clean /
    near-solid backgrounds our Google-feed heroes use; degrades gracefully on busy backgrounds."""
    im = Image.open(image).convert("RGB")
    w, h = im.size
    padded = _pad(rects, w, h, pad)

    # Mask: white where we must fill.
    mask = Image.new("L", (w, h), 0)
    from PIL import ImageDraw
    md = ImageDraw.Draw(mask)
    for x, y, rw, rh in padded:
        md.rectangle([x, y, x + rw, y + rh], fill=255)

    # Seed the hole with the median border colour of each region (a good starting guess so the
    # diffusion converges fast and doesn't bleed dark pixels in).
    work = im.copy()
    px = work.load()
    for x, y, rw, rh in padded:
        seed = _border_median(im, x, y, rw, rh)
        for yy in range(y, y + rh):
            for xx in range(x, x + rw):
                px[xx, yy] = seed

    for _ in range(max(1, iters)):
        blurred = work.filter(ImageFilter.GaussianBlur(radius=6))
        work.paste(blurred, (0, 0), mask)  # only inside the mask

    # Soft-feather the seam so the patch blends.
    feather = mask.filter(ImageFilter.GaussianBlur(radius=pad or 4))
    final = Image.composite(work, im, feather)
    final.save(out)
    return {"ok": True, "method": "pillow.diffusion-inpaint", "regions": len(rects), "iters": iters}


def _border_median(im, x, y, rw, rh):
    """Median colour of the 1px ring just OUTSIDE the region — the background to fill with."""
    w, h = im.size
    px = im.load()
    samples = []
    for xx in range(max(0, x - 1), min(w, x + rw + 1)):
        if y - 1 >= 0:
            samples.append(px[xx, y - 1])
        if y + rh < h:
            samples.append(px[xx, y + rh])
    for yy in range(max(0, y - 1), min(h, y + rh + 1)):
        if x - 1 >= 0:
            samples.append(px[x - 1, yy])
        if x + rw < w:
            samples.append(px[x + rw, yy])
    if not samples:
        return (255, 255, 255)
    return tuple(int(sorted(c)[len(c) // 2]) for c in zip(*samples))


def main() -> int:
    ap = argparse.ArgumentParser(description="Free, no-AI overlay remover for the image-QA gate.")
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", default=None, help="output path (default: <name>.cleaned<ext>)")
    ap.add_argument("--region", action="append", type=_parse_rect, default=[],
                    help="pixel rect X,Y,W,H to remove (repeatable)")
    ap.add_argument("--region-norm", dest="region_norm", action="append", type=_parse_rect, default=[],
                    help="normalized 0..1 rect X,Y,W,H (repeatable) — the vision model's overlay bbox")
    ap.add_argument("--ocr", action="store_true", help="auto-detect text regions (needs easyocr/pytesseract)")
    ap.add_argument("--pad", type=int, default=6, help="grow each region by N px before filling")
    ap.add_argument("--iters", type=int, default=24, help="Pillow diffusion passes (more = smoother fill)")
    ap.add_argument("--engine", choices=["auto", "cv2", "pillow"], default="auto")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        print(json.dumps({"ok": False, "error": f"image not found: {args.image}"}))
        return 2
    out = args.out or str(src.with_suffix("").as_posix() + ".cleaned" + src.suffix)

    if not _HAVE_PIL and not _HAVE_CV2:
        print(json.dumps({
            "ok": False, "engine": None,
            "error": "no local image engine (Pillow/cv2) — use the FREE manual route: "
                     "Canva remove-bg / magic-eraser, then re-scan.",
        }))
        return 3

    # Need an actual target region (model bbox / explicit rect / OCR). No region → nothing to do.
    if _HAVE_PIL:
        with Image.open(args.image) as _im:
            w, h = _im.size
    else:
        import numpy as _np  # via cv2
        w, h = cv2.imread(args.image).shape[1], cv2.imread(args.image).shape[0]
    rects = _px_rects(args, w, h)
    if not rects:
        print(json.dumps({
            "ok": False,
            "error": "no overlay region supplied — pass --region-norm from the vision scan, "
                     "--region, or --ocr (with easyocr/pytesseract installed).",
        }))
        return 4

    use_cv2 = (args.engine == "cv2") or (args.engine == "auto" and _HAVE_CV2)
    try:
        if use_cv2 and _HAVE_CV2:
            res = _clean_cv2(args.image, out, rects, args.pad)
        elif _HAVE_PIL:
            res = _clean_pillow(args.image, out, rects, args.pad, args.iters)
        else:
            res = _clean_cv2(args.image, out, rects, args.pad)
    except Exception as e:  # noqa: BLE001 — report any engine failure as a clean JSON line
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 5

    res.update({"in": str(src), "out": out})
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
