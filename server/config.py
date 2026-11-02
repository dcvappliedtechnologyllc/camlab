"""Load config.toml into a plain dict with defaults applied.

Two ways to run share this module:
  web/CLI   `python -m server.main` from the repo: config and data live in the
            repo folder (ROOT), as always.
  Mac app   the frozen bundle (sys.frozen): config and all writable data live
            in ~/Library/Application Support/camlab (HOME); the bundle only
            carries read-only defaults, which are seeded into HOME on first
            launch. $CAMLAB_HOME overrides HOME for either mode.
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent          # repo root, or the bundle's _internal dir when frozen
FROZEN = bool(getattr(sys, "frozen", False))
SEED_FILES = ("config.toml", "recipes.json", "GodoxLight.db", "GodoxLight.db.state.json", "godox_mesh.json")

DEFAULTS: dict[str, Any] = {
    "camera": {"backend": "auto"},
    "lights": {
        "key": {"backend": "dmx", "model": "Godox LE300Bi", "dmx_start": 1,
                "channels": {"intensity": 1, "cct": 2},
                "intensity_min_pct": 0, "intensity_max_pct": 100,
                "cct_min_k": 2800, "cct_max_k": 6500},
        "fill": {"backend": "manual", "model": "Godox LP800Bi",
                 "intensity_min_pct": 0, "intensity_max_pct": 100,
                 "cct_min_k": 2800, "cct_max_k": 6500},
    },
    "dmx": {"transport": "serial", "port": "auto", "universe": 0,
            "artnet_host": "192.0.2.60"},
    "paths": {"frames": "frames", "clips": "clips", "recipes": "recipes.json"},
    "claude": {"iteration_cap": 8, "arm_window_s": 60, "rate_limit_ms": 500},
    "ai": {"api_key": "", "model": "claude-opus-5-5", "cost_cap_usd": 1.0, "daily_cap_usd": 5.0, "effort": "medium", "max_run_s": 240},
    "server": {"host": "127.0.0.1", "port": 8765, "liveview_fps": 3, "liveview_hud_s": 3},
    "chat": {"preferences": "Colleen likes soft light."},
}


def home() -> Path:
    """Where config and writable data live."""
    env = os.environ.get("CAMLAB_HOME")
    if env:
        return Path(env).expanduser()
    if FROZEN:
        return Path.home() / "Library" / "Application Support" / "camlab"
    return ROOT


def seed_home(h: Path) -> list[str]:
    """First launch of the app: copy bundled defaults into HOME (never overwrites)."""
    h.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in SEED_FILES:
        src, dst = ROOT / name, h / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            copied.append(name)
    for d in ("clips", "frames", "logs"):
        (h / d).mkdir(exist_ok=True)
    return copied


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> dict[str, Any]:
    h = home()
    if FROZEN or os.environ.get("CAMLAB_HOME"):
        seed_home(h)
    path = path or h / "config.toml"
    data: dict[str, Any] = {}
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    cfg = _merge(DEFAULTS, data)
    # resolve paths relative to HOME
    for key in ("frames", "clips", "recipes"):
        p = Path(cfg["paths"][key])
        cfg["paths"][key] = str(p if p.is_absolute() else h / p)
    cfg["paths"]["home"] = str(h)
    cfg["paths"]["root"] = str(ROOT)
    cfg["paths"]["logs"] = str(h / "logs")
    cfg["paths"]["context"] = str(h / "context.json")   # last preset/scene/lens/orientation, restored at start
    # chat hand-off: one fixed, easy folder with constant file names (a chat or a skill can always find it)
    ho = Path(str(data.get("paths", {}).get("handoff", "~/Desktop/camlab-handoff"))).expanduser()
    cfg["paths"]["handoff"] = str(ho if ho.is_absolute() else h / ho)
    # light mesh exports: a bare filename means "in HOME" (fall back to the bundle/repo copy)
    for prof in cfg.get("lights", {}).values():
        if isinstance(prof, dict) and prof.get("mesh_export"):
            p = Path(prof["mesh_export"])
            if not p.is_absolute():
                cand = h / p
                prof["mesh_export"] = str(cand if cand.exists() or not (ROOT / p).exists() else ROOT / p)
    return cfg
