"""Write/control layer — drives the canonical pipeline state-machine scripts.

The backend WRAPS the pipeline; it does NOT reimplement the state machines. The two
stdlib CLIs in 06-launch-general-store/scripts are the single source of truth:

  candidate_queue.py  promote <keyword> --apply   (enforces the Lane-1 PASS gate;
                                                   itself shells to listing_queue add-category)
  listing_queue.py    set <slug> <sku> --state S  (candidate→…→winner|killed)
                      add-category <slug> --keyword …

Both honor GENERAL_STORES_DIR, so we point that at config.general_stores_dir() — the
exact directory readers.py reads from — and run them with the api's own venv Python
(the scripts are stdlib-only). Success prints to stdout; errors sys.exit(msg) + non-zero.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

from . import config

_SCRIPTS = config.repo_root() / "06-launch-general-store" / "scripts"


def _run_script(script: str, store: str, *cli_args: str) -> dict:
    """Run a canonical state-machine script and capture the result."""
    cmd = [sys.executable, str(_SCRIPTS / script), store, *cli_args]
    env = {**os.environ, "GENERAL_STORES_DIR": str(config.general_stores_dir())}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(config.repo_root()),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": "timed out after 60s"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def set_sku_state(store: str, slug: str, sku: str, state: str, note: str | None = None) -> dict:
    args = [slug, sku, "--state", state]
    if note:
        args += ["--note", note]
    return _run_script("listing_queue.py", store, "set", *args)


def remove_category(store: str, slug: str) -> dict:
    """Remove a keyword-category (and its SKUs) from the listing queue — undoes a mis-add."""
    return _run_script("listing_queue.py", store, "remove-category", slug)


def remove_sku(store: str, slug: str, sku: str) -> dict:
    """Remove a single SKU from a category — undoes a mis-added found product."""
    return _run_script("listing_queue.py", store, "remove-sku", slug, sku)


def _run_general(script: str, *cli_args: str) -> dict:
    """Run a 06-launch-general-store script that takes NO leading <store> arg (e.g. season_classify)."""
    cmd = [sys.executable, str(_SCRIPTS / script), *cli_args]
    env = {**os.environ, "GENERAL_STORES_DIR": str(config.general_stores_dir())}
    try:
        proc = subprocess.run(cmd, cwd=str(config.repo_root()), env=env,
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": "timed out"}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": (proc.stdout or "").strip(), "stderr": (proc.stderr or "").strip()}


def promote_candidate(store: str, keyword: str) -> dict:
    return _run_script("candidate_queue.py", store, "promote", keyword, "--apply")


def promote_keyword(store: str, keyword: str) -> dict:
    """Promote a RAW keyword (a Trend card) into the pipeline — the SAME meaning as Promote on the
    Keyword Research table. A trend keyword usually isn't a candidate yet, so:
      (1) try a straight promote (works if a keyword run already ingested it); else
      (2) ingest it from its trend-dossier keyword-data.json through a LOCAL season gate (no
          DataForSEO — the trend run already pulled the SV), score, then promote.
    Promote itself shells to listing_queue add-category; the ROUTE then fires chain_after_promote
    (SKU plan → product-find). Returns {ok, promoted, path, reason}."""
    from . import research_dfs  # module-level _slug; importing runs no main()
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "reason": "empty keyword"}

    # (1) Fast path — already a gate-cleared candidate (common once keyword research has run).
    direct = _run_script("candidate_queue.py", store, "promote", kw, "--apply")
    if direct["ok"]:
        return {"ok": True, "promoted": True, "path": "direct", "output": direct["stdout"]}

    # (2) Ingest from the trend dossier (local season gate — reuses the SV the trend run pulled).
    dd = config.dossiers_dir() / research_dfs._slug(kw)
    kw_path = dd / "keyword-data.json"
    if not kw_path.is_file():
        return {"ok": False,
                "reason": "no search-volume data for this keyword yet — run Trend or Keyword "
                          "research on it first, then Promote."}
    trends_path = dd / "trends.json"
    season_path = dd / "season.json"
    sc = _run_general("season_classify.py", "--keywords", str(kw_path),
                      "--trends", str(trends_path if trends_path.is_file() else kw_path),
                      "--geo", "US", "--out", str(season_path))
    if not sc["ok"] or not season_path.is_file():
        return {"ok": False, "reason": ("could not gate this keyword: " + sc["stderr"]).strip()[:300]}
    _run_script("candidate_queue.py", store, "ingest-keywords", "--season", str(season_path))
    _run_script("candidate_queue.py", store, "score")
    promo = _run_script("candidate_queue.py", store, "promote", kw, "--apply")
    if promo["ok"]:
        return {"ok": True, "promoted": True, "path": "ingest+promote", "output": promo["stdout"]}
    return {"ok": False, "gated": False,
            "reason": (promo["stderr"] or "this keyword didn't clear the 10k demand gate").strip()[:300]}


def add_category(
    store: str,
    slug: str,
    keyword: str | None = None,
    sv: int | None = None,
    capture: str | None = None,
) -> dict:
    args = [slug]
    if keyword:
        args += ["--keyword", keyword]
    if sv is not None:
        args += ["--sv", str(sv)]
    if capture:
        args += ["--capture", capture]
    return _run_script("listing_queue.py", store, "add-category", *args)


def _sku_id(p: dict) -> str:
    """A stable, collision-resistant SKU id for a found product: ``<source>-<8-hex>``.

    The hash is over the product URL (or title as fallback) so re-listing the same find is
    idempotent — it lands on the same SKU key instead of duplicating. Source-prefixed so the
    listing step can see at a glance which finder surfaced it (aliexpress / temu / 1688 / amazon)."""
    source = (p.get("source") or p.get("store_name") or "mp").strip().lower()
    source = "".join(c for c in source if c.isalnum())[:12] or "mp"
    basis = (p.get("url") or p.get("title") or "").strip()
    digest = hashlib.md5(basis.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"{source}-{digest}"


def add_found_products(store: str, keyword: str, products: list[dict]) -> dict:
    """Handoff: found products (AliExpress / Temu / 1688 / Amazon research) → the listing queue.

    Ensures the keyword exists as a category (idempotent), then writes each picked product as a
    ``candidate`` SKU carrying its research ref (url / image / source / sold demand). The FINAL
    listing price + landed COGS come from the research/listing step later — the marketplace price
    rides along only as the reference basis (never the sell price). One ``add-sku`` subprocess per
    product keeps the state-machine script the single source of truth (this layer never mutates
    listing-queue.json directly)."""
    from . import research_dfs  # module-level _slug; importing runs no main()

    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "reason": "empty keyword"}
    if not products:
        return {"ok": False, "reason": "no products selected"}
    slug = research_dfs._slug(kw)

    # 1) ensure the category exists (add-category setdefaults — safe to call repeatedly)
    cat = _run_script("listing_queue.py", store, "add-category", slug, "--keyword", kw)
    if not cat["ok"]:
        return {"ok": False, "reason": (cat["stderr"] or "add-category failed").strip()[:300]}

    # 2) add each product as a candidate SKU with its research ref.
    # DEDUP-CAP enforcement: skip products the Gemini-vision dedup marked as DROP (the SAME physical
    # product beyond `dedup_cap` in its photo group) — so the same product is never listed more than
    # `dedup_cap` times. Read from the stored dedup result so it holds regardless of the frontend payload.
    from . import photo_dedup
    drops = photo_dedup.dropped_keys(store, kw)
    dup_skipped: list[str] = []
    added: list[str] = []
    errors: list[str] = []
    for p in products:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        if p.get("photo_dup_drop") or (drops and photo_dedup._product_key(p) in drops):
            dup_skipped.append(_sku_id(p))
            continue
        sku_id = _sku_id(p)
        args = [slug, sku_id, "--title", title[:200]]
        price = p.get("price")
        if price is not None:
            try:
                args += ["--price", str(float(price))]
            except (TypeError, ValueError):
                pass
        if p.get("url"):
            args += ["--url", str(p["url"])[:500]]
        if p.get("image"):
            args += ["--image", str(p["image"])[:500]]
        src = p.get("source") or p.get("store_name")
        if src:
            args += ["--source", str(src)[:40]]
        sold = p.get("sold_count")
        if sold is None:
            sold = p.get("bought_past_month")
        if sold is not None:
            try:
                args += ["--sold", str(int(sold))]
            except (TypeError, ValueError):
                pass
        r = _run_script("listing_queue.py", store, "add-sku", *args)
        (added if r["ok"] else errors).append(sku_id)

    if not added:
        reason = (
            f"{len(errors)} add-sku call(s) failed" if errors
            else f"all {len(dup_skipped)} were dropped duplicates (dedup cap)" if dup_skipped
            else "no listable products (all missing titles)"
        )
        return {"ok": False, "slug": slug, "added": [], "errors": errors, "count": 0,
                "dup_skipped": dup_skipped, "reason": reason}
    return {"ok": True, "slug": slug, "added": added, "errors": errors, "count": len(added),
            "dup_skipped": dup_skipped}
