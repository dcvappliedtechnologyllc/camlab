"""Canon EDSDK backend (USB, Canon's own SDK via ctypes on EDSDK.framework).

Why (bench, Canon EOS R50 V, 2026-10-24): clips recorded under remote control
never appear in the generic PTP object list libgphoto2 uses, but Canon's
EDSDK lists them all, announces each new take with
kEdsObjectEvent_DirItemCreated and downloads it with EdsDownload (18 MB in
0.4 s). The SDK also covers the rest of the camlab surface, so this is the
primary USB backend; gphoto2 stays as the fallback and CCAPI as the Wi-Fi
alternative. The R50 V is on the SDK's confirmed-models list (v13.19.0+).

Layout:
  EdsDriver   thin ctypes layer over the framework. EVERYTHING runs on the
              process MAIN thread via server/mainloop.py (bench 2026-10-25:
              EDSDK on macOS discovers cameras only from the main thread;
              a worker thread never sees one). The main loop pumps
              EdsGetEvent + CFRunLoopRunInMode between jobs. The driver binds
              no delete function at all.
  EdsdkBackend the CameraBackend policy: value tables (edsdk_tables.py,
              generated from Canon's reference), snapping, kelvin implies
              Color Temperature, record = SaveTo camera + Record 4/0 with the
              DirItemCreated clip, list/pull by directory-item reference.
              Every driver call goes through _call(), which refuses any op
              or argument mentioning "delete". A fake driver is injected in
              tests (`EdsdkBackend(driver=...)`).

Framework location: vendor/EDSDK.framework (copied from Canon's
EDSDK_v13.20.21_Macintosh download; headers beside it) or $EDSDK_FRAMEWORK.
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
from pathlib import Path
from typing import Any, Callable

from . import edsdk_tables as T
from .base import CameraBackend, CameraError, CameraInfo, Frame
from .gphoto2_backend import _nearest, _num

log = logging.getLogger("camlab.edsdk")

VENDOR_FRAMEWORK = Path(__file__).resolve().parents[2] / "vendor" / "EDSDK.framework" / "EDSDK"
PTP_DAEMONS = ("ptpcamerad", "PTPCamera")

# property ids
P_PRODUCT_NAME, P_BATTERY, P_SAVE_TO, P_BODY_ID = 0x02, 0x08, 0x0B, 0x15
P_WB, P_COLOR_TEMP, P_PICTURE_STYLE = 0x106, 0x107, 0x114
AE_MANUAL = 3                                    # kEdsAEMode_Manual (EDSDKTypes.h)
P_AE_MODE, P_DRIVE_MODE, P_ISO, P_AF_MODE, P_AV, P_TV, P_EV, P_LENS_NAME = 0x400, 0x401, 0x402, 0x404, 0x405, 0x406, 0x407, 0x40D
P_EVF_OUTPUT, P_EVF_MODE, P_RECORD, P_MOVIE_PARAM = 0x500, 0x501, 0x510, 0x01000423
P_IMAGE_QUALITY, P_FIXED_MOVIE = 0x100, 0x01000422     # stills: JPEG/RAW choice; movie-mode flag (6.9 in the reference)
# commands / events / enums
CMD_DRIVE_LENS_EVF, CMD_DO_EVF_AF, CMD_EXTEND_SHUTDOWN_TIMER = 0x103, 0x102, 0x1
CMD_PRESS_SHUTTER, SHUTTER_COMPLETELY, SHUTTER_OFF = 0x4, 0x3, 0x0
IQ_JPEG, IQ_RAW, IQ_SIZE_LARGE = 1, 4, 0                   # EdsImageType / EdsImageSize bits of ImageQuality
STILL_EXTS = (".jpg", ".jpeg", ".heif", ".hif")
RAW_EXTS = (".cr3", ".cr2", ".crw")
STILL_SETTLE_S = 10.0
EV_OBJ_ALL, EV_DIR_ITEM_CREATED, EV_STATE_ALL, EV_STATE_SHUTDOWN = 0x200, 0x204, 0x300, 0x301
EV_STATE_WILL_SOON_SHUTDOWN = 0x303
KEEP_AWAKE_S, RECONNECT_S, RECONNECT_DISCOVERY_S = 60.0, 10.0, 1.0
SAVE_TO_CAMERA, EVF_PC = 1, 2
FMT_MP4, FMT_MOV = 0xB982, 0x3005
DRIVE_LENS = {"near1": 0x1, "near2": 0x2, "near3": 0x3, "far1": 0x8001, "far2": 0x8002, "far3": 0x8003}
ERR_DEVICE_BUSY, ERR_OBJECT_NOTREADY, ERR_COMM_DISCONNECTED, ERR_DEVICE_NOT_FOUND = 0x81, 0xA102, 0xC1, 0x80
CREATE_ALWAYS, ACCESS_READWRITE = 1, 2
EDS_MAX_NAME = 256
CLIP_EXTS = (".mp4", ".mov")
KELVIN_RANGE = [2500, 10000]
STOP_SETTLE_S = 8.0

PROPS = {"iso": P_ISO, "shutter": P_TV, "aperture": P_AV, "ev_comp": P_EV, "wb_mode": P_WB,
         "kelvin": P_COLOR_TEMP, "picture_style": P_PICTURE_STYLE, "focus_mode": P_AF_MODE}
TABLES: dict[str, dict[int, Any]] = {"iso": T.ISO, "shutter": T.TV, "aperture": T.AV, "ev_comp": T.EV,
                                     "wb_mode": T.WB, "picture_style": T.PICTURE_STYLE, "focus_mode": T.AF_MODE}
READ_KEYS = ["iso", "shutter", "aperture", "ev_comp", "wb_mode", "kelvin", "picture_style", "focus_mode"]
WB_ALIASES = {"kelvin": "Color Temperature", "k": "Color Temperature", "colortemp": "Color Temperature",
              "color_temperature": "Color Temperature", "colour temperature": "Color Temperature",
              "auto": "Auto", "awb white": "AWB White", "white priority": "AWB White"}


@dataclass
class FileEntry:
    folder: str
    name: str
    size: int
    format: int
    ref: int            # driver-side handle id (retained EdsDirectoryItemRef)
    mtime: int | None = None


def _guard(op: str, args: tuple[Any, ...]) -> None:
    if "delete" in op.lower():
        raise CameraError(f"refused: operation {op!r} would delete from the card")
    for a in args:
        if "delete" in str(a).lower():
            raise CameraError(f"refused: argument contains 'delete': {op} {args!r}")


# ======================================================================= driver
class EdsDriver:
    """ctypes over EDSDK.framework; all SDK work on one thread."""

    def __init__(self, framework: str | Path | None = None):
        self.framework = str(framework or os.environ.get("EDSDK_FRAMEWORK") or VENDOR_FRAMEWORK)
        self._loop: Any = None
        self.lib: Any = None
        self.cam: Any = None
        self.created: list[tuple[str, int, int]] = []     # (name, size, ref) from DirItemCreated
        self.shutdown = False          # kEdsStateEvent_Shutdown seen: the session is gone
        self.will_shutdown = False     # kEdsStateEvent_WillSoonShutDown seen: extend the timer now
        self.camera_added = False      # EdsSetCameraAddedHandler fired since last cleared
        self._items: dict[int, Any] = {}                   # ref id -> ctypes ref (retained)
        self._cb_obj = None
        self._cb_state = None
        self._cb_added = None

    @staticmethod
    def available(framework: str | Path | None = None) -> bool:
        p = Path(framework or os.environ.get("EDSDK_FRAMEWORK") or VENDOR_FRAMEWORK)
        return platform.system() == "Darwin" and p.exists()

    # -- main-thread plumbing (see server/mainloop.py)
    def start(self) -> None:
        if self.lib is not None:
            return
        from ..mainloop import LOOP
        self._loop = LOOP
        LOOP.submit(self._init_impl)
        LOOP.register_idle(self._pump_once)

    def _init_impl(self) -> None:
        self._bind()
        # ptpcamerad must be gone BEFORE the SDK initialises: EDSDK enumerates USB at
        # init and never picks up a device that was claimed at that moment.
        self.free_device()
        time.sleep(0.3)
        self._chk(self.L.EdsInitializeSDK(), "EdsInitializeSDK")
        self.lib = self.L
        # hot-plug: a plug-in or wake-up raises camera_added; the backend's idle path reconnects at once
        try:
            self._cb_added = self.ADDED_CB(self._on_added)
            self.L.EdsSetCameraAddedHandler(self._cb_added, None)
        except Exception as e:  # noqa: BLE001
            log.debug("camera-added handler: %s", e)

    def _on_added(self, ctx: Any) -> int:
        log.info("edsdk: camera added (plug-in or wake)")
        self.camera_added = True
        return 0

    def stop(self) -> None:
        if self.lib is None:
            return
        try:
            self.call(self._close_impl)
        except Exception as e:  # noqa: BLE001
            log.debug("edsdk close: %s", e)
        try:
            self._loop.unregister_idle(self._pump_once)
            self.call(lambda: self.L.EdsTerminateSDK())
        except Exception as e:  # noqa: BLE001
            log.debug("edsdk terminate: %s", e)
        self.lib = None

    def call(self, fn: Callable, *args: Any, timeout: float = 120) -> Any:
        if self.lib is None and fn is not self._init_impl:
            raise CameraError("EDSDK driver is not started")
        try:
            return self._loop.submit(fn, *args, timeout=timeout)
        except CameraError:
            raise
        except TimeoutError as e:
            raise CameraError(str(e)) from e
        except RuntimeError as e:
            raise CameraError(f"EDSDK: {e}") from e

    def _pump_once(self, slice_s: float = 0.02) -> None:
        if self.lib is None:
            return
        self.L.EdsGetEvent()
        self.cf.CFRunLoopRunInMode(self.kCFRunLoopDefaultMode, slice_s, False)

    def pump(self, seconds: float) -> None:
        """Run the SDK event pump for a while (on the main thread)."""
        def _p():
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                self._pump_once()
                time.sleep(0.005)
        self.call(_p)

    # -- ctypes binding
    def _bind(self) -> None:
        import ctypes as C
        self.C = C
        if not Path(self.framework).exists():
            raise CameraError(f"EDSDK framework not found at {self.framework}; copy Canon's EDSDK.framework "
                              "into vendor/ or set EDSDK_FRAMEWORK")
        L = C.CDLL(self.framework)
        self.L = L
        cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRunLoopRunInMode.restype = C.c_int32
        cf.CFRunLoopRunInMode.argtypes = [C.c_void_p, C.c_double, C.c_bool]
        self.cf = cf
        self.kCFRunLoopDefaultMode = C.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        u32, i32, u64, vp = C.c_uint32, C.c_int32, C.c_uint64, C.c_void_p

        class DeviceInfo(C.Structure):
            _fields_ = [("szPortName", C.c_char * EDS_MAX_NAME), ("szDeviceDescription", C.c_char * EDS_MAX_NAME),
                        ("deviceSubType", u32), ("reserved", u32)]

        class VolumeInfo(C.Structure):
            _fields_ = [("storageType", u32), ("access", u32), ("maxCapacity", u64), ("freeSpaceInBytes", u64),
                        ("szVolumeLabel", C.c_char * EDS_MAX_NAME)]

        class DirItemInfo(C.Structure):
            _fields_ = [("size", u64), ("isFolder", C.c_int), ("groupID", u32), ("option", u32),
                        ("szFileName", C.c_char * EDS_MAX_NAME), ("format", u32), ("dateTime", u32)]

        class PropertyDesc(C.Structure):
            _fields_ = [("form", i32), ("access", i32), ("numElements", i32), ("propDesc", i32 * 128)]

        self.DeviceInfo, self.VolumeInfo, self.DirItemInfo, self.PropertyDesc = DeviceInfo, VolumeInfo, DirItemInfo, PropertyDesc

        def proto(name, *args):
            f = getattr(L, name); f.restype = u32; f.argtypes = list(args); return f
        proto("EdsInitializeSDK"); proto("EdsTerminateSDK"); proto("EdsGetEvent")
        proto("EdsGetCameraList", C.POINTER(vp))
        proto("EdsGetChildCount", vp, C.POINTER(u32))
        proto("EdsGetChildAtIndex", vp, i32, C.POINTER(vp))
        proto("EdsGetDeviceInfo", vp, C.POINTER(DeviceInfo))
        proto("EdsOpenSession", vp); proto("EdsCloseSession", vp); proto("EdsRelease", vp); proto("EdsRetain", vp)
        proto("EdsSetPropertyData", vp, u32, i32, u32, vp)
        proto("EdsGetPropertyData", vp, u32, i32, u32, vp)
        proto("EdsGetPropertyDesc", vp, u32, C.POINTER(PropertyDesc))
        proto("EdsSendCommand", vp, u32, i32)
        proto("EdsGetVolumeInfo", vp, C.POINTER(VolumeInfo))
        proto("EdsGetDirectoryItemInfo", vp, C.POINTER(DirItemInfo))
        proto("EdsCreateFileStream", C.c_char_p, u32, u32, C.POINTER(vp))
        proto("EdsCreateMemoryStream", u64, C.POINTER(vp))
        proto("EdsCreateEvfImageRef", vp, C.POINTER(vp))
        proto("EdsDownloadEvfImage", vp, vp)
        proto("EdsGetPointer", vp, C.POINTER(vp))
        proto("EdsGetLength", vp, C.POINTER(u64))
        proto("EdsDownload", vp, u64, vp)
        proto("EdsDownloadComplete", vp)
        self.OBJ_CB = C.CFUNCTYPE(u32, u32, vp, vp)
        self.STATE_CB = C.CFUNCTYPE(u32, u32, u32, vp)
        self.ADDED_CB = C.CFUNCTYPE(u32, vp)
        proto("EdsSetObjectEventHandler", vp, u32, self.OBJ_CB, vp)
        proto("EdsSetCameraStateEventHandler", vp, u32, self.STATE_CB, vp)
        proto("EdsSetCameraAddedHandler", self.ADDED_CB, vp)
        # NOTE: EdsDeleteDirectoryItem / EdsFormatVolume are deliberately not bound.

    def _chk(self, err: int, what: str) -> None:
        if err != 0:
            names = {ERR_DEVICE_BUSY: "device busy", ERR_OBJECT_NOTREADY: "object not ready",
                     ERR_COMM_DISCONNECTED: "communication disconnected", ERR_DEVICE_NOT_FOUND: "device not found"}
            raise CameraError(f"EDSDK {what}: error 0x{err:08x} ({names.get(err, 'see EDSDKErrors.h')})")

    # -- operations (run on the SDK thread via call())
    def free_device(self) -> bool:
        if platform.system() != "Darwin":
            return False
        killed = False
        for d in PTP_DAEMONS:
            cp = subprocess.run(["pkill", "-9", "-x", d], capture_output=True)
            killed = killed or cp.returncode == 0
        if killed:
            log.info("freed camera from macOS PTP daemon(s)")
        return killed

    def open(self, discovery_s: float = 30.0, open_retries: int = 4, free: bool = True) -> str:
        return self.call(self._open_impl, discovery_s, open_retries, free)

    def _open_impl(self, discovery_s: float = 30.0, open_retries: int = 4, free: bool = True) -> str:
        C, L = self.C, self.L
        self.camera_added = False
        if free:
            # Killing ptpcamerad re-enumerates the body; the SDK then takes ~8 s to list it again.
            # Reconnects skip this (they only open once probe() already lists the camera).
            self.free_device()
        lst = C.c_void_p(); n = C.c_uint32()
        deadline = time.monotonic() + discovery_s
        attempt = 0
        while True:                            # discovery arrives via EdsGetEvent + the run loop; can take 10 s+
            end = time.monotonic() + 0.5
            while time.monotonic() < end:
                self._pump_once(0.05)
            if lst.value:
                L.EdsRelease(lst); lst = C.c_void_p()
            self._chk(L.EdsGetCameraList(C.byref(lst)), "EdsGetCameraList")
            self._chk(L.EdsGetChildCount(lst, C.byref(n)), "EdsGetChildCount")
            if n.value or time.monotonic() > deadline:
                break
            attempt += 1
            if attempt in (6, 20):
                self.free_device()
        if n.value:
            log.info("edsdk: camera found after %.1fs", discovery_s - (deadline - time.monotonic()))
        if not n.value:
            L.EdsRelease(lst)
            raise CameraError("EDSDK: no camera found. Check USB, the body's power, and that nothing else "
                              "(EOS Utility, Photos, a camlab gphoto2 session) holds it.")
        cam = C.c_void_p()
        self._chk(L.EdsGetChildAtIndex(lst, 0, C.byref(cam)), "EdsGetChildAtIndex")
        L.EdsRelease(lst)
        di = self.DeviceInfo()
        self._chk(L.EdsGetDeviceInfo(cam, C.byref(di)), "EdsGetDeviceInfo")
        model = di.szDeviceDescription.decode(errors="replace")
        self._cb_obj = self.OBJ_CB(self._on_object)
        self._cb_state = self.STATE_CB(self._on_state)
        self._chk(L.EdsSetObjectEventHandler(cam, EV_OBJ_ALL, self._cb_obj, None), "EdsSetObjectEventHandler")
        self._chk(L.EdsSetCameraStateEventHandler(cam, EV_STATE_ALL, self._cb_state, None), "EdsSetCameraStateEventHandler")
        try:   # the movie-mode flag (kEdsPropID_FixedMovie) is only served when asked for before the session opens
            pid = C.c_uint32(P_FIXED_MOVIE)
            L.EdsSetPropertyData(cam, 0x01000000, 0x17AF25B1, 4, C.byref(pid))
        except Exception as e:  # noqa: BLE001
            log.debug("enable FixedMovie: %s", e)
        last: CameraError | None = None
        for attempt in range(max(1, open_retries)):   # right after another process let go, the first open can fail
            try:
                self._chk(L.EdsOpenSession(cam), "EdsOpenSession")
                last = None
                break
            except CameraError as e:
                last = e
                if attempt + 1 >= open_retries:
                    break
                log.warning("edsdk: open session failed (%s); retrying", e)
                self.free_device()
                end = time.monotonic() + 1.0 * (attempt + 1)
                while time.monotonic() < end:
                    self._pump_once(0.05)
        if last:
            L.EdsRelease(cam)
            raise last
        self.cam = cam
        self.shutdown = False
        self.will_shutdown = False
        self._pump_once(0.3)
        return model

    def reinit(self) -> None:
        """After a shutdown/disconnect the SDK answers EdsOpenSession with
        EDS_ERR_SESSION_NOT_OPEN (0x2003) for a fresh camera ref (bench
        2026-10-25 09:23); a terminate + initialise cycle clears it, exactly
        like a fresh process does."""
        def _r():
            self._close_impl()
            try:
                self.L.EdsTerminateSDK()
            except Exception as e:  # noqa: BLE001
                log.debug("terminate: %s", e)
            self.free_device()
            time.sleep(0.3)
            self._chk(self.L.EdsInitializeSDK(), "EdsInitializeSDK")
            self.shutdown = False
            self.will_shutdown = False
            self.camera_added = False
            try:
                self._cb_added = self.ADDED_CB(self._on_added)
                self.L.EdsSetCameraAddedHandler(self._cb_added, None)
            except Exception as e:  # noqa: BLE001
                log.debug("camera-added handler: %s", e)
            log.info("edsdk: SDK re-initialised after disconnect")
        self.call(_r)

    def probe(self) -> bool:
        """Non-blocking: does the SDK currently list a camera? (Discovery itself
        arrives through the event pump that runs between main-loop ticks, so a
        disconnected backend can poll this cheaply instead of blocking.)"""
        def _p():
            C, L = self.C, self.L
            lst = C.c_void_p(); n = C.c_uint32()
            if L.EdsGetCameraList(C.byref(lst)) != 0:
                return False
            try:
                return L.EdsGetChildCount(lst, C.byref(n)) == 0 and n.value > 0
            finally:
                L.EdsRelease(lst)
        return bool(self.call(_p))

    def _close_impl(self) -> None:
        if self.cam is None:
            return
        for ref in list(self._items.values()):
            try:
                self.L.EdsRelease(ref)
            except Exception:  # noqa: BLE001
                pass
        self._items.clear()
        try:
            if not self.shutdown:              # after a shutdown event the session is already gone
                self.L.EdsCloseSession(self.cam)
            self.L.EdsRelease(self.cam)
        except Exception as e:  # noqa: BLE001
            log.debug("close session: %s", e)
        finally:
            self.cam = None

    def extend_shutdown_timer(self) -> None:
        """kEdsCameraCommand_ExtendShutDownTimer: keeps the body from auto-powering off."""
        self.will_shutdown = False
        self.send_command(CMD_EXTEND_SHUTDOWN_TIMER, 0)

    def close(self) -> None:
        self.call(self._close_impl)

    def _on_object(self, event: int, ref: Any, ctx: Any) -> int:
        try:
            if event == EV_DIR_ITEM_CREATED and ref:
                info = self.DirItemInfo()
                if self.L.EdsGetDirectoryItemInfo(ref, self.C.byref(info)) == 0 and not info.isFolder:
                    self.L.EdsRetain(ref)
                    r = self.C.c_void_p(ref)
                    self._items[ref] = r
                    name = info.szFileName.decode(errors="replace")
                    self.created.append((name, int(info.size), int(ref)))
                    log.info("edsdk: new file on card %s (%d bytes)", name, info.size)
        except Exception as e:  # noqa: BLE001
            log.debug("object event handler: %s", e)
        return 0

    def _on_state(self, event: int, data: int, ctx: Any) -> int:
        if event == EV_STATE_SHUTDOWN:
            log.warning("edsdk: camera reported shutdown/disconnect")
            self.shutdown = True
        elif event == EV_STATE_WILL_SOON_SHUTDOWN:
            log.info("edsdk: body about to auto power off (will-soon-shutdown)")
            self.will_shutdown = True
        return 0

    def _cam(self) -> Any:
        if self.cam is None:
            raise CameraError("EDSDK session is not open")
        return self.cam

    def get_u32(self, prop: int) -> int:
        def _g():
            v = self.C.c_uint32()
            self._chk(self.L.EdsGetPropertyData(self._cam(), prop, 0, 4, self.C.byref(v)), f"get 0x{prop:x}")
            return int(v.value)
        return self.call(_g)

    def set_u32(self, prop: int, value: int) -> None:
        def _s():
            v = self.C.c_uint32(int(value) & 0xFFFFFFFF)
            self._chk(self.L.EdsSetPropertyData(self._cam(), prop, 0, 4, self.C.byref(v)), f"set 0x{prop:x}={value}")
        self.call(_s)

    def get_str(self, prop: int) -> str:
        def _g():
            buf = self.C.create_string_buffer(EDS_MAX_NAME)
            err = self.L.EdsGetPropertyData(self._cam(), prop, 0, EDS_MAX_NAME, buf)
            return buf.value.decode(errors="replace") if err == 0 else ""
        return self.call(_g)

    def desc(self, prop: int) -> list[int]:
        def _d():
            d = self.PropertyDesc()
            err = self.L.EdsGetPropertyDesc(self._cam(), prop, self.C.byref(d))
            if err != 0:
                return []
            return [int(d.propDesc[i]) & 0xFFFFFFFF for i in range(max(0, min(d.numElements, 128)))]
        return self.call(_d)

    def send_command(self, cmd: int, param: int) -> None:
        self.call(lambda: self._chk(self.L.EdsSendCommand(self._cam(), cmd, param), f"command 0x{cmd:x}"))

    def evf_frame(self) -> bytes:
        def _f():
            C, L = self.C, self.L
            stream = C.c_void_p(); evf = C.c_void_p()
            self._chk(L.EdsCreateMemoryStream(0, C.byref(stream)), "EdsCreateMemoryStream")
            try:
                self._chk(L.EdsCreateEvfImageRef(stream, C.byref(evf)), "EdsCreateEvfImageRef")
                try:
                    self._chk(L.EdsDownloadEvfImage(self._cam(), evf), "EdsDownloadEvfImage")
                    p = C.c_void_p(); n = C.c_uint64()
                    self._chk(L.EdsGetPointer(stream, C.byref(p)), "EdsGetPointer")
                    self._chk(L.EdsGetLength(stream, C.byref(n)), "EdsGetLength")
                    return C.string_at(p, int(n.value)) if p.value and n.value else b""
                finally:
                    L.EdsRelease(evf)
            finally:
                L.EdsRelease(stream)
        return self.call(_f)

    def list_files(self) -> list[FileEntry]:
        def _l():
            C, L = self.C, self.L
            for ref in list(self._items.values()):      # drop last walk's refs (created ones are re-walked too)
                L.EdsRelease(ref)
            self._items.clear()
            out: list[FileEntry] = []
            nv = C.c_uint32(); self._chk(L.EdsGetChildCount(self._cam(), C.byref(nv)), "volumes")
            for v in range(nv.value):
                vol = C.c_void_p(); self._chk(L.EdsGetChildAtIndex(self._cam(), v, C.byref(vol)), "volume")
                vi = self.VolumeInfo(); self._chk(L.EdsGetVolumeInfo(vol, C.byref(vi)), "EdsGetVolumeInfo")
                label = vi.szVolumeLabel.decode(errors="replace") or f"vol{v}"
                self._walk(vol, f"/{label}", out)
                L.EdsRelease(vol)
            return out
        return self.call(_l)

    def _walk(self, ref: Any, path: str, out: list[FileEntry]) -> None:
        C, L = self.C, self.L
        n = C.c_uint32(); self._chk(L.EdsGetChildCount(ref, C.byref(n)), "EdsGetChildCount")
        for i in range(n.value):
            child = C.c_void_p(); self._chk(L.EdsGetChildAtIndex(ref, i, C.byref(child)), "EdsGetChildAtIndex")
            info = self.DirItemInfo(); self._chk(L.EdsGetDirectoryItemInfo(child, C.byref(info)), "EdsGetDirectoryItemInfo")
            name = info.szFileName.decode(errors="replace")
            if info.isFolder:
                self._walk(child, f"{path}/{name}", out)
                L.EdsRelease(child)
            else:
                self._items[child.value] = child
                out.append(FileEntry(folder=path, name=name, size=int(info.size), format=int(info.format),
                                     ref=int(child.value), mtime=int(info.dateTime) or None))

    def download(self, ref: int, size: int, dest: str) -> None:
        def _d():
            C, L = self.C, self.L
            item = self._items.get(ref)
            if item is None:
                raise CameraError("EDSDK: file handle is stale; list the card again")
            stream = C.c_void_p()
            self._chk(L.EdsCreateFileStream(dest.encode(), CREATE_ALWAYS, ACCESS_READWRITE, C.byref(stream)), "EdsCreateFileStream")
            try:
                self._chk(L.EdsDownload(item, int(size), stream), "EdsDownload")
                self._chk(L.EdsDownloadComplete(item), "EdsDownloadComplete")
            finally:
                L.EdsRelease(stream)
        self.call(_d)

    def take_created(self) -> list[tuple[str, int, int]]:
        def _t():
            out, self.created = self.created, []
            return out
        return self.call(_t)


# ====================================================================== backend
class EdsdkBackend(CameraBackend):
    name = "edsdk"

    def __init__(self, driver: Any | None = None, framework: str | Path | None = None):
        self._driver = driver
        self._framework = framework
        self.model: str | None = None
        self.capabilities: dict[str, Any] = {}
        self.choices: dict[str, list[int]] = {}       # key -> codes the body currently allows
        self.values: dict[str, int] = {}              # key -> last raw code
        self.lens: str | None = None
        self._connected = False
        self._lv_on = False
        self._orientation = "landscape"
        self._recording = False
        self._clips_before: set[str] = set()
        self._clip_refs: dict[str, FileEntry] = {}    # "folder/name" -> entry from the last listing
        self.stop_settle_s = STOP_SETTLE_S
        # keep-awake and self-reconnect (run from the main loop's idle path, see _idle)
        self.keep_awake = True
        self.keep_awake_s = KEEP_AWAKE_S
        self.reconnect_s = RECONNECT_S
        self.open_discovery_s = 30.0          # first open; the app shortens it and lets the idle path finish
        self._last_extend = 0.0
        self._last_reconnect = 0.0
        self._reinit_pending = False
        self._idle_registered = False
        self.last_error: str | None = None

    @property
    def driver(self) -> Any:
        if self._driver is None:
            self._driver = EdsDriver(self._framework)
        return self._driver

    @staticmethod
    def _lost(e: CameraError) -> bool:
        s = str(e)
        return "disconnected" in s or "not open" in s or "device not found" in s

    def _call(self, op: str, *args: Any, **kw: Any) -> Any:
        _guard(op, args)
        if not self._connected and op not in ("open", "start", "stop", "close", "pump", "free_device"):
            raise CameraError("camera off or asleep")
        log.debug("edsdk %s %s", op, args if args else "")
        try:
            return getattr(self.driver, op)(*args, **kw)
        except CameraError as e:
            if self._connected and (self._lost(e) or getattr(self.driver, "shutdown", False)):
                self._mark_lost(str(e))
                raise CameraError("camera off or asleep") from e
            raise

    def _mark_lost(self, why: str) -> None:
        if self._connected:
            log.warning("edsdk: camera session lost (%s); will reconnect in the background", why)
        self._connected = False
        self._lv_on = False
        self._recording = False
        self.last_error = why
        self._last_reconnect = time.monotonic()      # first retry after reconnect_s (camera-added short-cuts it)
        self._reinit_pending = True                  # the SDK must be re-initialised before the next open

    # ----------------------------------------------------------- connection
    def connect(self) -> CameraInfo:
        """Open the session; raises if no camera is found (the caller decides
        whether to keep this backend waiting, see connect_or_wait)."""
        self.driver.start()
        try:
            self.model = self._call("open", self.open_discovery_s)
        except CameraError as e:
            if "no camera found" in str(e):
                self._register_idle()        # keep waiting for the body (connect_or_wait path)
            raise
        self._register_idle()
        self._connected = True
        self._last_extend = time.monotonic()
        self._after_open()
        log.info("edsdk connected: %s", self.model)
        return self.info()

    def connect_or_wait(self) -> bool:
        """Like connect(), but a body that is off/asleep is not an error: the
        backend stays disconnected and reconnects from the idle path (every
        reconnect_s, or at once on a camera-added event). Returns connected."""
        try:
            self.connect()
            return True
        except CameraError as e:
            if "no camera found" not in str(e):
                raise
            self.last_error = str(e)
            log.warning("edsdk: %s -- waiting for the body (reconnect every %.0fs)", e, self.reconnect_s)
            self._last_reconnect = time.monotonic()
            return False

    def _register_idle(self) -> None:
        if self._idle_registered or self._driver is None:
            return
        loop = getattr(self._driver, "_loop", None)
        if loop is not None and hasattr(loop, "register_idle"):
            loop.register_idle(self._idle)
        self._idle_registered = True

    def _idle(self) -> None:
        """Main-loop idle hook: keep-awake while connected, reconnect while not.
        Cheap and non-blocking except for a bounded reconnect attempt."""
        d = self.driver
        now = time.monotonic()
        if self._connected:
            if getattr(d, "shutdown", False):
                self._mark_lost("shutdown event")
                return
            if self.keep_awake and (getattr(d, "will_shutdown", False) or now - self._last_extend >= self.keep_awake_s):
                try:
                    d.extend_shutdown_timer()
                    self._last_extend = now
                    log.info("edsdk: extend shutdown timer (keep-awake)")
                except CameraError as e:
                    log.debug("keep-awake: %s", e)
                    if self._lost(e) or getattr(d, "shutdown", False):
                        self._mark_lost(str(e))
            return
        # Disconnected. First, once per loss, re-initialise the SDK (stale state answers every
        # open with SESSION_NOT_OPEN otherwise). Then discovery arrives through the event pump
        # between ticks, so poll the SDK's camera list cheaply (never block the main thread
        # waiting for it); every reconnect_s also shoo ptpcamerad away in case it grabbed the body.
        if self._reinit_pending:
            self._reinit_pending = False
            try:
                d.reinit()
            except CameraError as e:
                log.warning("edsdk: SDK re-init failed (%s); will retry on the next loss", e)
            self._last_reconnect = now
            return
        if now - self._last_reconnect >= self.reconnect_s:
            self._last_reconnect = now
            try:
                d.free_device()
            except Exception as e:  # noqa: BLE001
                log.debug("free_device: %s", e)
        present = False
        try:
            present = bool(d.probe())
        except CameraError as e:
            log.debug("probe: %s", e)
        if present or getattr(d, "camera_added", False):
            self._try_reconnect()

    def _try_reconnect(self) -> bool:
        d = self.driver
        try:
            d.close()
        except Exception as e:  # noqa: BLE001
            log.debug("reconnect: close: %s", e)
        try:
            # the body is already listed (probe), so no discovery wait and a single open attempt:
            # this runs on the main thread (Cocoa run loop in the app) and must not stall the UI.
            self.model = d.open(RECONNECT_DISCOVERY_S, 1, False)
        except CameraError as e:
            self.last_error = str(e)
            log.info("edsdk reconnect attempt: %s", str(e)[:120])
            return False
        self._connected = True
        self._lv_on = False
        self._last_extend = time.monotonic()
        try:
            self._after_open()
        except CameraError as e:
            self._mark_lost(str(e))
            return False
        log.info("edsdk reconnected: %s", self.model)
        return True

    def _after_open(self) -> None:
        self._call("set_u32", P_SAVE_TO, SAVE_TO_CAMERA)
        self.lens = self._call("get_str", P_LENS_NAME) or None
        self._refresh_choices()
        self.capabilities = self._build_capabilities()

    def _refresh_choices(self) -> None:
        for key in ("iso", "shutter", "aperture", "ev_comp", "wb_mode", "picture_style", "focus_mode"):
            try:
                codes = self._call("desc", PROPS[key])
            except CameraError:
                codes = []
            table = TABLES[key]
            self.choices[key] = [c for c in codes if c in table] if codes else sorted(table)

    def _choice_strings(self, key: str) -> list[str]:
        table = TABLES[key]
        out = [self._norm(key, table[c]) for c in self.choices.get(key, [])]
        return out

    def _build_capabilities(self) -> dict[str, Any]:
        caps: dict[str, Any] = {k: self._choice_strings(k) for k in ("iso", "shutter", "aperture", "picture_style", "wb_mode", "focus_mode")}
        caps.update({"kelvin_range": list(KELVIN_RANGE), "focus_drive": True, "af_point": False,
                     "record": True, "liveview": True, "movie_modes": self._movie_modes(),
                     "raw_jpeg": self._quality_code(raw=True) is not None, "stills": True})
        notes = ["Canon EDSDK over USB: clips recorded remotely are listed and pulled through the SDK."]
        if not caps["movie_modes"]:
            notes.append("Movie size/fps not reported by the body over EDSDK; set them on the camera.")
        caps["notes"] = notes
        return caps

    def _movie_modes(self) -> list[dict[str, Any]]:
        try:
            codes = self._call("desc", P_MOVIE_PARAM)
        except CameraError:
            return []
        modes = []
        for c in codes:
            m = T.MOVIE_PARAM.get(c)
            if not m:
                continue
            w, h, fps, _ = m
            res = "4k" if h >= 2160 else ("1080p" if h >= 1080 else "720p")
            d = {"resolution": res, "fps": int(round(fps))}
            if d not in modes:
                modes.append(d)
        return modes

    def info(self) -> CameraInfo:
        mode = None
        if self._connected:
            try:
                mode = T.AE_MODE.get(self._call("get_u32", P_AE_MODE))
                if mode == "Manual Exposure":
                    mode = "Manual"
            except CameraError:
                pass
        return CameraInfo(model=self.model or "", backend=self.name, connected=self._connected,
                          lens=self.lens, focal_mm=None, mode=mode, capabilities=self.capabilities)

    def set_exposure_mode(self, mode: str) -> bool:
        """Write kEdsPropID_AEMode (0x400) = kEdsAEMode_Manual (3). Most bodies with a top dial
        refuse (the dial owns it); any error -> False and the dial readiness item stays red."""
        if str(mode).lower() not in ("manual", "m"):
            return False
        try:
            self._call("set_u32", P_AE_MODE, AE_MANUAL)
            return self._call("get_u32", P_AE_MODE) == AE_MANUAL
        except Exception as e:  # noqa: BLE001
            log.info("edsdk: AE mode write refused (%s)", e)
            return False

    def disconnect(self) -> None:
        if self._driver is None:
            return
        if self._recording:
            try:
                self.record("stop")
            except Exception as e:  # noqa: BLE001
                log.warning("disconnect while recording: stop failed: %s", e)
        try:
            if self._lv_on:
                cur = self.driver.get_u32(P_EVF_OUTPUT)
                self.driver.set_u32(P_EVF_OUTPUT, cur & ~EVF_PC)
        except Exception as e:  # noqa: BLE001
            log.debug("disconnect: live view off: %s", e)
        try:
            loop = getattr(self._driver, "_loop", None)
            if loop is not None and hasattr(loop, "unregister_idle"):
                loop.unregister_idle(self._idle)
            self._idle_registered = False
            self.driver.stop()
        except Exception as e:  # noqa: BLE001
            log.debug("disconnect: %s", e)
        self._lv_on = False
        self._connected = False

    # ------------------------------------------------------------- settings
    @staticmethod
    def _norm(key: str, v: Any) -> Any:
        if v is None:
            return None
        s = str(v)
        if key == "aperture":
            return "f/" + s
        if key == "kelvin":
            return str(int(_num(s))) if _num(s) is not None else s
        return s

    def _read(self, key: str) -> Any:
        code = self._call("get_u32", PROPS[key])
        self.values[key] = code
        if key == "kelvin":
            return self._norm(key, code)
        return self._norm(key, TABLES[key].get(code))

    def get_settings(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for k in READ_KEYS:
            try:
                res[k] = self._read(k)
            except CameraError as e:
                log.debug("get %s: %s", k, e)
                res[k] = None
        res["movie_resolution"], res["movie_fps"] = self._current_movie_mode()
        res["orientation"] = self._orientation
        return res

    def _current_movie_mode(self) -> tuple[Any, Any]:
        try:
            m = T.MOVIE_PARAM.get(self._call("get_u32", P_MOVIE_PARAM))
        except CameraError:
            return None, None
        if not m:
            return None, None
        w, h, fps, _ = m
        return ("4k" if h >= 2160 else "1080p" if h >= 1080 else "720p"), int(round(fps))

    def _resolve(self, key: str, value: Any) -> int:
        """camlab value -> EDSDK code, snapping numerics to the body's current list."""
        sval = str(value).strip()
        if key == "kelvin":
            n = _num(sval)
            if n is None:
                raise CameraError(f"kelvin: cannot interpret {value!r}")
            n = min(max(n, KELVIN_RANGE[0]), KELVIN_RANGE[1])
            return int(round(n / 100.0) * 100)
        table = TABLES[key]
        codes = self.choices.get(key) or sorted(table)
        if key == "wb_mode":
            sval = WB_ALIASES.get(sval.lower(), sval)
        if key == "aperture":
            sval = re.sub(r"^[fF]/?", "", sval)
        names = {c: str(table[c]) for c in codes if c in table}
        for c, nm in names.items():
            if nm == sval:
                return c
        for c, nm in names.items():
            if nm.lower() == sval.lower():
                return c
        if key in ("iso", "shutter", "aperture", "ev_comp"):
            n = _num(sval)
            if n is not None:
                numeric = {c: nm for c, nm in names.items() if _num(nm) is not None}
                best = _nearest(n, list(numeric.values()), log_scale=key in ("iso", "shutter", "aperture"))
                if best is not None:
                    return next(c for c, nm in numeric.items() if nm == best)
        raise CameraError(f"{key}: {value!r} is not valid; choices: {list(names.values())}")

    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        if changes.get("kelvin") is not None and changes.get("wb_mode") is None:
            changes = {"wb_mode": "Color Temperature", **changes}
        for key, value in changes.items():
            if value is None:
                continue
            if key == "orientation":
                applied[key] = self._orientation
                continue
            if key in ("movie_resolution", "movie_fps"):
                applied[key] = None            # MovieParam write not attempted; set on the body
                continue
            if key not in PROPS:
                raise CameraError(f"unsupported setting: {key}")
            code = self._resolve(key, value)
            self._call("set_u32", PROPS[key], code)
            applied[key] = self._read(key)
        return applied

    # -------------------------------------------------------------- capture
    def _ensure_liveview(self) -> None:
        if self._lv_on:
            return
        try:
            if self._call("get_u32", P_EVF_MODE) != 1:
                self._call("set_u32", P_EVF_MODE, 1)
        except CameraError as e:
            log.debug("evf mode: %s", e)
        cur = self._call("get_u32", P_EVF_OUTPUT)
        if not cur & EVF_PC:
            self._call("set_u32", P_EVF_OUTPUT, cur | EVF_PC)
        self._lv_on = True

    def capture_frame(self) -> Frame:
        self._ensure_liveview()
        data = b""
        last: CameraError | None = None
        for attempt in range(8):
            try:
                data = self._call("evf_frame")
                if data:
                    break
            except CameraError as e:
                last = e
                if "not ready" not in str(e) and "busy" not in str(e):
                    raise
            self._call("pump", 0.15)
        if not data:
            raise CameraError(f"EDSDK live view produced no frame ({last})")
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
        if exif_o in (5, 6, 7, 8):
            self._orientation = "portrait"
        elif exif_o in (1, 2, 3, 4):
            self._orientation = "landscape"
        return Frame(jpeg=bytes(data), width=w, height=h, orientation=self._orientation)

    def focus_drive(self, step: str) -> None:
        s = str(step).strip().lower()
        if s not in DRIVE_LENS:
            raise CameraError(f"focus step must be near1..3 or far1..3, got {step!r}")
        self._ensure_liveview()
        self._call("send_command", CMD_DRIVE_LENS_EVF, DRIVE_LENS[s])

    # --------------------------------------------------------------- stills
    def still_mode(self) -> bool | None:
        """kEdsPropID_FixedMovie: 1 = the body is in movie mode (dial on a movie position, or the SDK's own
        movie switch), 0 = still photos. The dial itself cannot be moved from here (reference 6.9.5)."""
        try:
            return self._call("get_u32", P_FIXED_MOVIE) == 0
        except CameraError:
            return None

    def _quality_codes(self) -> list[int]:
        try:
            return list(self._call("desc", P_IMAGE_QUALITY))
        except CameraError:
            return []

    def _quality_code(self, raw: bool) -> int | None:
        """Pick an ImageQuality code from the body's list: Large JPEG only, or RAW main + Large JPEG second."""
        def parts(c):
            return ((c >> 24) & 0xFF, (c >> 20) & 0xF, (c >> 16) & 0xF, (c >> 8) & 0xF, (c >> 4) & 0xF, c & 0xF)
        best = None
        for c in self._quality_codes():
            size, fmt, q, size2, fmt2, _ = parts(c)
            if raw:
                if fmt == IQ_RAW and fmt2 == IQ_JPEG and size2 == IQ_SIZE_LARGE:
                    if best is None or q > parts(best)[2]:
                        best = c
            else:
                if fmt == IQ_JPEG and size == IQ_SIZE_LARGE and fmt2 in (0, 0xF):
                    if best is None or q > parts(best)[2]:
                        best = c
        return best

    def set_image_quality(self, raw: bool = False) -> dict[str, Any]:
        code = self._quality_code(raw)
        if code is None:
            if raw:
                raise CameraError("this body lists no RAW+JPEG setting; shooting JPEG")
            return {"raw": False, "jpeg": True, "unchanged": True}
        self._call("set_u32", P_IMAGE_QUALITY, code)
        return {"raw": bool(raw), "jpeg": True, "code": code}

    def take_picture(self, dest_dir: str) -> dict[str, Any]:
        """Press the shutter fully, let go, wait for the body to announce the new file(s)
        (DirItemCreated, like a finalized clip) and copy them with EdsDownload. Card untouched otherwise."""
        os.makedirs(dest_dir, exist_ok=True)
        self._call("take_created")
        self._call("set_u32", P_SAVE_TO, SAVE_TO_CAMERA)
        last: CameraError | None = None
        for attempt in range(4):
            try:
                self._call("send_command", CMD_PRESS_SHUTTER, SHUTTER_COMPLETELY)
                last = None
                break
            except CameraError as e:
                last = e
                if "busy" not in str(e):
                    break
                self._call("pump", 0.5 * (attempt + 1))
        try:
            self._call("send_command", CMD_PRESS_SHUTTER, SHUTTER_OFF)
        except CameraError as e:
            log.debug("shutter release: %s", e)
        if last:
            raise CameraError(f"the camera refused the shutter ({last}). Is the top dial on a photo position, "
                              "and can it focus on something?") from last
        deadline = time.monotonic() + STILL_SETTLE_S
        created: list[tuple[str, int, int]] = []
        while time.monotonic() < deadline:
            self._call("pump", 0.3)
            created += self._call("take_created")
            if any(n.lower().endswith(STILL_EXTS) for n, _, _ in created):
                self._call("pump", 0.6)             # the RAW half, if any, lands right after the JPEG
                created += self._call("take_created")
                break
        jpg = next(((n, sz, r) for n, sz, r in created if n.lower().endswith(STILL_EXTS)), None)
        if jpg is None:
            raise CameraError("the shutter fired but the body announced no photo within 10 s")
        name, size, ref = jpg
        path = os.path.join(dest_dir, name)
        self._call("download", ref, size, path)
        raw_path = None
        rawf = next(((n, sz, r) for n, sz, r in created if n.lower().endswith(RAW_EXTS)), None)
        if rawf:
            raw_path = os.path.join(dest_dir, rawf[0])
            self._call("download", rawf[2], rawf[1], raw_path)
        log.info("still taken: %s (%d bytes)%s", name, size, f" + {rawf[0]}" if rawf else "")
        return {"name": name, "path": path, "raw_path": raw_path}

    # --------------------------------------------------------------- record
    def record(self, action: str) -> dict[str, Any]:
        """SaveTo=camera, Record=4 starts, Record=0 stops; the body then
        announces the finalized clip with DirItemCreated (name, size, ref)."""
        if action not in ("start", "stop"):
            raise CameraError(f"record action must be start|stop, got {action!r}")
        clip: str | None = None
        if action == "start":
            self._call("take_created")
            self._clips_before = set(self._clip_refs) if self._clip_refs else {f"{c['folder']}/{c['card_name']}" for c in self.list_clips()}
            self._call("set_u32", P_SAVE_TO, SAVE_TO_CAMERA)
            last: CameraError | None = None
            for attempt in range(4):
                try:
                    self._call("set_u32", P_RECORD, 4)
                    last = None
                    break
                except CameraError as e:
                    if "busy" not in str(e):
                        raise
                    last = e
                    self._call("pump", 0.6 * (attempt + 1))
            if last:
                raise CameraError("record start refused (device busy 4x). Is the body's photo/video switch on video?") from last
            self._recording = True
            self._lv_on = True
            self._call("pump", 0.2)
        else:
            if self._recording:
                self._call("set_u32", P_RECORD, 0)
            self._recording = False
            clip = self._await_clip()
        return {"recording": action == "start", "clip": clip}

    def _await_clip(self) -> str | None:
        deadline = time.monotonic() + self.stop_settle_s
        created: list[tuple[str, int, int]] = []
        while time.monotonic() < deadline:
            self._call("pump", 0.4)
            created += self._call("take_created")
            if created:
                self._call("pump", 0.5)
                created += self._call("take_created")
                break
        for name, size, ref in reversed(created):
            if name.lower().endswith(CLIP_EXTS) or size > 0:
                log.info("new clip (DirItemCreated): %s (%d bytes)", name, size)
                return name
        new = [c for c in self.list_clips() if f"{c['folder']}/{c['card_name']}" not in self._clips_before]
        if new:
            log.info("new clip found by relist: %s", new[-1]["name"])
            return new[-1]["name"]
        log.warning("record stopped but no new clip reported by EDSDK")
        return None

    # ---------------------------------------------------------------- clips
    @staticmethod
    def _is_clip(f: FileEntry) -> bool:
        return f.format in (FMT_MP4, FMT_MOV) or f.name.lower().endswith(CLIP_EXTS)

    def list_clips(self) -> list[dict[str, Any]]:
        files: list[FileEntry] = self._call("list_files")
        self._clip_refs = {}
        clips: list[dict[str, Any]] = []
        per_folder: dict[str, int] = {}
        for f in files:
            per_folder[f.folder] = per_folder.get(f.folder, 0) + 1
            if not self._is_clip(f):
                continue
            key = f"{f.folder}/{f.name}"
            self._clip_refs[key] = f
            clips.append({"name": f.name, "card_name": f.name, "folder": f.folder, "index": per_folder[f.folder],
                          "global_index": len(clips) + 1, "size": f.size,
                          "mime": "video/quicktime" if f.name.lower().endswith(".mov") else "video/mp4",
                          "mtime": f.mtime, "ref": f.ref})
        return clips

    def pull_clip(self, clip: dict[str, Any], dest_dir: str) -> str:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, clip["name"])
        key = f"{clip['folder']}/{clip.get('card_name', clip['name'])}"
        entry = self._clip_refs.get(key)
        if entry is None:
            self.list_clips()
            entry = self._clip_refs.get(key)
        if entry is None:
            raise CameraError(f"clip not on card: {key}")
        self._call("download", entry.ref, entry.size, dest)
        return dest
