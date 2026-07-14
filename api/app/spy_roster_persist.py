"""Persist the competitor-spy ROSTER across Railway redeploys.

The roster's four small files — stores.txt (tracked domains) + store_traffic.json / store_class.json
/ ads_transparency.json (the enrichment) — are read AND written (weekly `discover-general-stores`
finds, operator admit/remove) under `general_store_scripts_dir()`, which on Railway is the ephemeral
image dir that is wiped on every deploy. So new discoveries + manual edits were lost on the next
redeploy and the roster reset to the vendored baseline.

This mirrors those files to the persistent DATA volume:
  hydrate()  — on app boot, restore the persisted copy over the image dir (or, the first time,
               SEED the volume from the vendored baseline that ships in the image).
  persist()  — save the image dir back to the volume after any roster read/write.

Movers/snapshots already live on the volume (bestseller-spy output) — this is ONLY the roster's four
text/JSON files (~35 KB). Best-effort throughout: a persistence failure must never break a read/write.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config

ROSTER_FILES = ("stores.txt", "store_traffic.json", "store_class.json", "ads_transparency.json")


def _vol_dir() -> Path:
    return config.data_root() / "spy-roster"


def _scripts_dir() -> Path:
    return config.general_store_scripts_dir()


def hydrate() -> dict:
    """Boot: make the image-dir roster reflect the persisted state. Per file, if the volume has it,
    restore volume→image (the latest persisted copy over the vendored baseline); otherwise seed
    volume←image (first boot — the vendored baseline becomes the persisted seed)."""
    vol, scripts = _vol_dir(), _scripts_dir()
    restored: list[str] = []
    seeded: list[str] = []
    try:
        vol.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"restored": restored, "seeded": seeded, "error": "volume not writable"}
    for name in ROSTER_FILES:
        v, s = vol / name, scripts / name
        try:
            if v.is_file():
                shutil.copy2(v, s)  # restore the persisted copy over the vendored baseline
                restored.append(name)
            elif s.is_file():
                shutil.copy2(s, v)  # first boot — seed the volume from the vendored baseline
                seeded.append(name)
        except OSError:
            pass
    return {"restored": restored, "seeded": seeded}


def persist() -> None:
    """Save the current image-dir roster to the volume so it survives the next redeploy. Best-effort;
    never raises."""
    vol, scripts = _vol_dir(), _scripts_dir()
    try:
        vol.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in ROSTER_FILES:
        s = scripts / name
        if s.is_file():
            try:
                shutil.copy2(s, vol / name)
            except OSError:
                pass
