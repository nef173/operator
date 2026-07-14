#!/usr/bin/env bash
# Vendor the research/listing step scripts from the Google Stores tree into api/steps/
# so the Railway image can run them (jobs.py probes repo_root()/<step>/scripts/...).
# Re-run after editing any step script, THEN commit — GitHub deploys only ship git-tracked
# files (see memory: railway-deploy-untracked-files-and-service-layout).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # the Google Stores project root
DEST="$(cd "$(dirname "$0")/.." && pwd)/api/steps"

copy() { # copy() <src-dir-rel-to-root> — .py files (+ listed extras) only, no venv/caches
  local rel="$1"; shift
  mkdir -p "$DEST/$rel"
  find "$ROOT/$rel" -maxdepth 1 -name '*.py' -exec cp {} "$DEST/$rel/" \;
  for extra in "$@"; do
    [ -f "$ROOT/$rel/$extra" ] && cp "$ROOT/$rel/$extra" "$DEST/$rel/"
  done
}

copy "01-niche-discovery/scripts" requirements.txt
# stores.txt = the tracked domains; the 3 enrichment JSONs = the competitor-spy roster fill
# (TrendTrack traffic + GENERAL/NICHE class + Google-Ads footprint). Without them the live roster
# shows bare domains with zero data — spy_roster() reads these from repo_root()/06-.../scripts.
copy "06-launch-general-store/scripts" requirements.txt stores.txt store_traffic.json store_class.json ads_transparency.json
copy "05-launch-niche-store/china-source-match/scripts" requirements.txt

echo "synced -> $DEST"
find "$DEST" -type f | wc -l
