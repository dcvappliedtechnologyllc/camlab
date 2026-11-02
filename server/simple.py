"""One-screen control core: deterministic, no AI.

Five readiness checks the owner can act on (camera, dial, lights, subject,
upright), two modes (lights: the tool owns exposure with the body on M and the
lights doing the work; camera: the body's own auto exposure, the tool only locks
white balance and moves the lights), four nudges (brighter, dimmer, warmer,
cooler), a short brightness loop ("level") and the last good setup per shot
(keep/load). Every change goes through Controller.set_camera / set_light with
source "simple", so the rate limit, clamps and history apply.

Written after 2026-10-25: the AI loop failed all day because the top dial sat on
Scene Intelligent Auto (the body overrode every exposure write) and the lights
were off. Hence: verify every camera write by reading it back, and say so in one
plain sentence when the body takes it back.
"""
from __future__ import annotations

import base64
import io
import logging
import math
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from . import presets
from .camera.base import CameraError

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

log = logging.getLogger("camlab.simple")

MODES = ("lights", "camera")
MANUAL_MODES = {"m", "manual", "manual exposure"}
PERSON_PRESETS = {"reel", "talking_head"}
WARMTH_RANGE = (2800, 6500)
EXPOSURE_KEYS = ("iso", "shutter", "aperture")
VERIFY_KEYS = ("iso", "shutter", "aperture", "kelvin")
LEVEL_ROUNDS = 6
KELVIN_STEP = 300
LIGHT_STEP = 10.0

TEXT = {
    "camera_ok": "Camera is connected.",
    "camera_looking": "Looking for the camera: switch it on and check the USB cable.",
    "camera_missing": "Switch the camera on and check the USB cable.",
    "dial_ok": "The top dial is on M.",
    "dial_bad": "Turn the top dial on the camera to M.",
    "dial_unknown": "Make sure the top dial on the camera is on M.",
    "dial_camera_mode": "Camera is handling brightness itself.",
    "lights_ok": "Lights are on and answering.",
    "lights_bad": "Switch the lights on, or shoot without them: tap Camera mode.",
    "lights_connecting": "Still connecting to the lights: give it a few seconds.",
    "lights_camera_mode": "Camera mode: the lights are optional.",
    "face_ok": "Face found and steady.",
    "no_face": "I can't see a face: step into the middle of the frame.",
    "face_moving": "Hold still for a moment so I can see the face clearly.",
    "subject_other": "Put the subject in the middle.",
    "upright_ok": "Camera is the right way up.",
    "upright_bad": "Turn the camera upright on its bracket.",
    "override": "The camera is overriding me: turn the top dial to M.",
    "no_light_camera_mode": "No light I can move: in Camera mode the camera sets the brightness itself.",
    "level_ok": "Brightness is right.",
    "level_cap": "Nearly there: press Level again.",
    "level_no_light": "No light I can move: in Camera mode the camera sets the brightness itself.",
    "too_dark_limit": "I can't make it any brighter: move the key light closer.",
    "too_bright_limit": "I can't make it any darker: move the key light further away.",
    "no_last_good": "No saved setup for this shot yet: keep a good take first.",
}


# ====================================================================== faces
_VISION: dict[str, Any] = {}
_VISION_LOCK = threading.Lock()


def _vision_classes():
    """Vision.framework through pyobjc's runtime bridge (pyobjc-framework-Vision is not
    installed; loadBundle + lookUpClass is enough for the face request). None if unavailable."""
    with _VISION_LOCK:
        if "ok" not in _VISION:
            try:
                import objc
                from Foundation import NSData
                objc.loadBundle("Vision", {}, bundle_path="/System/Library/Frameworks/Vision.framework")
                _VISION.update(ok=True, objc=objc, NSData=NSData,
                               Req=objc.lookUpClass("VNDetectFaceRectanglesRequest"),
                               Handler=objc.lookUpClass("VNImageRequestHandler"))
            except Exception as e:  # noqa: BLE001
                log.info("macOS Vision unavailable (%s); faces from the skin mask", e)
                _VISION["ok"] = False
        return _VISION if _VISION["ok"] else None


def vision_available() -> bool:
    return _vision_classes() is not None


def _small_jpeg(jpeg: bytes, max_side: int = 640) -> tuple[bytes, Any]:
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
        b = io.BytesIO(); im.save(b, "JPEG", quality=85)
        return b.getvalue(), im
    return jpeg, im


def faces_vision(jpeg: bytes) -> list[dict[str, Any]] | None:
    """[{bbox [x,y,w,h] top-left 0-1, roll_deg, confidence}] or None when Vision is unavailable."""
    v = _vision_classes()
    if v is None:
        return None
    data, _ = _small_jpeg(jpeg)
    try:
        with v["objc"].autorelease_pool():
            nd = v["NSData"].dataWithBytes_length_(data, len(data))
            req = v["Req"].alloc().init()
            handler = v["Handler"].alloc().initWithData_options_(nd, {})
            handler.performRequests_error_([req], None)
            out = []
            for o in (req.results() or []):
                bb = o.boundingBox()
                x, y, w, h = bb.origin.x, bb.origin.y, bb.size.width, bb.size.height
                roll = o.roll() if hasattr(o, "roll") else None
                out.append({"bbox": [round(x, 4), round(1 - y - h, 4), round(w, 4), round(h, 4)],
                            "roll_deg": None if roll is None else round(math.degrees(float(roll)), 1),
                            "confidence": round(float(o.confidence()), 3)})
            out.sort(key=lambda f: -f["bbox"][2] * f["bbox"][3])
            return out
    except Exception as e:  # noqa: BLE001
        log.debug("vision face request failed: %s", e)
        return None


def _blobs(mask) -> list[tuple[int, int, int, int, int]]:
    """4-connected components of a small boolean grid: [(area, x0, y0, x1, y1)]."""
    h, w = mask.shape
    seen = mask.copy()
    out = []
    for y0 in range(h):
        for x0 in range(w):
            if not seen[y0, x0]:
                continue
            seen[y0, x0] = False
            stack, area = [(y0, x0)], 0
            bx0 = bx1 = x0; by0 = by1 = y0
            while stack:
                y, x = stack.pop(); area += 1
                bx0, bx1, by0, by1 = min(bx0, x), max(bx1, x), min(by0, y), max(by1, y)
                for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                    if 0 <= ny < h and 0 <= nx < w and seen[ny, nx]:
                        seen[ny, nx] = False
                        stack.append((ny, nx))
            out.append((area, bx0, by0, bx1, by1))
    return out


def faces_skin(jpeg: bytes) -> list[dict[str, Any]]:
    """Fallback without Vision: a face-shaped blob of skin-toned pixels (compact, roughly
    oval, not a band across the frame like a wooden bench or a sunlit wall)."""
    import numpy as np
    from . import analysis
    _, im = _small_jpeg(jpeg, 96)
    rgb8 = np.asarray(im)
    rgb = rgb8.astype(np.float32) + 1.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    # the YCbCr mask alone also takes grey-beige wood and warm walls; real skin is clearly redder than green
    m = analysis._skin_mask(rgb8) & ((r - g) / r > 0.15) & ((r - b) / r > 0.25)
    h, w = m.shape
    best = None
    for area, x0, y0, x1, y1 in _blobs(m):
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        frac = area / (h * w)
        if not (0.005 <= frac <= 0.35) or area / (bw * bh) < 0.55 or not (0.7 <= bh / bw <= 3.5):
            continue
        if bw > 0.6 * w or (x0 == 0 and x1 == w - 1):
            continue
        if best is None or area > best[0]:
            best = (area, [round(x0 / w, 4), round(y0 / h, 4), round(bw / w, 4), round(bh / h, 4)])
    return [{"bbox": best[1], "roll_deg": None, "confidence": None}] if best else []


# ====================================================================== core
def _num(v: Any) -> float | None:
    try:
        s = str(v).replace("f/", "")
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b)
        return float(s)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _pct(v: float) -> str:
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


class Override(Exception):
    """The body took a camera write back (dial on an auto mode)."""


class Simple:
    def __init__(self, ctl: "Controller"):
        self.ctl = ctl
        self.cfg = ctl.cfg.get("simple", {})
        self.verify_s = float(self.cfg.get("verify_s", 1.0))     # re-read a camera write after this long
        self.settle_s = float(self.cfg.get("settle_s", 0.8))     # let a light / ISO change show in live view
        self.window_s = float(self.cfg.get("face_window_s", 5.0))
        self.iso_min = float(self.cfg.get("iso_min", 100))
        self.iso_max = float(self.cfg.get("iso_max", 6400))
        self.faces: deque[dict[str, Any]] = deque(maxlen=12)
        self.override = False
        self.manual_write: bool | None = None          # last set_exposure_mode result
        self._last_sig: Any = None
        self._busy = threading.Lock()

    # ---------------------------------------------------------------- state
    @property
    def st(self) -> dict[str, Any]:
        return self.ctl.state.simple

    @property
    def mode(self) -> str:
        return self.st.get("mode", "lights")

    def _save(self) -> None:
        self.ctl.state.save_context(self.ctl.cfg["paths"].get("context"))

    def _sync(self) -> dict[str, Any]:
        """brightness = key light %, warmth = camera kelvin (or the key's colour)."""
        key = self._key_id()
        if key and key in self.ctl.state.lights:
            self.st["brightness"] = int(round(float(self.ctl.state.lights[key].get("intensity_pct") or 0)))
        k = _num(self.ctl.state.camera.get("settings", {}).get("kelvin"))
        if k is None and key and key in self.ctl.state.lights:
            k = _num(self.ctl.state.lights[key].get("cct_k"))
        if k:
            self.st["warmth"] = int(max(WARMTH_RANGE[0], min(WARMTH_RANGE[1], k)))
        return dict(self.st)

    def _key_id(self) -> str | None:
        if "key" in self.ctl.lights:
            return "key"
        return next(iter(self.ctl.lights), None)

    @staticmethod
    def _answering(light) -> bool:
        try:
            return bool(light.connected) and bool(light.scriptable)
        except Exception:  # noqa: BLE001
            return False

    def _movable_key(self):
        kid = self._key_id()
        light = self.ctl.lights.get(kid) if kid else None
        return (kid, light) if light is not None and self._answering(light) else (None, None)

    def _person(self) -> bool:
        c = self.ctl.state.context
        return c.preset in PERSON_PRESETS or str(c.scene).startswith("person")

    def _camera_connected(self) -> bool:
        from .controller import NoCamera
        cam = self.ctl.camera
        if cam is None or isinstance(cam, NoCamera):
            return False
        try:
            return bool(cam.info().connected)
        except Exception:  # noqa: BLE001
            return False

    def _body_mode(self) -> str | None:
        try:
            return self.ctl.camera.info().mode
        except Exception:  # noqa: BLE001
            return self.ctl.state.camera.get("mode")

    # ---------------------------------------------------------------- faces
    def _detector(self) -> str:
        want = str(self.cfg.get("face_detector", "auto")).lower()
        if want in ("vision", "skin"):
            return want
        # the mock's synthetic face is an ellipse Vision rightly ignores; real bodies get Vision
        if getattr(self.ctl.camera, "name", "") == "mock":
            return "skin"
        return "vision" if vision_available() else "skin"

    def observe(self, jpeg: bytes, now: float | None = None) -> dict[str, Any]:
        det = self._detector()
        found = faces_vision(jpeg) if det == "vision" else None
        if found is None:
            det, found = "skin", faces_skin(jpeg)
        sample = {"ts": time.time() if now is None else now, "detector": det,
                  "face": found[0] if found else None, "count": len(found)}
        self.faces.append(sample)
        return sample

    def _subject(self, now: float) -> tuple[bool, str, dict[str, Any]]:
        if not self._person():
            return True, TEXT["subject_other"], {"person": False}
        recent = [s for s in self.faces if now - s["ts"] <= self.window_s]
        detail: dict[str, Any] = {"person": True, "samples": len(recent),
                                  "detector": recent[-1]["detector"] if recent else self._detector(),
                                  "face": recent[-1]["face"] if recent else None}
        if not recent or recent[-1]["face"] is None:
            return False, TEXT["no_face"], detail
        run = []                                # the latest unbroken run of samples with a face
        for s in reversed(recent):
            if s["face"] is None:
                break
            run.append(s)
        run.reverse()
        span = run[-1]["ts"] - run[0]["ts"]
        cx = [f["face"]["bbox"][0] + f["face"]["bbox"][2] / 2 for f in run]
        cy = [f["face"]["bbox"][1] + f["face"]["bbox"][3] / 2 for f in run]
        moved = max(max(cx) - min(cx), max(cy) - min(cy))
        detail.update(steady_s=round(span, 1), moved=round(moved, 3))
        if span < 1.5 or moved > 0.10:
            return False, TEXT["face_moving"], detail
        return True, TEXT["face_ok"], detail

    # ---------------------------------------------------------------- readiness
    def ready(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        mode = self.mode
        items = []

        def item(i, ok, text, detail=None):
            items.append({"id": i, "ok": bool(ok), "text": text, "detail": detail or {}})

        cam_ok = self._camera_connected()
        looking = self.ctl.state.startup.get("camera") == "looking"
        item("camera", cam_ok, TEXT["camera_ok"] if cam_ok else TEXT["camera_looking" if looking else "camera_missing"],
             {"model": self.ctl.state.camera.get("model"), "backend": self.ctl.state.camera.get("backend")})

        body_mode = self._body_mode() if cam_ok else None
        dd = {"mode": body_mode, "override": self.override, "manual_write": self.manual_write}
        if mode == "camera":
            item("dial", True, TEXT["dial_camera_mode"], dd)
        elif self.override:
            item("dial", False, TEXT["dial_bad"], dd)
        elif body_mode is None:
            # the backend does not report the dial (gphoto2/ccapi, or no body): cannot prove it, do not block
            item("dial", cam_ok, TEXT["dial_unknown"] if cam_ok else TEXT["dial_bad"], dd | {"known": False})
        else:
            manual = str(body_mode).strip().lower() in MANUAL_MODES
            item("dial", manual, TEXT["dial_ok"] if manual else TEXT["dial_bad"], dd)

        ld = {lid: {"model": l.model, "connected": bool(getattr(l, "connected", False)),
                    "scriptable": bool(getattr(l, "scriptable", False))} for lid, l in self.ctl.lights.items()}
        wanted = list(self.ctl.cfg.get("lights", {}))
        missing = [lid for lid in wanted if lid not in self.ctl.lights]
        if mode == "camera":
            item("lights", True, TEXT["lights_camera_mode"], {"lights": ld})
        elif self.ctl.state.startup.get("lights") == "connecting":
            item("lights", False, TEXT["lights_connecting"], {"lights": ld, "missing": missing})
        else:
            ok = bool(self.ctl.lights) and not missing and all(self._answering(l) for l in self.ctl.lights.values())
            item("lights", ok, TEXT["lights_ok"] if ok else TEXT["lights_bad"], {"lights": ld, "missing": missing})

        ok, text, sd = self._subject(now)
        item("subject", ok, text, sd)

        c = self.ctl.state.context
        want = presets.PRESETS.get(c.preset, {}).get("orientation", "landscape")
        frame = self.ctl.state.camera.get("settings", {}).get("orientation") or c.orientation
        recent = [s for s in self.faces if now - s["ts"] <= self.window_s and s["face"]]
        roll = recent[-1]["face"].get("roll_deg") if recent else None
        sideways = roll is not None and 45 < abs(roll) < 135
        up = frame == want and not sideways
        item("upright", up, TEXT["upright_ok"] if up else TEXT["upright_bad"],
             {"want": want, "frame": frame, "face_roll_deg": roll})

        lg = self._last_good()
        return {"items": items, "all_ok": all(i["ok"] for i in items), "lights_mode": mode,
                "last_good": {"exists": True, "id": lg["id"], "name": lg["name"], "clip": lg.get("clip"),
                              "created": lg["created"]} if lg else {"exists": False},
                "simple": self._sync()}

    def publish(self, now: float | None = None, force: bool = False) -> dict[str, Any]:
        r = self.ready(now)
        sig = ([(i["id"], i["ok"], i["text"]) for i in r["items"]], r["lights_mode"], r["simple"],
               r["last_good"].get("id"))
        if force or sig != self._last_sig:
            self._last_sig = sig
            self.ctl.state.broadcast({"type": "ready", **r})
        return r

    def tick(self, jpeg: bytes | None = None, now: float | None = None) -> dict[str, Any]:
        """Live loop, every ~2 s: one face look on the current frame, then readiness (broadcast on change)."""
        if jpeg is None:
            try:
                jpeg = base64.b64decode(self.ctl.live_frame()["jpeg_b64"])
            except Exception as e:  # noqa: BLE001
                log.debug("ready tick: no frame (%s)", e)
        if jpeg:
            try:
                self.observe(jpeg, now)
            except Exception as e:  # noqa: BLE001
                log.debug("face look failed: %s", e)
        return self.publish(now)

    # ---------------------------------------------------------------- camera writes
    def _refuse_camera_while_recording(self) -> None:
        if self.ctl.state.recording:
            raise PermissionError("recording: stop the take before changing the camera")

    def _write_camera(self, changes: dict[str, Any], reason: str) -> dict[str, Any]:
        """set_camera, then re-read the body after verify_s; a value that changed back = Override."""
        self._refuse_camera_while_recording()
        applied = self.ctl.set_camera(changes, "simple", reason)
        if self.verify_s:
            time.sleep(self.verify_s)
        with self.ctl.lock:
            now = self.ctl.camera.get_settings()
        back = {k: now.get(k) for k in VERIFY_KEYS if applied.get(k) is not None and k in now}
        self.ctl.state.camera["settings"].update({k: v for k, v in back.items() if v is not None})
        reverted = {k: {"wrote": applied[k], "reads": v} for k, v in back.items() if str(v) != str(applied[k])}
        if reverted:
            self.override = True
            self.ctl.state.log("simple", "override", applied, back, "camera took the change back")
            raise Override(reverted)
        if any(k in applied for k in EXPOSURE_KEYS):
            self.override = False
        return applied

    def _choices(self, key: str) -> list[str]:
        caps = self.ctl.state.camera.get("capabilities", {}) or {}
        return [c for c in (caps.get(key) or []) if _num(c) is not None]

    def _nearest(self, key: str, want: float) -> str | None:
        ch = self._choices(key)
        if not ch:
            return None
        return min(ch, key=lambda c: abs(math.log(_num(c)) - math.log(want)))

    def _iso_step(self, direction: int, factor: float | None = None) -> str | None:
        """Next ISO up/down (or the one nearest iso*factor, at least one step), inside iso_min..iso_max."""
        ch = [c for c in self._choices("iso") if self.iso_min <= _num(c) <= self.iso_max]
        ch.sort(key=_num)
        if not ch:
            return None
        with self.ctl.lock:
            cur = _num(self.ctl.camera.get_settings().get("iso"))
        if cur is None:
            return None
        idx = min(range(len(ch)), key=lambda i: abs(math.log(_num(ch[i])) - math.log(cur)))
        step = 1
        if factor:
            step = max(1, min(6, round(abs(math.log2(factor)) * 3)))   # ISO lists are in 1/3 stops
        j = idx + direction * step
        j = max(0, min(len(ch) - 1, j))
        if j == idx or (direction > 0 and _num(ch[j]) <= cur) or (direction < 0 and _num(ch[j]) >= cur):
            return None
        return ch[j]

    def _aperture_for_shot(self) -> str | None:
        c = self.ctl.state.context
        r = self._last_good() or self.ctl.recipes.match(c.preset, c.scene, c.lens, c.focal_mm)
        if r and r.get("camera", {}).get("settings", {}).get("aperture"):
            return r["camera"]["settings"]["aperture"]
        t = presets.target_vector(c.preset, c.scene)
        if t.get("aperture"):
            return t["aperture"][0]
        return {"shallow": "f/4", "medium": "f/5.6", "deep": "f/8"}.get(t.get("dof"), "f/5.6")

    # ---------------------------------------------------------------- modes
    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}")
        self._refuse_camera_while_recording()
        self.st["mode"] = mode
        self._save()
        notes: list[str] = []
        out: dict[str, Any] = {"mode": mode, "camera": {}, "lights": {}, "manual": None}
        if not self._camera_connected():
            notes.append("camera not connected yet: settings follow when it is")
        elif mode == "camera":
            self.override = False
            try:
                out["camera"] = self._write_camera({"kelvin": self.st.get("warmth", 5600)}, "camera mode: lock colour")
            except Override:
                notes.append("white balance did not hold")
            except CameraError as e:
                notes.append(f"white balance: {e}")
        else:
            out["manual"] = self._enter_manual()
            changes: dict[str, Any] = {"kelvin": self.st.get("warmth", 5600)}
            fps = _num(self.ctl.state.camera.get("settings", {}).get("movie_fps")) or 30.0
            sh = self._nearest("shutter", 1.0 / (2 * fps)) or f"1/{int(2 * fps)}"
            changes["shutter"] = sh
            ap = self._aperture_for_shot()
            if ap:
                changes["aperture"] = self._nearest("aperture", _num(ap)) or ap
            with self.ctl.lock:
                iso = self.ctl.camera.get_settings().get("iso")
            if _num(iso) is None:          # Auto ISO would keep auto exposure alive even on M
                lg = self._last_good()
                changes["iso"] = (lg or {}).get("camera", {}).get("settings", {}).get("iso") or "400"
            self.override = False
            try:
                out["camera"] = self._write_camera(changes, "lights mode: manual exposure")
            except Override as e:
                notes.append(f"the camera took back {list(e.args[0])}")
            except CameraError as e:
                notes.append(f"camera: {e}")
            for lid, light in self.ctl.lights.items():
                if self._answering(light):
                    out["lights"][lid] = self.ctl.set_light(lid, None, self.st.get("warmth", 5600), "simple",
                                                            "lights mode: match colour")
        out["notes"] = notes
        out["simple"] = self._sync()
        self._save()
        out["ready"] = self.publish(force=True)
        return out

    def _enter_manual(self) -> bool:
        cam = self.ctl.camera
        fn = getattr(cam, "set_exposure_mode", None)
        wrote = False
        if fn is not None:
            try:
                with self.ctl.lock:
                    wrote = bool(fn("manual"))
            except Exception as e:  # noqa: BLE001
                log.info("set_exposure_mode failed: %s", e)
                wrote = False
        if wrote and self.verify_s:
            time.sleep(self.verify_s)
        mode = self._body_mode()
        self.manual_write = wrote
        ok = mode is not None and str(mode).strip().lower() in MANUAL_MODES
        self.ctl.state.log("simple", "exposure_mode", None, {"wrote": wrote, "reads": mode})
        try:
            self.ctl.refresh_camera_state()
        except Exception:  # noqa: BLE001
            pass
        return ok

    # ---------------------------------------------------------------- nudges
    def nudge(self, what: str) -> dict[str, Any]:
        if what not in ("brighter", "dimmer", "warmer", "cooler"):
            raise ValueError("what must be brighter|dimmer|warmer|cooler")
        applied: dict[str, Any] = {}
        if what in ("brighter", "dimmer"):
            text = self._nudge_brightness(+1 if what == "brighter" else -1, applied)
        else:
            text = self._nudge_colour(-1 if what == "warmer" else +1, applied)
        self._save()
        return {"what": what, "applied": applied, "text": text, "simple": self._sync(),
                "ready": self.publish(force=True)}

    def _nudge_brightness(self, d: int, applied: dict[str, Any]) -> str:
        kid, light = self._movable_key()
        if light is not None:
            st = light.state()
            cur = float(st.intensity_pct)
            new = max(st.intensity_min_pct, min(st.intensity_max_pct, cur + d * LIGHT_STEP))
            if abs(new - cur) > 1e-6:
                s = self.ctl.set_light(kid, new, None, "simple", "brighter" if d > 0 else "dimmer")
                applied.setdefault("lights", {})[kid] = {"intensity_pct": s["intensity_pct"], "cct_k": s["cct_k"]}
                return f"Key light {_pct(cur)} → {_pct(s['intensity_pct'])} %"
            if self.mode == "camera":
                return (f"Key light is already at {_pct(cur)} %: move it closer." if d > 0
                        else f"Key light is already at {_pct(cur)} %: move it further away.")
            prefix = f"Key light is at {_pct(cur)} %: "
        elif self.mode == "camera":
            return TEXT["no_light_camera_mode"]
        else:
            prefix = ""
        # lights mode, key at its limit (or no light to move): one ISO step
        iso = self._iso_step(d)
        if iso is None:
            return prefix + ("ISO is at its limit too: move the light closer." if d > 0
                             else "ISO is at its lowest: move the light further away.")
        with self.ctl.lock:
            before = self.ctl.camera.get_settings().get("iso")
        try:
            a = self._write_camera({"iso": iso}, "brighter" if d > 0 else "dimmer")
        except Override:
            return TEXT["override"]
        applied["camera"] = {"iso": a.get("iso")}
        return prefix + f"ISO {before} → {a.get('iso')}"

    def _nudge_colour(self, d: int, applied: dict[str, Any]) -> str:
        lights = {lid: l for lid, l in self.ctl.lights.items() if self._answering(l)}
        cur = int(self.st.get("warmth") or 5600)
        cam_k = _num(self.ctl.state.camera.get("settings", {}).get("kelvin"))
        if cam_k:
            cur = int(cam_k)
        new = int(max(WARMTH_RANGE[0], min(WARMTH_RANGE[1], cur + d * KELVIN_STEP)))
        if new == cur and all(l.state().cct_k == new for l in lights.values()):
            return f"Colour is already at {cur} K: that is as far as it goes."
        if self._camera_connected():
            try:
                a = self._write_camera({"kelvin": new}, "warmer" if d < 0 else "cooler")
                applied["camera"] = {"kelvin": a.get("kelvin")}
            except Override:
                return TEXT["override"]
        for lid, l in lights.items():
            s = self.ctl.set_light(lid, None, new, "simple", "warmer" if d < 0 else "cooler")
            applied.setdefault("lights", {})[lid] = {"intensity_pct": s["intensity_pct"], "cct_k": s["cct_k"]}
        self.st["warmth"] = new
        return f"Colour {cur} → {new} K"

    # ---------------------------------------------------------------- level
    def _band(self) -> tuple[str, list[float]]:
        c = self.ctl.state.context
        t = presets.target_vector(c.preset, c.scene)
        if self._person() and t.get("skin_luma"):
            return "skin", list(t["skin_luma"])
        return "luma", list(t.get("luma_median") or [0.40, 0.55])

    def level(self) -> dict[str, Any]:
        if self.ctl.state.recording:
            raise PermissionError("recording: Level waits until the take is stopped")
        ap = getattr(self.ctl, "autopilot", None)
        if ap is not None and getattr(ap, "running", False):
            raise ValueError("AI Auto is running: stop it before levelling")
        if not self._busy.acquire(blocking=False):
            raise ValueError("already levelling")
        try:
            out = self._level()
        finally:
            self._busy.release()
        self._save()
        out["simple"] = self._sync()
        out["ready"] = self.publish(force=True)
        return out

    def _level(self) -> dict[str, Any]:
        metric, band = self._band()
        lo, hi = band
        mid = (lo + hi) / 2
        trail: list[dict[str, Any]] = []
        k_resp = 0.5            # luma ~ light^k; refined from what each move actually did
        prev: tuple[float, float] | None = None     # (exposure factor product, value)
        expo = 1.0              # running product of the exposure moves made

        def done(status, text):
            return {"status": status, "text": text, "trail": trail, "metric": metric, "band": band}

        for rnd in range(1, LEVEL_ROUNDS + 1):
            if self.ctl.state.recording:
                return done("refused", "Stopped: a take started.")
            h = self.ctl.hud_analysis()
            m_used = metric
            v = h.get("skin_median") if metric == "skin" else h.get("luma_median")
            if v is None:
                m_used, v = "luma", h.get("luma_median")
            v = max(0.005, float(v))
            kid, light = self._movable_key()
            with self.ctl.lock:
                iso_now = self.ctl.camera.get_settings().get("iso")
            row = {"round": rnd, "metric": m_used, "value": round(v, 4), "band": band,
                   "key_pct": light.state().intensity_pct if light else None, "iso": iso_now, "moved": {}}
            trail.append(row)
            if lo <= v <= hi:
                return done("ok", TEXT["level_ok"] if rnd > 1 else "Brightness was already right.")
            if prev is not None and abs(math.log(expo / prev[0])) > 1e-3 and abs(math.log(v / prev[1])) > 1e-3:
                k_obs = math.log(v / prev[1]) / math.log(expo / prev[0])
                if k_obs > 0.05:            # a reading that moved the wrong way (noise, AE) teaches nothing
                    k_resp = max(0.25, min(1.5, k_obs))
            want = (mid / v) ** (1.0 / k_resp)          # exposure factor needed
            want = max(0.25, min(4.0, want))
            d = 1 if want > 1 else -1
            moved = 1.0
            if light is not None:
                st = light.state()
                cur = max(1.0, float(st.intensity_pct))
                new = max(st.intensity_min_pct, min(st.intensity_max_pct, cur * want))
                if abs(new - float(st.intensity_pct)) >= 0.5:
                    s = self.ctl.set_light(kid, round(new, 1), None, "simple", f"level round {rnd}")
                    row["moved"]["key_pct"] = s["intensity_pct"]
                    moved = max(1e-3, float(s["intensity_pct"])) / cur
            residual = want / moved
            if self.mode == "lights" and abs(math.log2(residual)) > 0.2:      # the light ran out of room
                iso = self._iso_step(1 if residual > 1 else -1, residual)
                if iso is not None:
                    try:
                        a = self._write_camera({"iso": iso}, f"level round {rnd}")
                    except Override:
                        return done("override", TEXT["override"])
                    except CameraError as e:
                        return done("error", f"The camera refused the change: {e}")
                    row["moved"]["iso"] = a.get("iso")
                    if _num(iso_now) and _num(a.get("iso")):
                        moved *= _num(a.get("iso")) / _num(iso_now)
            if not row["moved"]:
                if light is None and self.mode == "camera":
                    return done("no_light", TEXT["level_no_light"])
                return done("limit", TEXT["too_dark_limit"] if d > 0 else TEXT["too_bright_limit"])
            prev = (expo, v)
            expo *= moved
            if self.settle_s:
                time.sleep(self.settle_s)
        # one last look: the sixth move may have landed it
        h = self.ctl.hud_analysis()
        v = h.get("skin_median") if metric == "skin" else None
        v = v if v is not None else h.get("luma_median")
        if v is not None and lo <= v <= hi:
            trail.append({"round": LEVEL_ROUNDS + 1, "metric": metric, "value": round(float(v), 4), "band": band,
                          "moved": {}, "final_check": True})
            return done("ok", TEXT["level_ok"])
        return done("cap", TEXT["level_cap"])

    # ---------------------------------------------------------------- keep / load
    def _last_good(self) -> dict[str, Any] | None:
        c = self.ctl.state.context
        cands = [r for r in self.ctl.recipes.recipes if r.get("kind") == "last_good"
                 and r.get("preset") == c.preset and r.get("scene") == c.scene]
        exact = [r for r in cands if r.get("lens") == c.lens]
        cands = exact or cands
        return max(cands, key=lambda r: r.get("created", 0)) if cands else None

    def keep(self, clip: str | None = None) -> dict[str, Any]:
        clip = (clip or "").strip() or self.ctl.state.last_clip
        c = self.ctl.state.context
        old = [r["id"] for r in self.ctl.recipes.recipes if r.get("kind") == "last_good"
               and r.get("preset") == c.preset and r.get("scene") == c.scene and r.get("lens") == c.lens]
        with self.ctl.lock:
            try:
                self.ctl.refresh_camera_state()
            except Exception:  # noqa: BLE001
                pass
            snap = self.ctl.get_state()
        label = presets.PRESETS.get(c.preset, {}).get("label", c.preset)
        scene = presets.SCENES.get(c.scene, {}).get("label", c.scene)
        r = self.ctl.recipes.add(snap, f"Last good: {label}, {scene}", f"kept take {clip}" if clip else None)
        r.update(kind="last_good", clip=clip, simple_mode=self.mode)
        for rid in old:
            self.ctl.recipes.recipes = [x for x in self.ctl.recipes.recipes if x["id"] != rid]
        self.ctl.recipes.save()
        self.ctl.state.log("simple", "keep", None, {"recipe": r["id"], "clip": clip})
        self.ctl.state.broadcast({"type": "recipes", "recipes": self.ctl.recipes.list()})
        return {"recipe": r, "text": "Saved this setup for next time.", "ready": self.publish(force=True)}

    def load(self) -> dict[str, Any]:
        r = self._last_good()
        if r is None:
            raise KeyError(TEXT["no_last_good"])
        cam = r.get("camera", {}).get("settings", {}) or {}
        keys = ("iso", "shutter", "aperture", "kelvin") if self.mode == "lights" else ("kelvin",)
        changes = {k: cam[k] for k in keys if cam.get(k) is not None and (k != "iso" or _num(cam[k]) is not None)}
        out: dict[str, Any] = {"recipe": {"id": r["id"], "name": r["name"], "clip": r.get("clip")},
                               "camera": {}, "lights": {}}
        text = "Loaded the last good setup."
        if changes and self._camera_connected():
            try:
                out["camera"] = self._write_camera(changes, f"load {r['name']}")
            except Override:
                text = TEXT["override"]
        hand = []
        for lid, lv in (r.get("lights") or {}).items():
            if lid in self.ctl.lights:
                s = self.ctl.set_light(lid, lv.get("intensity_pct"), lv.get("cct_k"), "simple", f"load {r['name']}")
                out["lights"][lid] = {"intensity_pct": s["intensity_pct"], "cct_k": s["cct_k"]}
                if not s.get("scriptable", True):
                    hand.append(f"{s['model']} to {_pct(s['intensity_pct'])} % at {s['cct_k']} K")
        if hand and text == "Loaded the last good setup.":
            text = "Loaded the last good setup: set " + " and ".join(hand) + " by hand."
        if out["camera"].get("kelvin"):
            self.st["warmth"] = int(_num(out["camera"]["kelvin"]))
        out["text"] = text
        out["simple"] = self._sync()
        self._save()
        out["ready"] = self.publish(force=True)
        return out


# ====================================================================== http
from pydantic import BaseModel  # noqa: E402


class ModeBody(BaseModel):
    mode: str


class NudgeBody(BaseModel):
    what: str


class KeepBody(BaseModel):
    clip: str | None = None


def register_routes(app, ctl: "Controller", run) -> None:
    """Routes for the one-screen UI; `run` is api.build's thread + error mapper."""
    @app.get("/api/ready")
    async def ready():
        def _r():
            s = ctl.simple
            if not s.faces or time.time() - s.faces[-1]["ts"] > 2.5:
                return s.tick()           # nobody is watching live view: take a look now
            return s.ready()
        return await run(_r)

    @app.post("/api/simple/mode")
    async def simple_mode(body: ModeBody):
        return await run(ctl.simple.set_mode, body.mode)

    @app.post("/api/simple/nudge")
    async def simple_nudge(body: NudgeBody):
        return await run(ctl.simple.nudge, body.what)

    @app.post("/api/simple/level")
    async def simple_level():
        return await run(ctl.simple.level)

    @app.post("/api/simple/keep")
    async def simple_keep(body: KeepBody):
        return await run(ctl.simple.keep, body.clip)

    @app.post("/api/simple/load")
    async def simple_load():
        return await run(ctl.simple.load)
