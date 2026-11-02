"""Stills: the look table (server/stills.py) and the still-capture path against the mock camera."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import manual_lights
from server import config, presets, stills
from server.api import build as build_api
from server.controller import Controller
from server.stills import LOOKS, TEXT, look_recipe


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


@pytest.fixture
def ctl(tmp_path):
    c = Controller(make_cfg(tmp_path))
    c.start()
    yield c
    c.stop()


@pytest.fixture
def client(ctl):
    app = build_api(ctl, ctl.cfg)
    with TestClient(app) as cl:
        yield cl


# ------------------------------------------------------------------ look table
def test_five_looks_each_with_a_plain_sentence():
    assert list(LOOKS) == ["high_bokeh", "everything_sharp", "product_on_black", "clean_portrait", "flat_document"]
    for lid, lk in LOOKS.items():
        assert lk["label"] and lk["sentence"].endswith(".") and len(lk["sentence"]) < 200, lid
        for key in ("aperture", "shutter", "iso", "kelvin", "picture_style", "focus_mode", "lights"):
            assert key in lk, (lid, key)
        assert "key" in lk["lights"] and "fill" in lk["lights"]


def test_high_bokeh_opens_the_lens_to_its_widest():
    r = look_recipe("high_bokeh", lens_max="f/2.8")
    assert r["camera"]["aperture"] == "f/2.8"
    # a slower lens: the look can only open as far as the glass goes
    r = look_recipe("high_bokeh", lens_max="f/4.5")
    assert r["camera"]["aperture"] == "f/4.5"


def test_body_aperture_list_overrides_the_lens_table():
    # the body says what the lens can do at this focal length: f/4 is the widest it lists
    r = look_recipe("high_bokeh", lens_max="f/2.8", aperture_choices=["f/4", "f/5.6", "f/8", "f/11"])
    assert r["camera"]["aperture"] == "f/4"


def test_deep_looks_sit_in_the_bench_doc_aperture_range():
    lo, hi = presets.PRESETS["bench_doc"]["target"]["aperture"]
    for lid in ("everything_sharp", "flat_document"):
        f = float(look_recipe(lid)["camera"]["aperture"][2:])
        assert float(lo[2:]) <= f <= float(hi[2:]), lid


def test_portrait_look_matches_the_reel_colour_band():
    lo, hi = presets.PRESETS["reel"]["target"]["cct_k"]
    assert lo <= look_recipe("clean_portrait")["camera"]["kelvin"] <= hi


def test_product_look_is_low_key_and_fixed_iso():
    r = look_recipe("product_on_black")
    assert r["camera"]["iso"] != "auto"
    assert r["target"]["luma_median"] == presets.PRESETS["product_hero"]["target"]["luma_median"]


def test_unknown_look_raises():
    with pytest.raises(KeyError):
        look_recipe("sepia")


# ------------------------------------------------------------------ mock camera
def test_mock_takes_a_full_size_still(tmp_path):
    from server.camera.mock_backend import MockBackend
    cam = MockBackend(); cam.connect()
    assert cam.still_mode() is True
    res = cam.take_picture(str(tmp_path))
    im = Image.open(res["path"])
    assert im.size[0] >= 3000 and im.size[1] >= 2000
    assert res["name"].upper().endswith(".JPG")


def test_mock_raw_plus_jpeg(tmp_path):
    from server.camera.mock_backend import MockBackend
    cam = MockBackend(); cam.connect()
    assert cam.info().capabilities.get("raw_jpeg") is True
    cam.set_image_quality(raw=True)
    res = cam.take_picture(str(tmp_path))
    assert res["raw_path"] and res["raw_path"].upper().endswith(".CR3")
    assert Path(res["raw_path"]).exists()


# ------------------------------------------------------------------ controller + API
def test_switching_to_stills_applies_a_look_and_reports_readiness(ctl):
    r = ctl.stills.set_mode("stills")
    assert ctl.state.stills_mode is True
    assert r["ready"]["items"][0]["id"] == "camera"
    ids = [i["id"] for i in r["ready"]["items"]]
    assert "photo_dial" in ids and "arm" in ids
    arm = next(i for i in r["ready"]["items"] if i["id"] == "arm")
    assert arm["ok"] and arm["text"] == TEXT["arm"]
    out = ctl.stills.set_look("high_bokeh")
    assert out["applied"]["aperture"] == "f/2.8"
    assert ctl.state.stills_look == "high_bokeh"
    assert out["sentence"] == LOOKS["high_bokeh"]["sentence"]
    assert out["lights"]      # the key/fill suggestion comes back in words


def test_shoot_pulls_the_jpeg_and_analyzes_it(ctl):
    ctl.stills.set_mode("stills")
    ctl.stills.set_look("everything_sharp")
    ctl.set_context(tag="bench")
    events = []
    ctl.state.subscribe(events.append)
    st = ctl.stills.shoot()
    assert st["label"] == "bench-001.JPG"
    assert Path(st["path"]).exists() and Path(st["path"] + ".json").exists()
    a = st["analysis"]
    assert "luma_median" in a and "clip_high_pct" in a and "focus" in a and "subject_median" in a
    assert st["look"] == "everything_sharp"
    assert st["width"] >= 3000
    side = json.loads(Path(st["path"] + ".json").read_text())
    assert side["look"] == "everything_sharp" and side["settings"]["camera"]["aperture"] == "f/8"
    assert any(e.get("type") == "still" and e.get("still", {}).get("label") == "bench-001.JPG" for e in events)
    assert ctl.stills.list_today()[0]["label"] == "bench-001.JPG"
    st2 = ctl.stills.shoot()
    assert st2["label"] == "bench-002.JPG"


def test_shoot_refuses_in_video_mode_and_while_recording(ctl):
    with pytest.raises(PermissionError):
        ctl.stills.shoot()
    ctl.stills.set_mode("stills")
    ctl.state.recording = True
    with pytest.raises(PermissionError):
        ctl.stills.shoot()


def test_stills_never_delete(ctl):
    import inspect
    src = inspect.getsource(stills)
    assert "delete" not in src.lower().replace("never delete", "").replace("no delete", "")


def test_api_round_trip(client):
    r = client.get("/api/stills/looks").json()
    assert len(r["looks"]) == 5 and r["looks"][0]["id"] == "high_bokeh" and r["looks"][0]["sentence"]
    r = client.post("/api/stills/mode", json={"mode": "stills"})
    assert r.status_code == 200 and r.json()["mode"] == "stills"
    r = client.post("/api/stills/look", json={"look": "clean_portrait"})
    assert r.status_code == 200 and r.json()["applied"]["kelvin"]
    r = client.post("/api/stills/shoot", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["label"].endswith(".JPG") and body["analysis"]["luma_median"] is not None
    assert "jpeg_b64" in body
    lst = client.get("/api/stills").json()["stills"]
    assert lst and lst[-1]["label"] == body["label"] and lst[-1]["url"].startswith("http")
    img = client.get("/stills/" + body["label"])
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/jpeg")
    assert client.get("/stills/../config.toml").status_code in (400, 404)
    snd = client.post("/api/stills/send", json={"label": body["label"]})
    assert snd.status_code == 200 and "/stills/" in snd.json()["url"]
    assert client.post("/api/stills/send", json={"label": "nope.JPG"}).status_code == 404
    st = client.get("/api/state").json()
    assert st["stills_mode"] is True and st["stills_look"] == "clean_portrait"
    r = client.post("/api/stills/mode", json={"mode": "video"})
    assert r.json()["mode"] == "video"


def test_api_raw_option(client):
    client.post("/api/stills/mode", json={"mode": "stills"})
    r = client.post("/api/stills/shoot", json={"raw": True})
    assert r.status_code == 200 and r.json()["raw_path"].upper().endswith(".CR3")


def test_handoff_and_ask_take_a_still(ctl, monkeypatch):
    from server import handoff
    ctl.stills.set_mode("stills"); ctl.stills.set_look("product_on_black")
    st = ctl.stills.shoot()
    h = handoff.build_handoff(ctl, still=st["label"])
    assert h["kind"] == "still" and "look=Product_on_black" in h["string"]
    assert h["jpg_path"].endswith("sample.jpg")
    # Ask: the state text names the look and the photo
    lines = handoff.state_lines(ctl, still=ctl.stills.get(st["label"]))
    assert any(l.startswith("still=") and "look=Product_on_black" in l for l in lines)
