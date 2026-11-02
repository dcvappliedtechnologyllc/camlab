"""Canon EOS backend over libgphoto2 (PTP over USB) holding ONE persistent session.

Why a persistent session (bench, Canon EOS R50 V, 2026-10-24): movie recording
triggered over PTP (capturetarget=card, viewfinder=1, movierecordtarget=Card)
only runs while the PTP session that started it stays open; when the gphoto2
CLI process exited the body stopped and the clip never finalized. Per-call CLI
invocations also raced macOS's ptpcamerad on every claim and paid a full
USB handshake per live-view frame.

Approach chosen: the python-gphoto2 bindings (PyPI `gphoto2`, binary wheel
with libgphoto2 2.5.34 bundled, builds/installs cleanly on Python 3.14 arm64).
One `gp.Camera()` lives for the process: `init()` on connect, `exit()` on
disconnect; frames come from `capture_preview()`, settings from
`get_config`/`set_single_config`, clips from `folder_list_files`/`file_get`,
and the record cycle is confirmed with `wait_for_event`. The alternative
(`gphoto2 --shell` as a child process with sentinel parsing) was not needed.

Structure: `Gphoto2Backend` keeps all the camera policy (value snapping,
kelvin implies Color Temperature, auto-Manual, delete guard, MIME-based clip
listing with per-folder indexes) and talks to the device only through a small
`Session` object (`GpSession` here; tests inject a fake). Every session call
goes through `Gphoto2Backend._call(op, ...)`, which refuses any operation
whose name or arguments mention "delete": this tool must never remove files
from the card. `GpSession` deliberately exposes no delete method at all.

All methods are synchronous and are called under the controller's lock on a
worker thread, so libgphoto2 (not thread-safe) sees one call at a time.
"""
from __future__ import annotations

import io
import logging
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from .base import CameraBackend, CameraError, CameraInfo, Frame

log = logging.getLogger("camlab.gphoto2")

MAC_FIX = (
    "Could not claim the USB device. Quit Canon EOS Utility / Image Capture / "
    "Photos / any app that talks to the camera, then on macOS run "
    "`pkill -9 ptpcamerad` (older systems: `killall PTPCamera`) and retry. "
    "Also make sure the camera is on, awake, and in a mode that allows USB control."
)
NOT_FOUND_MSG = (
    "No camera detected by gphoto2. Check the USB cable and that the camera is on and awake. "
    "Active tether cables are directional (boosted end to the Mac). Quit EOS Utility, "
    "Image Capture and Photos, and on macOS run `pkill -9 ptpcamerad` (the daemon grabs the device)."
)
PTP_DAEMONS = ("ptpcamerad", "PTPCamera")   # macOS processes that claim PTP devices

PATHS = {
    "iso": "/main/imgsettings/iso",
    "shutter": "/main/capturesettings/shutterspeed",
    "aperture": "/main/capturesettings/aperture",
    "ev_comp": "/main/capturesettings/exposurecompensation",
    "wb_mode": "/main/imgsettings/whitebalance",
    "kelvin": "/main/imgsettings/colortemperature",
    "picture_style": "/main/capturesettings/picturestyle",
    "focus_mode": "/main/capturesettings/focusmode",
    "drive_mode": "/main/capturesettings/drivemode",
    "lens": "/main/status/lensname",
    "battery": "/main/status/batterylevel",
}
# Candidates tried in order; first one present on the camera wins.
MODE_PATHS = ["/main/capturesettings/autoexposuremode", "/main/status/autoexposuremode"]
MODE_DIAL = "/main/capturesettings/autoexposuremodedial"   # settable on EOS R bodies (R50 V verified)
# Recording destination doubles as the record trigger on EOS bodies: Card = start, None = stop.
MOVIE_TARGET_PATHS = ["/main/settings/movierecordtarget", "/main/capturesettings/movierecordtarget"]
CAPTURE_TARGET = "/main/settings/capturetarget"   # index 1 = Memory card
MOVIE_MODE = "/main/actions/eosmoviemode"       # 1 = switch body to movie mode (EOS R)
VIEWFINDER = "/main/actions/viewfinder"
MOVIE = "/main/actions/movie"                   # legacy DSLR toggle (77D); absent on R50 V
FOCUS_DRIVE = "/main/actions/manualfocusdrive"

NUMERIC_KEYS = {"iso", "shutter", "aperture", "kelvin", "ev_comp"}
ENUM_KEYS = {"wb_mode", "picture_style", "focus_mode", "drive_mode"}
READ_KEYS = ["iso", "shutter", "aperture", "ev_comp", "wb_mode", "kelvin",
             "picture_style", "focus_mode"]
KELVIN_RANGE = [2500, 10000]
MOVIE_MODES = [{"resolution": "1080p", "fps": 60},
               {"resolution": "1080p", "fps": 30},
               {"resolution": "720p", "fps": 60}]
CLIP_EXTS = (".mp4", ".mov")
STOP_SETTLE_S = 6.0        # how long to wait for the body to finalize a clip after stop
CLAIM_ERRORS = ("claim", "could not claim", "usb_claim")


@dataclass
class FileEntry:
    folder: str
    name: str
    size: int
    mime: str
    mtime: int | None


def _guard(op: str, args: tuple[Any, ...]) -> None:
    """Refuse anything that could delete files from the camera."""
    if "delete" in op.lower():
        raise CameraError(f"refused: operation {op!r} would delete from the card")
    for a in args:
        if "delete" in str(a).lower():
            raise CameraError(f"refused: argument contains 'delete': {op} {args!r}")


def parse_config(text: str) -> dict[str, dict[str, Any]]:
    """Parse gphoto2 CLI `--list-all-config` / `--get-config` text into
    {path: {label, type, current, choices[], readonly, bottom, top, step}}.
    GpSession builds the same shape from the widget tree; the parser stays so
    captured CLI dumps can be used as fixtures."""
    out: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("/") and ":" not in line.split("/")[-1] and " " not in line:
            cur = {"label": "", "type": "", "current": None, "choices": [],
                   "readonly": False}
            out[line] = cur
            continue
        if cur is None:
            continue
        if line == "END":
            cur = None
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        val = val.strip()
        if key == "Label":
            cur["label"] = val
        elif key == "Type":
            cur["type"] = val
        elif key == "Current":
            cur["current"] = val
        elif key == "Readonly":
            cur["readonly"] = val not in ("0", "")
        elif key == "Choice":
            m = re.match(r"(\d+)\s+(.*)$", val)
            cur["choices"].append(m.group(2) if m else val)
        elif key == "Bottom":
            cur["bottom"] = _num(val)
        elif key == "Top":
            cur["top"] = _num(val)
        elif key == "Step":
            cur["step"] = _num(val)
    return out


def _num(s: Any) -> float | None:
    """Parse '400', '5.6', 'f/5.6', '1/125', '0.5', '+1/3', '-2' -> float."""
    if s is None:
        return None
    t = str(s).strip().lower().replace("f/", "").replace("f", "").rstrip('"s').strip()
    m = re.fullmatch(r"([+-]?)(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", t)
    if m:
        d = float(m.group(3))
        if d == 0:
            return None
        v = float(m.group(2)) / d
        return -v if m.group(1) == "-" else v
    try:
        return float(t)
    except ValueError:
        return None


def _nearest(value: float, choices: list[str], log_scale: bool) -> str | None:
    import math
    best, bd = None, None
    for c in choices:
        n = _num(c)
        if n is None:
            continue
        if log_scale and n > 0 and value > 0:
            d = abs(math.log(n) - math.log(value))
        else:
            d = abs(n - value)
        if bd is None or d < bd:
            best, bd = c, d
    return best


def _fmt_num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


# ====================================================================== session
class GpSession:
    """The one libgphoto2 session for the process. Methods map 1:1 onto the
    operations the backend needs; there is intentionally no delete here.

    Config entries use the same dict shape as `parse_config` so the backend
    (and test fixtures written as CLI dumps) see one representation."""

    def __init__(self) -> None:
        import gphoto2 as gp   # lazy: tests and the mock path never import it
        self.gp = gp
        self.cam: Any = None
        self._widget_types = {
            gp.GP_WIDGET_TEXT: "TEXT", gp.GP_WIDGET_RANGE: "RANGE", gp.GP_WIDGET_TOGGLE: "TOGGLE",
            gp.GP_WIDGET_RADIO: "RADIO", gp.GP_WIDGET_MENU: "MENU", gp.GP_WIDGET_DATE: "DATE",
            gp.GP_WIDGET_BUTTON: "BUTTON",
        }

    # -- lifecycle
    def free_device(self) -> bool:
        """macOS: ptpcamerad (user-owned) claims every PTP device and ignores
        SIGTERM; a SIGKILL frees it until the next hot-plug. Returns True if
        anything was killed."""
        if platform.system() != "Darwin":
            return False
        killed = False
        for d in PTP_DAEMONS:
            cp = subprocess.run(["pkill", "-9", "-x", d], capture_output=True)
            killed = killed or cp.returncode == 0
        if killed:
            log.info("freed camera from macOS PTP daemon(s)")
            time.sleep(0.3)
        return killed

    def init(self) -> None:
        gp = self.gp
        if self.cam is not None:
            self.exit()
        cam = gp.Camera()
        try:
            cam.init()
        except gp.GPhoto2Error as e:
            raise self._map(e) from e
        self.cam = cam

    def exit(self) -> None:
        if self.cam is None:
            return
        try:
            self.cam.exit()
        except self.gp.GPhoto2Error as e:  # noqa: BLE001
            log.debug("camera exit: %s", e)
        finally:
            self.cam = None

    def model(self) -> str:
        return str(self._cam().get_abilities().model)

    # -- config
    def read_all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        self._walk(self._cfg(), "", out)
        return out

    def read(self, paths: list[str]) -> dict[str, dict[str, Any]]:
        if not paths:
            return {}
        # One tree fetch is one PTP round trip less than N single reads.
        full = self.read_all()
        return {p: full[p] for p in paths if p in full}

    def write(self, items: list[tuple[str, Any]]) -> None:
        """Set each (path, value) in order. Value semantics follow the gphoto2
        CLI: RADIO/MENU match a choice by text, else a bare integer is a
        choice index; TOGGLE wants 0/1; RANGE a float."""
        gp = self.gp
        cfg = self._cfg()
        for path, value in items:
            w = self._find(cfg, path)
            if w is None:
                raise CameraError(f"camera has no config path {path}")
            w.set_value(self._coerce(w, value))
            try:
                self._cam().set_single_config(path.rsplit("/", 1)[-1], w)
            except gp.GPhoto2Error as e:
                raise self._map(e, f"set {path}={value}") from e

    # -- capture / files / events
    def preview(self) -> bytes:
        gp = self.gp
        try:
            f = self._cam().capture_preview()
            return bytes(memoryview(f.get_data_and_size()))
        except gp.GPhoto2Error as e:
            raise self._map(e, "capture_preview") from e

    def list_files(self) -> list[FileEntry]:
        out: list[FileEntry] = []
        try:
            self._list_dir("/", out)
        except self.gp.GPhoto2Error as e:
            raise self._map(e, "list files") from e
        return out

    def file_info(self, folder: str, name: str) -> FileEntry | None:
        try:
            return self._info(folder, name)
        except self.gp.GPhoto2Error as e:
            log.debug("file_info %s/%s: %s", folder, name, e)
            return None

    def get_file(self, folder: str, name: str, dest: str) -> None:
        gp = self.gp
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            target = gp.CameraFile(fd)          # streams straight to disk, no RAM copy
            self._cam().file_get(folder, name, gp.GP_FILE_TYPE_NORMAL, target)
        except gp.GPhoto2Error as e:
            raise self._map(e, f"get {folder}/{name}") from e
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def wait_event(self, timeout_ms: int) -> tuple[str, Any]:
        """Returns (kind, data): kind in timeout|file_added|folder_added|
        capture_complete|unknown; data is (folder, name) for file_added, else
        the raw payload (str for unknown events)."""
        gp = self.gp
        try:
            typ, data = self._cam().wait_for_event(int(timeout_ms))
        except gp.GPhoto2Error as e:
            raise self._map(e, "wait_for_event") from e
        if typ == gp.GP_EVENT_FILE_ADDED:
            return "file_added", (str(data.folder), str(data.name))
        if typ == gp.GP_EVENT_FOLDER_ADDED:
            return "folder_added", (str(data.folder), str(data.name))
        if typ == gp.GP_EVENT_CAPTURE_COMPLETE:
            return "capture_complete", None
        if typ == gp.GP_EVENT_TIMEOUT:
            return "timeout", None
        return "unknown", str(data) if data is not None else ""

    # -- internals
    def _cam(self) -> Any:
        if self.cam is None:
            raise CameraError("camera session is not open")
        return self.cam

    def _cfg(self) -> Any:
        try:
            return self._cam().get_config()
        except self.gp.GPhoto2Error as e:
            raise self._map(e, "get_config") from e

    def _walk(self, w: Any, prefix: str, out: dict[str, dict[str, Any]]) -> None:
        gp = self.gp
        t = w.get_type()
        path = f"{prefix}/{w.get_name()}"
        if t in (gp.GP_WIDGET_WINDOW, gp.GP_WIDGET_SECTION):
            for c in w.get_children():
                self._walk(c, path, out)
            return
        entry: dict[str, Any] = {"label": str(w.get_label()), "type": self._widget_types.get(t, str(t)),
                                 "current": None, "choices": [], "readonly": bool(w.get_readonly())}
        try:
            v = w.get_value()
        except gp.GPhoto2Error:
            v = None
        if t == gp.GP_WIDGET_TOGGLE:
            entry["current"] = str(int(v)) if v is not None else None
        elif t == gp.GP_WIDGET_RANGE:
            entry["current"] = _fmt_num(v) if v is not None else None
            try:
                lo, hi, step = w.get_range()
                entry.update(bottom=float(lo), top=float(hi), step=float(step))
            except gp.GPhoto2Error:
                pass
        else:
            entry["current"] = str(v) if v is not None else None
        if t in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            entry["choices"] = [str(w.get_choice(i)) for i in range(w.count_choices())]
        out[path] = entry

    def _find(self, cfg: Any, path: str) -> Any:
        parts = [p for p in path.split("/") if p]
        w = cfg
        if parts and parts[0] == str(cfg.get_name()):
            parts = parts[1:]
        for name in parts:
            try:
                w = w.get_child_by_name(name)
            except self.gp.GPhoto2Error:
                return None
        return w

    def _coerce(self, w: Any, value: Any) -> Any:
        gp = self.gp
        t = w.get_type()
        s = str(value).strip()
        if t == gp.GP_WIDGET_TOGGLE:
            return int(float(s))
        if t == gp.GP_WIDGET_RANGE:
            return float(s)
        if t == gp.GP_WIDGET_DATE:
            return int(float(s))
        if t in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            choices = [str(w.get_choice(i)) for i in range(w.count_choices())]
            if s in choices:
                return s
            low = [c.lower() for c in choices]
            if s.lower() in low:
                return choices[low.index(s.lower())]
            if re.fullmatch(r"\d+", s) and int(s) < len(choices):
                return choices[int(s)]       # CLI semantics: bare integer = choice index
            raise CameraError(f"{w.get_name()}: {value!r} is not one of {choices}")
        return s

    def _list_dir(self, folder: str, out: list[FileEntry]) -> None:
        cam = self._cam()
        for name, _ in cam.folder_list_files(folder):
            info = self._info(folder, str(name))
            if info:
                out.append(info)
        for sub, _ in cam.folder_list_folders(folder):
            self._list_dir(folder.rstrip("/") + "/" + str(sub), out)

    def _info(self, folder: str, name: str) -> FileEntry | None:
        info = self._cam().file_get_info(folder, name)
        f = info.file
        mime = str(f.type) if f.fields & self.gp.GP_FILE_INFO_TYPE else ""
        size = int(f.size) if f.fields & self.gp.GP_FILE_INFO_SIZE else 0
        mtime = int(f.mtime) if f.fields & self.gp.GP_FILE_INFO_MTIME else None
        return FileEntry(folder=folder, name=name, size=size, mime=mime, mtime=mtime)

    def _map(self, e: Any, what: str = "") -> CameraError:
        gp = self.gp
        code = getattr(e, "code", None)
        msg = gp.gp_result_as_string(code) if code is not None else str(e)
        where = f" ({what})" if what else ""
        if code == gp.GP_ERROR_MODEL_NOT_FOUND:
            return CameraError(NOT_FOUND_MSG)
        if code == gp.GP_ERROR_IO_USB_CLAIM:
            return CameraError(MAC_FIX + f"\n(libgphoto2: {msg})")
        if code == gp.GP_ERROR_CAMERA_BUSY:
            return CameraError(f"PTP Device Busy{where}")
        return CameraError(f"libgphoto2 error {code}{where}: {msg}")


# ====================================================================== backend
class Gphoto2Backend(CameraBackend):
    name = "gphoto2"

    def __init__(self, session: Any | None = None, session_factory: Callable[[], Any] | None = None,
                 timeout_s: float = 8):
        """`session` (or `session_factory`) is the injection point: anything
        with GpSession's method surface. Default builds a real GpSession on
        first use."""
        self._session = session
        self._factory = session_factory or GpSession
        self.timeout_s = timeout_s
        self.model: str | None = None
        self.config: dict[str, dict[str, Any]] = {}
        self.capabilities: dict[str, Any] = {}
        self._lv_on = False
        self._orientation = "landscape"
        self._connected = False
        self._recording = False
        self.stop_settle_s = STOP_SETTLE_S
        self._clips_before: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ core
    @property
    def session(self) -> Any:
        if self._session is None:
            self._session = self._factory()
        return self._session

    def _call(self, op: str, *args: Any, **kw: Any) -> Any:
        """Single funnel for every device operation: delete guard, logging,
        and a one-shot reconnect when the session has dropped."""
        _guard(op, args)
        log.debug("gphoto2 %s %s", op, args if args else "")
        fn = getattr(self.session, op)
        try:
            return fn(*args, **kw)
        except CameraError as e:
            if self._connected and self._session_lost(e) and op not in ("init", "exit", "free_device"):
                log.warning("camera session lost (%s); reopening", e)
                self._open_session()
                return getattr(self.session, op)(*args, **kw)
            raise

    @staticmethod
    def _session_lost(e: CameraError) -> bool:
        s = str(e).lower()
        return "not open" in s or bool(re.search(r"error -(7|52)\b", s)) or "i/o problem" in s

    def _open_session(self) -> None:
        """init() with the macOS claim dance: SIGKILL ptpcamerad and retry once.
        ptpcamerad re-grabs the body within a fraction of a second of our
        exit(), which then reads as 'no camera detected' rather than a claim
        error, so both are retried."""
        try:
            self.session.init()
        except CameraError as e:
            low = str(e).lower()
            if (any(k in low for k in CLAIM_ERRORS) or "no camera detected" in low) and self.session.free_device():
                time.sleep(0.5)
                self.session.init()
            else:
                raise

    def _get_config(self, paths: list[str]) -> dict[str, dict[str, Any]]:
        if not paths:
            return {}
        got = self._call("read", list(paths))
        for p, e in got.items():
            self.config[p] = e
        return got

    def _refresh_config(self) -> None:
        self.config = self._call("read_all")

    def _set(self, items: list[tuple[str, Any]]) -> None:
        self._call("write", [(p, str(v)) for p, v in items])

    def _path(self, key: str) -> str | None:
        """Config path for a key, or None if the camera doesn't expose it."""
        if key in PATHS:
            p = PATHS[key]
            return p if (not self.config or p in self.config) else None
        if key == "mode":
            for p in MODE_PATHS:
                if p in self.config:
                    return p
        return None

    # ----------------------------------------------------------- connection
    def connect(self) -> CameraInfo:
        self._open_session()
        model = self._call("model")
        if not model:
            raise CameraError(NOT_FOUND_MSG)
        self.model = model
        self._refresh_config()
        self._connected = True
        self._ensure_manual()
        self.capabilities = self._build_capabilities()
        log.info("gphoto2 session open: %s", model)
        return self.info()

    def _ensure_manual(self) -> None:
        """In Auto/P the body reports one choice per exposure setting and
        refuses writes. EOS R bodies accept the mode dial over PTP; flip it to
        Manual and re-read the config so the choice lists populate."""
        e = self.config.get(MODE_DIAL)
        if not e or str(e.get("current", "")).lower() == "manual":
            return
        if "Manual" not in e.get("choices", []):
            log.warning("mode dial is %s and not settable over PTP; set the camera to M", e.get("current"))
            return
        try:
            self._set([(MODE_DIAL, "Manual")])
            log.info("exposure mode %s -> Manual (over PTP)", e.get("current"))
            self._refresh_config()
        except CameraError as ex:
            log.warning("could not switch to Manual: %s", ex)

    def _choices(self, key: str) -> list[str]:
        p = self._path(key)
        ch = list(self.config.get(p, {}).get("choices", [])) if p else []
        if key == "aperture":
            ch = [self._fmt_aperture(c) for c in ch]
        return ch

    @staticmethod
    def _fmt_aperture(v: str) -> str:
        v = str(v).strip()
        return v if v.lower().startswith("f/") else f"f/{v}"

    def _build_capabilities(self) -> dict[str, Any]:
        caps: dict[str, Any] = {}
        for k in ("iso", "shutter", "aperture", "picture_style", "wb_mode", "focus_mode"):
            caps[k] = self._choices(k)
        caps.update({
            "kelvin_range": list(KELVIN_RANGE), "focus_drive": True, "af_point": False,
            "record": True, "liveview": True,
            "movie_modes": [dict(m) for m in MOVIE_MODES],
        })
        notes = ["Movie resolution/fps are not exposed over PTP; set them on the camera. "
                 "movie_resolution/movie_fps read as None."]
        if not self._movie_target() and MOVIE not in self.config:
            notes.append("No movie record path found; record will fail.")
            caps["record"] = False
        for k in ("iso", "shutter", "aperture", "picture_style", "wb_mode", "focus_mode"):
            if not caps[k]:
                notes.append(f"{k}: config path missing or no choices reported "
                             "(camera mode dial may restrict it).")
        caps["notes"] = notes
        return caps

    def _val(self, path: str | None) -> str | None:
        if not path:
            return None
        v = self.config.get(path, {}).get("current")
        return v if v not in ("", None) else None

    def info(self) -> CameraInfo:
        return CameraInfo(
            model=self.model or "", backend=self.name, connected=self._connected,
            lens=self._val(self._path("lens")), focal_mm=None,
            mode=self._val(self._path("mode")), capabilities=self.capabilities)

    def disconnect(self) -> None:
        if self._session is not None:
            if self._recording:
                try:
                    self.record("stop")
                except Exception as e:  # noqa: BLE001
                    log.warning("disconnect while recording: stop failed: %s", e)
            try:
                if self._lv_on:
                    self._set([(VIEWFINDER, 0)])
            except Exception as e:  # noqa: BLE001
                log.debug("disconnect: ignoring %s", e)
            try:
                self._session.exit()
            except Exception as e:  # noqa: BLE001
                log.debug("disconnect: exit: %s", e)
        self._lv_on = False
        self._connected = False

    # ------------------------------------------------------------- settings
    def _norm(self, key: str, v: str | None) -> Any:
        if v is None:
            return None
        if key == "aperture":
            return self._fmt_aperture(v)
        if key == "kelvin":
            n = _num(v)
            return str(int(n)) if n is not None else v
        return v

    def get_settings(self) -> dict[str, Any]:
        paths = {k: self._path(k) for k in READ_KEYS}
        present = [p for p in paths.values() if p]
        self._get_config(present)
        res: dict[str, Any] = {}
        for k in READ_KEYS:
            res[k] = self._norm(k, self._val(paths[k]))
        res["movie_resolution"] = None
        res["movie_fps"] = None
        res["orientation"] = self._orientation
        return res

    def _ensure_liveview(self) -> None:
        if not self._lv_on:
            self._set([(VIEWFINDER, 1)])
            self._lv_on = True

    def _resolve(self, key: str, value: Any) -> str:
        """Map a requested value to the raw string the camera expects."""
        path = PATHS[key]
        entry = self.config.get(path, {})
        raw_choices: list[str] = entry.get("choices", [])
        sval = str(value).strip()

        if key in ENUM_KEYS:
            if key == "wb_mode" and sval.lower() in ("kelvin", "k", "color_temperature", "colour_temperature"):
                sval = "Color Temperature"     # preset/mock vocabulary -> Canon's choice name
            if not raw_choices:
                return sval
            for c in raw_choices:
                if c == sval:
                    return c
            for c in raw_choices:
                if c.lower() == sval.lower():
                    return c
            raise CameraError(f"{key}: {sval!r} is not valid; choices: {raw_choices}")

        # numeric keys
        if key == "aperture":
            sval = re.sub(r"^[fF]/?", "", sval)
        if raw_choices:
            if sval in raw_choices:
                return sval
            n = _num(sval)
            if n is None:
                for c in raw_choices:
                    if c.lower() == sval.lower():
                        return c
                raise CameraError(f"{key}: cannot interpret {value!r}; choices: {raw_choices}")
            best = _nearest(n, raw_choices, log_scale=key in ("iso", "aperture", "shutter"))
            if best is None:
                raise CameraError(f"{key}: no numeric choices to snap {value!r} to")
            return best
        # RANGE / free-text (kelvin typically)
        n = _num(sval)
        if n is None:
            raise CameraError(f"{key}: cannot interpret {value!r}")
        lo, hi, step = entry.get("bottom"), entry.get("top"), entry.get("step")
        if key == "kelvin":
            lo = lo if lo is not None else KELVIN_RANGE[0]
            hi = hi if hi is not None else KELVIN_RANGE[1]
            step = step or 100
        if lo is not None and hi is not None:
            n = min(max(n, lo), hi)
            if step:
                n = lo + round((n - lo) / step) * step
        return str(int(n)) if float(n).is_integer() else str(n)

    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        todo: dict[str, tuple[str, str]] = {}   # key -> (path, raw value)
        # Canon only honours colortemperature when whitebalance is "Color Temperature";
        # a kelvin write implies that mode, and it must go first in the batch.
        if changes.get("kelvin") is not None and changes.get("wb_mode") is None:
            wb_choices = self.config.get(PATHS["wb_mode"], {}).get("choices", [])
            if "Color Temperature" in wb_choices:
                changes = {"wb_mode": "Color Temperature", **changes}
        for key, value in changes.items():
            if value is None:
                continue
            if key == "orientation":
                applied[key] = self._orientation   # derived from EXIF, not settable
                continue
            if key in ("movie_resolution", "movie_fps"):
                applied[key] = None                # not exposed over PTP
                continue
            if key not in PATHS or key in ("lens", "battery"):
                raise CameraError(f"unsupported setting: {key}")
            path = self._path(key)
            if path is None:
                applied[key] = None                # camera lacks it; degrade gracefully
                continue
            todo[key] = (path, self._resolve(key, value))
        if todo:
            self._ensure_liveview()
            self._set(list(todo.values()))
            got = self._get_config([p for p, _ in todo.values()])
            for key, (path, raw) in todo.items():
                cur = got.get(path, {}).get("current", raw)
                applied[key] = self._norm(key, cur)
        return applied

    # -------------------------------------------------------------- capture
    def capture_frame(self) -> Frame:
        data = self._call("preview")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise CameraError("capture_preview returned no data")
        # Some bodies prepend noise; trim to the JPEG SOI marker.
        i = data.find(b"\xff\xd8")
        if i > 0:
            data = data[i:]
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
        self._lv_on = True          # capture_preview switches the EOS to live view itself
        if exif_o in (5, 6, 7, 8):
            self._orientation = "portrait"
        elif exif_o in (1, 2, 3, 4):
            self._orientation = "landscape"
        return Frame(jpeg=bytes(data), width=w, height=h, orientation=self._orientation)

    def focus_drive(self, step: str) -> None:
        m = re.fullmatch(r"(near|far)([123])", str(step).strip().lower())
        if not m:
            raise CameraError(f"focus step must be near1..3 or far1..3, got {step!r}")
        self._ensure_liveview()
        self._set([(FOCUS_DRIVE, f"{m.group(1).capitalize()} {m.group(2)}")])

    # --------------------------------------------------------------- record
    def _movie_target(self) -> str | None:
        return next((p for p in MOVIE_TARGET_PATHS if p in self.config), None)

    def record(self, action: str) -> dict[str, Any]:
        """EOS R (R50 V verified 2026-10-24): recording starts only when the
        body's photo/video switch is on video. capturetarget=card, viewfinder=1
        and movierecordtarget=Card go in the SAME open session that then stays
        open for the whole take (closing it stops the body and loses the clip).
        movierecordtarget=None stops. Setting None while idle is refused with
        'PTP Device Busy', which we treat as 'already stopped'. After stop we
        drain the event queue for the FILE_ADDED of the finalized clip."""
        if action not in ("start", "stop"):
            raise CameraError(f"record action must be start|stop, got {action!r}")
        on = action == "start"
        t = self._movie_target()
        clip: str | None = None
        if t:
            if on:
                sw = self.config.get("/main/status/eosmovieswitch", {}).get("current")
                if sw is not None and str(sw) == "0":
                    try:
                        self._set([(MOVIE_MODE, 1)])
                    except CameraError:
                        pass   # errors when already in movie mode
                # Snapshot the card BEFORE rolling: the R50 V refuses file listing
                # while recording (libgphoto2 error -1), so the stop-side diff needs this.
                self._clips_before = self._clip_keys()
                items = []
                if CAPTURE_TARGET in self.config or not self.config:
                    items.append((CAPTURE_TARGET, 1))
                items += [(VIEWFINDER, 1), (t, "Card")]
                last: CameraError | None = None
                for attempt in range(4):
                    try:
                        self._set(items)
                        last = None
                        break
                    except CameraError as e:
                        if "busy" not in str(e).lower():
                            raise
                        last = e
                        time.sleep(0.8 * (attempt + 1))
                if last:
                    raise CameraError("record start refused (PTP Device Busy 4x). Is the body's "
                                      "photo/video switch on video?") from last
                self._lv_on = True
                self._recording = True
                self._drain_events(0.2)
            else:
                was_rolling = True
                try:
                    self._set([(t, "None")])
                except CameraError as e:
                    if "busy" not in str(e).lower():
                        raise
                    log.info("record stop: body reports busy; it was not recording")
                    was_rolling = False
                self._recording = False
                if was_rolling:
                    clip = self._await_clip() or self._find_new_clip()
        elif MOVIE in self.config:   # legacy DSLR toggle (77D)
            self._set([(MOVIE, 1 if on else 0)])
            self._recording = on
        else:
            raise CameraError("this body exposes no movie record control over PTP")
        return {"recording": on, "clip": clip}

    def _drain_events(self, seconds: float) -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            left = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                ev = self._call("wait_event", min(left, 500))
            except CameraError as e:
                log.debug("wait_event: %s", e)
                break
            if ev[0] != "timeout":
                events.append(ev)
        return events

    def _await_clip(self) -> str | None:
        """After stop, the body finalizes the file and announces it as
        FILE_ADDED. Collect for a few seconds; prefer the newest video."""
        added: list[tuple[str, str]] = []
        deadline = time.monotonic() + self.stop_settle_s
        quiet_after = None
        while time.monotonic() < deadline:
            for kind, data in self._drain_events(0.5):
                log.debug("event %s %s", kind, data)
                if kind == "file_added":
                    added.append(data)
                    quiet_after = time.monotonic() + 1.0
            if quiet_after and time.monotonic() > quiet_after:
                break
        for folder, name in reversed(added):
            info = self._call("file_info", folder, name)
            entry = self._clip_entry(info, 0) if info else None
            if entry:
                log.info("new clip on card: %s/%s (%d bytes)", folder, name, entry["size"])
                return entry["name"]
            if info is None and not name.lower().endswith((".jpg", ".cr3", ".cr2")):
                return name     # info unavailable, but it isn't a still
        return None

    def _clip_keys(self) -> set[tuple[str, str]]:
        try:
            return {(c["folder"], c["card_name"]) for c in self.list_clips()}
        except CameraError as e:
            log.debug("clip snapshot failed: %s", e)
            return set()

    def _find_new_clip(self) -> str | None:
        """Bench finding (R50 V, 2026-10-24): PTP-triggered clips do finalize on
        the card, but the body does not announce them and libgphoto2's object
        list may not pick them up within the session that recorded them. Try,
        in order: re-list in the open session; re-init the session and list
        again. Logs which step surfaced the clip so the README can say so."""
        before = getattr(self, "_clips_before", set())
        for step in ("relist", "reinit"):
            if step == "reinit":
                log.info("new clip not in listing; re-opening the PTP session to re-enumerate")
                try:
                    self.session.exit()
                    self.session.free_device()      # macOS re-claims the body the moment we let go
                    self._open_session()
                    self._lv_on = False
                except CameraError as e:
                    log.warning("session re-init failed: %s", e)
                    return None
            new = [c for c in self.list_clips() if (c["folder"], c["card_name"]) not in before]
            if new:
                c = max(new, key=lambda c: (c.get("mtime") or 0, c["card_name"]))
                log.info("new clip found by %s: %s/%s (%d bytes)", step, c["folder"], c["card_name"], c["size"])
                return c["name"]
        log.warning("record stopped but no new clip is visible over PTP; check the card in the body")
        return None

    # ---------------------------------------------------------------- clips
    @staticmethod
    def _clip_entry(f: FileEntry, index: int) -> dict[str, Any] | None:
        mime = f.mime or ""
        name = f.name
        # EOS R bodies store clips without an extension (XFVC/A_0001C001A2, video/mp4):
        # trust the MIME type first, the name second.
        if not (mime.lower().startswith("video/") or name.lower().endswith(CLIP_EXTS)):
            return None
        local = name if name.lower().endswith(CLIP_EXTS) else name + (".mov" if "quicktime" in mime.lower() else ".mp4")
        return {"name": local, "card_name": name, "folder": f.folder, "index": index,
                "size": int(f.size), "mime": mime, "mtime": f.mtime}

    def list_clips(self) -> list[dict[str, Any]]:
        files: list[FileEntry] = self._call("list_files")
        clips: list[dict[str, Any]] = []
        per_folder: dict[str, int] = {}      # 1-based index within folder, as `gphoto2 --get-file` counts
        for f in files:
            per_folder[f.folder] = per_folder.get(f.folder, 0) + 1
            entry = self._clip_entry(f, per_folder[f.folder])
            if entry:
                entry["global_index"] = len(clips) + 1
                clips.append(entry)
        return clips

    def pull_clip(self, clip: dict[str, Any], dest_dir: str) -> str:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, clip["name"])          # local name carries an extension
        self._call("get_file", clip["folder"], clip.get("card_name", clip["name"]), dest)
        return dest
