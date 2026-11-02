import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import manual_lights
from server import config
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


def test_config_resolves_context_path():
    cfg = config.load()
    assert cfg["paths"]["context"].endswith("context.json")
    assert Path(cfg["paths"]["context"]).parent == Path(cfg["paths"]["home"])


def test_context_persists_across_controllers(tmp_path):
    cfg = make_cfg(tmp_path)
    a = Controller(cfg); a.start()
    try:
        a.set_context(preset="reel", scene="person_portrait", focal_mm=50)
    finally:
        a.stop()
    saved = json.loads((tmp_path / "context.json").read_text())
    assert saved["preset"] == "reel" and saved["orientation"] == "portrait"
    b = Controller(cfg); b.start()
    try:
        c = b.state.context
        assert c.preset == "reel" and c.scene == "person_portrait" and c.orientation == "portrait" and c.focal_mm == 50
    finally:
        b.stop()


def test_context_file_garbage_is_ignored(tmp_path):
    cfg = make_cfg(tmp_path)
    (tmp_path / "context.json").write_text("{not json")
    ctl = Controller(cfg); ctl.start()
    try:
        assert ctl.state.context.preset == "product_hero"
    finally:
        ctl.stop()


def test_set_context_broadcasts_camera(tmp_path):
    cfg = make_cfg(tmp_path)
    ctl = Controller(cfg); ctl.start()
    got = []
    ctl.state.subscribe(lambda m: got.append(m))
    try:
        ctl.set_context(orientation="portrait")
    finally:
        ctl.stop()
    cams = [m for m in got if m["type"] == "camera"]
    assert cams and cams[-1]["camera"]["settings"]["orientation"] == "portrait"


def test_window_size_follows_context(tmp_path):
    from app.launcher import window_size
    cfg = {"paths": {"context": str(tmp_path / "context.json")}}
    assert window_size(cfg) == (1280, 860)
    (tmp_path / "context.json").write_text(json.dumps({"orientation": "portrait"}))
    assert window_size(cfg) == (1100, 1000)
