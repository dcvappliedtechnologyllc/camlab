"""Single in-memory state + change history + WebSocket broadcast hooks.
Everything the MCP tools and the REST/WS API touch goes through here."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Context:
    preset: str = "product_hero"
    scene: str = "part_dark"
    lens: str = "sigma_17_50"
    focal_mm: float | None = 35
    orientation: str = "landscape"
    rotation: str = "cw"               # when portrait is intended but the body sends landscape pixels: cw | ccw
    roi: list[float] | None = None     # user-drawn subject ROI [x,y,w,h] 0-1
    tag: str = ""                      # location / shoot name: pulled clips become <tag>-001.MP4, <tag>-002.MP4 ...


@dataclass
class ClaudeLoop:
    request: str | None = None         # "optimize" | "step" | None
    status: str = "idle"               # idle | requested | running | converged | stopped | cap | error
    iteration: int = 0
    cap: int = 8
    messages: list[dict] = field(default_factory=list)
    mode: str | None = None            # "ai" (server-side autopilot) | "mcp" (external Claude session) | None
    cost_usd: float = 0.0              # spent by the current/last AI Auto run
    run_id: int = 0


class State:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.camera: dict[str, Any] = {"connected": False, "model": None, "backend": None,
                                       "settings": {}, "capabilities": {}}
        self.lights: dict[str, dict] = {}
        self.context = Context()
        self.claude = ClaudeLoop(cap=cfg["claude"]["iteration_cap"])
        self.armed_until: float = 0.0
        self.recording: bool = False
        self.rec_started: float | None = None
        self.last_clip: str | None = None
        self.takes: list[dict[str, Any]] = []        # auto-pulled takes, newest last
        self.last_take: dict[str, Any] | None = None
        self.startup: dict[str, str] = {"camera": "ok", "lights": "ok"}   # looking|waiting|ok / connecting|ok
        self.simple: dict[str, Any] = {"mode": "lights", "brightness": 80, "warmth": 5600}   # server/simple.py
        self.stills_mode: bool = False                 # server/stills.py: Stills page on (photos, not clips)
        self.stills_look: str | None = None            # the look picked last
        self.stills: list[dict[str, Any]] = []         # photos taken this process, newest last
        self.last_still: dict[str, Any] | None = None
        self.last_frame: dict[str, Any] | None = None   # {path, width, height, analysis, ts}
        self.prev_frame: dict[str, Any] | None = None
        self.history: deque[dict] = deque(maxlen=500)
        self._last_change_ts: float = 0.0
        self._listeners: list[Callable[[dict], Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- arming / guardrails -------------------------------------------
    @property
    def armed(self) -> bool:
        return time.time() < self.armed_until

    def arm(self) -> float:
        self.armed_until = time.time() + self.cfg["claude"]["arm_window_s"]
        self.broadcast({"type": "armed", "until": self.armed_until})
        return self.armed_until

    def disarm(self) -> None:
        self.armed_until = 0.0
        self.broadcast({"type": "armed", "until": 0})

    def rate_limit_ok(self) -> bool:
        now = time.time()
        if now - self._last_change_ts < self.cfg["claude"]["rate_limit_ms"] / 1000:
            return False
        self._last_change_ts = now
        return True

    # ---- history -------------------------------------------------------
    def log(self, source: str, kind: str, before: Any, after: Any, reason: str | None = None) -> dict:
        entry = {"ts": time.time(), "source": source, "kind": kind,
                 "before": before, "after": after, "reason": reason}
        self.history.append(entry)
        self.broadcast({"type": "history", "entry": entry})
        return entry

    # ---- broadcast -----------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, fn: Callable[[dict], Any]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def broadcast(self, msg: dict) -> None:
        for fn in list(self._listeners):
            try:
                res = fn(msg)
                if asyncio.iscoroutine(res) and self._loop:
                    asyncio.run_coroutine_threadsafe(res, self._loop)
            except Exception:
                pass

    # ---- snapshot ------------------------------------------------------
    def snapshot(self) -> dict:
        from .presets import lens_fov, target_vector
        extra = 1.5 if (self.camera.get("settings", {}).get("movie_resolution") == "4k"
                        and self.camera.get("settings", {}).get("movie_fps") == 60) else 1.0
        return {
            "camera": self.camera,
            "lights": self.lights,
            "context": self.context.__dict__ | {
                "lens_fov": lens_fov(self.context.focal_mm, extra),
                "target": target_vector(self.context.preset, self.context.scene)},
            "claude": {"request": self.claude.request, "status": self.claude.status,
                       "iteration": self.claude.iteration, "cap": self.claude.cap,
                       "mode": self.claude.mode, "cost_usd": self.claude.cost_usd, "run_id": self.claude.run_id,
                       "messages": self.claude.messages[-20:]},
            "armed": self.armed, "armed_until": self.armed_until,
            "recording": self.recording, "rec_started": self.rec_started,
            "last_clip": self.last_clip,
            "takes": list(self.takes)[-20:], "last_take": self.last_take,
            "startup": dict(self.startup),
            "simple": dict(self.simple),
            "stills_mode": self.stills_mode, "stills_look": self.stills_look,
            "stills": [{k: v for k, v in s.items() if k != "jpeg"} for s in self.stills[-20:]], "last_still": self.last_still,
            "last_frame": {k: v for k, v in (self.last_frame or {}).items() if k != "jpeg"} or None,
            "history": list(self.history)[-30:],
            "ts": time.time(),
        }

    def snapshot_json(self) -> str:
        return json.dumps(self.snapshot(), default=str)

    # ---- context persistence (preset/scene/lens/orientation survive a restart) ----
    CONTEXT_KEYS = ("preset", "scene", "lens", "focal_mm", "orientation", "rotation", "tag")

    def save_context(self, path: str | None) -> None:
        if not path:
            return
        try:
            data = {k: getattr(self.context, k) for k in self.CONTEXT_KEYS}
            data["simple"] = dict(self.simple)
            data["stills"] = {"mode": self.stills_mode, "look": self.stills_look}
            with open(path, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def load_context(self, path: str | None) -> bool:
        """Restore the saved context; a missing or unreadable file leaves the defaults."""
        if not path:
            return False
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        for k in self.CONTEXT_KEYS:
            if k in data and data[k] is not None:
                setattr(self.context, k, data[k])
        stl = data.get("stills")
        if isinstance(stl, dict):
            self.stills_mode = bool(stl.get("mode"))
            self.stills_look = stl.get("look") or None
        sim = data.get("simple")
        if isinstance(sim, dict):
            if sim.get("mode") in ("lights", "camera"):
                self.simple["mode"] = sim["mode"]
            for k, lo, hi in (("brightness", 0, 100), ("warmth", 2800, 6500)):
                if isinstance(sim.get(k), (int, float)):
                    self.simple[k] = int(max(lo, min(hi, sim[k])))
        return True
