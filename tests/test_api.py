import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import manual_lights
from fastapi.testclient import TestClient

from server import config
from server.api import build as build_api
from server.controller import Controller


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cfg = config.load()
    tmp = tmp_path_factory.mktemp("camlab")
    cfg["camera"]["backend"] = "mock"
    cfg["dmx"]["transport"] = "none"
    manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp / "frames"), "clips": str(tmp / "clips"), "recipes": str(tmp / "recipes.json")}
    cfg["claude"]["rate_limit_ms"] = 0
    ctl = Controller(cfg); ctl.start()
    with TestClient(build_api(ctl, cfg)) as c:
        yield c
    ctl.stop()


def test_state_and_presets(client):
    s = client.get("/api/state").json()
    assert s["camera"]["model"].startswith("Mock") and "key" in s["lights"]
    assert client.get("/api/presets").json()["presets"]["reel"]["frame"] == "9:16"


def test_capture_and_camera(client):
    fr = client.post("/api/capture").json()
    assert fr["analysis"]["luma"]["median"] > 0 and "target_check" in fr["analysis"]
    assert client.post("/api/camera", json={"iso": "450"}).json() == {"iso": "400"}
    assert client.post("/api/camera", json={"aperture": "f/8"}).json()["aperture"] == "f/8"
    assert client.get("/api/frame/last.jpg").headers["content-type"] == "image/jpeg"


def test_light_clamps(client):
    r = client.post("/api/light/key", json={"intensity_pct": 140, "cct_k": 9000}).json()
    assert r["intensity_pct"] == 100 and r["cct_k"] == 6500
    assert client.post("/api/light/nope", json={}).status_code == 400


def test_record_requires_arm(client):
    assert client.post("/api/record", json={"action": "start"}).status_code == 403
    client.post("/api/arm")
    assert client.post("/api/record", json={"action": "start"}).json()["recording"] is True
    stop = client.post("/api/record", json={"action": "stop"}).json()
    assert stop["recording"] is False and stop["clip"]
    pulled = client.post("/api/clips/pull").json()
    assert len(pulled) == 1
    frames = client.post("/api/clips/frames", json={"clip_id": pulled[0]["id"], "timestamps": [0.2]}).json()
    assert frames[0]["analysis"]["luma"]["median"] > 0


def test_context_and_recipes(client):
    c = client.post("/api/context", json={"preset": "reel", "scene": "person_portrait", "roi": [0.4, 0.2, 0.2, 0.4]}).json()
    assert c["preset"] == "reel" and c["roi"] == [0.4, 0.2, 0.2, 0.4]
    assert client.post("/api/context", json={"preset": "bogus"}).status_code == 400
    r = client.post("/api/recipes", json={"name": "colleen-default"}).json()
    assert r["status"] == "saved"
    assert client.get("/api/state").json()["recipe_match"]["name"] == "colleen-default"
    client.post("/api/camera", json={"iso": "1600"})
    loaded = client.post("/api/recipes/load", json={"id": r["recipe"]["id"]}).json()
    assert loaded["camera_applied"]["iso"] == "400"


def test_claude_loop_and_undo(client):
    assert client.post("/api/claude", json={"request": "optimize"}).json()["request"] == "optimize"
    assert client.post("/api/claude", json={"request": "stop"}).json()["status"] == "stopped"
    client.post("/api/camera", json={"iso": "800"})
    assert client.post("/api/undo").json()["iso"] == "400"


def test_websocket_hello(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "state"


def test_chat_message_roundtrip(client):
    r = client.post("/api/claude/message", json={"text": "warm it up a touch"}).json()
    assert r["request"] == "message" and r["messages"][-1]["role"] == "user"
    assert client.post("/api/claude/message", json={"text": "  "}).status_code == 400
    s = client.get("/api/state").json()["claude"]
    assert s["request"] == "message"


def test_live_hud_payload(client):
    import time
    with client.websocket_connect("/ws") as ws:
        hud = None
        deadline = time.time() + 6
        while time.time() < deadline and hud is None:
            m = ws.receive_json()
            if m.get("type") == "live" and m.get("hud"):
                hud = m["hud"]
    assert hud and 0 < hud["luma_median"] < 1 and isinstance(hud["target_check"], dict)
    for k in ("clip_high_pct", "clip_low_pct", "wb_estimate_k", "focus", "ts"):
        assert k in hud


def test_config_hides_api_key(client):
    cfg = client.get("/api/config").json()
    assert "api_key" not in cfg["ai"] and cfg["ai"]["configured"] in (True, False)
    assert cfg["ai"]["cost_cap_usd"] == 1.0
