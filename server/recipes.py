"""Recipes: named snapshots of full state keyed by preset + scene + lens."""
from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any


class RecipeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.recipes: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.recipes = json.loads(self.path.read_text() or "[]")
            except json.JSONDecodeError:
                self.recipes = []
        else:
            self.recipes = []

    def save(self) -> None:
        self.path.write_text(json.dumps(self.recipes, indent=2, default=str))

    def add(self, snapshot: dict[str, Any], name: str, notes: str | None = None) -> dict[str, Any]:
        snapshot = copy.deepcopy(snapshot)   # never alias live state
        ctx = snapshot["context"]
        r = {
            "id": uuid.uuid4().hex[:8], "name": name, "notes": notes, "created": time.time(),
            "preset": ctx["preset"], "scene": ctx["scene"], "lens": ctx.get("lens"),
            "focal_mm": ctx.get("focal_mm"), "orientation": ctx.get("orientation"), "roi": ctx.get("roi"),
            "camera": {"model": snapshot["camera"].get("model"), "settings": snapshot["camera"].get("settings", {})},
            "lights": {k: {"intensity_pct": v.get("intensity_pct"), "cct_k": v.get("cct_k")}
                       for k, v in snapshot.get("lights", {}).items()},
            "analysis": (snapshot.get("last_frame") or {}).get("analysis_summary"),
        }
        self.recipes.append(r)
        self.save()
        return r

    def get(self, rid: str) -> dict[str, Any] | None:
        return next((r for r in self.recipes if r["id"] == rid), None)

    def delete(self, rid: str) -> bool:
        before = len(self.recipes)
        self.recipes = [r for r in self.recipes if r["id"] != rid]
        self.save()
        return len(self.recipes) < before

    def match(self, preset: str, scene: str, lens: str | None = None,
              focal_mm: float | None = None) -> dict[str, Any] | None:
        cands = [r for r in self.recipes if r["preset"] == preset and r["scene"] == scene]
        if lens:
            exact = [r for r in cands if r.get("lens") == lens]
            cands = exact or cands
        if not cands:
            return None
        if focal_mm:
            cands.sort(key=lambda r: abs((r.get("focal_mm") or focal_mm) - focal_mm))
            return cands[0]
        return max(cands, key=lambda r: r["created"])

    def list(self) -> list[dict[str, Any]]:
        return sorted(self.recipes, key=lambda r: -r["created"])
