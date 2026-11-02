"""Clip tag: a location/shoot name with sequential numbering on pulled clips."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from conftest import manual_lights
from server import config
from server.api import build as build_api
from server.controller import Controller


def make(tmp_path):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"; manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp_path / "frames"), "clips": str(tmp_path / "clips"), "recipes": str(tmp_path / "recipes.json"),
                    "context": str(tmp_path / "context.json"), "home": str(tmp_path)}
    cfg["claude"]["rate_limit_ms"] = 0
    return cfg


def take(ctl):
    ctl.arm(); ctl.record("start"); ctl.record("stop")


def test_tagged_pull_numbers_clips(tmp_path):
    ctl = Controller(make(tmp_path)); ctl.start()
    try:
        ctl.set_context(tag="studio")
        take(ctl); p1 = ctl.pull_clips()
        take(ctl); p2 = ctl.pull_clips()
        assert Path(p1[0]["path"]).name == "studio-001.MP4" and p1[0]["seq"] == 1 and p1[0]["tag"] == "studio"
        assert Path(p2[0]["path"]).name == "studio-002.MP4" and p2[0]["seq"] == 2
        assert p1[0]["name"].startswith("MVI_")                      # the card's own name is kept in the index
        assert ctl.state.last_clip == "studio-002.MP4"
        assert ctl.export_clip("studio-002.MP4", None)["path"].endswith("studio-002.MP4")
        ctl.set_context(tag="kitchen")
        take(ctl); p3 = ctl.pull_clips()
        assert Path(p3[0]["path"]).name == "kitchen-001.MP4"
        ctl.set_context(tag="studio")
        take(ctl); p4 = ctl.pull_clips()
        assert Path(p4[0]["path"]).name == "studio-003.MP4"           # numbering continues per tag
    finally:
        ctl.stop()


def test_untagged_pull_keeps_card_names(tmp_path):
    ctl = Controller(make(tmp_path)); ctl.start()
    try:
        take(ctl); p = ctl.pull_clips()
        assert Path(p[0]["path"]).name == p[0]["name"] and p[0].get("tag") in (None, "")
    finally:
        ctl.stop()


def test_tag_is_sanitised_and_persisted(tmp_path):
    cfg = make(tmp_path)
    a = Controller(cfg); a.start(); a.set_context(tag="  Front Room / Take 2 "); a.stop()
    b = Controller(cfg); b.start()
    try:
        assert b.state.context.tag == "front-room-take-2"
    finally:
        b.stop()


def test_remote_page_and_tag_api(tmp_path):
    cfg = make(tmp_path)
    ctl = Controller(cfg); ctl.start()
    with TestClient(build_api(ctl, cfg)) as c:
        r = c.get("/remote")
        assert r.status_code == 200 and "RECORD" in r.text.upper()
        assert c.post("/api/context", json={"tag": "porch"}).json()["tag"] == "porch"
    ctl.stop()
