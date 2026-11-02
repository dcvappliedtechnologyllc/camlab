"""One-screen control core (server/simple.py): readiness, modes, nudges, the
brightness loop, keep/load. Mock camera only; scriptable lights are a fake."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from conftest import manual_lights
from server import config
from server.api import build as build_api
from server.camera.mock_backend import MockBackend
from server.controller import Controller
from server.lights.base import LightBackend
from server.simple import TEXT


class FakeLight(LightBackend):
    """A light that answers (like the mesh lights when they are on)."""
    def connect(self): return True
    def apply(self, intensity_pct, cct_k): pass


def make_cfg(tmp):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"
    cfg["dmx"]["transport"] = "none"
    manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp / "frames"), "clips": str(tmp / "clips"), "recipes": str(tmp / "recipes.json"),
                    "context": str(tmp / "context.json"), "home": str(tmp)}
    cfg["claude"]["rate_limit_ms"] = 0
    cfg["simple"] = {"verify_s": 0, "settle_s": 0}
    return cfg


def make_ctl(tmp, preset="reel", scene="person_portrait", lights="fake", camera=None):
    ctl = Controller(make_cfg(tmp))
    ctl.start()
    if camera is not None:
        camera.connect()
        ctl.camera = camera
        ctl.refresh_camera_state()
    if lights == "fake":
        fakes = {lid: FakeLight(lid, prof) for lid, prof in ctl.cfg["lights"].items()}
        for lid, l in fakes.items():
            l.set(80 if lid == "key" else 30, 5600)
        ctl._install_lights(fakes)
    ctl.set_context(preset=preset, scene=scene)
    return ctl


def items(ready):
    return {i["id"]: i for i in ready["items"]}


def steady_face(ctl, t0=1000.0):
    ctl.simple.tick(now=t0)
    ctl.simple.tick(now=t0 + 2.0)


# ------------------------------------------------------------------ readiness
def test_ready_lights_mode_all_green_with_answering_lights(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("lights")
        assert ctl.camera.info().mode == "M"            # the mock's dial turned to M
        ctl.set_light("key", 40)                         # f/4 doubles the light: keep the face out of clipping
        steady_face(ctl)
        r = ctl.simple.ready(now=1002.0)
        assert [i["id"] for i in r["items"]] == ["camera", "dial", "lights", "subject", "upright"]
        assert r["lights_mode"] == "lights"
        bad = [i for i in r["items"] if not i["ok"]]
        assert not bad, bad
        assert r["all_ok"] is True
        for i in r["items"]:
            assert isinstance(i["text"], str) and i["text"].endswith(".") and isinstance(i["detail"], dict)
        assert r["last_good"] == {"exists": False}
    finally:
        ctl.stop()


def test_ready_manual_lights_and_auto_dial_are_red(tmp_path, monkeypatch):
    ctl = make_ctl(tmp_path, lights="manual")
    try:
        monkeypatch.setattr(ctl.camera, "set_exposure_mode", lambda mode: False)   # a body that refuses
        out = ctl.simple.set_mode("lights")
        assert out["manual"] is False
        r = items(ctl.simple.ready())
        assert r["dial"]["ok"] is False and r["dial"]["text"] == "Turn the top dial on the camera to M."
        assert r["dial"]["detail"]["mode"] == "movie"
        assert r["lights"]["ok"] is False
        assert r["lights"]["text"] == "Switch the lights on, or shoot without them: tap Camera mode."
        assert r["lights"]["detail"]["lights"]["key"]["scriptable"] is False
    finally:
        ctl.stop()


def test_ready_camera_mode_needs_neither_dial_nor_lights(tmp_path):
    ctl = make_ctl(tmp_path, lights="manual")
    try:
        ctl.simple.set_mode("camera")
        r = ctl.simple.ready()
        it = items(r)
        assert r["lights_mode"] == "camera"
        assert it["dial"]["ok"] and it["dial"]["text"] == "Camera is handling brightness itself."
        assert it["lights"]["ok"]
    finally:
        ctl.stop()


def test_subject_needs_a_steady_face_for_person_presets(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.tick(now=1000.0)
        it = items(ctl.simple.ready(now=1000.0))
        assert it["subject"]["ok"] is False                    # one look is not "steady" yet
        ctl.simple.tick(now=1002.0)
        it = items(ctl.simple.ready(now=1002.0))
        assert it["subject"]["ok"] is True and it["subject"]["detail"]["detector"] == "skin"
        # stale samples do not count
        assert items(ctl.simple.ready(now=1030.0))["subject"]["ok"] is False
    finally:
        ctl.stop()


def test_subject_no_face_on_a_person_preset(tmp_path):
    ctl = make_ctl(tmp_path, scene="part_dark")         # reel preset, but nobody in the mock's frame
    try:
        steady_face(ctl)
        it = items(ctl.simple.ready(now=1002.0))
        assert it["subject"]["ok"] is False and it["subject"]["text"] == TEXT["no_face"]
    finally:
        ctl.stop()


def test_subject_non_person_preset_is_ok(tmp_path):
    ctl = make_ctl(tmp_path, preset="product_hero", scene="part_dark")
    try:
        it = items(ctl.simple.ready())
        assert it["subject"]["ok"] and it["subject"]["text"] == "Put the subject in the middle."
    finally:
        ctl.stop()


def test_upright_follows_the_preset_orientation(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        assert items(ctl.simple.ready())["upright"]["ok"]
        ctl.set_context(orientation="landscape")
        ctl.live_frame()
        it = items(ctl.simple.ready())["upright"]
        assert it["ok"] is False and it["text"] == "Turn the camera upright on its bracket."
        assert it["detail"]["want"] == "portrait" and it["detail"]["frame"] == "landscape"
    finally:
        ctl.stop()


def test_ready_broadcasts_only_on_change(tmp_path):
    ctl = make_ctl(tmp_path)
    got = []
    ctl.state.subscribe(lambda m: got.append(m) if m["type"] == "ready" else None)
    try:
        ctl.simple.tick(now=1000.0)
        ctl.simple.tick(now=1000.5)
        n = len(got)
        assert n == 1 and {"items", "all_ok", "lights_mode", "simple"} <= set(got[0])
        ctl.simple.tick(now=1002.0)                     # subject turns green -> one more
        assert len(got) == n + 1
    finally:
        ctl.stop()


# ------------------------------------------------------------------ modes
def test_lights_mode_locks_manual_shutter_aperture_iso_and_persists(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.camera.settings["iso"] = "400"
        ctl.simple.set_mode("lights")
        s = ctl.camera.get_settings()
        assert s["shutter"] == "1/60"                    # 1/(2 x 30 fps)
        assert s["aperture"] == "f/4"                    # reel: shallow depth of field
        assert s["wb_mode"] == "kelvin" and s["kelvin"] == 5600
        snap = ctl.get_state()["simple"]
        assert snap["mode"] == "lights" and 0 <= snap["brightness"] <= 100 and 2800 <= snap["warmth"] <= 6500
        ctl.simple.set_mode("camera")
        saved = json.loads(Path(ctl.cfg["paths"]["context"]).read_text())
        assert saved["simple"]["mode"] == "camera" and saved["preset"] == "reel"
    finally:
        ctl.stop()
    again = Controller(make_cfg(tmp_path)); again.start()
    try:
        assert again.get_state()["simple"]["mode"] == "camera"
    finally:
        again.stop()


def test_shutter_follows_fps(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.camera.settings["movie_fps"] = 60; ctl.refresh_camera_state()
        ctl.simple.set_mode("lights")
        assert ctl.camera.get_settings()["shutter"] == "1/125"   # 1/120 snaps to the body's 1/125
    finally:
        ctl.stop()


def test_bad_mode_is_rejected(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        with pytest.raises(ValueError):
            ctl.simple.set_mode("auto")
    finally:
        ctl.stop()


# ------------------------------------------------------------------ nudges
def test_brighter_moves_key_then_iso_at_the_limit(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("lights")
        ctl.set_light("key", 80)
        a = ctl.simple.nudge("brighter")
        assert a["applied"]["lights"]["key"]["intensity_pct"] == 90 and a["text"] == "Key light 80 → 90 %"
        ctl.simple.nudge("brighter")
        iso0 = ctl.camera.get_settings()["iso"]
        b = ctl.simple.nudge("brighter")                 # key is at 100 %: ISO one step
        assert ctl.lights["key"].state().intensity_pct == 100
        assert b["applied"]["camera"]["iso"] != iso0 and ctl.camera.get_settings()["iso"] == "500"
        assert "ISO 400 → 500" in b["text"]
        d = ctl.simple.nudge("dimmer")
        assert d["applied"]["lights"]["key"]["intensity_pct"] == 90 and "camera" not in d["applied"]
        hist = [h for h in ctl.state.history if h["kind"] in ("camera", "light:key") and h["source"] == "simple"]
        assert hist                                         # everything went through the controller
    finally:
        ctl.stop()


def test_dimmer_clamps_at_zero(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("camera")
        ctl.set_light("key", 5)
        a = ctl.simple.nudge("dimmer")
        assert a["applied"]["lights"]["key"]["intensity_pct"] == 0
    finally:
        ctl.stop()


def test_warmer_cooler_move_camera_and_both_lights(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("lights")
        a = ctl.simple.nudge("warmer")
        assert a["applied"]["camera"]["kelvin"] == 5300
        assert {l["cct_k"] for l in a["applied"]["lights"].values()} == {5300}
        assert ctl.get_state()["simple"]["warmth"] == 5300
        for _ in range(12):
            ctl.simple.nudge("warmer")
        assert ctl.camera.get_settings()["kelvin"] == 2800 and ctl.lights["fill"].state().cct_k == 2800
        c = ctl.simple.nudge("cooler")
        assert c["applied"]["camera"]["kelvin"] == 3100
        with pytest.raises(ValueError):
            ctl.simple.nudge("sideways")
    finally:
        ctl.stop()


def test_camera_mode_never_writes_exposure(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("camera")
        ctl.set_light("key", 95)
        for what in ("brighter", "brighter", "brighter", "dimmer", "warmer", "cooler"):
            ctl.simple.nudge(what)
        ctl.set_light("key", 100)
        ctl.simple.level()
        ctl.simple.keep("MVI_0001.MP4")
        ctl.simple.load()
        exposure = {"iso", "shutter", "aperture", "ev_comp"}
        for h in ctl.state.history:
            if h["source"] == "simple" and h["kind"] == "camera":
                assert not exposure & set(h["after"] or {}), h
    finally:
        ctl.stop()


def test_camera_mode_without_lights_says_so(tmp_path):
    ctl = make_ctl(tmp_path, lights="manual")
    try:
        ctl.simple.set_mode("camera")
        a = ctl.simple.nudge("brighter")
        assert a["applied"] == {} and a["text"] == TEXT["no_light_camera_mode"]
    finally:
        ctl.stop()


def test_nudge_iso_respects_recording(tmp_path):
    ctl = make_ctl(tmp_path, lights="manual")
    try:
        ctl.simple.set_mode("lights")
        ctl.arm(); ctl.record("start")
        with pytest.raises(PermissionError):
            ctl.simple.nudge("brighter")               # it would have to move ISO mid-take
        ctl.record("stop")
    finally:
        ctl.stop()


# ------------------------------------------------------------------ level
def test_level_converges_on_the_mock_person_in_lights_mode(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("lights")
        ctl.set_light("key", 15)
        out = ctl.simple.level()
        assert out["status"] == "ok", out
        assert 1 <= len(out["trail"]) <= 6
        lo, hi = out["band"]
        assert lo <= out["trail"][-1]["value"] <= hi
        assert out["text"].startswith("Brightness is right")
        h = ctl.hud_analysis()
        assert lo <= h["skin_median"] <= hi
    finally:
        ctl.stop()


def test_level_from_too_bright(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("lights")
        ctl.set_light("key", 100)
        ctl.set_camera({"iso": "1600"})
        out = ctl.simple.level()
        assert out["status"] == "ok", out
    finally:
        ctl.stop()


def test_level_refuses_while_recording(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.arm(); ctl.record("start")
        with pytest.raises(PermissionError):
            ctl.simple.level()
        ctl.record("stop")
    finally:
        ctl.stop()


class RevertingMock(MockBackend):
    """Scene Intelligent Auto: the body takes ISO back as soon as it is written."""
    def set_exposure_mode(self, mode):
        return False

    def get_settings(self):
        s = super().get_settings()
        s["iso"] = "400"
        return s


def test_override_detector_trips_when_the_body_reverts(tmp_path):
    ctl = make_ctl(tmp_path, lights="manual", camera=RevertingMock(subject="person_portrait"))
    try:
        ctl.set_context(scene="person_portrait")
        ctl.simple.set_mode("lights")
        ctl.camera.settings["aperture"] = "f/22"           # far too dark, no light to move: ISO must go up
        out = ctl.simple.level()
        assert out["status"] == "override"
        assert out["text"] == "The camera is overriding me: turn the top dial to M."
        it = items(ctl.simple.ready())["dial"]
        assert it["ok"] is False and it["detail"]["override"] is True
    finally:
        ctl.stop()


# ------------------------------------------------------------------ keep / load
def test_keep_and_load_round_trip(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        ctl.simple.set_mode("lights")
        ctl.set_light("key", 55, 4400); ctl.set_camera({"iso": "800", "kelvin": 4400})
        with pytest.raises(KeyError):
            ctl.simple.load()
        k = ctl.simple.keep("MVI_0007.MP4")
        assert k["recipe"]["clip"] == "MVI_0007.MP4" and k["recipe"]["kind"] == "last_good"
        assert ctl.simple.ready()["last_good"]["exists"] is True
        ctl.set_light("key", 20, 5600); ctl.set_camera({"iso": "200", "kelvin": 5600})
        out = ctl.simple.load()
        assert ctl.lights["key"].state().intensity_pct == 55 and ctl.lights["key"].state().cct_k == 4400
        assert ctl.camera.get_settings()["iso"] == "800" and ctl.camera.get_settings()["kelvin"] == 4400
        assert out["text"]
        ctl.simple.keep("MVI_0008.MP4")                    # replaces, does not pile up
        assert len([r for r in ctl.recipes.recipes if r.get("kind") == "last_good"]) == 1
    finally:
        ctl.stop()


# ------------------------------------------------------------------ edsdk / mock hooks
def test_mock_and_base_exposure_mode():
    from server.controller import NoCamera
    assert NoCamera().set_exposure_mode("manual") is False
    m = MockBackend(); m.connect()
    assert m.info().mode == "movie" and m.set_exposure_mode("manual") is True and m.info().mode == "M"


def test_edsdk_set_exposure_mode_writes_ae_mode():
    from test_edsdk_backend import FakeDriver
    from server.camera.edsdk_backend import EdsdkBackend
    f = FakeDriver(); f.props[0x400] = 22                    # Scene Intelligent Auto
    b = EdsdkBackend(driver=f); b.connect()
    assert b.info().mode == "Scene Intelligent Auto"
    assert b.set_exposure_mode("manual") is True and ("set", 0x400, 3) in f.calls
    assert b.info().mode == "Manual"

    def refuse(p, v):
        raise RuntimeError("EDSDK: property not writable")
    f.set_u32 = refuse
    f.props[0x400] = 22
    assert b.set_exposure_mode("manual") is False


# ------------------------------------------------------------------ http
def test_http_contract(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        with TestClient(build_api(ctl, ctl.cfg)) as c:
            r = c.get("/api/ready").json()
            assert {"items", "all_ok", "lights_mode"} <= set(r)
            assert c.post("/api/simple/mode", json={"mode": "camera"}).json()["simple"]["mode"] == "camera"
            assert c.post("/api/simple/mode", json={"mode": "nope"}).status_code == 400
            assert c.post("/api/simple/mode", json={"mode": "lights"}).status_code == 200
            n = c.post("/api/simple/nudge", json={"what": "dimmer"}).json()
            assert n["text"] == "Key light 80 → 70 %"
            assert c.post("/api/simple/nudge", json={"what": "x"}).status_code == 400
            lv = c.post("/api/simple/level").json()
            assert lv["status"] in ("ok", "limit", "cap") and lv["trail"]
            assert c.post("/api/simple/load").status_code == 400
            assert c.post("/api/simple/keep", json={"clip": "MVI_0001.MP4"}).json()["recipe"]["kind"] == "last_good"
            assert c.post("/api/simple/load").status_code == 200
            assert c.get("/api/state").json()["simple"]["mode"] == "lights"
            c.post("/api/arm"); c.post("/api/record", json={"action": "start"})
            assert c.post("/api/simple/level").status_code == 403
            c.post("/api/record", json={"action": "stop"})
    finally:
        ctl.stop()


def test_vision_finds_a_real_face_when_available():
    """macOS Vision through pyobjc's runtime bridge, on a bench frame with Colleen in it."""
    from server import simple
    frame = Path(__file__).resolve().parents[1] / "frames" / "clips" / "A_0001C001A2_0001.00s.jpg"
    if not frame.exists() or not simple.vision_available():
        pytest.skip("no bench frame or no Vision on this machine")
    faces = simple.faces_vision(frame.read_bytes())
    assert faces and 0 <= faces[0]["bbox"][0] <= 1 and abs(faces[0]["roll_deg"]) < 45
