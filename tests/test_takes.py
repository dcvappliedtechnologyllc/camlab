"""Take delivery: auto-pull after Stop, /clips for the phone, /api/takes, send to phone, sidecar."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from conftest import manual_lights
from server import config
from server.api import build as build_api
from server.controller import Controller


@pytest.fixture(autouse=True)
def auto_pull_on(monkeypatch):
    monkeypatch.setattr(Controller, "AUTO_PULL", True)


def make(tmp_path):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"; manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp_path / "frames"), "clips": str(tmp_path / "clips"), "recipes": str(tmp_path / "recipes.json"),
                    "context": str(tmp_path / "context.json"), "home": str(tmp_path)}
    cfg["claude"]["rate_limit_ms"] = 0
    return cfg


def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return None


def shoot(ctl):
    ctl.arm(); ctl.record("start"); ctl.record("stop")


@pytest.fixture
def rig(tmp_path):
    cfg = make(tmp_path)
    ctl = Controller(cfg); ctl.start()
    events = []
    ctl.state.subscribe(events.append)
    ctl.set_context(tag="studio", preset="reel_interior")
    app = build_api(ctl, cfg)
    with TestClient(app) as client:
        yield ctl, client, events, tmp_path
    ctl.stop()


def test_stop_auto_pulls_and_broadcasts(rig):
    ctl, client, events, tmp = rig
    shoot(ctl)
    assert wait_for(lambda: ctl.state.takes), "no take within 3 s"
    take = ctl.state.takes[-1]
    assert set(take) >= {"label", "name", "path", "duration_s", "size", "seq", "tag", "ts"}
    assert take["label"] == "studio-001.MP4" and take["tag"] == "studio" and take["seq"] == 1
    assert Path(take["path"]).exists()
    msgs = [e for e in events if e.get("type") == "take"]
    assert msgs and msgs[-1]["take"]["label"] == "studio-001.MP4"
    snap = ctl.state.snapshot()
    assert snap["takes"][-1]["label"] == "studio-001.MP4" and snap["last_take"]["label"] == "studio-001.MP4"


def test_auto_pull_never_while_recording_and_retries_once(tmp_path, monkeypatch):
    cfg = make(tmp_path)
    ctl = Controller(cfg); ctl.start()
    ctl.RETRY_DELAY_S = 0.1
    events = []; ctl.state.subscribe(events.append)
    calls = {"n": 0}
    real = ctl.clips.pull
    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("card went away")
        return real(*a, **kw)
    monkeypatch.setattr(ctl.clips, "pull", flaky)
    try:
        shoot(ctl)
        assert wait_for(lambda: ctl.state.takes)
        assert calls["n"] == 2
        errs = [e for e in events if e.get("type") == "take" and "error" in e]
        assert len(errs) == 1 and "card went away" in errs[0]["error"]
    finally:
        ctl.stop()


def test_sidecar_written(rig):
    ctl, client, events, tmp = rig
    shoot(ctl)
    take = wait_for(lambda: ctl.state.takes and ctl.state.takes[-1])
    side = Path(take["path"] + ".json")
    assert wait_for(side.exists)
    data = json.loads(side.read_text())
    assert data["tag"] == "studio" and data["preset"] == "reel_interior" and "scene" in data
    assert "camera" in data["settings"] and "lights" in data["settings"]
    assert data["verdict"] is None and data["notes"] == ""


def test_clips_route_serves_with_range(rig):
    ctl, client, events, tmp = rig
    shoot(ctl)
    take = wait_for(lambda: ctl.state.takes and ctl.state.takes[-1])
    size = Path(take["path"]).stat().st_size
    r = client.get(f"/clips/{take['label']}")
    assert r.status_code == 200 and len(r.content) == size
    assert "attachment" in r.headers["content-disposition"] and take["label"] in r.headers["content-disposition"]
    assert r.headers["content-type"].startswith("video/mp4")
    r2 = client.get(f"/clips/{take['label']}", headers={"Range": "bytes=0-99"})
    assert r2.status_code == 206 and len(r2.content) == 100
    assert r2.headers["content-range"].startswith("bytes 0-99/")
    assert client.get(f"/clips/{take['label']}?play=1").headers['content-disposition'].startswith('inline')
    # by card name too
    assert client.get(f"/clips/{take['name']}").status_code == 200


def test_clips_route_refuses_outside_folder(rig):
    ctl, client, events, tmp = rig
    (tmp / "secret.MP4").write_bytes(b"x" * 10)
    for bad in ("../secret.MP4", "..%2Fsecret.MP4", "%2e%2e/secret.MP4", "nope.MP4", "index.json", "/etc/passwd"):
        assert client.get(f"/clips/{bad}").status_code in (400, 404), bad


def test_api_takes_lists_with_urls(rig):
    ctl, client, events, tmp = rig
    shoot(ctl); wait_for(lambda: ctl.state.takes)
    shoot(ctl); wait_for(lambda: len(ctl.state.takes) >= 2)
    j = client.get("/api/takes").json()
    assert [t["label"] for t in j["takes"]] == ["studio-001.MP4", "studio-002.MP4"]
    assert all(t["url"].startswith("http://") and t["url"].endswith("/clips/" + t["label"]) for t in j["takes"])


def test_send_returns_url_and_qr(rig):
    ctl, client, events, tmp = rig
    shoot(ctl); take = wait_for(lambda: ctl.state.takes and ctl.state.takes[-1])
    j = client.post("/api/takes/send", json={"label": take["label"]}).json()
    assert j["url"].endswith("/clips/studio-001.MP4")
    try:
        import segno  # noqa: F401
        assert j["qr_svg"].lstrip().startswith("<svg")
    except ImportError:
        assert j["qr_svg"] is None
    assert client.post("/api/takes/send", json={"label": "ghost.MP4"}).status_code in (400, 404)


def test_airdrop_falls_back_to_finder(rig, monkeypatch):
    ctl, client, events, tmp = rig
    import subprocess
    ran = []
    shoot(ctl); take = wait_for(lambda: ctl.state.takes and ctl.state.takes[-1])
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: ran.append(cmd))
    j = client.post("/api/takes/airdrop", json={"label": take["label"]}).json()
    assert j["method"] == "finder" and ran and ran[0][:2] == ["open", "-R"]


def _restart(tmp_path):
    """A new controller on the same clips folder: the mock card's names start again at MVI_0001."""
    ctl = Controller(make(tmp_path)); ctl.start()
    events = []; ctl.state.subscribe(events.append)
    return ctl, events


def test_reused_card_name_same_size_reports_error(tmp_path):
    ctl, ev = _restart(tmp_path)
    ctl.set_context(tag="studio")
    try:
        shoot(ctl); assert wait_for(lambda: ctl.state.takes)
    finally:
        ctl.stop()
    ctl2, events = _restart(tmp_path)
    ctl2.set_context(tag="studio")
    try:
        shoot(ctl2)
        err = wait_for(lambda: [e for e in events if e.get("type") == "take" and "error" in e])
        assert err and "reused the name MVI_0001.MP4" in err[0]["error"]
        assert not ctl2.state.takes and len(ctl2.clips.index) == 1
    finally:
        ctl2.stop()


def test_reused_card_name_different_size_is_pulled_numbered(tmp_path, monkeypatch):
    ctl, ev = _restart(tmp_path)
    ctl.set_context(tag="studio")
    try:
        shoot(ctl); assert wait_for(lambda: ctl.state.takes)
    finally:
        ctl.stop()
    ctl2, events = _restart(tmp_path)
    ctl2.set_context(tag="studio")
    real = ctl2.camera.list_clips
    monkeypatch.setattr(ctl2.camera, "list_clips", lambda: [{**c, "size": 49_000_000} for c in real()])
    try:
        shoot(ctl2)
        assert wait_for(lambda: ctl2.state.takes)
        take = ctl2.state.takes[-1]
        assert take["label"] == "studio-002.MP4" and take["name"] == "MVI_0001.MP4" and Path(take["path"]).exists()
        assert Path(ctl2.clips.index[0]["path"]).exists() and len(ctl2.clips.index) == 2
        assert [e for e in events if e.get("type") == "take" and e.get("take")]
    finally:
        ctl2.stop()
