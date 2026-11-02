"""Gphoto2Backend against a fake persistent session. The fixtures are gphoto2
CLI config dumps (parse_config turns them into the dict shape GpSession
builds from the widget tree), so captured `--list-all-config` output can be
pasted in as-is."""
import copy
import io

import pytest
from PIL import Image

from server.camera.base import CameraError, Frame
from server.camera.gphoto2_backend import FileEntry, Gphoto2Backend, parse_config

CFG_ISO = """/main/imgsettings/iso
Label: ISO Speed
Readonly: 0
Type: RADIO
Current: 100
Choice: 0 Auto
Choice: 1 100
Choice: 2 200
Choice: 3 400
Choice: 4 800
Choice: 5 1600
END
"""
CFG_SHUTTER = """/main/capturesettings/shutterspeed
Label: Shutter Speed
Readonly: 0
Type: RADIO
Current: 1/60
Choice: 0 1/125
Choice: 1 1/60
Choice: 2 1/30
Choice: 3 0.5
END
"""
CFG_AP = """/main/capturesettings/aperture
Label: Aperture
Readonly: 0
Type: RADIO
Current: 2.8
Choice: 0 2.8
Choice: 1 4
Choice: 2 5.6
Choice: 3 8
END
"""
CFG_KELVIN = """/main/imgsettings/colortemperature
Label: Color Temperature
Readonly: 0
Type: RADIO
Current: 5200
Choice: 0 2500
Choice: 1 4300
Choice: 2 5200
Choice: 3 6500
END
"""
CFG_WB = """/main/imgsettings/whitebalance
Label: WhiteBalance
Readonly: 0
Type: RADIO
Current: Auto
Choice: 0 Auto
Choice: 1 Daylight
Choice: 2 Tungsten
Choice: 3 Color Temperature
END
"""
CFG_LENS = """/main/status/lensname
Label: Lens Name
Readonly: 1
Type: TEXT
Current: Sigma 17-50mm
END
"""
CFG_MOVIE = """/main/settings/movierecordtarget
Label: Recording Destination
Readonly: 0
Type: RADIO
Current: None
Choice: 0 Card
Choice: 1 None
Choice: 2 SDRAM
END
/main/settings/capturetarget
Label: Capture Target
Readonly: 0
Type: RADIO
Current: Internal RAM
Choice: 0 Internal RAM
Choice: 1 Memory card
END
/main/actions/viewfinder
Label: Canon EOS Viewfinder
Readonly: 0
Type: TOGGLE
Current: 0
END
/main/actions/manualfocusdrive
Label: Drive Canon DSLR Manual focus
Readonly: 0
Type: RADIO
Current: None
Choice: 0 Near 1
Choice: 1 Near 2
Choice: 2 Near 3
Choice: 3 None
Choice: 4 Far 1
Choice: 5 Far 2
Choice: 6 Far 3
END
/main/actions/eosmoviemode
Label: Movie Mode
Readonly: 0
Type: TOGGLE
Current: 0
END
"""
def CFG_DIAL(cur="Manual"):
    return f"""/main/capturesettings/autoexposuremodedial
Label: Canon Auto Exposure Mode Dial
Readonly: 0
Type: RADIO
Current: {cur}
Choice: 0 P
Choice: 1 Manual
Choice: 2 AV
Choice: 3 Auto
END
"""
LIST_ALL = CFG_ISO + CFG_SHUTTER + CFG_AP + CFG_WB + CFG_KELVIN + CFG_LENS + CFG_MOVIE + CFG_DIAL()
LIST_ALL_AUTO = CFG_ISO + CFG_SHUTTER + CFG_AP + CFG_WB + CFG_KELVIN + CFG_LENS + CFG_MOVIE + CFG_DIAL("Auto")
FILES = [
    FileEntry("/store_00020001/DCIM/100CANON", "IMG_0001.JPG", 3500 * 1024, "image/jpeg", 1700000000),
    FileEntry("/store_00020001/DCIM/100CANON", "MVI_0002.MP4", 20480 * 1024, "video/mp4", 1700000100),
    # EOS R style: extensionless clip under XFVC, found by MIME type
    FileEntry("/store_00020001/DCIM/XFVC", "A_0001C001A2", 90 * 1024 * 1024, "video/mp4", 1700000200),
]


def jpeg(size=(96, 64), orient=None):
    im = Image.new("RGB", size, (200, 10, 10))
    b = io.BytesIO()
    if orient:
        ex = Image.Exif()
        ex[274] = orient
        im.save(b, "JPEG", exif=ex)
    else:
        im.save(b, "JPEG")
    return b.getvalue()


class FakeSession:
    """Stands in for GpSession: one open 'PTP session' whose state persists
    across calls. Records every operation in `calls` as (op, *args)."""

    def __init__(self, jpg=None, list_all=None, model="Canon EOS 77D", files=None):
        self.calls = []
        self.jpg = jpg or jpeg()
        self.state = {}             # path -> value written
        self.cfg_text = list_all or LIST_ALL
        self._model = model
        self.files = list(files if files is not None else FILES)
        self.events = []            # queued (kind, data) for wait_event
        self.open = False
        self.init_errors = []       # CameraErrors to raise from init(), in order
        self.freed = 0

    # lifecycle
    def init(self):
        self.calls.append(("init",))
        if self.init_errors:
            raise self.init_errors.pop(0)
        self.open = True

    def exit(self):
        self.calls.append(("exit",))
        self.open = False

    def free_device(self):
        self.freed += 1
        return False

    def model(self):
        self.calls.append(("model",))
        return self._model

    # config
    def _config(self):
        text = self.cfg_text
        if self.state.get("/main/capturesettings/autoexposuremodedial") == "Manual":
            text = LIST_ALL      # once Manual, the body reports full choice lists
        cfg = parse_config(text)
        for p, v in self.state.items():
            if p in cfg:
                cfg[p]["current"] = v
        return cfg

    def read_all(self):
        self.calls.append(("read_all",))
        return self._config()

    def read(self, paths):
        self.calls.append(("read", list(paths)))
        cfg = self._config()
        return {p: copy.deepcopy(cfg[p]) for p in paths if p in cfg}

    def write(self, items):
        self.calls.append(("write", list(items)))
        for p, v in items:
            self.state[p] = v

    # capture / files / events
    def preview(self):
        self.calls.append(("preview",))
        return self.jpg

    def list_files(self):
        self.calls.append(("list_files",))
        return list(self.files)

    def file_info(self, folder, name):
        self.calls.append(("file_info", folder, name))
        return next((f for f in self.files if f.folder == folder and f.name == name), None)

    def get_file(self, folder, name, dest):
        self.calls.append(("get_file", folder, name, dest))
        with open(dest, "wb") as fh:
            fh.write(b"\x00" * 16)

    def wait_event(self, timeout_ms):
        self.calls.append(("wait_event", timeout_ms))
        return self.events.pop(0) if self.events else ("timeout", None)


def writes(f):
    """All (path, value) pairs written, in order."""
    return [item for c in f.calls if c[0] == "write" for item in c[1]]


@pytest.fixture
def cam():
    f = FakeSession()
    b = Gphoto2Backend(session=f)
    return b, f


def test_connect_opens_one_session(cam):
    b, f = cam
    info = b.connect()
    assert info.model == "Canon EOS 77D"
    assert info.lens == "Sigma 17-50mm"
    assert info.capabilities["iso"] == ["Auto", "100", "200", "400", "800", "1600"]
    assert info.capabilities["aperture"][0] == "f/2.8"
    assert info.capabilities["kelvin_range"] == [2500, 10000]
    assert any("movie" in n.lower() for n in info.capabilities["notes"])
    assert sum(1 for c in f.calls if c[0] == "read_all") == 1
    assert sum(1 for c in f.calls if c[0] == "init") == 1 and f.open
    # the session stays open across calls
    b.get_settings(); b.capture_frame(); b.set_settings({"iso": 200})
    assert sum(1 for c in f.calls if c[0] in ("init", "exit")) == 1
    b.disconnect()
    assert f.calls[-1] == ("exit",) and not f.open


def test_connect_no_camera():
    f = FakeSession(model="")
    b = Gphoto2Backend(session=f)
    with pytest.raises(CameraError, match="ptpcamerad"):
        b.connect()


def test_claim_error_frees_daemon_and_retries():
    f = FakeSession()
    f.init_errors = [CameraError("Could not claim the USB device")]
    f.free_device = lambda: True          # pretend we killed ptpcamerad
    b = Gphoto2Backend(session=f)
    b.connect()
    assert sum(1 for c in f.calls if c[0] == "init") == 2

    f2 = FakeSession()
    f2.init_errors = [CameraError("Could not claim the USB device. ... `pkill -9 ptpcamerad`")]
    with pytest.raises(CameraError, match="pkill -9 ptpcamerad"):
        Gphoto2Backend(session=f2).connect()   # free_device() False -> give up


def test_auto_dial_switched_to_manual():
    f = FakeSession(list_all=LIST_ALL_AUTO)
    b = Gphoto2Backend(session=f)
    b.connect()
    assert f.state["/main/capturesettings/autoexposuremodedial"] == "Manual"
    assert sum(1 for c in f.calls if c[0] == "read_all") == 2


def test_set_snaps_and_batches(cam):
    b, f = cam
    b.connect()
    out = b.set_settings({"aperture": "f/5.7", "iso": 450})
    assert out == {"aperture": "f/5.6", "iso": "400"}
    batches = [c[1] for c in f.calls if c[0] == "write" and any("iso" in p for p, _ in c[1])]
    assert len(batches) == 1
    assert batches[0] == [("/main/capturesettings/aperture", "5.6"), ("/main/imgsettings/iso", "400")]
    assert ("/main/actions/viewfinder", "1") in writes(f)


def test_set_accepts_plain_aperture_and_bad_enum(cam):
    b, f = cam
    b.connect()
    assert b.set_settings({"aperture": "4"}) == {"aperture": "f/4"}
    with pytest.raises(CameraError):
        b.set_settings({"wb_mode": "Moonlight"})


def test_missing_path_degrades(cam):
    b, f = cam
    b.connect()
    assert b.set_settings({"picture_style": "Neutral"}) == {"picture_style": None}


def test_capture_frame(cam):
    b, f = cam
    b.connect()
    fr = b.capture_frame()
    assert isinstance(fr, Frame) and (fr.width, fr.height) == (96, 64)
    assert fr.orientation == "landscape"
    assert f.calls[-1] == ("preview",)


def test_capture_portrait_exif():
    f = FakeSession(jpeg((64, 96), orient=6))
    b = Gphoto2Backend(session=f)
    b.connect()
    assert b.capture_frame().orientation == "portrait"


def test_focus_record_clips(cam, tmp_path):
    b, f = cam
    b.connect()
    b.focus_drive("near2")
    assert writes(f)[-1] == ("/main/actions/manualfocusdrive", "Near 2")

    assert b.record("start") == {"recording": True, "clip": None}
    start = [c for c in f.calls if c[0] == "write"][-1][1]
    assert start == [("/main/settings/capturetarget", "1"), ("/main/actions/viewfinder", "1"),
                     ("/main/settings/movierecordtarget", "Card")]
    assert f.open      # session stays open between start and stop

    # the body finalizes the clip and announces it after stop
    new = FileEntry("/store_00020001/DCIM/XFVC", "A_0001C002A2", 12 * 1024 * 1024, "video/mp4", 1700000300)
    f.files.append(new)
    f.events = [("unknown", "PTP Property changed"), ("file_added", (new.folder, new.name))]
    res = b.record("stop")
    assert writes(f)[-1] == ("/main/settings/movierecordtarget", "None")
    assert res == {"recording": False, "clip": "A_0001C002A2.mp4"}
    assert f.open and not any(c[0] == "exit" for c in f.calls)

    clips = b.list_clips()
    assert [c["name"] for c in clips] == ["MVI_0002.MP4", "A_0001C001A2.mp4", "A_0001C002A2.mp4"]
    c = clips[0]
    assert (c["card_name"], c["index"], c["size"]) == ("MVI_0002.MP4", 2, 20480 * 1024)
    assert c["folder"] == "/store_00020001/DCIM/100CANON"
    x = clips[2]
    assert (x["folder"], x["card_name"], x["index"]) == ("/store_00020001/DCIM/XFVC", "A_0001C002A2", 2)
    assert x["mime"] == "video/mp4"
    dest = b.pull_clip(x, str(tmp_path))
    assert dest.endswith("A_0001C002A2.mp4")
    assert f.calls[-1][:3] == ("get_file", "/store_00020001/DCIM/XFVC", "A_0001C002A2")


def test_stop_without_event_relists_then_reinits(cam):
    b, f = cam
    b.connect()
    b.stop_settle_s = 0.1
    b.record("start")
    n_init = sum(1 for c in f.calls if c[0] == "init")
    # body finalizes the clip silently (no FILE_ADDED), and the open session's
    # object list is stale: the clip only shows up after a session re-init
    new = FileEntry("/store_00020001/DCIM/XFVC", "A_0001C003A2", 5 * 1024 * 1024, "video/mp4", 1700000400)
    real_list = f.list_files
    def stale_list():
        f.calls.append(("list_files",))
        return list(f.files) if sum(1 for c in f.calls if c[0] == "init") > n_init else [x for x in f.files if x is not new]
    f.list_files = stale_list
    f.files.append(new)
    res = b.record("stop")
    assert res["clip"] == "A_0001C003A2.mp4"
    assert sum(1 for c in f.calls if c[0] == "init") == n_init + 1   # one re-init
    assert f.open

    # and when the re-list in the open session already shows it, no re-init happens
    f.list_files = real_list
    b.record("start")
    n_init = sum(1 for c in f.calls if c[0] == "init")
    new2 = FileEntry("/store_00020001/DCIM/XFVC", "A_0001C004A2", 5 * 1024 * 1024, "video/mp4", 1700000500)
    f.files.append(new2)
    assert b.record("stop")["clip"] == "A_0001C004A2.mp4"
    assert sum(1 for c in f.calls if c[0] == "init") == n_init


def test_record_stop_while_idle_is_not_an_error(cam):
    b, f = cam
    b.connect()
    def busy(items):
        f.calls.append(("write", list(items)))
        raise CameraError("PTP Device Busy (set /main/settings/movierecordtarget=None)")
    f.write = busy
    assert b.record("stop")["recording"] is False


def test_delete_refused(cam):
    b, f = cam
    n = len(f.calls)
    with pytest.raises(CameraError):
        b._call("file_delete", "/store_00020001/DCIM/100CANON", "IMG_0001.JPG")
    with pytest.raises(CameraError):
        b._call("folder_delete_all", "/")
    with pytest.raises(CameraError):
        b._call("write", [("/main/actions/delete", "1")])
    assert len(f.calls) == n
    # and the real session class has no delete surface at all
    from server.camera.gphoto2_backend import GpSession
    assert not [m for m in dir(GpSession) if "delete" in m.lower()]


def test_session_reopened_after_io_loss(cam):
    b, f = cam
    b.connect()
    n_init = sum(1 for c in f.calls if c[0] == "init")
    once = {"done": False}
    real = f.preview
    def flaky():
        if not once["done"]:
            once["done"] = True
            raise CameraError("libgphoto2 error -7 (capture_preview): I/O problem")
        return real()
    f.preview = flaky
    assert b.capture_frame().width == 96
    assert sum(1 for c in f.calls if c[0] == "init") == n_init + 1


def test_kelvin_implies_color_temperature_mode(cam):
    b, f = cam
    b.connect()
    wb_choices = b.config["/main/imgsettings/whitebalance"]["choices"]
    if "Color Temperature" not in wb_choices:
        pytest.skip("fixture WB list lacks Color Temperature")
    b.set_settings({"kelvin": 4300})
    batch = [c for c in f.calls if c[0] == "write"][-1][1]
    paths = [p for p, _ in batch]
    assert ("/main/imgsettings/whitebalance", "Color Temperature") in batch
    assert paths.index("/main/imgsettings/whitebalance") < paths.index("/main/imgsettings/colortemperature")
    assert b.set_settings({"wb_mode": "kelvin"})["wb_mode"] in ("Color Temperature", None)
