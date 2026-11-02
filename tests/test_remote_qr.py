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
        c.port = cfg["server"]["port"]
        yield c
    ctl.stop()


def test_remote_url(client):
    r = client.get("/api/remote_url").json()
    assert r["url"].startswith("http://") and r["url"].endswith(f":{client.port}/remote")
    assert r["url"] == f"http://{r['ip']}:{client.port}/remote"


def test_remote_qr_svg(client):
    r = client.get("/api/remote_qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text


def test_lan_ip_falls_back(monkeypatch):
    import socket
    from server import api

    class Boom:
        def __init__(self, *a, **k): raise OSError("no network")
    monkeypatch.setattr(socket, "socket", Boom)
    assert api.lan_ip() == "127.0.0.1"
