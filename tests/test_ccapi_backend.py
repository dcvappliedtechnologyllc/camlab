"""CCAPIBackend against a fake Canon CCAPI server (in-process, via the `http`
injection point). Shapes follow Canon's CCAPI reference; nothing here has
been checked against a real body yet."""
import io
import json

import pytest
from PIL import Image

from server.camera.base import CameraError
from server.camera.ccapi_backend import CCAPIBackend, _parse_movie_quality

HOST = "192.0.2.50"
BASE = f"http://{HOST}:8080/ccapi"


def jpeg(size=(96, 64)):
    im = Image.new("RGB", size, (10, 200, 10)); b = io.BytesIO(); im.save(b, "JPEG"); return b.getvalue()


class Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self.content = json.dumps(body).encode() if body is not None else content
        self.headers = {}
    def json(self):
        return json.loads(self.content)
    def iter_content(self, n):
        yield self.content


class FakeCCAPI:
    def __init__(self):
        self.calls = []
        self.settings = {
            "iso": {"value": "400", "ability": ["auto", "100", "200", "400", "800", "1600"]},
            "tv": {"value": "1/125", "ability": ["1/60", "1/125", "1/250"]},
            "av": {"value": "f5.6", "ability": ["f2.8", "f4.0", "f5.6", "f8.0"]},
            "exposure": {"value": "+0.0", "ability": ["-1.0", "+0.0", "+1.0"]},
            "wb": {"value": "auto", "ability": ["auto", "daylight", "colortemp"]},
            "colortemperature": {"value": 5200, "ability": {"min": 2500, "max": 10000, "step": 100}},
            "picturestyle": {"value": "auto", "ability": ["auto", "standard", "neutral"]},
            "afoperation": {"value": "oneshot", "ability": ["oneshot", "servo"]},
            "moviequality": {"value": "fhd_2997_ipb", "ability": ["4k_2997_ipb", "fhd_5994_ipb", "fhd_2997_ipb"]},
        }
        self.liveview = "off"
        self.movie = False
        self.recording = False
        self.files = [f"{BASE}/ver110/contents/sd/100CANON/IMG_0001.JPG",
                      f"{BASE}/ver110/contents/sd/100CANON/MVI_0002.MP4"]
        self.added = []

    def __call__(self, method, url, json=None, stream=False, timeout=None):
        self.calls.append((method, url.replace(BASE, ""), json))
        path = url.replace(BASE, "")
        if method == "DELETE":
            raise AssertionError("DELETE must never reach the camera")
        if path == "":
            return Resp(200, {"ver100": [{"path": "/ccapi/ver100/deviceinformation", "get": True}],
                              "ver110": [{"path": "/ccapi/ver110/contents", "get": True}]})
        if path == "/ver100/deviceinformation":
            return Resp(200, {"productname": "Canon EOS R50 V", "serialnumber": "1", "firmwareversion": "1.0.0"})
        if path == "/ver100/devicestatus/lens":
            return Resp(200, {"name": "RF-S18-45mm F4.5-6.3 IS STM", "mount": True})
        if path == "/ver100/shooting/settings":
            return Resp(200, self.settings)
        if path.startswith("/ver100/shooting/settings/"):
            name = path.rsplit("/", 1)[-1]
            if name not in self.settings:
                return Resp(404, {"message": "Not found"})
            if method == "PUT":
                ab = self.settings[name]["ability"]
                v = json["value"]
                if isinstance(ab, list) and v not in ab:
                    return Resp(400, {"message": "Invalid parameter"})
                self.settings[name]["value"] = v
                return Resp(200, {"value": v})
            return Resp(200, self.settings[name])
        if path == "/ver100/shooting/liveview":
            self.liveview = json["liveviewsize"]; return Resp(200, {})
        if path == "/ver100/shooting/liveview/flip":
            return Resp(200, content=jpeg() if self.liveview != "off" else b"")
        if path == "/ver100/shooting/control/drivefocus":
            assert json["value"] in ("near1", "near2", "near3", "far1", "far2", "far3"); return Resp(200, {})
        if path == "/ver100/shooting/liveview/afframeposition":
            return Resp(200, {})
        if path == "/ver100/shooting/control/moviemode":
            self.movie = json["action"] == "on"; return Resp(200, {})
        if path == "/ver100/shooting/control/shutterbutton":
            assert self.movie
            self.recording = not self.recording
            if not self.recording:
                new = f"{BASE}/ver110/contents/sd/100CANON/MVI_0003.MP4"
                self.files.append(new); self.added.append(new)
            return Resp(200, {})
        if path.startswith("/ver100/event/polling"):
            ev = {"addedcontents": list(self.added)}; self.added.clear(); return Resp(200, ev)
        if path == "/ver110/contents":
            return Resp(200, {"path": [f"{BASE}/ver110/contents/sd"]})
        if path == "/ver110/contents/sd":
            return Resp(200, {"path": [f"{BASE}/ver110/contents/sd/100CANON"]})
        if path.startswith("/ver110/contents/sd/100CANON?"):
            return Resp(200, {"path": list(self.files), "pagenumber": 1})
        if path.endswith("?kind=info"):
            name = path.split("/")[-1].split("?")[0]
            return Resp(200, {"filesize": 123456, "lastmodifieddate": "Fri, 24 Oct 2026 17:00:00 GMT"} if name.endswith("MP4") else {})
        if path.startswith("/ver110/contents/sd/100CANON/"):
            return Resp(200, content=b"MP4DATA" * 100)
        return Resp(404, {"message": f"no route {path}"})


@pytest.fixture
def cam():
    f = FakeCCAPI()
    return CCAPIBackend(HOST, http=f), f


def test_connect_reads_abilities_and_starts_liveview(cam):
    b, f = cam
    info = b.connect()
    assert info.model == "Canon EOS R50 V" and info.lens.startswith("RF-S")
    assert info.capabilities["iso"] == ["auto", "100", "200", "400", "800", "1600"]
    assert info.capabilities["aperture"] == ["f/2.8", "f/4.0", "f/5.6", "f/8.0"]
    assert info.capabilities["kelvin_range"] == [2500, 10000]
    assert {"resolution": "4k", "fps": 30} in info.capabilities["movie_modes"]
    assert f.liveview == "small"
    assert b.versions["/contents"] == ["ver110"]


def test_get_settings(cam):
    b, f = cam
    b.connect()
    s = b.get_settings()
    assert s["aperture"] == "f/5.6" and s["iso"] == "400" and s["kelvin"] == "5200"
    assert (s["movie_resolution"], s["movie_fps"]) == ("1080p", 30)


def test_set_snaps_and_maps_vocabulary(cam):
    b, f = cam
    b.connect()
    out = b.set_settings({"aperture": "f/5.7", "iso": 450, "shutter": "1/100"})
    assert out == {"aperture": "f/5.6", "iso": "400", "shutter": "1/125"}
    assert f.settings["av"]["value"] == "f5.6"
    assert b.set_settings({"wb_mode": "Color Temperature"}) == {"wb_mode": "colortemp"}
    with pytest.raises(CameraError):
        b.set_settings({"wb_mode": "moonlight"})


def test_kelvin_implies_colortemp_and_clamps(cam):
    b, f = cam
    b.connect()
    f.settings["wb"]["value"] = "auto"
    out = b.set_settings({"kelvin": 4321})
    assert out["wb_mode"] == "colortemp" and out["kelvin"] == "4300"
    puts = [c for c in f.calls if c[0] == "PUT"]
    assert puts[-2][1].endswith("/wb") and puts[-1][1].endswith("/colortemperature")
    assert b.set_settings({"kelvin": 99999})["kelvin"] == "10000"


def test_movie_mode_from_quality_list(cam):
    b, f = cam
    b.connect()
    assert b.set_settings({"movie_resolution": "4k", "movie_fps": 30}) == {"movie_resolution": "4k", "movie_fps": 30}
    assert f.settings["moviequality"]["value"] == "4k_2997_ipb"
    assert b.set_settings({"movie_resolution": "720p", "movie_fps": 60}) == {"movie_resolution": None, "movie_fps": None}


def test_capture_restarts_liveview(cam):
    b, f = cam
    b.connect()
    fr = b.capture_frame()
    assert (fr.width, fr.height, fr.orientation) == (96, 64, "landscape")
    f.liveview = "off"            # body dropped live view
    assert b.capture_frame().width == 96
    assert f.liveview == "small"


def test_focus_record_clips_pull(cam, tmp_path):
    b, f = cam
    b.connect()
    b.focus_drive("far2")
    assert f.calls[-1] == ("POST", "/ver100/shooting/control/drivefocus", {"value": "far2"})
    b.capture_frame()                 # AF point is scaled to the last live-view frame (96x64)
    b.set_af_point(0.5, 0.25)
    assert f.calls[-1][2] == {"positionx": 48, "positiony": 16}
    assert b.record("start") == {"recording": True, "clip": None}
    assert f.movie and f.recording
    res = b.record("stop")
    assert res == {"recording": False, "clip": "MVI_0003.MP4"} and not f.recording
    clips = b.list_clips()
    assert [c["name"] for c in clips] == ["MVI_0002.MP4", "MVI_0003.MP4"]
    assert clips[0]["size"] == 123456 and clips[0]["mime"] == "video/mp4" and clips[0]["mtime"]
    dest = b.pull_clip(clips[1], str(tmp_path))
    assert dest.endswith("MVI_0003.MP4") and open(dest, "rb").read().startswith(b"MP4DATA")


def test_stop_falls_back_to_relist(cam):
    b, f = cam
    b.connect()
    b.stop_settle_s = 0.2
    b.record("start")
    real = f.__call__
    f.added_backup = None
    orig_files = list(f.files)
    # stop produces a file but no event
    f.recording = False
    f.files.append(f"{BASE}/ver110/contents/sd/100CANON/MVI_0009.MP4")
    b._recording = False
    assert b._await_clip() == "MVI_0009.MP4"


def test_delete_refused_and_unreachable(cam):
    b, f = cam
    with pytest.raises(CameraError, match="refused"):
        b._req("DELETE", "/ccapi/ver110/contents/sd/100CANON/MVI_0002.MP4")
    assert not any(c[0] == "DELETE" for c in f.calls)
    def down(*a, **k):
        raise OSError("connection refused")
    with pytest.raises(CameraError, match="unreachable"):
        CCAPIBackend(HOST, http=down).connect()


def test_parse_movie_quality():
    assert _parse_movie_quality("fhd_5994_ipb") == {"resolution": "1080p", "fps": 60}
    assert _parse_movie_quality("4k_2398_ipb_crop") == {"resolution": "4k", "fps": 24}
    assert _parse_movie_quality("garbage") is None
