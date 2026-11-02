"""camlab Mac app launcher.

Runs the same server as `python -m server.main` inside a pywebview (WKWebView)
window, with the Cocoa run loop driving the main-thread job loop:

  * the window is created first (a small "starting" page) so it appears at once;
  * an NSTimer on the main run loop fires every 20 ms: the first tick builds the
    controller + server on the MAIN thread (Canon's EDSDK only discovers cameras
    and delivers events there) and starts uvicorn in a background thread; every
    later tick calls LOOP.tick() (drains main-thread jobs, pumps EDSDK events);
  * once the port answers, the window loads http://127.0.0.1:<port>/;
  * closing the window (or Cmd-Q) stops uvicorn, closes the camera and exits.

Works both from the venv (`CAMLAB_CAMERA=mock .venv/bin/python app/launcher.py`)
and frozen by PyInstaller (app/camlab.spec -> dist/camlab.app).

Environment:
  CAMLAB_CAMERA   auto | edsdk | gphoto2 | ccapi | mock   (overrides [camera].backend)
  CAMLAB_PORT     TCP port (overrides [server].port)
  CAMLAB_HOME     config/data folder (default: ~/Library/Application Support/camlab when frozen)
  EDSDK_FRAMEWORK path to EDSDK.framework/EDSDK (set automatically to the bundled copy when frozen)
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
REPO = Path(__file__).resolve().parent.parent
if not FROZEN and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# ---- environment, before any server import ---------------------------------
# ffmpeg/ffprobe: Finder-launched apps get a minimal PATH without Homebrew.
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")

if FROZEN and not os.environ.get("EDSDK_FRAMEWORK"):
    # Contents/MacOS/camlab -> Contents/Frameworks/EDSDK.framework/EDSDK
    bundled = Path(sys.executable).resolve().parent.parent / "Frameworks" / "EDSDK.framework" / "EDSDK"
    if bundled.exists():
        os.environ["EDSDK_FRAMEWORK"] = str(bundled)

from server import config  # noqa: E402
from server.main import build, setup_logging, start_threads  # noqa: E402
from server.mainloop import LOOP  # noqa: E402

log = logging.getLogger("camlab.app")

WINDOW_TITLE = "camlab"
WINDOW_SIZE = (1280, 860)            # landscape work
WINDOW_SIZE_PORTRAIT = (1100, 1000)  # a Reel: the viewfinder is a tall column, give it height


def window_size(cfg: dict) -> tuple[int, int]:
    """Open taller when the last session was a portrait (Reel) context."""
    import json
    try:
        with open(cfg["paths"]["context"]) as f:
            if json.load(f).get("orientation") == "portrait":
                return WINDOW_SIZE_PORTRAIT
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    return WINDOW_SIZE
TICK_S = 0.02

SPLASH = """<!doctype html><html><head><meta charset="utf-8"><title>camlab</title>
<style>html,body{height:100%;margin:0;background:#0B0C0E;color:#8A9099;font:13px -apple-system,Inter,Helvetica,sans-serif;
display:flex;align-items:center;justify-content:center;letter-spacing:.02em}
.w{text-align:center}.m{font:600 11px -apple-system,Helvetica,sans-serif;letter-spacing:.22em;color:#F2EDE4;margin-bottom:14px}
.bar{width:220px;height:2px;background:#23262c;margin:0 auto 12px;overflow:hidden;border-radius:1px}
.bar i{display:block;width:40%;height:100%;background:#C9A86A;animation:s 1.1s ease-in-out infinite alternate}
@keyframes s{from{transform:translateX(-100%)}to{transform:translateX(250%)}}</style></head>
<body><div class="w"><div class="m">CAMLAB</div><div class="bar"><i></i></div><p id="t">starting the server&hellip;</p></div></body></html>"""


def load_config() -> dict:
    cfg = config.load()
    cam = os.environ.get("CAMLAB_CAMERA", "").strip().lower()
    if cam:
        if cam not in ("auto", "edsdk", "gphoto2", "ccapi", "mock"):
            raise SystemExit(f"CAMLAB_CAMERA={cam!r}: expected auto|edsdk|gphoto2|ccapi|mock")
        cfg["camera"]["backend"] = cam
    port = os.environ.get("CAMLAB_PORT", "").strip()
    if port:
        cfg["server"]["port"] = int(port)
    return cfg


def wait_for_port(port: int, timeout: float = 60.0) -> bool:
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class App:
    """Holds the server pieces; built lazily on the first timer tick (main thread)."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.url = f"http://127.0.0.1:{cfg['server']['port']}/"
        self.window = None
        self.ctl = None
        self.server = None
        self.threads: list[threading.Thread] = []
        self.built = False
        self.camera_pending = False
        self.stopped = False
        self._in_tick = False
        self._lock = threading.Lock()

    # -- main-thread work --------------------------------------------------
    def tick(self) -> None:
        if self._in_tick or self.stopped:       # EDSDK's pump may spin a nested run loop
            return
        self._in_tick = True
        try:
            if not self.built:
                self.built = True
                self._build()
            elif getattr(self, "camera_pending", False):
                self._connect_camera()
            else:
                LOOP.tick()
        except Exception:                         # noqa: BLE001
            log.exception("main-thread tick failed")
        finally:
            self._in_tick = False

    def _build(self) -> None:
        """Tick 1: state + server only (fast), so the UI is up within a second. The hardware
        follows: lights from a worker thread (Bluetooth scans), the camera on the next tick
        (EDSDK wants the main thread) with a short first discovery; the page shows both phases."""
        log.info("building controller + server (camera backend=%s)", self.cfg["camera"]["backend"])
        try:
            self.ctl, _app, mcp, self.server = build(self.cfg, deferred=True)
        except Exception as e:                    # noqa: BLE001
            log.exception("server build failed")
            self._show_error(f"camlab could not start: {e}")
            return
        self.threads = start_threads(self.server, mcp, stdio=False)
        _app.state.save_dialog = self._save_dialog          # "Save to…" uses the window's native panel
        threading.Thread(target=self._load_when_ready, name="load-ui", daemon=True).start()
        threading.Thread(target=self.ctl.connect_lights, name="connect-lights", daemon=True).start()
        self.camera_pending = True

    def _save_dialog(self, default_dir: str, filename: str):
        """Native save panel (pywebview dispatches to the main thread). Returns a path or None."""
        import webview
        if self.window is None:
            return None
        try:
            res = self.window.create_file_dialog(webview.FileDialog.SAVE, directory=default_dir, save_filename=filename)
        except Exception as e:                    # noqa: BLE001
            log.warning("save dialog failed: %s", e)
            return None
        if isinstance(res, (list, tuple)):
            return res[0] if res else None
        return res

    def _connect_camera(self) -> None:
        self.camera_pending = False
        if self.ctl is not None:
            self.ctl.connect_camera()

    def _load_when_ready(self) -> None:
        if wait_for_port(self.cfg["server"]["port"]):
            log.info("UI ready at %s", self.url)
            if self.window is not None:
                self.window.load_url(self.url)
        else:
            self._show_error("server did not start listening on port %d" % self.cfg["server"]["port"])

    def _show_error(self, msg: str) -> None:
        if self.window is not None:
            self.window.load_html(SPLASH.replace("starting server, looking for camera&hellip;", msg)
                                  .replace("#ddd", "#f88"))

    # -- shutdown (main thread, after webview.start() returns) ---------------
    def stop(self) -> None:
        with self._lock:
            if self.stopped:
                return
            self.stopped = True
        log.info("shutting down")
        if self.server is not None:
            self.server.should_exit = True
        try:
            if self.ctl is not None:
                self.ctl.stop()
        except Exception:                         # noqa: BLE001
            log.exception("controller stop failed")
        for t in self.threads:
            t.join(timeout=10)
        LOOP.end_external()


def install_timer(app: App):
    """NSTimer on the main run loop (common modes, so it keeps firing during window drags)."""
    from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

    class Ticker(NSObject):
        def tick_(self, _timer):
            app.tick()

    ticker = Ticker.alloc().init()
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(TICK_S, ticker, "tick:", None, True)
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    return ticker, timer


def main() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise SystemExit("camlab launcher must run on the main thread")
    cfg = load_config()
    setup_logging(False, cfg["paths"]["logs"])
    log.info("camlab app start  frozen=%s home=%s root=%s edsdk=%s", FROZEN, cfg["paths"]["home"],
             cfg["paths"]["root"], os.environ.get("EDSDK_FRAMEWORK", "vendor"))

    import webview

    app = App(cfg)
    size = window_size(cfg)
    app.window = webview.create_window(WINDOW_TITLE, html=SPLASH, width=size[0], height=size[1],
                                       resizable=True, min_size=(800, 500), background_color="#111111")

    # Window close -> webview.start() returns -> finally. Cmd-Q -> NSApp.terminate exits the process
    # without unwinding Python, but pywebview fires `closing` (on the main thread) first, so shut
    # down there too; stop() is idempotent.
    def on_closing():
        app.stop()                              # (returning False here would CANCEL the close in pywebview)
    app.window.events.closing += on_closing

    LOOP.begin_external()
    keep = install_timer(app)                   # noqa: F841  (keep the target alive)
    try:
        webview.start(gui="cocoa")              # NSApplication run loop; returns when the window closes
    finally:
        app.stop()
    logging.shutdown()
    os._exit(0)                                 # don't wait on daemon threads / BLE loops


if __name__ == "__main__":
    main()
