"""Mock camera: synthesizes a live-view frame that responds to exposure,
WB, focus and (via a shared hook) light intensity, so the whole loop can be
exercised without hardware. Scene: a dark anodized "part" on a bench with a
specular edge, or a face-toned ellipse when scene looks like a person."""
from __future__ import annotations

import io
import math
import random
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from .base import CameraBackend, CameraError, CameraInfo, Frame

ISO = ["100", "125", "160", "200", "250", "320", "400", "500", "640", "800", "1000", "1250", "1600",
       "2000", "2500", "3200", "4000", "5000", "6400", "8000", "10000", "12800", "25600"]
SHUTTER = ["1/4000", "1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60", "1/50", "1/30", "1/25", "1/15", "1/8", "1/4", "0.5", "1"]
APERTURE = ["f/2.8", "f/3.2", "f/3.5", "f/4", "f/4.5", "f/5", "f/5.6", "f/6.3", "f/7.1", "f/8", "f/9", "f/10", "f/11", "f/13", "f/14", "f/16", "f/22"]
STYLES = ["standard", "portrait", "landscape", "fine_detail", "neutral", "faithful", "monochrome"]
WB = ["auto", "daylight", "shade", "cloudy", "tungsten", "fluorescent", "flash", "kelvin"]
WB_K = {"auto": 5200, "daylight": 5200, "shade": 7000, "cloudy": 6000, "tungsten": 3200, "fluorescent": 4000, "flash": 5500}


def _shutter_s(s: str) -> float:
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b)
    return float(s)


def _fnum(a: str) -> float:
    return float(a.replace("f/", ""))


class MockBackend(CameraBackend):
    name = "mock"

    def __init__(self, subject: str = "part_dark", seed: int = 7):
        self.subject = subject
        self.rng = random.Random(seed)
        self.settings: dict[str, Any] = {
            "iso": "400", "shutter": "1/60", "aperture": "f/5.6", "ev_comp": "0",
            "wb_mode": "kelvin", "kelvin": 5600, "picture_style": "neutral",
            "focus_mode": "af_one_shot", "movie_resolution": "1080p", "movie_fps": 30,
            "orientation": "landscape",
        }
        self.focus_pos = 0.35          # 0..1, sharp at 0.5
        self.scene_cct = 5600          # colour of the light hitting the scene
        self.light_level = 0.8         # 0..1, key light relative output
        self.recording = False
        self.clip_counter = 0
        self._connected = False
        self.mode = "movie"            # what the top dial reports; set_exposure_mode("manual") -> "M"
        self.raw = False               # RAW+JPEG asked for (set_image_quality)
        self.still_counter = 0

    # hooks for the server to tie lights to the synthetic scene
    def set_scene_light(self, level: float, cct_k: int) -> None:
        self.light_level, self.scene_cct = max(0.0, min(1.0, level)), cct_k

    def connect(self) -> CameraInfo:
        self._connected = True
        return self.info()

    def disconnect(self) -> None:
        self._connected = False

    def info(self) -> CameraInfo:
        return CameraInfo(
            model="Mock EOS 77D", backend="mock", connected=self._connected,
            lens="Sigma 17-50 f/2.8", focal_mm=35, mode=self.mode,
            capabilities={"iso": ISO, "shutter": SHUTTER, "aperture": APERTURE,
                          "picture_style": STYLES, "wb_mode": WB, "kelvin_range": [2500, 10000],
                          "focus_mode": ["af_one_shot", "af_servo", "mf"],
                          "movie_modes": [{"resolution": "1080p", "fps": 60}, {"resolution": "1080p", "fps": 30},
                                          {"resolution": "1080p", "fps": 24}],
                          "focus_drive": True, "af_point": True, "record": True, "liveview": True,
                          "raw_jpeg": True})

    def get_settings(self) -> dict[str, Any]:
        return dict(self.settings)

    def set_exposure_mode(self, mode: str) -> bool:
        if str(mode).lower() in ("manual", "m"):
            self.mode = "M"
            return True
        return False

    @staticmethod
    def _snap(val: str, choices: list[str], key=lambda v: v) -> str:
        if val in choices:
            return val
        try:
            fv = key(val)
            return min(choices, key=lambda c: abs(key(c) - fv))
        except Exception:
            raise CameraError(f"invalid value {val!r}; choices {choices}")

    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        for k, v in changes.items():
            if v is None:
                continue
            if k == "iso":
                v = self._snap(str(v), ISO, float)
            elif k == "shutter":
                v = self._snap(str(v), SHUTTER, _shutter_s)
            elif k == "aperture":
                v = self._snap(str(v) if str(v).startswith("f/") else f"f/{v}", APERTURE, _fnum)
            elif k == "kelvin":
                v = int(max(2500, min(10000, int(v))))
                self.settings["wb_mode"] = "kelvin"
                applied["wb_mode"] = "kelvin"
            elif k == "wb_mode":
                if v not in WB:
                    raise CameraError(f"wb_mode {v!r} not in {WB}")
            elif k == "picture_style":
                if v not in STYLES:
                    raise CameraError(f"picture_style {v!r} not in {STYLES}")
            elif k == "movie_fps":
                v = int(v)
            self.settings[k] = v
            applied[k] = v
        return applied

    def focus_drive(self, step: str) -> None:
        sign = -1 if step.startswith("near") else 1
        mag = {"1": 0.02, "2": 0.06, "3": 0.15}[step[-1]]
        self.focus_pos = max(0.0, min(1.0, self.focus_pos + sign * mag))

    def set_af_point(self, x: float, y: float) -> None:
        self.focus_pos = 0.5  # AF lands

    def record(self, action: str) -> dict[str, Any]:
        if action == "start":
            self.recording = True
            return {"recording": True, "clip": None}
        self.recording = False
        self.clip_counter += 1
        return {"recording": False, "clip": f"MVI_{self.clip_counter:04d}.MP4"}

    def list_clips(self) -> list[dict[str, Any]]:
        return [{"name": f"MVI_{i:04d}.MP4", "folder": "/store_00020001/DCIM/100CANON",
                 "size": 48_000_000, "mtime": 0, "index": i} for i in range(1, self.clip_counter + 1)]

    def pull_clip(self, clip: dict[str, Any], dest_dir: str) -> str:
        """Writes a short synthetic MP4 with ffmpeg if available, else a frame JPEG."""
        import os, shutil, subprocess
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, clip["name"])
        fr = self.capture_frame()
        tmp = dest + ".jpg"
        with open(tmp, "wb") as f:
            f.write(fr.jpeg)
        if shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", tmp, "-t", "2",
                            "-r", "30", "-pix_fmt", "yuv420p", "-vf", "scale=960:640", dest], check=False)
            os.remove(tmp)
            if os.path.exists(dest):
                return dest
        os.replace(tmp, dest)
        return dest

    # ---- stills ---------------------------------------------------------
    def still_mode(self) -> bool | None:
        return True                    # the mock has no photo/movie switch to flip

    def set_image_quality(self, raw: bool = False) -> dict[str, Any]:
        self.raw = bool(raw)
        return {"raw": self.raw, "jpeg": True}

    def take_picture(self, dest_dir: str) -> dict[str, Any]:
        """A full-size (24 MP-ish) still of the same synthetic scene, plus a stand-in RAW when asked."""
        import os
        if not self._connected:
            raise CameraError("mock not connected")
        os.makedirs(dest_dir, exist_ok=True)
        self.still_counter += 1
        name = f"IMG_{self.still_counter:04d}.JPG"
        fr = self._render(3000, 2000) if self.settings["orientation"] == "landscape" else self._render(2000, 3000)
        path = os.path.join(dest_dir, name)
        with open(path, "wb") as f:
            f.write(fr.jpeg)
        raw_path = None
        if self.raw:
            raw_path = os.path.join(dest_dir, name[:-4] + ".CR3")
            with open(raw_path, "wb") as f:
                f.write(b"CR3-MOCK" + fr.jpeg[:4096])
        return {"name": name, "path": path, "raw_path": raw_path}

    # ---- synthetic frame ------------------------------------------------
    def _exposure_gain(self) -> float:
        iso = float(self.settings["iso"]) / 400
        sh = _shutter_s(self.settings["shutter"]) / (1 / 60)
        ap = (5.6 / _fnum(self.settings["aperture"])) ** 2
        ev = 2 ** float(self.settings.get("ev_comp") or 0)
        return iso * sh * ap * ev * (self.light_level / 0.8)

    def _wb_shift(self) -> tuple[float, float]:
        cam_k = self.settings["kelvin"] if self.settings["wb_mode"] == "kelvin" else WB_K[self.settings["wb_mode"]]
        if self.settings["wb_mode"] == "auto":
            cam_k = self.scene_cct
        # ratio > 1 means scene is warmer than camera expects -> orange cast
        r = math.log(self.scene_cct / cam_k) if cam_k else 0
        return 1 - 0.35 * r, 1 + 0.35 * r   # (r_gain, b_gain)

    def capture_frame(self) -> Frame:
        if not self._connected:
            raise CameraError("mock not connected")
        W, H = (960, 640) if self.settings["orientation"] == "landscape" else (640, 960)
        return self._render(W, H)

    def _render(self, W: int, H: int) -> Frame:
        y, x = np.mgrid[0:H, 0:W].astype(np.float32)
        img = np.zeros((H, W, 3), np.float32)
        # bench: mid-grey wood with slight gradient
        img[:] = (0.42 + 0.08 * (y / H))[..., None] * np.array([1.0, 0.92, 0.80])
        # bench edge line (for horizon detection) at 72% height, tilted 0.4 deg
        edge = (y > 0.72 * H + (x - W / 2) * math.tan(math.radians(0.4)))
        img[edge] *= 0.55
        if self.subject.startswith("person"):
            cx, cy, rx, ry = W / 2, H * 0.40, W * 0.11, H * 0.22
            face = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 < 1
            img[face] = np.array([0.72, 0.52, 0.42])  # skin
            eyes = (((x - cx - rx * 0.4) ** 2 + (y - cy + ry * 0.2) ** 2) < 60) | (((x - cx + rx * 0.4) ** 2 + (y - cy + ry * 0.2) ** 2) < 60)
            img[eyes] = 0.05
            shirt = (y > cy + ry * 0.9) & (np.abs(x - cx) < rx * 2.2)
            img[shirt] = np.array([0.15, 0.18, 0.30])
        else:
            # dark anodized upper: rounded rect with flutes and a specular rim
            cx, cy, rw, rh = W / 2, H * 0.52, W * 0.30, H * 0.14
            body = (np.abs(x - cx) < rw) & (np.abs(y - cy) < rh)
            flutes = 0.5 + 0.5 * np.sin((y - cy) / rh * math.pi * 6)
            dark = 0.10 + 0.05 * flutes if self.subject == "part_dark" else 0.55 + 0.3 * flutes
            img[body] = (dark[..., None] * np.array([0.95, 0.97, 1.0]))[body]
            rim = body & (y < cy - rh * 0.78)
            img[rim] = 1.3 if self.subject == "part_dark" else 2.2  # specular, clips easily
            if self.subject == "part_bright":
                refl = body & (np.abs(x - cx - rw * 0.3) < rw * 0.12)
                img[refl] = 1.3
        # exposure, WB, picture style
        img *= self._exposure_gain()
        rg, bg = self._wb_shift()
        img[..., 0] *= rg
        img[..., 2] *= bg
        if self.settings["picture_style"] in ("neutral", "faithful"):
            img = img ** 0.92
        # noise rises with ISO
        noise = self.rng.random() * 0 + (float(self.settings["iso"]) / 6400) * 0.08
        img += np.random.default_rng(1).normal(0, noise, img.shape).astype(np.float32)
        img = np.clip(img, 0, 1)
        pil = Image.fromarray((img * 255).astype(np.uint8))
        blur = abs(self.focus_pos - 0.5) * 14
        if blur > 0.3:
            pil = pil.filter(ImageFilter.GaussianBlur(blur))
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=85)
        return Frame(jpeg=buf.getvalue(), width=W, height=H, orientation="landscape" if W >= H else "portrait")
