"""camlab entry point.

  python -m server.main            HTTP UI on :8765 + MCP (streamable HTTP) at /mcp
  python -m server.main --stdio    same, plus MCP over stdio for Claude Desktop

The Mac app (app/launcher.py) reuses build() and start_threads() and runs
the main loop from the Cocoa run loop instead of serve_forever().
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import logging
import logging.handlers
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from server import config  # noqa: E402
from server.api import build as build_api  # noqa: E402
from server.controller import Controller  # noqa: E402
from server.mcp_tools import build as build_mcp  # noqa: E402


def make_app(cfg: dict, ctl: Controller):
    mcp = build_mcp(ctl)
    mcp_app = mcp.http_app(path="/mcp")
    api = build_api(ctl, cfg)
    # Run both lifespans: camlab's (state -> websocket wiring) and FastMCP's session manager.
    camlab_lifespan = api.state.camlab_lifespan

    @asynccontextmanager
    async def lifespan(app):
        async with camlab_lifespan(app), mcp_app.lifespan(app):
            yield

    api.router.lifespan_context = lifespan
    api.mount("/", mcp_app)
    return api, mcp


def setup_logging(verbose: bool, log_dir: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]   # stdout is MCP's in --stdio mode
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(Path(log_dir) / "camlab.log",
                                                             maxBytes=5_000_000, backupCount=3))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", handlers=handlers, force=True)
    logging.getLogger("bleak").setLevel(logging.WARNING)  # its DEBUG logs every BLE advert


def build(cfg: dict, deferred: bool = False):
    """Controller (hardware connected unless deferred), ASGI app, MCP server, uvicorn server. Call on the main thread."""
    ctl = Controller(cfg)
    ctl.start(deferred=deferred)
    app, mcp = make_app(cfg, ctl)
    uv = uvicorn.Config(app, host=cfg["server"]["host"], port=cfg["server"]["port"],
                        log_level="warning", lifespan="on")
    server = uvicorn.Server(uv)
    url = f"http://{cfg['server']['host']}:{cfg['server']['port']}"
    logging.getLogger("camlab").info("UI %s   MCP %s/mcp   camera=%s", url, url, ctl.state.camera["model"] or "(connecting)")
    return ctl, app, mcp, server


def start_threads(server, mcp, stdio: bool = False) -> list[threading.Thread]:
    """uvicorn (and MCP stdio) in background threads; the main thread stays free for
    jobs that must run there (Canon EDSDK: device discovery and events only work on
    the main thread, see server/mainloop.py)."""
    threads = [threading.Thread(target=server.run, name="uvicorn", daemon=True)]
    if stdio:
        threads.append(threading.Thread(target=lambda: mcp.run(transport="stdio", show_banner=False),
                                        name="mcp-stdio", daemon=True))
    for t in threads:
        t.start()
    return threads


def serve_forever(ctl, server, threads: list[threading.Thread]) -> None:
    from .mainloop import LOOP
    try:
        LOOP.run(lambda: server.should_exit or not threads[0].is_alive())
    except KeyboardInterrupt:
        server.should_exit = True
        threads[0].join(timeout=10)
    finally:
        ctl.stop()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="camlab")
    p.add_argument("--stdio", action="store_true", help="also serve MCP over stdio (Claude Desktop)")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--camera", choices=["auto", "edsdk", "gphoto2", "ccapi", "mock"])
    p.add_argument("-v", action="store_true")
    a = p.parse_args(argv)

    cfg = config.load()
    setup_logging(a.v, cfg["paths"].get("logs"))
    if a.port: cfg["server"]["port"] = a.port
    if a.host: cfg["server"]["host"] = a.host
    if a.camera: cfg["camera"]["backend"] = a.camera

    ctl, app, mcp, server = build(cfg)
    threads = start_threads(server, mcp, a.stdio)
    serve_forever(ctl, server, threads)


if __name__ == "__main__":
    main()
