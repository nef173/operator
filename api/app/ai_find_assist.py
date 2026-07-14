"""AI finding-assist — cheap-LLM helpers that make Find Products BROADER, reusing the same gateway
and locked cheap tier as ai_product_gate (ASSISTANT_LLM_BASE_URL / ASSISTANT_LLM_API_KEY). Fail-open
and cached: with no gateway configured, or on any error, the finder runs EXACTLY as before on the
bare keyword — zero behaviour change, never blocks or nukes a Find.

The operator's ask — "run Find Products with the AI assistant helping, in parallel." `expand_terms`
is that help: the LLM proposes related buyer search phrasings (synonyms + form/spec variants) for the
SAME product, so the marketplace lanes (AliExpress / 1688 / Temu) fan out across all of them
CONCURRENTLY and surface real products a single literal query misses. The AI widens the net; the
existing parallel lanes do the fetching. Steady-state cost is ~0 — a keyword's synonyms don't change,
so every unique keyword is expanded once and cached.
"""
from __future__ import annotations

import json

from . import config, connections
from .ai_product_gate import _call, _configured, _parse_json_array

_MAX_TERMS = 6  # the head keyword + up to 5 AI variants — bounded so the lane fan-out stays cheap

_SYSTEM = (
    "You widen a dropshipping product-research search. Given ONE product keyword, return up to 5 "
    "ADDITIONAL search phrasings that a supplier catalog (AliExpress / 1688 / Temu) indexes "
    "differently — synonyms, form-factor variants, spec variants, common buyer phrasings — every one "
    "naming the SAME product category as the input. Do NOT drift to accessories, brand names, or a "
    "different product. Return ONLY a JSON array of short lowercase strings (no prose, no markdown). "
    'Example — input "bamboo sheets" -> ["bamboo bed sheet set","bamboo fitted sheet","organic '
    'bamboo bedding","bamboo viscose sheets","cooling bamboo sheet set"].'
)


def _slug(term: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in (term or "").lower()).strip("-")[:60] or "kw"


def expand_terms(keyword: str) -> list[str]:
    """[keyword] + up to 5 AI-proposed related search terms (deduped, order-stable, head first). Cached
    per keyword under finding-cache/ai-terms so steady-state cost is ~0; only a brand-new keyword hits
    the gateway. Fail-open: no gateway configured / any error / unparseable reply -> just [keyword]."""
    kw = (keyword or "").strip()
    if not kw:
        return []
    if not _configured():
        return [kw]
    cache = config.spy_data_dir() / "finding-cache" / "ai-terms" / f"{_slug(kw)}.json"
    try:
        c = json.loads(cache.read_text())
        if isinstance(c, list) and c:
            return [str(t) for t in c]
    except (OSError, ValueError):
        pass
    out = [kw]
    try:
        for t in (_parse_json_array(_call(_SYSTEM, kw)) or []):
            s = str(t).strip().lower()
            if s and s not in out:
                out.append(s)
            if len(out) >= _MAX_TERMS:
                break
    except Exception:  # noqa: BLE001 — advisory; fail open to the bare keyword
        return [kw]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
    except OSError:
        pass
    return out
