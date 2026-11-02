"""Controller: the one object both the MCP tools and the HTTP API call.
Owns camera, lights, state, recipes, clips. All methods are synchronous and
serialized by a lock; the async layers wrap them with asyncio.to_thread."""
from __future__ import annotations

import base64
import logging
import os
import importlib.util
import threading
import time
from pathlib import Path
from typing import Any

from . import analysis, presets
from .camera.base import CameraBackend, CameraError, CameraInfo
from .clips import ClipStore
from .lights.base import LightBackend
from .lights.manual import ManualLight
from .recipes import RecipeStore
from .state import State

log = logging.getLogger("camlab")

CAMERA_KEYS = ("iso", "shutter", "aperture", "ev_comp", "wb_mode", "kelvin", "picture_style",
               "focus_mode", "movie_resolution", "movie_fps")


class NoCamera(CameraBackend):
    """Placeholder while the app is still looking for the body: every call fails fast
    with a readable error instead of an AttributeError."""
    name = "connecting"

    def connect(self): return self.info()
    def disconnect(self) -> None: pass
    def info(self): return CameraInfo(model=None, backend="connecting", connected=False)
    def get_settings(self): return {}
    def set_settings(self, changes): raise CameraError("camera not connected yet: still looking for the body")
    def set_exposure_mode(self, mode): return False
    def capture_frame(self): raise CameraError("camera not connected yet: still looking for the body")


class Controller:
    AUTO_PULL = True              # after Stop, pull the clip in a background thread
    RETRY_DELAY_S = 5.0
    STARTUP_DISCOVERY_S = 8.0     # first EDSDK look when the UI is already up; the idle path keeps looking

    def __init__(self, cfg: dict[str, Any], client_factory=None):
        self.cfg = cfg
        self.state = State(cfg)
        self.lock = threading.RLock()
        self.camera: CameraBackend | None = None
        self.lights: dict[str, LightBackend] = {}
        self.recipes = RecipeStore(cfg["paths"]["recipes"])
        self.clips = ClipStore(cfg["paths"]["clips"])
        self.frames_dir = Path(cfg["paths"]["frames"]); self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.pending_recipe: dict[str, Any] | None = None
        self._universe = None
        from .autopilot import Autopilot
        self.autopilot = Autopilot(self, cfg, client_factory)
        from .simple import Simple
        self.simple = Simple(self)          # one-screen core: readiness, modes, nudges, level, keep/load
        from .ask import Ask
        self.ask = Ask(self, cfg, client_factory)
        from .stills import Stills
        self.stills = Stills(self)          # the Stills page: looks, one-press capture, photo list

    # ------------------------------------------------------------ startup
    def start(self, deferred: bool = False) -> None:
        """deferred=True (the Mac app): serve state at once with a placeholder camera and no
        lights; the launcher then calls connect_camera() on the main thread and
        connect_lights() from a worker while the UI shows progress."""
        with self.lock:
            self.state.load_context(self.cfg["paths"].get("context"))
            if deferred:
                self.camera = NoCamera()
                self.state.startup = {"camera": "looking", "lights": "connecting"}
                self.refresh_camera_state()
                return
            self._start_camera(discovery_s=None)
            self._apply_context_to_camera()
            self._install_lights(self._build_lights())
            self.refresh_camera_state()

    def _apply_context_to_camera(self) -> None:
        ctx = self.state.context
        if hasattr(self.camera, "subject"):
            self.camera.subject = ctx.scene
        cam_settings = getattr(self.camera, "settings", None)
        if isinstance(cam_settings, dict) and "orientation" in cam_settings:
            cam_settings["orientation"] = ctx.orientation

    def _startup(self, **phases: str) -> None:
        self.state.startup.update(phases)
        self.state.broadcast({"type": "startup", "startup": dict(self.state.startup)})

    def connect_camera(self) -> None:
        """Main thread (EDSDK). Short first discovery; the backend's idle path keeps looking."""
        with self.lock:
            try:
                self._start_camera(discovery_s=self.STARTUP_DISCOVERY_S)
                self._apply_context_to_camera()
                self.refresh_camera_state()
                self._sync_mock_light()
                ok = bool(self.state.camera.get("connected"))
            except Exception as e:  # noqa: BLE001
                log.exception("camera connect failed")
                self.state.camera["error"] = str(e)
                ok = False
            self._startup(camera="ok" if ok else "waiting")

    def connect_lights(self) -> None:
        """Any thread: the Bluetooth scans (seconds each) run outside the lock."""
        try:
            lights = self._build_lights()
        except Exception as e:  # noqa: BLE001
            log.exception("lights failed to start")
            lights = {}
        with self.lock:
            self._install_lights(lights)
            self.state.broadcast({"type": "lights", "lights": self.state.lights})
            self._startup(lights="ok")

    def _start_camera(self, discovery_s: float | None = None) -> None:
        want = self.cfg["camera"].get("backend", "auto")
        if want in ("auto", "edsdk"):
            try:
                from .camera.edsdk_backend import EdsDriver, EdsdkBackend
                edsdk_ok = EdsDriver.available()
            except Exception as e:  # noqa: BLE001  -- a broken EDSDK module must never take the server down
                log.warning("edsdk backend unavailable (%s)", e)
                edsdk_ok = False
                if want == "edsdk":
                    raise CameraError(f"EDSDK backend failed to load: {e}") from e
            if edsdk_ok:
                try:
                    cam = EdsdkBackend()
                    if discovery_s:
                        cam.open_discovery_s = float(discovery_s)
                    # A body that is off or asleep is not an error: keep the EDSDK backend in its
                    # disconnected state; it reconnects by itself (idle path) when the body comes back.
                    if cam.connect_or_wait():
                        log.info("camera: edsdk %s", cam.info().model)
                    else:
                        log.info("camera: edsdk (waiting for the body: %s)", cam.last_error)
                    self.camera = cam
                    return
                except CameraError as e:
                    log.warning("edsdk: %s", e)
                    if want == "edsdk":
                        raise
            elif want == "edsdk":
                raise CameraError("EDSDK framework not found; copy Canon's EDSDK.framework into vendor/")
        if want in ("auto", "gphoto2"):
            if importlib.util.find_spec("gphoto2") is not None:   # python-gphoto2 bindings (libgphoto2 bundled)
                from .camera.gphoto2_backend import Gphoto2Backend
                try:
                    cam = Gphoto2Backend()
                    cam.connect()
                    self.camera = cam
                    log.info("camera: gphoto2 %s", cam.info().model)
                    return
                except CameraError as e:
                    log.warning("gphoto2: %s", e)
                    if want == "gphoto2":
                        raise
            elif want == "gphoto2":
                raise CameraError("python-gphoto2 not installed; .venv/bin/pip install gphoto2")
        if want == "ccapi" or (want == "auto" and self.cfg["camera"].get("ccapi_host")):
            from .camera.ccapi_backend import CCAPIBackend
            try:
                cam = CCAPIBackend(self.cfg["camera"].get("ccapi_host", ""), int(self.cfg["camera"].get("ccapi_port", 8080)))
                cam.connect(); self.camera = cam
                log.info("camera: ccapi %s at %s", cam.info().model, cam.host)
                return
            except CameraError as e:
                log.warning("ccapi: %s", e)
                if want == "ccapi":
                    raise
        from .camera.mock_backend import MockBackend
        self.camera = MockBackend(subject=self.state.context.scene)
        self.camera.connect()
        log.info("camera: mock (no body detected)")

    def _build_lights(self) -> dict[str, LightBackend]:
        """Construct and connect the lights (slow: Bluetooth scans). No lock needed."""
        from .lights.dmx import DMXLight, build_universe
        if self._universe is None:
            self._universe = build_universe(self.cfg["dmx"])
        lights: dict[str, LightBackend] = {}
        for lid, prof in self.cfg["lights"].items():
            backend = str(prof.get("backend", "manual")).lower()
            if backend == "dmx":
                light = DMXLight(lid, prof, self._universe)
            elif backend == "godox_mesh":
                light = self._godox_mesh_or_manual(lid, prof)
            else:
                light = ManualLight(lid, prof)
            light.connect()
            # sensible defaults so the first frame isn't black
            light.set(80 if lid == "key" else 30, 5600)
            lights[lid] = light
        return lights

    def _install_lights(self, lights: dict[str, LightBackend]) -> None:
        for lid, light in lights.items():
            self.lights[lid] = light
            self.state.lights[lid] = light.state().to_dict()
        try:
            if self._universe is not None:
                self._universe.start(fps=30)
        except Exception as e:  # NullTransport etc.
            log.debug("universe refresh not started: %s", e)
        self._sync_mock_light()

    @staticmethod
    def _godox_mesh_or_manual(lid: str, prof: dict) -> LightBackend:
        """Bluetooth Mesh (Godox app protocol). Any construction failure -- bleak
        or cryptography missing, export unreadable, light not in export -- falls
        back to ManualLight so the server still starts."""
        try:
            from .lights.godox_mesh import GodoxMeshLight
            return GodoxMeshLight(lid, prof)
        except Exception as e:
            log.warning("light %s: godox_mesh backend unavailable (%s); using manual", lid, e)
            return ManualLight(lid, prof)

    def _sync_mock_light(self) -> None:
        cam = self.camera
        if cam is not None and hasattr(cam, "set_scene_light") and "key" in self.lights:
            s = self.lights["key"].state()
            cam.set_scene_light(s.intensity_pct / 100, s.cct_k)

    def stop(self) -> None:
        if getattr(self, "autopilot", None) is not None:
            self.autopilot.stop()
        with self.lock:
            if self._universe is not None:
                try: self._universe.stop()
                except Exception: pass
            for light in self.lights.values():
                close = getattr(light, "close", None)
                if close:
                    try: close()
                    except Exception: pass
            if self.camera is not None:
                try: self.camera.disconnect()
                except Exception: pass

    def switch_camera(self, backend: str) -> dict[str, Any]:
        """Runtime toggle between camera backends (gphoto2 over USB, ccapi over
        Wi-Fi, mock). Disconnects the current one, connects the new one; on
        failure reconnects the previous backend and re-raises."""
        if backend not in ("auto", "edsdk", "gphoto2", "ccapi", "mock"):
            raise ValueError("backend must be auto|edsdk|gphoto2|ccapi|mock")
        with self.lock:
            if self.state.recording:
                raise RuntimeError("stop recording before switching camera backends")
            prev = self.cfg["camera"].get("backend", "auto")
            prev_cam = self.camera
            if prev_cam is not None:
                try: prev_cam.disconnect()
                except Exception as e: log.debug("switch: disconnect %s", e)   # noqa: BLE001
            self.cfg["camera"]["backend"] = backend
            try:
                self._start_camera()
            except Exception as e:
                log.warning("camera switch to %s failed: %s; restoring %s", backend, e, prev)
                self.cfg["camera"]["backend"] = prev
                try:
                    self._start_camera()
                except Exception as e2:  # noqa: BLE001
                    log.error("could not restore %s either: %s", prev, e2)
                self.refresh_camera_state()
                self.state.broadcast({"type": "state", "state": self.get_state()})
                raise
            self.state.log("ui", "camera_backend", prev, backend)
            st = self.refresh_camera_state()
            self.state.broadcast({"type": "state", "state": self.get_state()})
            return st

    # ------------------------------------------------------------ state
    def refresh_camera_state(self) -> dict[str, Any]:
        cam = self.camera
        info = cam.info()
        settings = {k: None for k in CAMERA_KEYS} | cam.get_settings()
        self.state.camera = {"connected": info.connected, "model": info.model, "backend": info.backend,
                             "lens": info.lens, "focal_mm": info.focal_mm, "mode": info.mode,
                             "settings": settings, "capabilities": info.capabilities}
        if info.focal_mm and not self.state.context.focal_mm:
            self.state.context.focal_mm = info.focal_mm
        self.state.broadcast({"type": "camera", "camera": self.state.camera})
        return self.state.camera

    def _sync_camera_connected(self) -> None:
        """The EDSDK backend connects and loses the body from its own idle hook; mirror that into
        state.camera and the startup phase so the pill and the strip follow without a restart."""
        cam = self.camera
        if cam is None or isinstance(cam, NoCamera):
            return
        try:
            now = bool(cam.info().connected)
        except Exception:  # noqa: BLE001
            return
        if now == bool(self.state.camera.get("connected")):
            return
        try:
            self.refresh_camera_state()
        except Exception as e:  # noqa: BLE001
            log.debug("camera state refresh failed: %s", e)
            self.state.camera["connected"] = now
            self.state.broadcast({"type": "camera", "camera": self.state.camera})
        if self.state.startup.get("camera") != "connecting":
            self._startup(camera="ok" if now else "waiting")

    def _sync_lights_connected(self) -> None:
        """A mesh light reconnects (or drops) from its own retry path; mirror that into state.lights so the
        header dot and the readiness list follow without waiting for the next slider move."""
        changed = False
        for lid, light in list(self.lights.items()):
            try:
                now = bool(light.connected)
            except Exception:  # noqa: BLE001
                continue
            cached = self.state.lights.get(lid, {})
            if now != bool(cached.get("connected")):
                try:
                    self.state.lights[lid] = light.state().to_dict()
                except Exception as e:  # noqa: BLE001
                    log.debug("light %s state refresh failed: %s", lid, e)
                    self.state.lights[lid] = {**cached, "connected": now}
                changed = True
        if changed:
            self.state.broadcast({"type": "lights", "lights": self.state.lights})

    def get_state(self) -> dict[str, Any]:
        with self.lock:
            self._sync_camera_connected()
            self._sync_lights_connected()
            snap = self.state.snapshot()
            snap["pending_recipe"] = self.pending_recipe
            snap["recipe_match"] = self._match_current()
            return snap

    def _match_current(self) -> dict[str, Any] | None:
        c = self.state.context
        m = self.recipes.match(c.preset, c.scene, c.lens, c.focal_mm)
        return {"id": m["id"], "name": m["name"], "created": m["created"]} if m else None

    # ------------------------------------------------------------ frames
    def _oriented(self, fr):
        """Live-view JPEGs come out as sensor-landscape pixels with no
        orientation tag (R50 V verified). When the operator says the body is
        in portrait, rotate the frame so analysis, saved frames and the UI
        all see it upright. context.rotation picks the direction (which way
        the grip went on the L-bracket)."""
        ctx = self.state.context
        if ctx.orientation != "portrait" or fr.orientation == "portrait" or fr.width <= fr.height:
            return fr
        import io
        from PIL import Image
        from .camera.base import Frame
        im = Image.open(io.BytesIO(fr.jpeg))
        im = im.transpose(Image.Transpose.ROTATE_270 if ctx.rotation == "cw" else Image.Transpose.ROTATE_90)
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
        return Frame(jpeg=buf.getvalue(), width=im.width, height=im.height, orientation="portrait")

    def capture_frame(self, analyze: bool = True, save: bool = True, source: str = "claude") -> dict[str, Any]:
        with self.lock:
            fr = self._oriented(self.camera.capture_frame())
            ctx = self.state.context
            path = None
            if save:
                day = self.frames_dir / time.strftime("%Y-%m-%d"); day.mkdir(exist_ok=True)
                path = str(day / (time.strftime("%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.jpg"))
                with open(path, "wb") as f:
                    f.write(fr.jpeg)
            result: dict[str, Any] = {"path": path, "width": fr.width, "height": fr.height,
                                      "orientation": fr.orientation, "ts": time.time()}
            if analyze:
                scene = presets.SCENES.get(ctx.scene, {}).get("roi", "center")
                a = analysis.analyze(fr.jpeg, roi=ctx.roi, scene_roi=scene,
                                     target=presets.target_vector(ctx.preset, ctx.scene))
                result["analysis"] = a
                result["delta"] = analysis.diff((self.state.last_frame or {}).get("analysis"), a)
                result["analysis_summary"] = {"luma_median": a["luma"]["median"], "clip": a["clip"],
                                              "focus": a["focus"]["laplacian_var"],
                                              "wb_estimate_k": a["channels"]["wb_estimate_k"],
                                              "flags": a["flags"]}
                if ctx.preset == "reel" and a["orientation"] != "portrait":
                    a["flags"].append("orientation_mismatch_reel_wants_portrait")
                self.state.prev_frame = self.state.last_frame
                self.state.last_frame = {**result, "jpeg": fr.jpeg}
                self.state.camera["settings"]["orientation"] = fr.orientation
            self.state.broadcast({"type": "frame", "frame": {**result, "jpeg_b64": base64.b64encode(fr.jpeg).decode()}})
            return {**result, "jpeg": fr.jpeg}

    def hud_analysis(self) -> dict[str, Any]:
        """Light measurement for the UI's plain-language readouts: analyze the
        current frame against the context target without saving a frame, touching
        history or the capture delta."""
        with self.lock:
            fr = self._oriented(self.camera.capture_frame())
            ctx = self.state.context
            roi, scene = ctx.roi, presets.SCENES.get(ctx.scene, {}).get("roi", "center")
            target = presets.target_vector(ctx.preset, ctx.scene)
        a = analysis.analyze(fr.jpeg, roi=roi, scene_roi=scene, target=target)   # the slow part, lock released
        skin = a["roi"].get("skin") or a["roi"].get("face") or {}
        return {"ts": time.time(), "luma_median": a["luma"]["median"],
                "clip_high_pct": a["clip"]["high_pct"], "clip_low_pct": a["clip"]["low_pct"],
                "wb_estimate_k": a["channels"]["wb_estimate_k"], "focus": a["focus"]["laplacian_var"],
                "skin_median": skin.get("median"), "face_clip_high_pct": skin.get("high_pct"),
                "target_check": a.get("target_check", {})}

    def live_frame(self) -> dict[str, Any]:
        """Cheap live-view grab for the UI loop: no disk, histogram only."""
        with self.lock:
            self._sync_camera_connected()
            fr = self._oriented(self.camera.capture_frame())
            if self.state.camera.get("settings", {}).get("orientation") != fr.orientation:
                self.state.camera["settings"]["orientation"] = fr.orientation
        import io
        import numpy as np
        from PIL import Image
        img = Image.open(io.BytesIO(fr.jpeg)).convert("L").resize((240, 160))
        hist, _ = np.histogram(np.asarray(img), bins=64, range=(0, 255))
        return {"jpeg_b64": base64.b64encode(fr.jpeg).decode(), "hist": hist.astype(int).tolist(),
                "width": fr.width, "height": fr.height, "ts": time.time()}

    # ------------------------------------------------------------ camera settings
    def _throttle(self) -> None:
        while not self.state.rate_limit_ok():
            time.sleep(0.05)

    def _no_ai_while_recording(self, source: str) -> None:
        """Automation never touches the picture while a take rolls; a human turning a dial is their call."""
        if source in ("ai", "ask") and self.state.recording:
            raise PermissionError("recording: AI Auto makes no changes while a take rolls")

    def set_camera(self, changes: dict[str, Any], source: str = "claude", reason: str | None = None,
                   batch: str | None = None) -> dict[str, Any]:
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            return {}
        with self.lock:
            self._no_ai_while_recording(source)
            self._throttle()
            before = {k: self.state.camera["settings"].get(k) for k in changes}
            applied = self.camera.set_settings(changes)
            self.state.camera["settings"].update(applied)
            entry = self.state.log(source, "camera", before, applied, reason)
            if batch:
                entry["batch"] = batch           # one undo reverts every change with this batch id
            self.state.broadcast({"type": "camera", "camera": self.state.camera})
            return applied

    def set_focus(self, mode: str | None = None, drive: str | None = None,
                  af_point: list[float] | None = None, source: str = "claude", reason: str | None = None) -> dict[str, Any]:
        with self.lock:
            self._no_ai_while_recording(source)
            self._throttle()
            out: dict[str, Any] = {}
            if mode:
                out.update(self.camera.set_settings({"focus_mode": mode}))
                self.state.camera["settings"].update(out)
            if af_point:
                self.camera.set_af_point(float(af_point[0]), float(af_point[1])); out["af_point"] = af_point
            if drive:
                self.camera.focus_drive(drive); out["drive"] = drive
            self.state.log(source, "focus", None, out, reason)
            fr = self.capture_frame(analyze=True, save=False, source=source)
            out["focus_metric"] = fr["analysis"]["focus"]["laplacian_var"]
            out["focus_delta"] = fr["delta"].get("focus")
            self.state.broadcast({"type": "camera", "camera": self.state.camera})
            return out

    def set_light(self, light_id: str, intensity_pct: float | None = None, cct_k: int | None = None,
                  source: str = "claude", reason: str | None = None, batch: str | None = None) -> dict[str, Any]:
        with self.lock:
            if light_id not in self.lights:
                raise KeyError(f"unknown light {light_id!r}; have {list(self.lights)}")
            self._no_ai_while_recording(source)
            self._throttle()
            light = self.lights[light_id]
            before = self.state.lights.get(light_id, {})
            st = light.set(intensity_pct, cct_k).to_dict()
            self.state.lights[light_id] = st
            entry = self.state.log(source, f"light:{light_id}", {k: before.get(k) for k in ("intensity_pct", "cct_k")},
                                   {k: st[k] for k in ("intensity_pct", "cct_k")}, reason)
            if batch:
                entry["batch"] = batch
            self._sync_mock_light()
            self.state.broadcast({"type": "lights", "lights": self.state.lights})
            if not st["scriptable"]:
                st["note"] = f"{st['model']} is not scriptable; set it by hand to these values. Recorded for the recipe."
            return st

    def set_movie_mode(self, resolution: str, fps: int, codec: str | None = None, source: str = "claude") -> dict[str, Any]:
        with self.lock:
            modes = self.state.camera["capabilities"].get("movie_modes") or []
            if modes and not any(m["resolution"] == resolution and int(m["fps"]) == int(fps) for m in modes):
                raise ValueError(f"{resolution}@{fps} not supported by {self.state.camera['model']}; options {modes}")
            return self.set_camera({"movie_resolution": resolution, "movie_fps": int(fps)}, source)

    @staticmethod
    def _clean_tag(tag: str) -> str:
        import re
        t = re.sub(r"[^a-z0-9]+", "-", str(tag).strip().lower()).strip("-")
        return t[:40]

    def set_context(self, preset: str | None = None, scene: str | None = None, lens: str | None = None,
                    focal_mm: float | None = None, orientation: str | None = None,
                    roi: list[float] | None = None, clear_roi: bool = False, source: str = "ui",
                    rotation: str | None = None, tag: str | None = None) -> dict[str, Any]:
        with self.lock:
            c = self.state.context
            if tag is not None:
                c.tag = self._clean_tag(tag)
            if rotation:
                if rotation not in ("cw", "ccw"): raise ValueError("rotation must be cw|ccw")
                c.rotation = rotation
            if preset:
                if preset not in presets.PRESETS: raise ValueError(f"unknown preset {preset}")
                c.preset = preset
                if not orientation:   # intended orientation follows the preset unless given
                    orientation = presets.PRESETS[preset].get("orientation")
            if scene:
                if scene not in presets.SCENES: raise ValueError(f"unknown scene {scene}")
                c.scene = scene
                if hasattr(self.camera, "subject"):
                    self.camera.subject = scene
            if lens: c.lens = lens
            if focal_mm: c.focal_mm = float(focal_mm)
            if orientation:
                c.orientation = orientation
                # the mock camera has no gravity sensor: rotate it with the intent
                cam_settings = getattr(self.camera, "settings", None)
                if isinstance(cam_settings, dict) and "orientation" in cam_settings:
                    cam_settings["orientation"] = orientation
                self.state.camera["settings"]["orientation"] = orientation   # real bodies: _oriented() rotates
            if roi is not None: c.roi = [float(v) for v in roi]
            if clear_roi: c.roi = None
            self.state.log(source, "context", None, c.__dict__.copy())
            self.state.save_context(self.cfg["paths"].get("context"))
            self.state.broadcast({"type": "context", "context": self.get_state()["context"],
                                  "recipe_match": self._match_current()})
            # the UI's orientation banner reads camera.settings.orientation: keep it current
            self.state.broadcast({"type": "camera", "camera": self.state.camera})
            return c.__dict__ | {"recipe_match": self._match_current(),
                                 "target": presets.target_vector(c.preset, c.scene)}

    # ------------------------------------------------------------ claude loop
    def claude_request(self, request: str | None) -> dict[str, Any]:
        ap = self.autopilot
        if ap is not None and ap.configured():
            # AI Auto: the server runs the loop itself
            if request == "stop":
                ap.stop()
                if not ap.running:
                    with self.lock:
                        self.state.claude.request, self.state.claude.status = None, "stopped"
                        self.state.broadcast({"type": "claude", "claude": self.get_state()["claude"]})
            elif request in ("optimize", "step"):
                try:
                    ap.start(request)
                except RuntimeError as e:
                    raise ValueError(f"AI Auto not started: {e}") from e
            return self.get_state()["claude"]
        with self.lock:
            cl = self.state.claude
            cl.mode = "mcp"
            if request == "stop":
                cl.request, cl.status = None, "stopped"
            elif request in ("optimize", "step"):
                cl.request, cl.status, cl.iteration = request, "requested", 0
                cl.messages.append({"ts": time.time(), "role": "ui", "text": f"{request} requested for "
                                    f"{self.state.context.preset}/{self.state.context.scene} — no API key in "
                                    f"config.toml, handing it to the MCP-connected Claude session"})
            else:
                cl.request = None
            self.state.broadcast({"type": "claude", "claude": self.get_state()["claude"]})
            return self.get_state()["claude"]

    def claude_message(self, text: str) -> dict[str, Any]:
        """User typed into the UI chat line. Becomes a 'message' request the
        MCP-connected Claude session picks up via get_state / wait_for_request."""
        with self.lock:
            cl = self.state.claude
            cl.messages.append({"ts": time.time(), "role": "user", "text": text})
            ai = self.autopilot is not None and self.autopilot.configured()
            if ai:
                # AI Auto has no chat channel; say so instead of showing a request nobody will answer
                cl.messages.append({"ts": time.time(), "role": "ui",
                                    "text": "AI Auto does not read chat yet: press AI Auto to run it, or attach an MCP Claude session"})
            else:
                if cl.request not in ("optimize", "step"):
                    cl.request = "message"
                if cl.status in ("idle", "converged", "stopped"):
                    cl.status = "requested"
            self.state.broadcast({"type": "claude", "claude": self.get_state()["claude"]})
            return self.get_state()["claude"]

    def wait_for_request(self, timeout_s: float = 30.0) -> dict[str, Any]:
        """Block (outside the lock) until the UI posts a request or a user
        message, or until timeout. Returns the claude state plus any user
        messages newer than the last Claude report."""
        deadline = time.time() + max(0.0, min(timeout_s, 120.0))
        while True:
            with self.lock:
                cl = self.state.claude
                last_claude = max((m["ts"] for m in cl.messages if m["role"] == "claude"), default=0.0)
                unread = [m for m in cl.messages if m["role"] == "user" and m["ts"] > last_claude]
                if cl.request or unread or time.time() >= deadline:
                    return {"request": cl.request, "status": cl.status, "unread_user_messages": unread,
                            "timed_out": not (cl.request or unread), "context": self.get_state()["context"]}
            time.sleep(0.25)

    def claude_report(self, message: str, status: str | None = None, iteration: int | None = None) -> dict[str, Any]:
        with self.lock:
            cl = self.state.claude
            if iteration is not None: cl.iteration = iteration
            if cl.request == "message":
                cl.request = None          # a reply answers the message
                if not status: cl.status = "idle"
            if status:
                cl.status = status
                if status in ("converged", "stopped", "idle"):
                    cl.request = None
            cl.messages.append({"ts": time.time(), "role": "claude", "text": message,
                                "iteration": cl.iteration, "status": cl.status})
            self.state.broadcast({"type": "claude", "claude": self.get_state()["claude"]})
            return self.get_state()["claude"]

    def undo(self, source: str = "ui") -> dict[str, Any] | None:
        """Revert the newest camera/light change. Changes that carry a batch id (an applied Ask
        Claude proposal) are reverted together: one undo puts the whole proposal back. The
        reverting entries get batch "undo:<id>", so a second undo re-applies the batch as one."""
        with self.lock:
            hist = list(self.state.history)
            idx = next((i for i in range(len(hist) - 1, -1, -1)
                        if hist[i]["before"] and (hist[i]["kind"] == "camera" or hist[i]["kind"].startswith("light:"))), None)
            if idx is None:
                return None
            batch = hist[idx].get("batch")
            if not batch:
                e = hist[idx]
                if e["kind"] == "camera":
                    return self.set_camera(e["before"], source, "undo")
                return self.set_light(e["kind"].split(":")[1], **e["before"], source=source, reason="undo")
            group = []                       # the newest contiguous run with this batch id
            for e in reversed(hist[:idx + 1]):
                revertible = e["before"] and (e["kind"] == "camera" or e["kind"].startswith("light:"))
                if not revertible:
                    continue
                if e.get("batch") != batch:
                    break
                group.insert(0, e)
            back = batch[5:] if batch.startswith("undo:") else f"undo:{batch}"
            out: dict[str, Any] = {"undone": batch[5:] if batch.startswith("undo:") else batch,
                                   "redo": batch.startswith("undo:"), "camera": {}, "lights": {}}
            for e in reversed(group):
                if e["kind"] == "camera":
                    out["camera"].update(self.set_camera(e["before"], source, "undo", batch=back))
                else:
                    lid = e["kind"].split(":")[1]
                    st = self.set_light(lid, **e["before"], source=source, reason="undo", batch=back)
                    out["lights"][lid] = {"intensity_pct": st["intensity_pct"], "cct_k": st["cct_k"]}
            return out

    # ------------------------------------------------------------ record / clips
    def arm(self) -> float:
        return self.state.arm()

    def record(self, action: str, source: str = "ui") -> dict[str, Any]:
        with self.lock:
            if action == "start":
                if not self.state.armed:
                    raise PermissionError("not armed: click ARM in the UI (60 s window) before recording")
                if self.autopilot is not None:
                    self.autopilot.stop()          # nothing changes on the camera while a take rolls
                res = self.camera.record("start")
                self.state.recording, self.state.rec_started = True, time.time()
                self.state.disarm()
            elif action == "stop":
                res = self.camera.record("stop")
                self.state.recording, self.state.rec_started = False, None
                if res.get("clip"): self.state.last_clip = res["clip"]
                if res.get("clip") and self.AUTO_PULL:
                    self._start_auto_pull(res["clip"])
            else:
                raise ValueError("action must be start|stop")
            self.state.log(source, "record", None, action)
            self.state.broadcast({"type": "record", "recording": self.state.recording, "last_clip": self.state.last_clip})
            return {**res, "recording": self.state.recording}

    # ------------------------------------------------------------ takes (auto-pull after Stop)
    def _start_auto_pull(self, clip: str) -> None:
        """Called under the lock right after Stop: snapshot the settings of this take, then pull on a thread."""
        st = self.state
        meta = {"stopped": time.time(), "camera": dict(st.camera.get("settings") or {}),
                "lights": {k: dict(v) if isinstance(v, dict) else v for k, v in st.lights.items()},
                "tag": st.context.tag, "preset": st.context.preset, "scene": st.context.scene,
                "lens": st.context.lens, "orientation": st.context.orientation}
        threading.Thread(target=self._auto_pull, args=(clip, meta), name="camlab-take", daemon=True).start()

    def _auto_pull(self, clip: str, meta: dict[str, Any]) -> None:
        err = None
        for attempt in (1, 2):
            try:
                self._pull_take(clip, meta)
                return
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                log.warning("auto-pull of %s failed (try %d): %s", clip, attempt, err)
                self.state.broadcast({"type": "take", "error": err})
                if attempt == 1:
                    time.sleep(self.RETRY_DELAY_S)

    def _pull_take(self, clip: str, meta: dict[str, Any]) -> None:
        import json as _json
        deadline = time.time() + 120
        while True:
            with self.lock:
                if not self.state.recording:       # never pull while a take is rolling
                    old = self.clips.get(clip)
                    if old is not None and (old.get("pulled") or 0) < meta.get("stopped", 0):
                        # the camera reused this name (counter reset after a restart)
                        again = self.clips.pull_reused(self.camera, clip, tag=meta["tag"] or None)
                        if again is None:
                            self.state.broadcast({"type": "take", "error":
                                f"The camera reused the name {clip}; the earlier copy is kept. Pull by hand from More controls."})
                            return
                        pulled = [again]
                    else:
                        pulled = self.clips.pull(self.camera, None, tag=meta["tag"] or None)
                    break
            if time.time() > deadline:
                raise RuntimeError("still recording; take not pulled yet")
            time.sleep(0.5)
        self.state.broadcast({"type": "clips", "pulled": pulled, "clips": self.clips.index[-20:]})
        if not pulled:
            if self.clips.get(clip) is None:
                raise FileNotFoundError(f"{clip} was not found on the card")
            return
        for entry in pulled:
            take = self.take_from_entry(entry)
            side = {"label": take["label"], "name": take["name"], "ts": take["ts"], "tag": meta["tag"],
                    "preset": meta["preset"], "scene": meta["scene"], "lens": meta["lens"],
                    "orientation": meta["orientation"],
                    "settings": {"camera": meta["camera"], "lights": meta["lights"]},
                    "verdict": None, "notes": ""}
            try:
                Path(str(take["path"]) + ".json").write_text(_json.dumps(side, indent=2, default=str))
            except OSError as e:
                log.warning("sidecar for %s not written: %s", take["label"], e)
            with self.lock:
                self.state.takes.append(take)
                del self.state.takes[:-20]
                self.state.last_take = take
                self.state.last_clip = take["label"]
            self.state.broadcast({"type": "take", "take": take})

    @staticmethod
    def take_from_entry(e: dict[str, Any]) -> dict[str, Any]:
        path = str(e.get("path") or "")
        return {"label": e.get("label") or os.path.basename(path) or e.get("name"), "name": e.get("name"),
                "path": path, "duration_s": e.get("duration_s"), "size": e.get("size"),
                "seq": e.get("seq"), "tag": e.get("tag") or "", "ts": e.get("pulled")}

    def takes_today(self) -> list[dict[str, Any]]:
        day = time.strftime("%Y-%m-%d")
        with self.lock:
            return [self.take_from_entry(e) for e in self.clips.index
                    if e.get("pulled") and time.strftime("%Y-%m-%d", time.localtime(e["pulled"])) == day
                    and e.get("path") and os.path.exists(e["path"])]

    def clip_file(self, name: str) -> Path | None:
        """Resolve a label/card name/file name to a video file inside the clips folder, else None."""
        root = Path(self.cfg["paths"]["clips"]).resolve()
        cand = None
        entry = self.clips.get(name)
        if entry and entry.get("path"):
            cand = Path(entry["path"])
        elif name and "\\" not in name:
            cand = root / name
        if cand is None:
            return None
        try:
            p = cand.resolve()
            p.relative_to(root)
        except (OSError, ValueError):
            return None
        if not p.is_file() or p.suffix.lower() not in (".mp4", ".mov", ".m4v"):
            return None
        return p

    def pull_clips(self, since: float | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if self.state.recording:
                raise RuntimeError("stop recording before pulling clips")
            pulled = self.clips.pull(self.camera, since, tag=self.state.context.tag or None)
            if pulled: self.state.last_clip = pulled[-1].get("label") or pulled[-1]["name"]
            self.state.broadcast({"type": "clips", "pulled": pulled, "clips": self.clips.index[-20:]})
            return pulled

    def export_clip(self, clip: str, chooser=None) -> dict[str, Any]:
        """Copy a pulled clip to a place the user picks. `chooser(default_dir, filename)` returns the
        chosen path or None (the app passes its native save dialog); without one, report where the
        local copy already is. The chosen folder is remembered in <home>/export.json."""
        import json as _json
        import shutil
        entry = self.clips.get(clip)
        if entry is None:
            self.pull_clips()
            entry = self.clips.get(clip)
        if entry is None:
            raise KeyError(f"clip {clip!r} is not on the card or on this Mac")
        src = Path(entry["path"])
        if chooser is None:
            return {"path": str(src), "dialog": False, "name": entry["name"]}
        mem = Path(self.cfg["paths"].get("home") or self.frames_dir.parent) / "export.json"
        try:
            default_dir = _json.loads(mem.read_text()).get("dir") or str(Path.home() / "Movies")
        except (OSError, ValueError):
            default_dir = str(Path.home() / "Movies")
        chosen = chooser(default_dir, entry["name"])
        if isinstance(chosen, (list, tuple)):
            chosen = chosen[0] if chosen else None
        if not chosen:
            return {"path": None, "dialog": True, "name": entry["name"]}
        dest = Path(str(chosen)).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        try:
            mem.write_text(_json.dumps({"dir": str(dest.parent)}))
        except OSError:
            pass
        self.state.log("ui", "export", None, {"clip": entry["name"], "to": str(dest)})
        return {"path": str(dest), "dialog": True, "name": entry["name"]}

    def extract_frames(self, clip_id: str, timestamps: list[float]) -> list[dict[str, Any]]:
        ctx = self.state.context
        scene = presets.SCENES.get(ctx.scene, {}).get("roi", "center")
        out = self.clips.extract_frames(clip_id, timestamps, self.frames_dir / "clips")
        for r in out:
            with open(r["path"], "rb") as f:
                jpeg = f.read()
            r["analysis"] = analysis.analyze(jpeg, roi=ctx.roi, scene_roi=scene,
                                             target=presets.target_vector(ctx.preset, ctx.scene))
            r["jpeg"] = jpeg
        return out

    # ------------------------------------------------------------ recipes
    def save_recipe(self, name: str, notes: str | None = None, source: str = "claude") -> dict[str, Any]:
        with self.lock:
            snap = self.get_state()
            if source == "ui":
                r = self.recipes.add(snap, name, notes)
                self.pending_recipe = None
                self.state.broadcast({"type": "recipes", "recipes": self.recipes.list()})
                return {"status": "saved", "recipe": r}
            self.pending_recipe = {"name": name, "notes": notes, "snapshot": snap, "ts": time.time()}
            self.state.broadcast({"type": "pending_recipe", "pending": {"name": name, "notes": notes}})
            return {"status": "pending_confirm", "name": name,
                    "note": "User must click Confirm in the UI (or POST /api/recipes/confirm)."}

    def confirm_recipe(self, accept: bool = True) -> dict[str, Any]:
        with self.lock:
            p, self.pending_recipe = self.pending_recipe, None
            if not p:
                return {"status": "nothing_pending"}
            if not accept:
                self.state.broadcast({"type": "pending_recipe", "pending": None})
                return {"status": "rejected"}
            r = self.recipes.add(p["snapshot"], p["name"], p["notes"])
            self.state.broadcast({"type": "recipes", "recipes": self.recipes.list()})
            self.state.broadcast({"type": "pending_recipe", "pending": None})
            return {"status": "saved", "recipe": r}

    def load_recipe(self, rid: str | None = None, match: dict[str, Any] | None = None, source: str = "ui") -> dict[str, Any]:
        with self.lock:
            r = self.recipes.get(rid) if rid else self.recipes.match(**(match or {}))
            if not r:
                raise KeyError("no matching recipe")
            self.set_context(r["preset"], r["scene"], r.get("lens"), r.get("focal_mm"), r.get("orientation"),
                             roi=r.get("roi"), source=source)
            cam = {k: v for k, v in r["camera"]["settings"].items() if k in CAMERA_KEYS and v is not None}
            applied = self.set_camera(cam, source, f"load recipe {r['name']}")
            lights = {}
            for lid, lv in r.get("lights", {}).items():
                if lid in self.lights:
                    lights[lid] = self.set_light(lid, lv.get("intensity_pct"), lv.get("cct_k"), source, f"load recipe {r['name']}")
            return {"recipe": r, "camera_applied": applied, "lights_applied": lights}
