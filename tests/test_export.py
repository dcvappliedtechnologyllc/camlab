"""Save to…: copy a pulled clip to a place the user picks (native dialog in the app)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import manual_lights
from server import config
from server.controller import Controller


@pytest.fixture
def ctl(tmp_path):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"; manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp_path / "frames"), "clips": str(tmp_path / "clips"), "recipes": str(tmp_path / "recipes.json"),
                    "context": str(tmp_path / "context.json"), "home": str(tmp_path)}
    cfg["claude"]["rate_limit_ms"] = 0
    c = Controller(cfg); c.start()
    c.arm(); c.record("start"); c.record("stop")
    yield c
    c.stop()


def test_export_pulls_then_copies_where_the_chooser_says(ctl, tmp_path):
    dest = tmp_path / "Movies" / "take one.mp4"
    calls = []
    def chooser(default_dir, filename):
        calls.append((default_dir, filename)); dest.parent.mkdir(parents=True, exist_ok=True); return str(dest)
    out = ctl.export_clip(ctl.state.last_clip, chooser)
    assert out["path"] == str(dest) and dest.exists() and dest.stat().st_size > 0
    assert calls and calls[0][1].endswith(".mp4") or calls[0][1].endswith(".MP4")
    # the folder is remembered for next time
    out2 = ctl.export_clip(ctl.state.last_clip, lambda d, f: (calls.append((d, f)), None)[1])
    assert out2["path"] is None and calls[-1][0] == str(dest.parent)


def test_export_without_chooser_reports_the_local_copy(ctl):
    out = ctl.export_clip(ctl.state.last_clip, None)
    assert out["dialog"] is False and Path(out["path"]).exists()


def test_export_unknown_clip(ctl):
    with pytest.raises(KeyError):
        ctl.export_clip("nope.mp4", None)
