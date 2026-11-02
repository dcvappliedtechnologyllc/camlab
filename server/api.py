"""REST + WebSocket API for ui/index.html. Mirrors the MCP tools 1:1."""
from __future__ import annotations

import asyncio
import json
import io
import logging
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import handoff, howto, presets, stills
from .ask import AskError
from .camera.base import CameraError
from .controller import Controller

log = logging.getLogger("camlab.api")
ROOT = Path(__file__).resolve().parent.parent


class CameraBody(BaseModel):
    iso: str | None = None; shutter: str | None = None; aperture: str | None = None; ev_comp: str | None = None
    wb_mode: str | None = None; kelvin: int | None = None; picture_style: str | None = None
    movie_resolution: str | None = None; movie_fps: int | None = None; reason: str | None = None

class FocusBody(BaseModel):
    mode: str | None = None; drive: str | None = None; af_point: list[float] | None = None

class LightBody(BaseModel):
    intensity_pct: float | None = None; cct_k: int | None = None

class ContextBody(BaseModel):
    preset: str | None = None; scene: str | None = None; lens: str | None = None; focal_mm: float | None = None
    orientation: str | None = None; roi: list[float] | None = None; clear_roi: bool = False
    rotation: str | None = None; tag: str | None = None

class RecipeBody(BaseModel):
    name: str; notes: str | None = None

class LoadBody(BaseModel):
    id: str | None = None; preset: str | None = None; scene: str | None = None; lens: str | None = None

class RequestBody(BaseModel):
    request: str | None = None   # optimize | step | stop

class MessageBody(BaseModel):
    text: str

class ActionBody(BaseModel):
    action: str

class FramesBody(BaseModel):
    clip_id: str; timestamps: list[float]

class HandoffReply(BaseModel):
    text: str; force: bool = False

class HandoffBody(BaseModel):
    clip: str | None = None
    still: str | None = None

class SheetBody(BaseModel):
    clip: str

class AskBody(BaseModel):
    text: str; clip: str | None = None; still: str | None = None

class AskApplyBody(BaseModel):
    id: str

class RevealBody(BaseModel):
    path: str

class ExportBody(BaseModel):
    clip: str

class TakeBody(BaseModel):
    label: str


class Hub:
    """WebSocket fan-out + live-view loop that only runs while someone is watching."""
    def __init__(self, ctl: Controller, fps: float, hud_s: float = 3.0):
        self.ctl, self.fps, self.hud_s = ctl, fps, hud_s
        self.clients: set[WebSocket] = set()
        self.live_task: asyncio.Task | None = None
        self.last_hud = 0.0
        self.ready_s = float(getattr(ctl, "cfg", {}).get("server", {}).get("ready_s", 2.0))
        self.last_ready = 0.0

    async def ready_tick(self, fr: dict | None) -> None:
        """Every ~2 s: one face look on the frame just sent + readiness; simple.py broadcasts
        {"type": "ready", ...} only when an item changes."""
        if self.ready_s <= 0 or time.time() - self.last_ready < self.ready_s:
            return
        self.last_ready = time.time()
        import base64
        jpeg = base64.b64decode(fr["jpeg_b64"]) if fr and fr.get("jpeg_b64") else b""
        try:
            await asyncio.to_thread(self.ctl.simple.tick, jpeg)
        except Exception as e:  # noqa: BLE001  -- readiness never breaks live view
            log.debug("ready tick failed: %s", e)

    async def send_all(self, msg: dict) -> None:
        data = json.dumps(msg, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def on_state(self, msg: dict):
        return self.send_all(msg)

    async def live_loop(self) -> None:
        period = 1.0 / max(0.5, self.fps)
        while self.clients:
            t0 = time.time()
            fr = None
            try:
                fr = await asyncio.to_thread(self.ctl.live_frame)
                if self.hud_s > 0 and t0 - self.last_hud >= self.hud_s:
                    self.last_hud = t0
                    try:
                        fr["hud"] = await asyncio.to_thread(self.ctl.hud_analysis)
                    except Exception as e:  # noqa: BLE001  -- a bad measurement never breaks live view
                        log.warning("hud analysis failed: %s", e)
                await self.send_all({"type": "live", **fr})
            except Exception as e:
                # a camera that is still being looked for is not an error worth a toast every second
                if "not connected yet" not in str(e) and "off or asleep" not in str(e):
                    await self.send_all({"type": "error", "where": "live", "error": str(e)})
                await asyncio.sleep(1.0)
            await self.ready_tick(fr)
            await asyncio.sleep(max(0.0, period - (time.time() - t0)))
        self.live_task = None

    def ensure_live(self) -> None:
        if self.clients and (self.live_task is None or self.live_task.done()):
            self.live_task = asyncio.create_task(self.live_loop())


def lan_ip() -> str:
    """The Mac's LAN address, found without sending anything (UDP connect only)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def build(ctl: Controller, cfg: dict[str, Any]) -> FastAPI:
    hub = Hub(ctl, cfg["server"]["liveview_fps"], float(cfg["server"].get("liveview_hud_s", 3)))

    @asynccontextmanager
    async def lifespan(_app):
        # wire state broadcasts -> websocket fan-out on the running loop
        ctl.state.attach_loop(asyncio.get_running_loop())
        unsub = ctl.state.subscribe(hub.on_state)
        try:
            yield
        finally:
            unsub()

    app = FastAPI(title="camlab", version="0.1", lifespan=lifespan)
    app.state.hub = hub
    app.state.camlab_lifespan = lifespan

    async def run(fn, *a, **kw):
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except AskError as e:
            return JSONResponse(status_code=e.status, content={"detail": str(e), "code": e.code})
        except handoff.WrongRound as e:
            return JSONResponse(status_code=409, content={"detail": str(e), "code": e.code,
                                                          "expected": e.expected, "got": e.got})
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except (KeyError, ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        except CameraError as e:
            raise HTTPException(409, str(e))
        except Exception as e:
            log.exception("api error")
            raise HTTPException(500, f"{type(e).__name__}: {e}")

    # ---- static ----
    ui = ROOT / "ui"
    assets = ROOT / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        p = ui / "index.html"
        return HTMLResponse(p.read_text() if p.exists() else "<h1>camlab</h1><p>ui/index.html missing</p>")

    @app.get("/remote", response_class=HTMLResponse)
    async def remote():
        p = ui / "remote.html"
        return HTMLResponse(p.read_text() if p.exists() else "<h1>camlab</h1><p>ui/remote.html missing</p>")

    def _remote_url() -> dict[str, str]:
        ip = lan_ip()
        return {"url": f"http://{ip}:{cfg['server']['port']}/remote", "ip": ip}

    @app.get("/api/remote_url")
    async def remote_url():
        return _remote_url()

    @app.get("/api/remote_qr.svg")
    async def remote_qr():
        import segno
        buf = io.BytesIO()
        segno.make(_remote_url()["url"], error="m").save(buf, kind="svg", scale=8, border=2, dark="#000", light="#fff", xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    # ---- takes: serve to the phone, send, AirDrop-ish ----
    def _base_url(request: Request) -> str:
        return f"http://{lan_ip()}:{cfg['server']['port']}"

    def _take_url(request: Request, label: str) -> str:
        from urllib.parse import quote
        return f"{_base_url(request)}/clips/{quote(label)}"

    @app.get("/clips/{name:path}")
    async def clip_download(name: str, play: int = 0):
        p = ctl.clip_file(name)
        if p is None:
            raise HTTPException(404, "no such clip")
        media = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime"}[p.suffix.lower()]
        return FileResponse(str(p), media_type=media, filename=p.name, content_disposition_type="inline" if play else "attachment")

    @app.get("/api/takes")
    async def takes(request: Request):
        out = await run(ctl.takes_today)
        return {"takes": [t | {"url": _take_url(request, t["label"])} for t in out]}

    @app.post("/api/takes/send")
    async def take_send(body: TakeBody, request: Request):
        if ctl.clip_file(body.label) is None:
            raise HTTPException(404, "no such clip")
        url = _take_url(request, body.label)
        qr = None
        try:
            import segno
            buf = io.BytesIO()
            segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False)
            qr = buf.getvalue().decode()
        except ImportError:
            pass
        return {"url": url, "qr_svg": qr}

    @app.post("/api/takes/airdrop")
    async def take_airdrop(body: TakeBody):
        """macOS has no command-line AirDrop. Reveal the file in Finder, where Share > AirDrop is one click."""
        p = ctl.clip_file(body.label)
        if p is None:
            raise HTTPException(404, "no such clip")
        import subprocess
        subprocess.Popen(["open", "-R", str(p)])
        return {"method": "finder", "path": str(p)}

    stills.register_routes(app, ctl, run, _take_url)

    @app.get("/frames/{path:path}")
    async def frame_file(path: str):
        p = (Path(cfg["paths"]["frames"]) / path).resolve()
        if not str(p).startswith(str(Path(cfg["paths"]["frames"]).resolve())) or not p.exists():
            raise HTTPException(404)
        return FileResponse(str(p))

    # ---- one-screen core: /api/ready, /api/simple/* (server/simple.py) ----
    from .simple import register_routes as _simple_routes
    _simple_routes(app, ctl, run)

    # ---- state ----
    @app.get("/api/state")
    async def state():
        return await run(ctl.get_state)

    @app.get("/api/presets")
    async def list_presets():
        return presets.list_all()

    @app.post("/api/capture")
    async def capture(analyze: bool = True):
        fr = await run(ctl.capture_frame, analyze, True, "ui")
        fr.pop("jpeg", None)
        return fr

    @app.get("/api/frame/last.jpg")
    async def last_frame():
        lf = ctl.state.last_frame
        if not lf: raise HTTPException(404)
        return Response(lf["jpeg"], media_type="image/jpeg")

    @app.post("/api/camera")
    async def set_camera(body: CameraBody):
        d = body.model_dump(); reason = d.pop("reason")
        return await run(ctl.set_camera, d, "ui", reason)

    @app.post("/api/focus")
    async def set_focus(body: FocusBody):
        return await run(ctl.set_focus, body.mode, body.drive, body.af_point, "ui")

    @app.post("/api/light/{light_id}")
    async def set_light(light_id: str, body: LightBody):
        return await run(ctl.set_light, light_id, body.intensity_pct, body.cct_k, "ui")

    @app.post("/api/context")
    async def set_context(body: ContextBody):
        d = body.model_dump()
        return await run(ctl.set_context, d["preset"], d["scene"], d["lens"], d["focal_mm"], d["orientation"],
                         d["roi"], d["clear_roi"], "ui", d["rotation"], d["tag"])

    @app.post("/api/camera/backend")
    async def switch_backend(body: dict):
        return await run(ctl.switch_camera, str(body.get("backend", "auto")))

    @app.post("/api/undo")
    async def undo():
        return await run(ctl.undo, "ui") or {}

    # ---- claude loop ----
    @app.post("/api/claude")
    async def claude(body: RequestBody):
        return await run(ctl.claude_request, body.request)

    @app.post("/api/claude/message")
    async def claude_message(body: MessageBody):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "empty message")
        return await run(ctl.claude_message, text[:2000])

    # ---- record / clips ----
    @app.post("/api/arm")
    async def arm():
        return {"armed_until": await run(ctl.arm)}

    @app.post("/api/disarm")
    async def disarm():
        ctl.state.disarm(); return {"armed": False}

    @app.post("/api/record")
    async def record(body: ActionBody):
        return await run(ctl.record, body.action, "ui")

    @app.get("/api/howto")
    async def howto_all():
        return {"howto": list(howto.HOWTO.values()), "by_readiness": howto.BY_READINESS}

    @app.get("/api/howto/{hid}")
    async def howto_one(hid: str):
        if hid not in howto.HOWTO:
            raise HTTPException(404, f"no how-to called {hid!r}")
        return howto.HOWTO[hid]

    @app.get("/api/clips")
    async def clips():
        return ctl.clips.index

    @app.post("/api/clips/pull")
    async def pull():
        return await run(ctl.pull_clips, None)

    @app.post("/api/clips/export")
    async def export_clip(body: ExportBody):
        """Save to…: the Mac app installs a native save dialog as app.state.save_dialog; the web version has none."""
        chooser = getattr(app.state, "save_dialog", None)
        out = await run(ctl.export_clip, body.clip, chooser)
        if out.get("path") and out.get("dialog"):
            import subprocess
            subprocess.Popen(["open", "-R", out["path"]])
        return out

    @app.post("/api/clips/frames")
    async def frames(body: FramesBody):
        out = await run(ctl.extract_frames, body.clip_id, body.timestamps)
        for r in out: r.pop("jpeg", None)
        return out

    # ---- recipes ----
    @app.get("/api/recipes")
    async def recipes():
        return ctl.recipes.list()

    @app.post("/api/recipes")
    async def save_recipe(body: RecipeBody):
        return await run(ctl.save_recipe, body.name, body.notes, "ui")

    @app.post("/api/recipes/confirm")
    async def confirm(accept: bool = True):
        return await run(ctl.confirm_recipe, accept)

    @app.post("/api/recipes/load")
    async def load(body: LoadBody):
        match = {"preset": body.preset, "scene": body.scene, "lens": body.lens} if body.preset else None
        return await run(ctl.load_recipe, body.id, match, "ui")

    @app.delete("/api/recipes/{rid}")
    async def delete_recipe(rid: str):
        return {"deleted": ctl.recipes.delete(rid)}

    # ---- chat mode: hand-off file out, config string in ----
    @app.post("/api/handoff")
    async def make_handoff(body: HandoffBody | None = None):
        return await run(handoff.build_handoff, ctl, body.clip if body else None, body.still if body else None)

    @app.post("/api/handoff/apply")
    async def apply_handoff(body: HandoffReply):
        return await run(handoff.apply_reply, ctl, body.text, body.force)

    @app.get("/api/handoff/rounds")
    async def handoff_rounds():
        return await run(handoff.rounds_summary, ctl)

    @app.post("/api/handoff/new_session")
    async def handoff_new_session():
        return await run(handoff.new_session, ctl)

    @app.post("/api/clips/sheet")
    async def clip_sheet(body: SheetBody):
        out = await run(handoff.build_sheet, ctl, body.clip)
        if isinstance(out, dict):
            out.pop("jpeg", None)
        return out

    # ---- Ask Claude: one API call per message, the human applies ----
    @app.post("/api/ask")
    async def ask(body: AskBody):
        return await run(ctl.ask.ask, body.text, body.clip, body.still)

    @app.post("/api/ask/apply")
    async def ask_apply(body: AskApplyBody):
        return await run(ctl.ask.apply, body.id)

    @app.post("/api/ask/reset")
    async def ask_reset():
        return await run(ctl.ask.reset)

    @app.post("/api/handoff/reveal")
    async def reveal_handoff(body: RevealBody):
        home = Path(cfg["paths"].get("home", ROOT)).resolve()
        hod = Path(cfg["paths"].get("handoff", home)).expanduser().resolve()
        p = Path(body.path).resolve()
        if (home not in p.parents and hod not in p.parents) or not p.exists():
            raise HTTPException(400, "not a hand-off file")
        import subprocess
        subprocess.Popen(["open", "-R", str(p)])
        return {"revealed": str(p)}

    @app.get("/api/config")
    async def get_config():
        out = {k: v for k, v in cfg.items() if k != "ai"}
        ai = {k: v for k, v in cfg.get("ai", {}).items() if k != "api_key"}   # never ship the key to a browser
        ai["configured"] = ctl.autopilot.configured() if getattr(ctl, "autopilot", None) else False
        return out | {"ai": ai}

    # ---- websocket ----
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        hub.clients.add(sock)
        hub.ensure_live()
        try:
            await sock.send_text(json.dumps({"type": "state", "state": await asyncio.to_thread(ctl.get_state)}, default=str))
            while True:
                raw = await sock.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "ping":
                    await sock.send_text(json.dumps({"type": "pong", "ts": time.time()}))
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(sock)

    return app
