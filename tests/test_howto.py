import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from server import howto
from server.api import build as build_api
from server.controller import Controller
from server import config
from conftest import manual_lights

IDS = {"dial_manual", "iso_fixed", "movie_switch", "wb_kelvin", "movie_size_fps",
       "power_saving_off", "lights_on", "stabilisation", "servo_af_faces", "photo_dial"}


def test_entries_complete():
    assert set(howto.HOWTO) == IDS
    for k, e in howto.HOWTO.items():
        assert e["id"] == k
        assert 3 <= len(e["steps"]) <= 6, k
        if k == "lights_on":                      # no official Godox manual URL found
            assert e["manual_url"] is None and e["verified"] is False
        else:
            assert e["manual_url"].startswith("https://"), k
        assert e["why"].strip().endswith("."), k
        assert e["title"] and e["applies_to"] and isinstance(e["verified"], bool)


def test_readiness_mapping():
    assert howto.BY_READINESS["dial"] == ["dial_manual", "iso_fixed"]
    assert howto.BY_READINESS["camera"] == ["power_saving_off", "movie_switch"]
    assert howto.BY_READINESS["lights"] == ["lights_on", "wb_kelvin"] and "wb_kelvin" in howto.HOWTO
    for ids in howto.BY_READINESS.values():
        assert all(i in howto.HOWTO for i in ids)


def test_routes(tmp_path):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"
    manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp_path / "f"), "clips": str(tmp_path / "c"), "recipes": str(tmp_path / "r.json")}
    ctl = Controller(cfg); ctl.start()
    try:
        with TestClient(build_api(ctl, cfg)) as c:
            r = c.get("/api/howto").json()
            assert {e["id"] for e in r["howto"]} == IDS
            assert r["by_readiness"]["dial"] == ["dial_manual", "iso_fixed"]
            assert c.get("/api/howto/iso_fixed").json()["id"] == "iso_fixed"
            assert c.get("/api/howto/nope").status_code == 404
    finally:
        ctl.stop()
