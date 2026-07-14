"""Server-side RESEARCH orchestrator — the always-on path for Trend Radar + Keyword Discovery.

This is what turns the two research steps from "copy a slash command into your own Claude Code"
into jobs the hosted background worker RUNS itself, 24/7, with only the DataForSEO creds already
configured in Connections. No MCP, no browser, no image-gen, no local project — just DFS.

It mirrors `discover_stores_dfs.py`'s design: a stdlib-only orchestrator (runs under the api
interpreter) that shells out to the EXISTING pipeline scripts, writing the SAME artifacts the
readers already read, so the Trend Research + Keyword Discovery surfaces populate automatically.

Two modes (the two manual specs this replaces):

  --mode trend    Trend Radar. Pulls DFS Google-Trends for the seed keyword (+ its related
                  queries) across the geos and writes  dossiers/<slug>/trends.json  (the exact
                  shape readers._trend_rows / trends_overview read). Also pulls SV so the
                  trend cards + dashboard "Found trends" stat fill in.

  --mode keyword  Keyword Discovery funnel. Runs the full Lane-1 chain server-side:
                    keyword_data.py (SV pull) -> season_classify.py (10k gate + capture bucket)
                    -> candidate_queue.py ingest-keywords + score
                  writing  general-stores/<store>/candidate-queue.json  (what
                  readers.keyword_discovery reads). Also emits trends.json (byproduct, feeds
                  momentum + the trends surface).

Seed expansion is FREE: the seed's own Google-Trends `related_queries` (top + rising) become the
extra keywords fed to the SV pull, so one seed fans out into a real candidate set with no extra
API cost beyond the trends call already made.

keyword_data.py + trends_dfs.py need `requests` -> run under the general-store venv (--python).
season_classify.py + candidate_queue.py are stdlib -> run under the same interpreter.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _slug(s: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40] or "research"


def _run(cmd: list[str], cwd: str, env: dict, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


# A Trends `related_queries` harvest is noisy: it mixes in service/trade intent ("ac repair",
# "air cooler installation"), info/navigational queries ("how to…", "…reviews"), and local/travel
# co-searches ("beach near me", "on the beach", "jet2 holidays") that clear the SV floor but are
# NOT products to list. Drop them at expansion so junk never becomes a candidate. (Kept in sync
# with readers._NON_SKU_* — this is the candidate-level guard the gate was missing.)
_JUNK_PHRASES = ("near me", "for sale", "for rent", "how to", "what is", "second hand", "vs ")
_JUNK_WORDS = {
    "repair", "repairs", "installation", "install", "installed", "fitting", "service", "servicing",
    "services", "company", "companies", "contractor", "contractors", "rental", "rentals", "hire",
    "quote", "quotes", "job", "jobs", "salary", "how", "what", "why", "when", "who", "vs", "versus",
    "review", "reviews", "rating", "meaning", "definition", "wiki", "reddit", "youtube", "login",
    "download", "holiday", "holidays", "flight", "flights", "hotel", "hotels", "insurance",
    "weather", "near", "map", "directions",
}


def _is_product_keyword(term: str) -> bool:
    """A candidate keyword must plausibly name a PRODUCT to list — not a service/info/local query."""
    t = (term or "").strip().lower()
    if len(t) < 3 or re.fullmatch(r"[0-9]+", t):  # too short / a bare number ("10")
        return False
    if any(p in t for p in _JUNK_PHRASES):
        return False
    return not (set(re.findall(r"[a-z0-9]+", t)) & _JUNK_WORDS)


def _related_from_trends(trends: list, cap: int) -> list[str]:
    """Harvest extra keyword ideas from the seed's Google-Trends related_queries (top + rising).
    Handles both the dict form ({'query': ...} / {'keyword': ...}) and bare strings. Non-product
    (service / info / local) queries are dropped so they never become candidates."""
    picked: list[str] = []
    seen: set[str] = set()

    def _add(term: object) -> None:
        t = ""
        if isinstance(term, str):
            t = term
        elif isinstance(term, dict):
            t = str(term.get("query") or term.get("keyword") or term.get("value") or "")
        t = re.sub(r"\s+", " ", t).strip()
        if t and t.lower() not in seen and _is_product_keyword(t):
            seen.add(t.lower())
            picked.append(t)

    for r in trends or []:
        if not isinstance(r, dict) or r.get("error"):
            continue
        rq = r.get("related_queries") or {}
        if isinstance(rq, dict):
            for bucket in ("rising", "top"):
                for item in (rq.get(bucket) or []):
                    _add(item)
        elif isinstance(rq, list):
            for item in rq:
                _add(item)
    return picked[:cap]


def _process_seed(seed, args, py, niche, general, env, repo_root, dossiers_root, workdir) -> dict:
    """Run the pipeline for ONE seed → its own dossier + per-seed result. Trend mode stops after
    the trend + SV pull; keyword mode also season-gates + ingests (score runs ONCE in main, after
    every seed, so a 12-seed batch scores the queue once, not 12×)."""
    dossier = dossiers_root / _slug(seed)
    dossier.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []

    # STEP 1 — Google-Trends for the seed -> dossiers/<slug>/trends.json (one card).
    trends_path = dossier / "trends.json"
    tp = _run(
        [py, str(niche / "trends_dfs.py"), "--keywords", seed, "--geo", args.geo,
         "--out", str(trends_path)],
        cwd=repo_root, env=env, timeout=300,
    )
    steps.append({"step": "trends", "rc": tp.returncode,
                  "err": (tp.stderr or "").strip()[-400:] if tp.returncode else ""})
    trends_data: list = []
    if trends_path.is_file():
        try:
            trends_data = json.loads(trends_path.read_text())
        except ValueError:
            trends_data = []
    trend_ok = sum(1 for r in trends_data if isinstance(r, dict) and not r.get("error"))

    # STEP 2 — expand the seed via its related queries, pull SV -> keyword-data.json.
    keywords = [seed] + _related_from_trends(trends_data, args.expand_cap)
    kw_path = dossier / "keyword-data.json"
    kd = _run(
        [py, str(niche / "keyword_data.py"), "--keywords", ",".join(keywords),
         "--location", (args.geo.split(",")[0] or "US"), "--out", str(kw_path)],
        cwd=repo_root, env=env, timeout=300,
    )
    steps.append({"step": "keyword_data", "rc": kd.returncode,
                  "err": (kd.stderr or "").strip()[-400:] if kd.returncode else ""})
    kw_count = 0
    if kw_path.is_file():
        try:
            kw_count = len(json.loads(kw_path.read_text()).get("results") or [])
        except (ValueError, AttributeError):
            kw_count = 0

    result = {
        "ok": tp.returncode == 0, "seed": seed, "slug": _slug(seed),
        "keywords_expanded": len(keywords), "keywords_with_data": kw_count,
        "trends_ok": trend_ok, "steps": steps,
    }
    if args.mode == "trend":
        return result

    # KEYWORD mode — season classify (10k gate + bucket) -> candidate queue ingest (NOT score).
    season_path = workdir / f"season-{result['slug']}.json"
    sc = _run(
        [py, str(general / "season_classify.py"), "--keywords", str(kw_path),
         "--trends", str(trends_path), "--geo", (args.geo.split(",")[0] or "US"),
         "--out", str(season_path)],
        cwd=repo_root, env=env, timeout=120,
    )
    steps.append({"step": "season_classify", "rc": sc.returncode,
                  "err": (sc.stderr or "").strip()[-400:] if sc.returncode else ""})
    gate_pass = 0
    if season_path.is_file():
        try:
            sdoc = json.loads(season_path.read_text())
            rows = sdoc if isinstance(sdoc, list) else (sdoc.get("results") or sdoc.get("classified") or [])
            gate_pass = sum(1 for r in rows if isinstance(r, dict)
                            and str(r.get("gate", "")).upper().startswith("PASS"))
        except ValueError:
            pass
    if sc.returncode == 0 and season_path.is_file():
        ing = _run(
            [py, str(general / "candidate_queue.py"), args.store, "ingest-keywords",
             "--season", str(season_path)],
            cwd=repo_root, env=env, timeout=120,
        )
        steps.append({"step": "candidate_ingest", "rc": ing.returncode,
                      "err": (ing.stderr or "").strip()[-400:] if ing.returncode else ""})
    result["gate_cleared"] = gate_pass
    result["ok"] = all(s["rc"] == 0 for s in steps)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Server-side research orchestrator (trend + keyword).")
    ap.add_argument("--mode", choices=["trend", "keyword"], required=True)
    ap.add_argument("--keyword", required=True,
                    help="seed keyword — OR a comma-separated pool for a batch run (10-15 seeds)")
    ap.add_argument("--store", default="demo")
    ap.add_argument("--geo", default="US,GB")
    ap.add_argument("--python", required=True, help="interpreter with requests (general-store venv)")
    ap.add_argument("--niche-scripts", required=True, help="01-niche-discovery/scripts dir")
    ap.add_argument("--general-scripts", required=True, help="06-launch-general-store/scripts dir")
    ap.add_argument("--dossiers", required=True, help="dossiers dir (readers read trends.json here)")
    ap.add_argument("--general-stores-dir", required=True, help="general-stores dir (candidate-queue.json)")
    ap.add_argument("--out", required=True, help="scratch workdir (season.json etc.)")
    ap.add_argument("--repo-root", default="", help="repo root for the pipeline scripts' cwd")
    ap.add_argument("--expand-cap", type=int, default=24, help="max related keywords to fan out to")
    ap.add_argument("--max-seeds", type=int, default=15, help="cap on batch seeds processed per run")
    args = ap.parse_args()

    # ONE seed or a POOL: the arg-less trend/keyword run passes a comma-separated mix (candidate
    # queue + upcoming events + news + curated) so a single run fills the surface with 10-15 diverse
    # ideas — a healthy mix across every path — instead of one card. De-duped, order preserved.
    seen: set[str] = set()
    seeds: list[str] = []
    for s in (args.keyword or "").split(","):
        s = re.sub(r"\s+", " ", s).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            seeds.append(s)
    if not seeds:
        print(json.dumps({"ok": False, "error": "empty keyword"}))
        return 1
    seeds = seeds[:max(1, args.max_seeds)]

    if not (os.environ.get("DATAFORSEO_USERNAME") and os.environ.get("DATAFORSEO_PASSWORD")):
        print(json.dumps({"ok": False, "error": "no DataForSEO credentials — set DATAFORSEO_USERNAME "
                          "+ DATAFORSEO_PASSWORD in Settings -> Connections -> Data."}))
        return 1

    py = args.python
    niche = Path(args.niche_scripts)
    general = Path(args.general_scripts)
    dossiers_root = Path(args.dossiers)
    workdir = Path(args.out)
    workdir.mkdir(parents=True, exist_ok=True)
    repo_root = args.repo_root or str(Path(args.general_scripts).resolve().parents[1])
    env = dict(os.environ)
    env["GENERAL_STORES_DIR"] = str(Path(args.general_stores_dir).resolve())

    results: list[dict] = []
    for seed in seeds:
        try:
            results.append(
                _process_seed(seed, args, py, niche, general, env, repo_root, dossiers_root, workdir))
        except Exception as e:  # noqa: BLE001 — one bad seed never sinks the whole batch
            results.append({"ok": False, "seed": seed, "slug": _slug(seed), "error": str(e)})

    cq_count = None
    if args.mode == "keyword":
        # Score the candidate queue ONCE, after every seed has ingested (idempotent full re-score).
        scr = _run([py, str(general / "candidate_queue.py"), args.store, "score"],
                   cwd=repo_root, env=env, timeout=180)
        for r in results:
            r.setdefault("steps", []).append({"step": "candidate_score", "rc": scr.returncode,
                "err": (scr.stderr or "").strip()[-400:] if scr.returncode else ""})
        cq_path = Path(env["GENERAL_STORES_DIR"]) / args.store / "candidate-queue.json"
        if cq_path.is_file():
            try:
                cq = json.loads(cq_path.read_text()).get("candidates")
                cq_count = len(cq) if isinstance(cq, (list, dict)) else 0
            except (ValueError, AttributeError):
                cq_count = 0

    ok = any(r.get("ok") for r in results)
    out = {
        "ok": ok, "mode": args.mode, "store": args.store, "geo": args.geo,
        "seeds": seeds, "seed_count": len(seeds),
        "cards": sum(1 for r in results if r.get("trends_ok")),
        "gate_cleared": sum(int(r.get("gate_cleared") or 0) for r in results),
        "candidate_queue_size": cq_count, "results": results,
    }
    print(json.dumps(out, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
