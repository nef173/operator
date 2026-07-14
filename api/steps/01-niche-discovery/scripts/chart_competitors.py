#!/usr/bin/env python3
"""
Generate a Google-Shopping-style competitor grid PNG from a serp-data.json file
(produced by serp_spy.py). Renders the top N competitors as cards arranged in
a grid — image on top, title + price + merchant + rating below.

Image sources handled:
- HTTPS image URLs (downloaded)
- base64 data URLs (decoded inline)
- Falls back to placeholder rectangle if neither loads

Usage:
  python chart_competitors.py \\
      --serp-data dossiers/premium-sleep-mask/serp-data.json \\
      --keyword "weighted sleep mask" \\
      --out dossiers/premium-sleep-mask/competitors-grid.png \\
      --top 8
"""

import argparse
import base64
import io
import json
import re
import sys
import textwrap
from collections import Counter
from urllib.request import urlopen, Request

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image


# Use a clean sans-serif stack; falls back to DejaVu Sans which ships w/ matplotlib
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]


def load_image(url: str) -> Image.Image | None:
    """Load a PIL image from either an https URL or a data: URL. None on failure."""
    if not url:
        return None
    try:
        if url.startswith("data:"):
            # data:image/webp;base64,XXXX
            header, _, b64 = url.partition(",")
            if not b64:
                return None
            raw = base64.b64decode(b64)
            return Image.open(io.BytesIO(raw)).convert("RGB")
        if url.startswith("http"):
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=10) as r:
                raw = r.read()
            return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as e:
        print(f"  image load failed: {e}", file=sys.stderr)
    return None


def pick_top_competitors(listings: list[dict], n: int,
                         rank_by: str = "serp_position") -> list[dict]:
    """Pick up to N strategically interesting competitors from listings.

    Ranking modes (`rank_by`):
    - "serp_position" (default): Order by the FIRST position each merchant occupies
      in Serper's SERP response — i.e., what Google shows buyers first. Most
      strategically informative for buyer-click capture.
    - "listing_count": Order by how many products each merchant has indexed in
      this SERP — measures SERP impression dominance.
    - "rating_reviews": Order by rating × review count — buyer-trust hierarchy.
    - "price_asc" / "price_desc": Order by price.

    Within each merchant's listings, prefer the one with a loadable image.
    """
    # Group listings by merchant; track first-appearance position
    by_merchant: dict[str, list[dict]] = {}
    first_pos: dict[str, int] = {}
    for i, l in enumerate(listings):
        src = l.get("source") or "Unknown"
        by_merchant.setdefault(src, []).append(l)
        if src not in first_pos:
            first_pos[src] = i

    def merchant_rating_score(m_listings: list[dict]) -> float:
        # rating × reviews, fall back to 0 if missing
        best = 0.0
        for l in m_listings:
            r = l.get("rating") or 0
            rv = l.get("reviews") or 0
            try:
                best = max(best, float(r) * float(rv))
            except Exception:
                pass
        return best

    def merchant_min_price(m_listings: list[dict]) -> float:
        prices = []
        for l in m_listings:
            p = l.get("price") or ""
            m = re.search(r"[\d,]+\.\d{2}", p) or re.search(r"[\d,]+", p)
            if m:
                try:
                    prices.append(float(m.group().replace(",", "")))
                except Exception:
                    pass
        return min(prices) if prices else float("inf")

    if rank_by == "serp_position":
        sorted_merchants = sorted(by_merchant.items(),
                                  key=lambda kv: first_pos[kv[0]])
    elif rank_by == "listing_count":
        sorted_merchants = sorted(by_merchant.items(), key=lambda kv: -len(kv[1]))
    elif rank_by == "rating_reviews":
        sorted_merchants = sorted(by_merchant.items(),
                                  key=lambda kv: -merchant_rating_score(kv[1]))
    elif rank_by == "price_asc":
        sorted_merchants = sorted(by_merchant.items(),
                                  key=lambda kv: merchant_min_price(kv[1]))
    elif rank_by == "price_desc":
        sorted_merchants = sorted(by_merchant.items(),
                                  key=lambda kv: -merchant_min_price(kv[1]))
    else:
        raise ValueError(f"unknown rank_by: {rank_by}")

    picked: list[dict] = []
    for merchant, m_listings in sorted_merchants:
        # Prefer HTTP image first, base64 second, none third
        m_listings_sorted = sorted(
            m_listings,
            key=lambda l: (
                0 if (l.get("image_url") or "").startswith("http")
                else (1 if (l.get("image_url") or "").startswith("data:") else 2)
            ),
        )
        chosen = m_listings_sorted[0].copy()
        chosen["_merchant_listing_count"] = len(m_listings)
        chosen["_serp_first_position"] = first_pos[merchant]
        picked.append(chosen)
        if len(picked) >= n:
            break
    return picked


def parse_price(p: str | None) -> str:
    """Normalize price string for display."""
    if not p:
        return "—"
    return p.strip()


def _esc(s: str | None) -> str:
    """Escape $ so matplotlib doesn't interpret as math-mode."""
    return (s or "").replace("$", r"\$")


def _wrap_title(title: str, width: int = 38, max_lines: int = 2) -> str:
    """Wrap title to fixed line count, ellipsis on overflow, pad to max_lines."""
    if not title:
        return "\n" * (max_lines - 1)
    lines = textwrap.wrap(title.strip(), width=width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        if len(last) > width - 3:
            last = last[:width - 3]
        lines[-1] = last.rstrip() + "..."
    while len(lines) < max_lines:
        lines.append("")
    return "\n".join(lines)


def render_grid(competitors: list[dict], keyword: str, out_path: str,
                cols: int = 4) -> None:
    n = len(competitors)
    rows = (n + cols - 1) // cols
    # Bigger, taller cards. Higher DPI for crisp text.
    # hspace = big vertical gap so text below image doesn't bleed into next row.
    fig, axes = plt.subplots(rows, cols,
                             figsize=(3.8 * cols, 7.5 * rows),
                             dpi=120,
                             gridspec_kw={"hspace": 1.05, "wspace": 0.20})
    fig.suptitle(f'Google Shopping competitors — "{keyword}"',
                 fontsize=20, y=0.985, fontweight="600", color="#202124")

    if rows == 1:
        axes_flat = [axes] if cols == 1 else list(axes)
    else:
        axes_flat = [ax for row in axes for ax in row]

    for i, ax in enumerate(axes_flat):
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_edgecolor("#e5e7eb")
            spine.set_linewidth(1)
        if i >= n:
            ax.axis("off")
            continue
        l = competitors[i]

        # Image (top of card)
        img = load_image(l.get("image_url", ""))
        if img is None:
            ax.text(0.5, 0.55, "[no image]", ha="center", va="center",
                    fontsize=11, color="#9ca3af", transform=ax.transAxes)
        else:
            ax.imshow(img, aspect="equal")
            ax.set_xlim(0, img.width)
            ax.set_ylim(img.height, 0)

        # Extract + format fields
        title_wrapped = _esc(_wrap_title(l.get("title") or "", width=38, max_lines=2))
        price = _esc(parse_price(l.get("price")))
        merchant = _esc(l.get("source") or "—")
        m_count = l.get("_merchant_listing_count", 1)
        serp_pos = l.get("_serp_first_position")
        meta = f"#{(serp_pos or 0) + 1} in SERP  ·  {m_count} listing{'s' if m_count != 1 else ''}"
        rating = l.get("rating")
        reviews = l.get("reviews")
        rating_line = ""
        if rating:
            rating_line = f"★ {rating}"
            if reviews:
                rating_line += f"  ({reviews})"

        # ---- Stacked text below image with proper hierarchy ----
        # Title — Google-Shopping-style blue, medium weight, 2 fixed lines
        ax.text(0.5, -0.06, title_wrapped, transform=ax.transAxes,
                ha="center", va="top", fontsize=12, color="#1a73e8",
                linespacing=1.35, fontweight="500")

        # Price — large, bold black (the dominant element)
        ax.text(0.5, -0.24, price, transform=ax.transAxes,
                ha="center", va="top", fontsize=17, color="#202124",
                fontweight="700")

        # Merchant — medium, dark gray
        ax.text(0.5, -0.305, merchant, transform=ax.transAxes,
                ha="center", va="top", fontsize=12, color="#3c4043",
                fontweight="500")

        # SERP position + listing count — small, light gray
        ax.text(0.5, -0.355, meta, transform=ax.transAxes, ha="center",
                va="top", fontsize=10, color="#80868b")

        # Rating — small, Google-yellow star color. Use a unicode-safe star fallback.
        if rating_line:
            # ★ may be missing from some fonts; matplotlib will warn but still render via fallback
            ax.text(0.5, -0.405, rating_line, transform=ax.transAxes,
                    ha="center", va="top", fontsize=11,
                    color="#f9ab00", fontweight="600",
                    family="DejaVu Sans")  # has the ★ glyph

    # Don't use tight_layout (it shrinks text-below-axes); use explicit padding.
    # Bottom padding needs to fit text below the LAST row's axes (rating extends to -0.405 of axes height).
    plt.subplots_adjust(top=0.94, bottom=0.10, left=0.025, right=0.975)
    plt.savefig(out_path, dpi=120, facecolor="white")
    plt.close(fig)
    print(f"Wrote {out_path}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serp-data", required=True,
                    help="Path to serp-data.json from serp_spy.py")
    ap.add_argument("--keyword", required=True,
                    help="Which keyword's SERP to pull competitors from")
    ap.add_argument("--out", required=True, help="Output PNG path")
    ap.add_argument("--top", type=int, default=8,
                    help="How many competitors to include (default 8)")
    ap.add_argument("--cols", type=int, default=4,
                    help="Number of columns in the grid (default 4)")
    ap.add_argument("--rank-by",
                    choices=["serp_position", "listing_count",
                             "rating_reviews", "price_asc", "price_desc"],
                    default="serp_position",
                    help="How to order competitors (default: serp_position = "
                         "what Google shows buyers first)")
    args = ap.parse_args()

    with open(args.serp_data) as f:
        data = json.load(f)

    if not isinstance(data, list):
        print("ERROR: serp-data.json must be a list", file=sys.stderr)
        sys.exit(1)

    serp = next((s for s in data if s.get("keyword") == args.keyword), None)
    if not serp:
        keywords = [s.get("keyword") for s in data]
        print(f"ERROR: keyword '{args.keyword}' not found. Available: {keywords}",
              file=sys.stderr)
        sys.exit(1)

    listings = serp.get("listings") or []
    if not listings:
        print(f"ERROR: no listings in SERP for '{args.keyword}'", file=sys.stderr)
        sys.exit(1)

    competitors = pick_top_competitors(listings, args.top, rank_by=args.rank_by)
    print(f"Picked {len(competitors)} competitors from {len(listings)} listings "
          f"({len(set(l.get('source') for l in listings))} unique merchants) "
          f"— ranked by {args.rank_by}",
          file=sys.stderr)
    for i, c in enumerate(competitors, 1):
        print(f"  #{i}  pos={c.get('_serp_first_position'):>2}  "
              f"listings={c.get('_merchant_listing_count')}  "
              f"{c.get('source')}", file=sys.stderr)

    # Write a sidecar JSON with links for the markdown report to consume
    sidecar_path = args.out.rsplit(".", 1)[0] + ".json"
    with open(sidecar_path, "w") as f:
        json.dump([{
            "rank": i + 1,
            "merchant": c.get("source"),
            "title": c.get("title"),
            "price": c.get("price"),
            "rating": c.get("rating"),
            "reviews": c.get("reviews"),
            "link": c.get("link"),
            "image_url": c.get("image_url") if (c.get("image_url") or "").startswith("http") else None,
            "serp_position": c.get("_serp_first_position"),
            "merchant_listing_count": c.get("_merchant_listing_count"),
        } for i, c in enumerate(competitors)], f, indent=2)
    print(f"Wrote {sidecar_path} (competitor metadata + links)", file=sys.stderr)

    render_grid(competitors, args.keyword, args.out, cols=args.cols)


if __name__ == "__main__":
    main()
