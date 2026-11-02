"""Canon CCAPI backend: HTTP/JSON over Wi-Fi (Canon Camera Control API).

Why: the R50 V records fine under PTP remote control but never exports those
clips over PTP (bench, 2026-10-24). CCAPI has its own contents API that lists
and downloads card files, so it is the candidate route for pulling takes, and
it covers the rest of the camlab surface too (live view, exposure, WB, focus
drive, movie record). The USB/gphoto2 backend stays; the controller can switch
between them at runtime.

Endpoints (Canon CCAPI reference, ver100 unless noted):
  GET  /ccapi                                   supported versions per endpoint
  GET  /deviceinformation                       productname, serialnumber, firmwareversion
  GET  /devicestatus/lens                       {name, mount}
  POST /shooting/liveview                       {liveviewsize: small|medium|off, cameradisplay: on|off|keep}
  GET  /shooting/liveview/flip                  one JPEG
  GET  /shooting/settings                       every setting as {value, ability}
  GET/PUT /shooting/settings/{iso|tv|av|exposure|wb|colortemperature|picturestyle|afoperation|moviequality}
  POST /shooting/control/drivefocus             {value: near1..3 | far1..3}
  POST /shooting/liveview/afframeposition       {positionx, positiony}
  POST /shooting/control/moviemode              {action: on|off}
  POST /shooting/control/shutterbutton          {af: bool}   (in movie mode: toggles recording)
  GET  /event/polling?continue=off              changes incl. addedcontents[]
  GET  /contents  -> storages -> directories -> ?type=all&kind=list&page=N  (ver110; ver100 lacks paging)
  GET  <content url>                            file body (?kind=info for size/date)

Everything goes through CCAPIBackend._req(); an `http` callable with the
requests.Session.request signature can be injected for tests. Nothing here
deletes: there is no DELETE anywhere in this module, and _req refuses the
method outright.

Not yet verified on hardware: the body has to be on Wi-Fi with CCAPI
activated (Canon's activation tool). `python -m server.camera.ccapi_backend
probe <host>` reports what it finds.
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
import time
from typing import Any, Callable

from .base import CameraBackend, CameraError, CameraInfo, Frame
from .gphoto2_backend import _nearest, _num

log = logging.getLogger("camlab.ccapi")

SET = {"iso": "iso", "shutter": "tv", "aperture": "av", "ev_comp": "exposure", "wb_mode": "wb",
       "kelvin": "colortemperature", "picture_style": "picturestyle", "focus_mode": "afoperation"}
MOVIE_SETTING = "moviequality"
WB_ALIASES = {"kelvin": "colortemp", "k": "colortemp", "color temperature": "colortemp",
              "color_temperature": "colortemp", "colour temperature": "colortemp",
              "shade": "shade", "cloudy": "cloudy", "tungsten": "tungsten", "daylight": "daylight",
              "auto": "auto", "awb white": "awbwhite"}
CLIP_EXTS = (".mp4", ".mov", ".crm", ".mxf")
KELVIN_RANGE = [2500, 10000]
STOP_SETTLE_S = 6.0
RES_WORDS = {"4k": "4k", "uhd": "4k", "fhd": "1080p", "1080": "1080p", "hd": "720p", "720": "720p", "8k": "8k"}
FPS_WORDS = {"2398": 24, "2400": 24, "2500": 25, "2997": 30, "3000": 30, "5000": 50, "5994": 60,
             "6000": 60, "100": 100, "11988": 120, "12000": 120, "24": 24, "25": 25, "30": 30, "50": 50, "60": 60}


class CCAPIBackend(CameraBackend):
    name = "ccapi"

    def __init__(self, host: str, port: int = 8080, timeout_s: float = 5.0,
                 http: Callable[..., Any] | None = None, liveview_size: str = "small"):
        if not host:
            raise CameraError("ccapi: no host configured (set [camera] ccapi_host in config.toml)")
        self.host, self.port = host, int(port)
        self.root = f"http://{host}:{port}/ccapi"
        self.t = timeout_s
        self.liveview_size = liveview_size
        self._http = http
        self.model = ""
        self.lens: str | None = None
        self.abilities: dict[str, Any] = {}      # ccapi setting name -> ability (list or range dict)
        self.values: dict[str, Any] = {}         # ccapi setting name -> last known raw value
        self.capabilities: dict[str, Any] = {}
        self.versions: dict[str, list[str]] = {}  # endpoint path -> versions supported
        self._connected = False
        self._lv_on = False
        self._orientation = "landscape"
        self._last_size: tuple[int, int] | None = None
        self._recording = False
        self._clips_before: set[str] = set()
        self.stop_settle_s = STOP_SETTLE_S

    # ------------------------------------------------------------------ http
    @property
    def http(self) -> Callable[..., Any]:
        if self._http is None:
            import requests
            s = requests.Session()
            s.headers["Connection"] = "keep-alive"
            self._http = s.request
        return self._http

    def _req(self, method: str, path: str, json: Any = None, stream: bool = False,
             timeout: float | None = None, ok404: bool = False) -> Any:
        """path is absolute (starts with /ccapi) or a full URL. Returns the
        response object (status_code, headers, content, json())."""
        if method.upper() == "DELETE":
            raise CameraError("refused: CCAPI DELETE is never used (nothing deletes from the card)")
        url = path if path.startswith("http") else f"http://{self.host}:{self.port}{path}"
        log.debug("ccapi %s %s %s", method, url, json if json is not None else "")
        try:
            r = self.http(method, url, json=json, stream=stream, timeout=timeout or self.t)
        except Exception as e:  # noqa: BLE001  (requests exceptions; keep requests optional in tests)
            raise CameraError(f"CCAPI unreachable at {url}: {e}") from e
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("message", "")
            except Exception:  # noqa: BLE001
                msg = (r.content or b"")[:200].decode("utf-8", "replace")
            raise CameraError(f"CCAPI {method} {path}: {r.status_code} {msg}".rstrip())
        return r

    def _json(self, method: str, path: str, json: Any = None, ok404: bool = False) -> Any:
        r = self._req(method, path, json=json, ok404=ok404)
        if r is None:
            return None
        try:
            return r.json() if r.content else {}
        except Exception as e:  # noqa: BLE001
            raise CameraError(f"CCAPI {path}: not JSON ({e})") from e

    def _ver(self, endpoint: str, prefer: tuple[str, ...] = ("ver110", "ver100")) -> str:
        """Highest supported version for an endpoint, from GET /ccapi."""
        vs = self.versions.get(endpoint) or []
        for v in prefer:
            if v in vs:
                return v
        return prefer[-1]

    # ----------------------------------------------------------- connection
    def connect(self) -> CameraInfo:
        root = self._json("GET", "/ccapi", ok404=True) or {}
        self.versions = {}
        for ver, eps in root.items():
            if not isinstance(eps, list):
                continue
            for ep in eps:
                p = ep.get("path", "") if isinstance(ep, dict) else str(ep)
                key = p.split(f"/{ver}", 1)[-1] if f"/{ver}" in p else p
                self.versions.setdefault(key, []).append(ver)
        d = self._json("GET", "/ccapi/ver100/deviceinformation")
        self.model = d.get("productname") or "Canon CCAPI"
        lens = self._json("GET", "/ccapi/ver100/devicestatus/lens", ok404=True) or {}
        self.lens = lens.get("name") or None
        self._read_all_settings()
        self._connected = True
        self.capabilities = self._build_capabilities()
        try:
            self._ensure_liveview()
        except CameraError as e:
            log.warning("ccapi: live view not started: %s", e)
        log.info("ccapi connected: %s at %s", self.model, self.host)
        return self.info()

    def _read_all_settings(self) -> None:
        allset = self._json("GET", "/ccapi/ver100/shooting/settings", ok404=True) or {}
        for name, entry in allset.items():
            if isinstance(entry, dict):
                self.values[name] = entry.get("value")
                if "ability" in entry:
                    self.abilities[name] = entry["ability"]
        for name in list(SET.values()) + [MOVIE_SETTING]:
            if name not in self.abilities:
                e = self._json("GET", f"/ccapi/ver100/shooting/settings/{name}", ok404=True)
                if isinstance(e, dict) and "value" in e:
                    self.values[name] = e.get("value")
                    self.abilities[name] = e.get("ability", [])

    def _build_capabilities(self) -> dict[str, Any]:
        caps: dict[str, Any] = {}
        for key, ep in SET.items():
            if key in ("ev_comp", "kelvin"):
                continue
            ab = self.abilities.get(ep, [])
            caps[key] = [self._norm(key, v) for v in ab] if isinstance(ab, list) else []
        kr = self.abilities.get("colortemperature")
        caps["kelvin_range"] = [int(kr["min"]), int(kr["max"])] if isinstance(kr, dict) and "min" in kr else list(KELVIN_RANGE)
        caps.update({"focus_drive": True, "af_point": True, "record": True, "liveview": True})
        mq = self.abilities.get(MOVIE_SETTING, [])
        modes = []
        for q in mq if isinstance(mq, list) else []:
            m = _parse_movie_quality(q)
            if m and m not in modes:
                modes.append(m)
        caps["movie_modes"] = modes or [{"resolution": "1080p", "fps": 30}]
        caps["movie_quality"] = list(mq) if isinstance(mq, list) else []
        notes = ["CCAPI over Wi-Fi; contents API lists the card independently of PTP."]
        if not modes:
            notes.append("moviequality not exposed; set movie size/fps on the body.")
        for k in ("iso", "shutter", "aperture", "picture_style", "wb_mode", "focus_mode"):
            if not caps.get(k):
                notes.append(f"{k}: no ability list from the camera.")
        caps["notes"] = notes
        return caps

    def info(self) -> CameraInfo:
        return CameraInfo(model=self.model, backend=self.name, connected=self._connected, lens=self.lens,
                          focal_mm=None, mode=self.values.get("shootingmodedial") or self.values.get("shootingmode"),
                          capabilities=self.capabilities)

    def disconnect(self) -> None:
        if self._recording:
            try:
                self.record("stop")
            except Exception as e:  # noqa: BLE001
                log.warning("disconnect while recording: stop failed: %s", e)
        try:
            if self._lv_on:
                self._json("POST", "/ccapi/ver100/shooting/liveview", {"liveviewsize": "off", "cameradisplay": "on"})
        except Exception as e:  # noqa: BLE001
            log.debug("disconnect: %s", e)
        self._lv_on = False
        self._connected = False

    # ------------------------------------------------------------- settings
    @staticmethod
    def _norm(key: str, v: Any) -> Any:
        """CCAPI raw value -> camlab vocabulary (strings as the camera names them,
        except aperture which camlab writes as f/5.6)."""
        if v is None:
            return None
        s = str(v)
        if key == "aperture":
            return "f/" + re.sub(r"^[fF]/?", "", s)
        if key == "kelvin":
            n = _num(s)
            return str(int(n)) if n is not None else s
        return s

    def get_settings(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for key, ep in SET.items():
            e = self._json("GET", f"/ccapi/ver100/shooting/settings/{ep}", ok404=True)
            v = e.get("value") if isinstance(e, dict) else None
            self.values[ep] = v
            res[key] = self._norm(key, v)
        mq = self._json("GET", f"/ccapi/ver100/shooting/settings/{MOVIE_SETTING}", ok404=True)
        mode = _parse_movie_quality(mq.get("value")) if isinstance(mq, dict) else None
        res["movie_resolution"] = mode["resolution"] if mode else None
        res["movie_fps"] = mode["fps"] if mode else None
        res["orientation"] = self._orientation
        return res

    def _resolve(self, key: str, value: Any) -> str:
        ep = SET[key]
        ab = self.abilities.get(ep, [])
        sval = str(value).strip()
        if key == "wb_mode":
            sval = WB_ALIASES.get(sval.lower(), sval.lower().replace(" ", ""))
        if key == "aperture":
            sval = re.sub(r"^[fF]/?", "", sval)
        if key == "focus_mode" or key == "picture_style":
            sval = sval.lower().replace(" ", "")
        if isinstance(ab, dict):                    # range: colortemperature {min,max,step}
            n = _num(sval)
            if n is None:
                raise CameraError(f"{key}: cannot interpret {value!r}")
            lo, hi, step = float(ab.get("min", KELVIN_RANGE[0])), float(ab.get("max", KELVIN_RANGE[1])), float(ab.get("step") or 100)
            n = min(max(n, lo), hi)
            n = lo + round((n - lo) / step) * step
            return str(int(n))
        choices = [str(c) for c in ab] if isinstance(ab, list) else []
        if not choices:
            return sval if key != "aperture" else f"f{sval}"
        if key == "aperture":
            raw = [re.sub(r"^[fF]/?", "", c) for c in choices]
            if sval in raw:
                return choices[raw.index(sval)]
            n = _num(sval)
            best = _nearest(n, raw, log_scale=True) if n is not None else None
            if best is None:
                raise CameraError(f"{key}: cannot interpret {value!r}; choices: {choices}")
            return choices[raw.index(best)]
        if sval in choices:
            return sval
        low = [c.lower() for c in choices]
        if sval.lower() in low:
            return choices[low.index(sval.lower())]
        if key in ("iso", "shutter", "ev_comp", "kelvin"):
            n = _num(sval)
            if n is not None:
                best = _nearest(n, choices, log_scale=key in ("iso", "shutter"))
                if best is not None:
                    return best
        raise CameraError(f"{key}: {value!r} is not valid; choices: {choices}")

    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        if changes.get("kelvin") is not None and changes.get("wb_mode") is None:
            changes = {"wb_mode": "colortemp", **changes}      # Canon only honours K in colortemp mode
        movie = {k: changes.get(k) for k in ("movie_resolution", "movie_fps") if changes.get(k) is not None}
        for key, value in changes.items():
            if value is None:
                continue
            if key == "orientation":
                applied[key] = self._orientation
                continue
            if key in ("movie_resolution", "movie_fps"):
                continue
            if key not in SET:
                raise CameraError(f"unsupported setting: {key}")
            ep = SET[key]
            if ep not in self.abilities and ep not in self.values:
                applied[key] = None
                continue
            raw = self._resolve(key, value)
            got = self._json("PUT", f"/ccapi/ver100/shooting/settings/{ep}", {"value": raw})
            cur = got.get("value", raw) if isinstance(got, dict) else raw
            self.values[ep] = cur
            applied[key] = self._norm(key, cur)
        if movie:
            applied.update(self._set_movie_mode(movie.get("movie_resolution"), movie.get("movie_fps")))
        return applied

    def _set_movie_mode(self, resolution: Any, fps: Any) -> dict[str, Any]:
        cur = _parse_movie_quality(self.values.get(MOVIE_SETTING)) or {}
        want_res = str(resolution or cur.get("resolution") or "").lower()
        want_fps = int(fps or cur.get("fps") or 0)
        for q in self.abilities.get(MOVIE_SETTING, []) or []:
            m = _parse_movie_quality(q)
            if m and m["resolution"] == want_res and m["fps"] == want_fps:
                got = self._json("PUT", f"/ccapi/ver100/shooting/settings/{MOVIE_SETTING}", {"value": q})
                self.values[MOVIE_SETTING] = got.get("value", q) if isinstance(got, dict) else q
                return {"movie_resolution": m["resolution"], "movie_fps": m["fps"]}
        return {"movie_resolution": None, "movie_fps": None}

    # -------------------------------------------------------------- capture
    def _ensure_liveview(self) -> None:
        if not self._lv_on:
            self._json("POST", "/ccapi/ver100/shooting/liveview",
                       {"liveviewsize": self.liveview_size, "cameradisplay": "on"})
            self._lv_on = True

    def capture_frame(self) -> Frame:
        self._ensure_liveview()
        data = b""
        for attempt in range(2):
            r = self._req("GET", "/ccapi/ver100/shooting/liveview/flip")
            data = bytes(r.content or b"")
            if data.startswith(b"\xff\xd8"):
                break
            self._lv_on = False          # body dropped live view (sleep, mode change); restart it
            self._ensure_liveview()
            time.sleep(0.2)
        if not data.startswith(b"\xff\xd8"):
            raise CameraError("CCAPI live view returned no JPEG")
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            w, h = im.size
            try:
                exif_o = im.getexif().get(274)
            except Exception:  # noqa: BLE001
                exif_o = None
        except Exception as e:  # noqa: BLE001
            raise CameraError(f"live view frame is not a valid JPEG: {e}") from e
        self._last_size = (w, h)
        if exif_o in (5, 6, 7, 8):
            self._orientation = "portrait"
        elif exif_o in (1, 2, 3, 4):
            self._orientation = "landscape"
        return Frame(jpeg=data, width=w, height=h, orientation=self._orientation)

    def focus_drive(self, step: str) -> None:
        m = re.fullmatch(r"(near|far)([123])", str(step).strip().lower())
        if not m:
            raise CameraError(f"focus step must be near1..3 or far1..3, got {step!r}")
        self._ensure_liveview()
        self._json("POST", "/ccapi/ver100/shooting/control/drivefocus", {"value": f"{m.group(1)}{m.group(2)}"})

    def set_af_point(self, x: float, y: float) -> None:
        self._ensure_liveview()
        w, h = self._last_size or (1024, 576)
        self._json("POST", "/ccapi/ver100/shooting/liveview/afframeposition",
                   {"positionx": int(max(0.0, min(1.0, x)) * w), "positiony": int(max(0.0, min(1.0, y)) * h)})

    # --------------------------------------------------------------- record
    def record(self, action: str) -> dict[str, Any]:
        """Movie mode on, then the shutter button toggles recording. CCAPI
        has no explicit start/stop; we track state locally and confirm new
        files through event polling (addedcontents) and a contents re-list."""
        if action not in ("start", "stop"):
            raise CameraError(f"record action must be start|stop, got {action!r}")
        clip: str | None = None
        if action == "start":
            self._clips_before = {c["url"] for c in self.list_clips()}
            try:
                self._json("POST", "/ccapi/ver100/shooting/control/moviemode", {"action": "on"})
            except CameraError as e:
                log.debug("moviemode on: %s (already in movie mode?)", e)
            self._ensure_liveview()
            self._json("POST", "/ccapi/ver100/shooting/control/shutterbutton", {"af": False})
            self._recording = True
        else:
            if self._recording:
                self._json("POST", "/ccapi/ver100/shooting/control/shutterbutton", {"af": False})
            self._recording = False
            clip = self._await_clip()
        return {"recording": action == "start", "clip": clip}

    def _await_clip(self) -> str | None:
        deadline = time.monotonic() + self.stop_settle_s
        added: list[str] = []
        while time.monotonic() < deadline:
            ev = self._json("GET", "/ccapi/ver100/event/polling?continue=off", ok404=True) or {}
            for url in ev.get("addedcontents", []) or []:
                if url not in added:
                    added.append(url)
            if added:
                break
            time.sleep(0.5)
        if added:
            name = added[-1].rsplit("/", 1)[-1]
            log.info("new clip (event): %s", added[-1])
            return name
        new = [c for c in self.list_clips() if c["url"] not in self._clips_before]
        if new:
            log.info("new clip found by relist: %s", new[-1]["url"])
            return new[-1]["name"]
        log.warning("record stopped but no new clip is visible through CCAPI contents")
        return None

    # ---------------------------------------------------------------- clips
    def _contents_root(self) -> str:
        return f"/ccapi/{self._ver('/contents')}/contents"

    def _list_dir(self, dir_url: str) -> list[str]:
        """All file URLs in a directory (ver110 pages; ver100 returns everything)."""
        urls: list[str] = []
        if "/ver110/" in dir_url:
            page = 1
            while True:
                got = self._json("GET", f"{dir_url}?type=all&kind=list&page={page}", ok404=True) or {}
                batch = got.get("path", []) or []
                urls += [str(u) for u in batch]
                total_pages = got.get("pagenumber")
                if not batch or (isinstance(total_pages, int) and page >= total_pages) or page > 200:
                    break
                page += 1
        else:
            got = self._json("GET", dir_url, ok404=True) or {}
            urls = [str(u) for u in got.get("path", []) or []]
        return urls

    def list_clips(self) -> list[dict[str, Any]]:
        root = self._json("GET", self._contents_root(), ok404=True) or {}
        clips: list[dict[str, Any]] = []
        for storage in root.get("path", []) or []:
            dirs = (self._json("GET", str(storage), ok404=True) or {}).get("path", []) or []
            for d in dirs:
                files = self._list_dir(str(d))
                for i, url in enumerate(files, 1):
                    name = url.rsplit("/", 1)[-1]
                    if not name.lower().endswith(CLIP_EXTS):
                        continue
                    info = self._json("GET", f"{url}?kind=info", ok404=True) or {}
                    clips.append({"name": name, "card_name": name, "folder": str(d), "url": url, "index": i,
                                  "size": int(info.get("filesize") or 0) or None,
                                  "mime": "video/mp4" if name.lower().endswith(".mp4") else "video/quicktime",
                                  "mtime": _parse_date(info.get("lastmodifieddate"))})
        return clips

    def pull_clip(self, clip: dict[str, Any], dest_dir: str) -> str:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, clip["name"])
        r = self._req("GET", clip["url"], stream=True, timeout=600)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
        return dest


# ------------------------------------------------------------------ helpers
def _parse_movie_quality(q: Any) -> dict[str, Any] | None:
    """'fhd_5994_ipb' / '4k_2997_ipb_crop' / 'fhd_30p' -> {resolution, fps} (best effort)."""
    if not q:
        return None
    toks = re.split(r"[_\-\s]+", str(q).lower())
    res = next((RES_WORDS[t] for t in toks if t in RES_WORDS), None)
    fps = None
    for t in toks:
        t2 = t.rstrip("p")
        if t2 in FPS_WORDS:
            fps = FPS_WORDS[t2]
            break
    if res is None or fps is None:
        return None
    return {"resolution": res, "fps": fps}


def _parse_date(s: Any) -> int | None:
    if not s:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return int(parsedate_to_datetime(str(s)).timestamp())
    except Exception:  # noqa: BLE001
        pass
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def probe(host: str, port: int = 8080) -> int:
    """First-contact report: device info, settings, live view, contents (video
    files flagged). Exit 0 if the camera answered and listed its card."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    b = CCAPIBackend(host, port)
    try:
        info = b.connect()
    except CameraError as e:
        print(f"FAIL connect: {e}")
        print("Is the body on the same Wi-Fi, CCAPI activated (Canon's activation tool), and the USB tether off?")
        return 1
    print(f"camera: {info.model}  lens: {info.lens}  mode: {info.mode}")
    print("versions:", {k: v for k, v in b.versions.items() if k in ("/contents", "/shooting/liveview", "/event/polling")})
    for k in ("iso", "shutter", "aperture", "wb_mode", "picture_style", "focus_mode"):
        print(f"  {k}: {info.capabilities.get(k)}")
    print("  movie_quality:", info.capabilities.get("movie_quality"))
    print("settings:", b.get_settings())
    try:
        t0 = time.time()
        fr = b.capture_frame()
        print(f"live view: {fr.width}x{fr.height} {fr.orientation} in {time.time() - t0:.2f}s")
    except CameraError as e:
        print(f"live view FAILED: {e}")
    ok = True
    try:
        clips = b.list_clips()
        print(f"clips on card ({len(clips)}):")
        for c in clips:
            print(f"  {c['url']}  {c['size']}  {c['mtime']}")
        if not clips:
            print("  (none; a body-recorded clip should appear here, PTP-recorded ones are the open question)")
    except CameraError as e:
        print(f"contents FAILED: {e}")
        ok = False
    b.disconnect()
    return 0 if ok else 2


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "probe":
        port = int(sys.argv[3]) if len(sys.argv) > 3 else 8080
        sys.exit(probe(sys.argv[2], port))
    print("usage: python -m server.camera.ccapi_backend probe <host> [port]")
    sys.exit(2)
