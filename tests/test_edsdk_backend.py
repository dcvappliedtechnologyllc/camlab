"""EdsdkBackend against a fake driver (the ctypes layer is replaced; the
policy on top is what's tested). Codes follow Canon's tables in
server/camera/edsdk_tables.py."""
import io

import pytest
from PIL import Image

from server.camera.base import CameraError, Frame
from server.camera.edsdk_backend import (CMD_DRIVE_LENS_EVF, CMD_PRESS_SHUTTER, FMT_MP4, P_AV, P_COLOR_TEMP,
                                         P_EVF_OUTPUT, P_FIXED_MOVIE, P_IMAGE_QUALITY, P_ISO, P_RECORD, P_SAVE_TO,
                                         P_TV, P_WB, EdsdkBackend, FileEntry)


def jpeg(size=(96, 64), orient=None):
    im = Image.new("RGB", size, (10, 10, 200)); b = io.BytesIO()
    if orient:
        ex = Image.Exif(); ex[274] = orient; im.save(b, "JPEG", exif=ex)
    else:
        im.save(b, "JPEG")
    return b.getvalue()


class FakeDriver:
    def __init__(self, jpg=None):
        self.calls = []
        self.jpg = jpg or jpeg()
        self.props = {P_ISO: 0x58, P_TV: 0x70, P_AV: 0x30, 0x407: 0x00, P_WB: 0, P_COLOR_TEMP: 5200,
                      0x114: 0x87, 0x404: 0, 0x400: 3, P_EVF_OUTPUT: 1, 0x501: 1, P_SAVE_TO: 2, P_RECORD: 0,
                      0x01000423: 0x530}
        self.descs = {P_ISO: [0, 0x48, 0x50, 0x58, 0x60, 0x68], P_TV: [0x68, 0x6B, 0x70, 0x75], P_AV: [0x28, 0x2B, 0x30, 0x38],
                      P_WB: [0, 1, 3, 9, 23], 0x114: [0x81, 0x84, 0x87], 0x404: [0, 1], 0x407: [0xFD, 0, 3],
                      0x01000423: [0x530, 0x730],
                      # ImageQuality: L JPEG fine, M JPEG fine, RAW only, RAW + L JPEG fine (Canon bit layout)
                      P_IMAGE_QUALITY: [0x00130F0F, 0x01130F0F, 0x0040FF0F, 0x00400013]}
        self.props[P_IMAGE_QUALITY] = 0x00130F0F
        self.props[P_FIXED_MOVIE] = 1              # the bench body sits on a movie position
        self.files = [FileEntry("/SD1/DCIM/100CANON", "IMG_0001.JPG", 4852768, 0x3801, 11),
                      FileEntry("/SD1/XFVC/REEL_0001", "A_0001C001A261024_153553JY_CANON.MP4", 30704897, FMT_MP4, 12)]
        self.created = []
        self.started = self.opened = False
        self.shutdown = False          # kEdsStateEvent_Shutdown seen
        self.will_shutdown = False     # kEdsStateEvent_WillSoonShutDown seen
        self.camera_added = False      # EdsSetCameraAddedHandler fired
        self.no_camera = False         # make open() fail like an asleep body
        self.commands = []
        self.opens = []                # discovery_s passed to each open()

    def start(self): self.calls.append(("start",)); self.started = True
    def stop(self): self.calls.append(("stop",)); self.started = False; self.opened = False
    def open(self, discovery_s=30.0, open_retries=4, free=True):
        self.calls.append(("open",)); self.opens.append(discovery_s)
        if self.no_camera:
            raise CameraError("EDSDK: no camera found. Check USB, the body's power ...")
        self.opened = True; self.shutdown = False; self.will_shutdown = False; self.camera_added = False
        return "Canon EOS R50 V"
    def close(self): self.calls.append(("close",)); self.opened = False
    def free_device(self): return False
    def probe(self): self.calls.append(("probe",)); return not self.no_camera
    def reinit(self):
        self.calls.append(("reinit",)); self.opened = False; self.shutdown = False; self.will_shutdown = False
    def extend_shutdown_timer(self):
        if self.shutdown: raise CameraError("EDSDK command 0x1: error 0x000000c1 (communication disconnected)")
        self.will_shutdown = False; self.commands.append((0x1, 0))
    def get_u32(self, p): self.calls.append(("get", p)); return self.props[p]
    def set_u32(self, p, v):
        self.calls.append(("set", p, v)); self.props[p] = v
        if p == P_RECORD and v == 0:
            new = FileEntry("/SD1/XFVC/REEL_0001", "A_0001C015A261024_175221IY_CANON.MP4", 18451154, FMT_MP4, 99)
            self.files.append(new); self.created.append((new.name, new.size, new.ref))
    def get_str(self, p): return "RF-S18-45mm F4.5-6.3 IS STM"
    def desc(self, p): return list(self.descs.get(p, []))
    def send_command(self, c, v):
        self.commands.append((c, v))
        if c == CMD_PRESS_SHUTTER and v == 3:          # full press: the body writes a JPEG (and a CR3 in RAW+JPEG)
            n = 100 + len(self.commands)
            jpg = FileEntry("/SD1/DCIM/100CANON", f"IMG_{n:04d}.JPG", 9_000_000, 0x3801, 500 + n)
            self.files.append(jpg); self.created.append((jpg.name, jpg.size, jpg.ref))
            if ((self.props.get(P_IMAGE_QUALITY, 0) >> 20) & 0xF) == 4:
                raw = FileEntry("/SD1/DCIM/100CANON", f"IMG_{n:04d}.CR3", 30_000_000, 0xB108, 700 + n)
                self.files.append(raw); self.created.append((raw.name, raw.size, raw.ref))
    def evf_frame(self): self.calls.append(("evf",)); return self.jpg
    def pump(self, s): self.calls.append(("pump", s))
    def list_files(self): self.calls.append(("list",)); return list(self.files)
    def download(self, ref, size, dest):
        self.calls.append(("download", ref, size, dest))
        with open(dest, "wb") as f: f.write(b"\0" * 16)
    def take_created(self):
        out, self.created = self.created, []
        return out


@pytest.fixture
def cam():
    f = FakeDriver()
    return EdsdkBackend(driver=f), f


def test_connect(cam):
    b, f = cam
    info = b.connect()
    assert info.model == "Canon EOS R50 V" and info.lens.startswith("RF-S") and info.mode == "Manual"
    assert info.capabilities["iso"] == ["Auto", "100", "200", "400", "800", "1600"]
    assert info.capabilities["shutter"] == ["1/60", "1/80", "1/125", "1/200"]
    assert info.capabilities["aperture"] == ["f/4", "f/4.5", "f/5.6", "f/8"]
    assert info.capabilities["wb_mode"] == ["Auto", "Daylight", "Tungsten", "Color Temperature", "AWB White"]
    assert info.capabilities["picture_style"] == ["Standard", "Neutral", "Auto"]
    assert {"resolution": "1080p", "fps": 30} in info.capabilities["movie_modes"]
    assert ("set", P_SAVE_TO, 1) in f.calls          # SaveTo = camera on connect


def test_get_settings(cam):
    b, f = cam
    b.connect()
    s = b.get_settings()
    assert (s["iso"], s["shutter"], s["aperture"], s["kelvin"], s["wb_mode"]) == ("400", "1/125", "f/5.6", "5200", "Auto")
    assert (s["movie_resolution"], s["movie_fps"]) == ("1080p", 30)


def test_set_snaps_to_codes(cam):
    b, f = cam
    b.connect()
    out = b.set_settings({"aperture": "f/5.7", "iso": 450, "shutter": "1/100"})
    assert out == {"aperture": "f/5.6", "iso": "400", "shutter": "1/80"}
    assert f.props[P_AV] == 0x30 and f.props[P_ISO] == 0x58 and f.props[P_TV] == 0x6B
    assert b.set_settings({"wb_mode": "daylight"}) == {"wb_mode": "Daylight"}
    with pytest.raises(CameraError):
        b.set_settings({"wb_mode": "Moonlight"})
    with pytest.raises(CameraError):
        b.set_settings({"lens": "x"})


def test_kelvin_implies_color_temperature(cam):
    b, f = cam
    b.connect()
    out = b.set_settings({"kelvin": 4321})
    assert out == {"wb_mode": "Color Temperature", "kelvin": "4300"}
    sets = [c for c in f.calls if c[0] == "set" and c[1] in (P_WB, P_COLOR_TEMP)]
    assert sets[-2] == ("set", P_WB, 9) and sets[-1] == ("set", P_COLOR_TEMP, 4300)


def test_capture_turns_on_pc_liveview(cam):
    b, f = cam
    b.connect()
    fr = b.capture_frame()
    assert isinstance(fr, Frame) and (fr.width, fr.height) == (96, 64)
    assert f.props[P_EVF_OUTPUT] & 2
    n = len(f.calls)
    b.capture_frame()
    assert not any(c[0] == "set" for c in f.calls[n:])     # live view stays on


def test_capture_portrait_exif():
    f = FakeDriver(jpeg((64, 96), orient=6))
    b = EdsdkBackend(driver=f); b.connect()
    assert b.capture_frame().orientation == "portrait"


def test_focus_drive(cam):
    b, f = cam
    b.connect()
    b.focus_drive("far2")
    assert f.commands[-1] == (CMD_DRIVE_LENS_EVF, 0x8002)
    with pytest.raises(CameraError):
        b.focus_drive("near9")


def test_record_cycle_lists_and_pulls(cam, tmp_path):
    b, f = cam
    b.connect()
    b.stop_settle_s = 0.5
    assert b.record("start") == {"recording": True, "clip": None}
    assert f.props[P_RECORD] == 4
    res = b.record("stop")
    assert f.props[P_RECORD] == 0
    assert res == {"recording": False, "clip": "A_0001C015A261024_175221IY_CANON.MP4"}
    clips = b.list_clips()
    assert [c["name"] for c in clips] == ["A_0001C001A261024_153553JY_CANON.MP4", "A_0001C015A261024_175221IY_CANON.MP4"]
    assert clips[1]["folder"] == "/SD1/XFVC/REEL_0001" and clips[1]["size"] == 18451154 and clips[1]["index"] == 2
    dest = b.pull_clip(clips[1], str(tmp_path))
    assert dest.endswith("A_0001C015A261024_175221IY_CANON.MP4")
    assert f.calls[-1][:3] == ("download", 99, 18451154)
    # pulling a clip described by name/folder only (e.g. from the index) re-lists to find its handle
    b._clip_refs = {}
    b.pull_clip({"name": "A_0001C001A261024_153553JY_CANON.MP4", "folder": "/SD1/XFVC/REEL_0001"}, str(tmp_path))
    assert f.calls[-1][:3] == ("download", 12, 30704897)


def test_stop_without_event_relists(cam):
    b, f = cam
    b.connect()
    b.stop_settle_s = 0.2
    b.record("start")
    real = f.set_u32
    def quiet(p, v):
        f.calls.append(("set", p, v)); f.props[p] = v
        if p == P_RECORD and v == 0:
            f.files.append(FileEntry("/SD1/XFVC/REEL_0001", "A_0001C016A261024_180000AA_CANON.MP4", 100, FMT_MP4, 77))
    f.set_u32 = quiet
    assert b.record("stop")["clip"] == "A_0001C016A261024_180000AA_CANON.MP4"


def test_delete_refused_and_disconnect(cam):
    b, f = cam
    b.connect()
    with pytest.raises(CameraError, match="refused"):
        b._call("delete_directory_item", 12)
    with pytest.raises(CameraError, match="refused"):
        b._call("send_command", "EdsDeleteDirectoryItem")
    from server.camera.edsdk_backend import EdsDriver
    assert not [m for m in dir(EdsDriver) if not m.startswith("__") and ("delete" in m.lower() or "format" in m.lower())]
    b.disconnect()
    assert f.calls[-1] == ("stop",) and not b.info().connected


def test_disconnect_error_marks_lost_then_idle_reconnects(cam):
    b, f = cam
    b.connect()
    real = f.evf_frame
    def flaky():
        raise CameraError("EDSDK EdsDownloadEvfImage: error 0x000000c1 (communication disconnected)")
    f.evf_frame = flaky
    with pytest.raises(CameraError, match="camera off or asleep"):
        b.capture_frame()
    assert not b.info().connected
    n_evf = sum(1 for c in f.calls if c[0] == "evf")
    with pytest.raises(CameraError, match="camera off or asleep"):
        b.capture_frame()                       # no SDK call while disconnected
    assert sum(1 for c in f.calls if c[0] == "evf") == n_evf
    f.evf_frame = real
    b._idle()                                   # first idle after a loss: SDK re-init only
    assert f.calls[-1] == ("reinit",)
    f.no_camera = True                          # body not listed yet: probe says no, nothing opens
    b._idle()
    assert sum(1 for c in f.calls if c[0] == "open") == 1 and ("probe",) in f.calls
    f.no_camera = False                         # listed again: open at once, no discovery wait
    b._idle()
    assert sum(1 for c in f.calls if c[0] == "open") == 2 and f.opens[-1] == 1.0
    assert b.info().connected and b.capture_frame().width == 96


def test_keep_awake_extends_timer(cam):
    b, f = cam
    b.connect()
    b._idle()
    assert f.commands == []                     # just connected: nothing to extend yet
    b._last_extend = 0.0
    b._idle()
    assert f.commands[-1] == (0x1, 0)           # kEdsCameraCommand_ExtendShutDownTimer after keep_awake_s
    n = len(f.commands)
    f.will_shutdown = True                      # body says it is about to power off
    b._idle()
    assert len(f.commands) == n + 1 and not f.will_shutdown
    b.keep_awake = False
    b._last_extend = 0.0
    b._idle()
    assert len(f.commands) == n + 1


def test_shutdown_event_then_camera_added_reconnects_at_once(cam):
    b, f = cam
    b.connect()
    f.shutdown = True                           # kEdsStateEvent_Shutdown arrived (body powered off)
    b._idle()
    assert not b.info().connected
    with pytest.raises(CameraError, match="camera off or asleep"):
        b.set_settings({"iso": 400})
    assert b.get_settings()["iso"] is None      # reads degrade to None instead of raising
    b._idle()                                   # re-init tick
    assert ("reinit",) in f.calls
    b._last_reconnect = 1e12                    # poll window not due...
    f.camera_added = True                       # ...but the body just came back (hot-plug / wake)
    b._idle()
    assert b.info().connected and sum(1 for c in f.calls if c[0] == "open") == 2


def test_connect_or_wait_when_body_asleep(cam):
    b, f = cam
    f.no_camera = True
    assert b.connect_or_wait() is False
    assert not b.info().connected and "no camera found" in (b.last_error or "")
    with pytest.raises(CameraError, match="camera off or asleep"):
        b.capture_frame()
    b._idle()
    assert not b.info().connected              # still absent: probe says no
    f.no_camera = False
    b._idle()
    assert b.info().connected and b.info().model == "Canon EOS R50 V"
    f.no_camera = True
    with pytest.raises(CameraError, match="no camera found"):
        EdsdkBackend(driver=FakeDriver.__new__(FakeDriver)) if False else b.connect()   # plain connect still raises


def test_still_mode_reads_the_movie_flag(cam):
    b, f = cam
    b.connect()
    assert b.still_mode() is False
    f.props[P_FIXED_MOVIE] = 0
    assert b.still_mode() is True
    assert b.info().capabilities["raw_jpeg"] is True


def test_take_picture_downloads_the_jpeg(cam, tmp_path):
    b, f = cam
    b.connect()
    f.props[P_FIXED_MOVIE] = 0
    res = b.take_picture(str(tmp_path))
    assert res["name"].startswith("IMG_") and res["name"].endswith(".JPG") and res["raw_path"] is None
    assert (CMD_PRESS_SHUTTER, 3) in f.commands and f.commands[-1] == (CMD_PRESS_SHUTTER, 0)
    assert any(c[0] == "download" and c[3] == res["path"] for c in f.calls)
    assert f.props[P_SAVE_TO] == 1


def test_take_picture_raw_plus_jpeg(cam, tmp_path):
    b, f = cam
    b.connect()
    b.set_image_quality(raw=True)
    assert f.props[P_IMAGE_QUALITY] == 0x00400013
    res = b.take_picture(str(tmp_path))
    assert res["raw_path"].endswith(".CR3") and res["path"].endswith(".JPG")
    b.set_image_quality(raw=False)
    assert f.props[P_IMAGE_QUALITY] == 0x00130F0F
    assert "delete" not in " ".join(str(c) for c in f.calls).lower()
