"""Deferred startup: the app shows the UI first, then connects the camera (main
thread) and the lights (background) while the page reports progress."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import manual_lights
from server import config
from server.camera.base import CameraError
from server.controller import Controller


def make_cfg(tmp):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"
    cfg["dmx"]["transport"] = "none"
    manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp / "frames"), "clips": str(tmp / "clips"), "recipes": str(tmp / "recipes.json"),
                    "context": str(tmp / "context.json")}
    cfg["claude"]["rate_limit_ms"] = 0
    return cfg


def test_deferred_start_serves_state_before_hardware(tmp_path):
    ctl = Controller(make_cfg(tmp_path)); ctl.start(deferred=True)
    try:
        s = ctl.get_state()
        assert s["camera"]["connected"] is False and s["camera"]["backend"] == "connecting"
        assert s["startup"] == {"camera": "looking", "lights": "connecting"}
        assert s["lights"] == {}
        with pytest.raises(CameraError, match="not connected yet"):
            ctl.capture_frame()
    finally:
        ctl.stop()


def test_connect_lights_then_camera_reports_progress(tmp_path):
    ctl = Controller(make_cfg(tmp_path)); ctl.start(deferred=True)
    got = []
    ctl.state.subscribe(lambda m: got.append(m["type"]))
    try:
        ctl.connect_lights()
        assert set(ctl.get_state()["lights"]) == {"key", "fill"}
        assert ctl.get_state()["startup"]["lights"] == "ok"
        ctl.connect_camera()
        s = ctl.get_state()
        assert s["camera"]["connected"] and s["camera"]["backend"] == "mock"
        assert s["startup"]["camera"] == "ok"
        assert ctl.capture_frame()["width"] > 0
        assert got.count("startup") >= 2
    finally:
        ctl.stop()


def test_deferred_start_keeps_saved_context(tmp_path):
    cfg = make_cfg(tmp_path)
    a = Controller(cfg); a.start(); a.set_context(preset="reel", scene="person_portrait"); a.stop()
    b = Controller(cfg); b.start(deferred=True)
    try:
        assert b.state.context.preset == "reel"
        b.connect_camera()
        assert b.camera.subject == "person_portrait" and b.camera.settings["orientation"] == "portrait"
    finally:
        b.stop()


def test_plain_start_has_no_startup_phase(tmp_path):
    ctl = Controller(make_cfg(tmp_path)); ctl.start()
    try:
        assert ctl.get_state()["startup"] == {"camera": "ok", "lights": "ok"}
    finally:
        ctl.stop()


def test_state_follows_a_backend_reconnect(tmp_path):
    """The EDSDK backend reconnects by itself from its idle hook; the controller must notice
    without a restart, so the pill and the startup strip follow."""
    ctl = Controller(make_cfg(tmp_path)); ctl.start(deferred=True)
    try:
        ctl.connect_camera()
        # pretend the body went away and came back behind the controller's back
        ctl.state.camera["connected"] = False
        ctl.state.startup["camera"] = "waiting"
        got = []
        ctl.state.subscribe(lambda m: got.append(m["type"]))
        s = ctl.get_state()
        assert s["camera"]["connected"] is True and s["startup"]["camera"] == "ok"
        assert "camera" in got and "startup" in got
        # and the other way round: a lost body shows as waiting
        ctl.camera._connected = False
        s = ctl.get_state()
        assert s["camera"]["connected"] is False and s["startup"]["camera"] == "waiting"
    finally:
        ctl.stop()
