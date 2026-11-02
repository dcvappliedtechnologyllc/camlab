"""Stills: named looks instead of settings, and one-press still capture.

The owner picks a look ("High bokeh", "Everything sharp", ...), not an aperture. Each look
is a row in LOOKS: the camera recipe (aperture, shutter, ISO strategy, white balance,
picture style, AF mode), a key/fill light suggestion in words, and one plain sentence that
says what it does and when to use it. `look_recipe()` turns a row into values the body can
take: the aperture is clamped to the lens (the body's own aperture list first, the lens
table in presets.py second) and the look borrows its target band from the matching
preset so the photo's analysis is judged the same way a clip would be.

Shoot: no ARM (that guardrail is for a rolling take; a photo is one press), refused while a
take rolls or in video mode. The backend takes the picture to the card, the JPEG (and the
RAW file when asked) is copied to <home>/stills/<date>/<tag>-NNN.JPG, analysed with the
usual measurements and announced as {"type": "still"}. The card is never written to or
cleared from here: nothing in this module removes a file anywhere.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import analysis, presets
from .camera.base import CameraError

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

log = logging.getLogger("camlab.stills")

MANUAL_MODES = {"m", "manual", "manual exposure"}

# iso: "auto" lets the body pick a clean exposure for the chosen aperture and shutter (ISO Auto in M);
# a number pins it. shutter: on a tripod; fast enough for a steady hand with the Sigma's stabiliser.
LOOKS: dict[str, dict[str, Any]] = {
    "high_bokeh": {
        "label": "High bokeh", "preset": "product_hero", "scene": "decor_detail",
        "sentence": "The lens opens as wide as it goes so the subject is sharp and everything behind it melts away; use it for one thing you want to stand out.",
        "aperture": "widest", "shutter": "1/125", "iso": "auto", "kelvin": 5200,
        "picture_style": "Standard", "focus_mode": "One Shot",
        "lights": {"key": "Key light 60 % from your left, 45 degrees off the lens, a foot above the subject, softened with the diffuser.",
                   "fill": "Fill 20 % from the right, flat on, so the shadow side keeps a little detail."},
        "target": {"dof": "shallow"},
    },
    "everything_sharp": {
        "label": "Everything sharp", "preset": "bench_doc", "scene": "assembly",
        "sentence": "The lens is stopped down so a whole part or assembly is in focus front to back; use it for parts, tools and how-to shots.",
        "aperture": "f/8", "shutter": "1/60", "iso": "auto", "kelvin": 5000,
        "picture_style": "Fine Detail", "focus_mode": "One Shot",
        "lights": {"key": "Key light 80 % from your left, 45 degrees off the lens, high enough to keep shadows out of recesses.",
                   "fill": "Fill 50 % from the right at the same height, so nothing goes black."},
        "target": {"dof": "deep"},
    },
    "product_on_black": {
        "label": "Product on black", "preset": "product_hero", "scene": "part_dark",
        "sentence": "A dark, moody shot: the part holds its detail, the background goes black and only the edges catch light; use it for marketing shots of dark parts.",
        "aperture": "f/5.6", "shutter": "1/125", "iso": "100", "kelvin": 5600,
        "picture_style": "Neutral", "focus_mode": "One Shot",
        "lights": {"key": "Key light 40 % from behind and to your left, 110 degrees off the lens, skimming the surface so edges glint.",
                   "fill": "Fill off, or 10 % from the right with a flag so the background stays black."},
        "target": {"dof": "medium"},
    },
    "clean_portrait": {
        "label": "Clean portrait", "preset": "reel", "scene": "person_portrait",
        "sentence": "Skin comes first: warm-neutral colour, a soft key light and a background that goes gently out of focus; use it for Colleen's portraits.",
        "aperture": "f/4", "shutter": "1/125", "iso": "auto", "kelvin": 4300,
        "picture_style": "Portrait", "focus_mode": "One Shot",
        "lights": {"key": "Key light 60 % from your left, 40 degrees off the lens, just above her eyes, through the diffuser (soft).",
                   "fill": "Fill 25 % from the right at eye height, so the far cheek keeps detail."},
        "target": {"dof": "shallow"},
    },
    "flat_document": {
        "label": "Flat document / overhead", "preset": "overhead", "scene": "flat_doc",
        "sentence": "Even, shadowless light and a square-on view so a page or a flat layout is readable edge to edge; use it with the camera pointing straight down.",
        "aperture": "f/8", "shutter": "1/60", "iso": "200", "kelvin": 5000,
        "picture_style": "Fine Detail", "focus_mode": "One Shot",
        "lights": {"key": "Key light 70 % from your left at 45 degrees up, far enough back to light the whole page evenly.",
                   "fill": "Fill 70 % from the right, mirrored, so neither side is brighter."},
        "target": {"dof": "deep"},
    },
}

TEXT = {
    "arm": "No arming needed: a photo is one press of Shoot.",
    "photo_ok": "The top dial is on M in the photo range.",
    "photo_movie": "Turn the top dial to a photo position (M). camlab cannot flip it from here.",
    "photo_not_m": "Turn the top dial to M in the photo range.",
    "photo_unknown": "Make sure the top dial is on M in the photo range.",
    "photo_no_camera": "Switch the camera on and check the USB cable.",
    "video_mode": "Switch to Stills first.",
    "recording": "A take is rolling: stop it before taking a photo.",
    "shot": "Photo taken.",
}


def _fnum(a: str | None) -> float | None:
    if a is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(a))
    return float(m.group(1)) if m else None


def _lens_max(lens_key: str | None) -> str:
    return presets.LENSES.get(lens_key or "", {}).get("max_aperture") or "f/2.8"


def look_recipe(look: str, lens_max: str | None = None, aperture_choices: list[str] | None = None,
                lens_key: str | None = None) -> dict[str, Any]:
    """The camera values a look asks for, respecting the glass: `aperture_choices` is the body's own
    list (what the lens can do right now), `lens_max` the lens table's widest stop (fallback)."""
    lk = LOOKS[look]
    widest = _fnum(lens_max) or _fnum(_lens_max(lens_key)) or 2.8
    if aperture_choices:
        nums = [f for f in (_fnum(a) for a in aperture_choices) if f is not None]
        if nums:
            widest = max(widest, min(nums)) if lens_max is None else min(nums)
    want = widest if lk["aperture"] == "widest" else max(float(_fnum(lk["aperture"]) or widest), widest)
    aperture = f"f/{want:g}"
    cam: dict[str, Any] = {"aperture": aperture, "shutter": lk["shutter"], "iso": lk["iso"], "kelvin": lk["kelvin"],
                           "picture_style": lk["picture_style"], "focus_mode": lk["focus_mode"]}
    target = dict(presets.target_vector(lk["preset"], lk["scene"]))
    target.update(lk.get("target", {}))
    target.pop("aperture", None)          # the look already decided the stop
    return {"id": look, "label": lk["label"], "sentence": lk["sentence"], "camera": cam,
            "lights": dict(lk["lights"]), "preset": lk["preset"], "scene": lk["scene"], "target": target}


def list_looks() -> list[dict[str, Any]]:
    return [{"id": k, "label": v["label"], "sentence": v["sentence"], "preset": v["preset"], "scene": v["scene"]}
            for k, v in LOOKS.items()]


def measure(a: dict[str, Any]) -> dict[str, Any]:
    """The flat measurement the UI shows under a photo (same names as the HUD)."""
    skin = a.get("roi", {}).get("skin") or a.get("roi", {}).get("face") or {}
    sub = a.get("roi", {}).get("subject") or {}
    return {"luma_median": a.get("luma", {}).get("median"), "clip_high_pct": a.get("clip", {}).get("high_pct"),
            "clip_low_pct": a.get("clip", {}).get("low_pct"), "focus": a.get("focus", {}).get("laplacian_var"),
            "wb_estimate_k": a.get("channels", {}).get("wb_estimate_k"),
            "skin_median": skin.get("median"), "face_clip_high_pct": skin.get("high_pct"),
            "subject_median": sub.get("median"), "subject_p1": sub.get("p1"), "subject_p99": sub.get("p99"),
            "subject_bbox": sub.get("bbox"), "skin_bbox": skin.get("bbox"),
            "flags": list(a.get("flags", [])), "target_check": a.get("target_check", {})}


class Stills:
    def __init__(self, ctl: "Controller"):
        self.ctl = ctl
        home = ctl.cfg["paths"].get("home") or str(Path(ctl.cfg["paths"]["clips"]).parent)
        self.root = Path(ctl.cfg["paths"].get("stills") or (Path(home) / "stills"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.index: list[dict[str, Any]] = []
        try:
            self.index = json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            self.index = []

    # ---------------------------------------------------------------- state
    @property
    def on(self) -> bool:
        return bool(self.ctl.state.stills_mode)

    def _save_index(self) -> None:
        try:
            self.index_path.write_text(json.dumps(self.index, indent=1, default=str))
        except OSError as e:
            log.warning("stills index not written: %s", e)

    def _camera_connected(self) -> bool:
        from .controller import NoCamera
        cam = self.ctl.camera
        if cam is None or isinstance(cam, NoCamera):
            return False
        try:
            return bool(cam.info().connected)
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------------------- readiness
    def ready(self) -> dict[str, Any]:
        """Like simple.ready() but for a photo: camera, the photo dial, lights, subject, no ARM."""
        base = self.ctl.simple.ready()
        items = [i for i in base["items"] if i["id"] in ("camera", "lights", "subject")]
        cam_ok = self._camera_connected()
        if cam_ok:
            try:
                still = self.ctl.camera.still_mode()
            except Exception as e:  # noqa: BLE001
                log.debug("still_mode: %s", e)
                still = None
            mode = self.ctl.simple._body_mode()
            manual = mode is not None and str(mode).strip().lower() in MANUAL_MODES
            if still is False:
                ok, text = False, TEXT["photo_movie"]
            elif still is None and mode is None:
                ok, text = True, TEXT["photo_unknown"]
            elif not manual:
                ok, text = False, TEXT["photo_not_m"]
            else:
                ok, text = True, TEXT["photo_ok"]
            detail = {"still": still, "mode": mode}
        else:
            ok, text, detail = False, TEXT["photo_no_camera"], {"still": None, "mode": None}
        items.insert(1, {"id": "photo_dial", "ok": ok, "text": text, "detail": detail})
        items.append({"id": "arm", "ok": True, "text": TEXT["arm"], "detail": {}})
        return {"items": items, "all_ok": all(i["ok"] for i in items), "mode": "stills" if self.on else "video",
                "look": self.ctl.state.stills_look, "raw_jpeg": bool(self.ctl.state.camera.get("capabilities", {}).get("raw_jpeg"))}

    # ---------------------------------------------------------------- mode / look
    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in ("stills", "video"):
            raise ValueError("mode must be stills|video")
        if self.ctl.state.recording:
            raise PermissionError(TEXT["recording"])
        with self.ctl.lock:
            self.ctl.state.stills_mode = mode == "stills"
            self.ctl.state.save_context(self.ctl.cfg["paths"].get("context"))
            self.ctl.state.log("ui", "stills_mode", None, mode)
        out: dict[str, Any] = {"mode": mode, "look": self.ctl.state.stills_look}
        if mode == "stills" and self.ctl.state.stills_look:
            try:
                out["applied"] = self.set_look(self.ctl.state.stills_look)["applied"]
            except Exception as e:  # noqa: BLE001
                out["notes"] = [str(e)]
        out["ready"] = self.ready()
        self.ctl.state.broadcast({"type": "stills", **{k: v for k, v in out.items() if k != "ready"}, "ready": out["ready"]})
        return out

    def set_look(self, look: str) -> dict[str, Any]:
        if look not in LOOKS:
            raise KeyError(f"unknown look {look!r}; have {list(LOOKS)}")
        if self.ctl.state.recording:
            raise PermissionError(TEXT["recording"])
        ctx = self.ctl.state.context
        caps = self.ctl.state.camera.get("capabilities", {}) or {}
        r = look_recipe(look, lens_max=_lens_max(ctx.lens), aperture_choices=caps.get("aperture"), lens_key=ctx.lens)
        cam = dict(r["camera"])
        if cam.get("iso") == "auto":
            isos = [str(x) for x in caps.get("iso", [])]
            if not any(x.lower() == "auto" for x in isos):
                cam["iso"] = "400"               # a body (or the mock) without ISO Auto: a safe fixed value
        for key in ("picture_style", "focus_mode"):
            cam[key] = self._match_choice(key, cam[key], caps.get(key) or [])
        applied: dict[str, Any] = {}
        notes: list[str] = []
        for key in ("aperture", "shutter", "iso", "kelvin", "picture_style", "focus_mode"):
            if cam.get(key) is None:
                continue
            try:
                applied.update(self.ctl.set_camera({key: cam[key]}, "stills", f"look {r['label']}"))
            except Exception as e:  # noqa: BLE001
                notes.append(f"{key}: {e}")
        with self.ctl.lock:
            self.ctl.state.stills_look = look
            self.ctl.state.save_context(self.ctl.cfg["paths"].get("context"))
        out = {"look": look, "label": r["label"], "sentence": r["sentence"], "applied": applied, "wanted": cam,
               "lights": r["lights"], "notes": notes, "target": r["target"], "ready": self.ready()}
        self.ctl.state.broadcast({"type": "stills", "mode": "stills" if self.on else "video", "look": look, "ready": out["ready"]})
        return out

    @staticmethod
    def _match_choice(key: str, want: Any, choices: list[Any]) -> Any:
        """Picture style / AF mode names differ by backend ('Fine Detail' vs 'fine_detail'): match loosely."""
        if not choices:
            return want
        norm = lambda s: re.sub(r"[^a-z]", "", str(s).lower())   # noqa: E731
        for c in choices:
            if norm(c) == norm(want):
                return c
        alias = {"oneshot": ["afoneshot", "oneshotaf"], "servo": ["afservo", "servoaf"], "manual": ["mf", "manualfocus"]}
        for c in choices:
            if norm(c) in alias.get(norm(want), []):
                return c
        return choices[0] if key == "focus_mode" else want

    # ---------------------------------------------------------------- shoot
    def _label(self, tag: str | None) -> str:
        prefix = tag or "photo"
        n = 1 + sum(1 for e in self.index if (e.get("tag") or "photo") == prefix)
        return f"{prefix}-{n:03d}.JPG"

    def shoot(self, raw: bool = False) -> dict[str, Any]:
        if not self.on:
            raise PermissionError(TEXT["video_mode"])
        if self.ctl.state.recording:
            raise PermissionError(TEXT["recording"])
        ctl = self.ctl
        with ctl.lock:
            if ctl.autopilot is not None:
                ctl.autopilot.stop()
            if ctl.state.camera.get("capabilities", {}).get("raw_jpeg"):
                try:
                    ctl.camera.set_image_quality(raw=bool(raw))
                except CameraError as e:
                    log.info("image quality: %s", e)
            day = self.root / time.strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True)
            tag = ctl.state.context.tag or None
            label = self._label(tag)
            res = ctl.camera.take_picture(str(day))
            ctl.state.log("ui", "still", None, {"name": res.get("name"), "look": ctl.state.stills_look})
            src = Path(res["path"])
            dest = day / label
            if src != dest:
                os.replace(src, dest)
            raw_path = None
            if res.get("raw_path"):
                rp = Path(res["raw_path"])
                raw_path = str(day / (label[:-4] + rp.suffix.upper()))
                if str(rp) != raw_path:
                    os.replace(rp, raw_path)
            jpeg = dest.read_bytes()
            ctx = ctl.state.context
            look = ctl.state.stills_look
            meta = {"camera": dict(ctl.state.camera.get("settings") or {}),
                    "lights": {k: dict(v) if isinstance(v, dict) else v for k, v in ctl.state.lights.items()}}
        target = look_recipe(look)["target"] if look else presets.target_vector(ctx.preset, ctx.scene)
        scene_key = LOOKS[look]["scene"] if look else ctx.scene
        scene_roi = presets.SCENES.get(scene_key, {}).get("roi", "center")
        a = analysis.analyze(jpeg, roi=ctx.roi, scene_roi=scene_roi, target=target)
        m = measure(a)
        still = {"label": label, "name": res.get("name"), "path": str(dest), "raw_path": raw_path, "size": dest.stat().st_size,
                 "width": a["width"], "height": a["height"], "ts": time.time(), "tag": tag or "", "look": look,
                 "look_label": LOOKS[look]["label"] if look else None, "analysis": m}
        side = {**{k: v for k, v in still.items() if k != "analysis"}, "settings": meta, "analysis": m,
                "preset": ctx.preset, "scene": ctx.scene, "lens": ctx.lens, "verdict": None, "notes": ""}
        try:
            Path(str(dest) + ".json").write_text(json.dumps(side, indent=2, default=str))
        except OSError as e:
            log.warning("sidecar for %s not written: %s", label, e)
        with ctl.lock:
            self.index.append({k: v for k, v in still.items() if k != "analysis"} | {"analysis": m})
            del self.index[:-500]
            self._save_index()
            ctl.state.stills.append(still)
            del ctl.state.stills[:-20]
            ctl.state.last_still = still
        ctl.state.broadcast({"type": "still", "still": still})
        return {**still, "jpeg": jpeg, "text": TEXT["shot"]}

    # ---------------------------------------------------------------- list / files
    def get(self, label: str) -> dict[str, Any] | None:
        base = os.path.basename(str(label))
        for e in reversed(self.index):
            if e.get("label") == base or e.get("name") == base or os.path.basename(str(e.get("path") or "")) == base:
                return e
        return None

    def list_today(self) -> list[dict[str, Any]]:
        day = time.strftime("%Y-%m-%d")
        return [e for e in self.index if e.get("ts") and time.strftime("%Y-%m-%d", time.localtime(e["ts"])) == day
                and e.get("path") and os.path.exists(e["path"])]

    def file(self, label: str) -> Path | None:
        """A still's JPEG inside the stills folder, else None (never anything outside it)."""
        root = self.root.resolve()
        e = self.get(label)
        cand = Path(e["path"]) if e and e.get("path") else (root / label if label and "\\" not in label else None)
        if cand is None:
            return None
        try:
            p = cand.resolve()
            p.relative_to(root)
        except (OSError, ValueError):
            return None
        return p if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg") else None

    def jpeg_for_ask(self, label: str, max_side: int = 1600) -> bytes:
        """The photo scaled for Claude / the hand-off (a 24 MP JPEG is far more than a chat needs)."""
        import io
        from PIL import Image
        p = self.file(label)
        if p is None:
            raise KeyError(f"photo {label!r} is not on this Mac")
        im = Image.open(p)
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=88)
        return buf.getvalue()


# ---------------------------------------------------------------------- routes
# (module level: with `from __future__ import annotations` FastAPI cannot resolve a model defined inside the function)
from fastapi import HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class ModeBody(BaseModel):
    mode: str


class LookBody(BaseModel):
    look: str


class ShootBody(BaseModel):
    raw: bool = False


class SendBody(BaseModel):
    label: str


def register_routes(app, ctl: "Controller", run, take_url) -> None:

    @app.get("/api/stills/looks")
    async def looks():
        return {"looks": list_looks()}

    @app.get("/api/stills/ready")
    async def stills_ready():
        return await run(ctl.stills.ready)

    @app.post("/api/stills/mode")
    async def stills_mode(body: ModeBody):
        return await run(ctl.stills.set_mode, body.mode)

    @app.post("/api/stills/look")
    async def stills_look(body: LookBody):
        return await run(ctl.stills.set_look, body.look)

    @app.post("/api/stills/shoot")
    async def stills_shoot(body: ShootBody | None = None):
        r = await run(ctl.stills.shoot, bool(body and body.raw))
        if isinstance(r, dict) and r.get("jpeg"):
            jpeg = r.pop("jpeg")
            r["jpeg_b64"] = base64.b64encode(ctl.stills.jpeg_for_ask(r["label"], 1280)).decode() if len(jpeg) > 400_000 \
                else base64.b64encode(jpeg).decode()
        return r

    @app.get("/api/stills")
    async def stills_list(request: Request):
        items = await run(ctl.stills.list_today)
        return {"stills": [dict(e, url=take_url(request, e["label"]).replace("/clips/", "/stills/")) for e in items]}

    @app.post("/api/stills/send")
    async def stills_send(body: SendBody, request: Request):
        if ctl.stills.file(body.label) is None:
            raise HTTPException(404, "no such photo")
        url = take_url(request, body.label).replace("/clips/", "/stills/")
        qr = None
        try:
            import io
            import segno
            buf = io.BytesIO()
            segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False)
            qr = buf.getvalue().decode()
        except ImportError:
            pass
        return {"url": url, "qr_svg": qr}

    @app.get("/stills/{name:path}")
    async def still_file(name: str):
        p = ctl.stills.file(name)
        if p is None:
            raise HTTPException(404, "no such photo")
        return FileResponse(str(p), media_type="image/jpeg", filename=p.name, content_disposition_type="inline")
