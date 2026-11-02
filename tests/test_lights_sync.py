"""A light that reconnects on its own (Godox mesh retry path) shows as connected without a slider move."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import manual_lights
from server import config
from server.controller import Controller
from server.lights.base import LightBackend


class FlakyLight(LightBackend):
    """Connected flag the backend flips by itself, like the mesh client's is_connected."""
    live = False
    def connect(self): return self.live
    def apply(self, intensity_pct, cct_k): pass
    @property
    def connected(self): return self.live


def test_state_follows_a_light_reconnect(tmp_path):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"; manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp_path / "f"), "clips": str(tmp_path / "c"), "recipes": str(tmp_path / "r.json"),
                    "context": str(tmp_path / "ctx.json"), "home": str(tmp_path)}
    ctl = Controller(cfg); ctl.start()
    try:
        light = FlakyLight("key", cfg["lights"]["key"])
        ctl._install_lights({"key": light})
        assert ctl.get_state()["lights"]["key"]["connected"] is False
        events = []
        ctl.state.subscribe(events.append)
        light.live = True                      # the background retry got through
        assert ctl.get_state()["lights"]["key"]["connected"] is True
        assert any(e.get("type") == "lights" for e in events)
        light.live = False                     # and a drop shows too
        assert ctl.get_state()["lights"]["key"]["connected"] is False
    finally:
        ctl.stop()
