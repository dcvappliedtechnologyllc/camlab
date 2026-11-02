import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def no_real_bluetooth(monkeypatch):
    """macOS kills a process that touches CoreBluetooth without a grant (TCC,
    signal 134), and the tool shell never gets a prompt. No test may reach the
    radio: mesh tests go through FakeNode, everything else gets manual lights."""
    try:
        import bleak
    except ImportError:
        return

    async def boom(*a, **kw):
        raise AssertionError("test reached the real Bluetooth radio")

    def no_client(*a, **kw):
        raise AssertionError("test built a real BleakClient")
    monkeypatch.setattr(bleak.BleakScanner, "discover", boom)
    monkeypatch.setattr(bleak, "BleakClient", no_client)


def manual_lights(cfg: dict) -> dict:
    """config.toml points both lights at the real mesh export; tests that are
    not about the mesh get ManualLight instead."""
    for prof in cfg["lights"].values():
        prof["backend"] = "manual"
        prof.pop("mesh_export", None)
        prof.pop("mesh_address", None)
    return cfg



@pytest.fixture(autouse=True)
def no_auto_pull(monkeypatch):
    """Older tests pull by hand right after Stop; the background auto-pull would race them.
    tests/test_takes.py turns it back on."""
    from server.controller import Controller
    monkeypatch.setattr(Controller, "AUTO_PULL", False)
