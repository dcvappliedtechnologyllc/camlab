"""MCP tool surface (spec §4). Frames come back as real image content so the
model can see them, plus JSON analysis as text. All tools are thin wrappers
around Controller; guardrails live there, not here."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from . import presets
from .controller import Controller

INSTRUCTIONS = """camlab: you are optimizing a tethered camera + key light for a target look.
Workflow: get_state -> capture_frame -> compare analysis.target_check to context.target ->
change ONE or TWO things (set_exposure / set_light / set_white_balance / set_focus) with a
one-line reason -> capture_frame again -> read `delta` -> repeat. Report each iteration with
claude_report(message, iteration). Stop when target_check.pass is true and the frame looks
right to your eye (specular hot spots, catchlight, framing), then claude_report(status="converged").
Hard rules: never call record unless the user has armed; never exceed claude.cap iterations
without asking; save_recipe needs the user to confirm in the UI.
The UI has a chat line. When the user types there, state.claude.request becomes "message" and
the text shows up as role "user" in claude.messages (also returned by wait_for_request as
unread_user_messages). Answer it with claude_report; that is the only channel the user sees.
"optimize" / "step" requests mean the user clicked a button: act on them. When idle, call
wait_for_request to block for the next instruction instead of polling get_state."""


def build(ctl: Controller) -> FastMCP:
    mcp = FastMCP("camlab", instructions=INSTRUCTIONS)

    def j(obj: Any) -> str:
        return json.dumps(obj, default=str)

    async def run(fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    @mcp.tool
    async def get_state() -> str:
        """Camera model, lens, mode, all current settings, light levels, active preset/scene,
        orientation, target vector, Claude loop request/status, armed flag, recent history.
        Call this first. If claude.request is set, the UI is asking you to optimize/step."""
        return j(await run(ctl.get_state))

    @mcp.tool
    async def capture_frame(analyze: bool = True) -> list:
        """Grab a live-view frame. Returns the JPEG (look at it) plus analysis JSON: luma stats,
        clip %, WB estimate, focus metric, subject/skin ROI stats, geometry, flags, target_check
        and `delta` vs the previous capture."""
        fr = await run(ctl.capture_frame, analyze, True, "claude")
        jpeg = fr.pop("jpeg")
        return [Image(data=jpeg, format="jpeg"), j(fr)]

    @mcp.tool
    async def set_exposure(iso: str | None = None, shutter: str | None = None, aperture: str | None = None,
                           ev_comp: str | None = None, reason: str | None = None) -> str:
        """Set any of iso ("400"), shutter ("1/125"), aperture ("f/5.6"), ev_comp ("-1/3").
        Camera snaps to the nearest legal step; applied values are returned. For video keep
        shutter at ~1/(2*fps) (180-degree rule) and prefer light/aperture/ISO changes."""
        return j(await run(ctl.set_camera, {"iso": iso, "shutter": shutter, "aperture": aperture, "ev_comp": ev_comp}, "claude", reason))

    @mcp.tool
    async def set_white_balance(mode: str | None = None, kelvin: int | None = None, reason: str | None = None) -> str:
        """mode: auto|daylight|tungsten|kelvin|... (see capabilities.wb_mode). kelvin sets mode=kelvin."""
        return j(await run(ctl.set_camera, {"wb_mode": mode, "kelvin": kelvin}, "claude", reason))

    @mcp.tool
    async def set_picture_style(style: str, reason: str | None = None) -> str:
        """Picture style from capabilities.picture_style (neutral|standard|portrait|faithful|...)."""
        return j(await run(ctl.set_camera, {"picture_style": style}, "claude", reason))

    @mcp.tool
    async def set_focus(mode: str | None = None, drive: str | None = None,
                        af_point: list[float] | None = None, reason: str | None = None) -> str:
        """mode: af_one_shot|af_servo|mf. drive: near3..near1|far1..far3 (relative MF step).
        af_point: [x, y] in 0-1. Returns the new focus metric and its delta: iterate drive
        until the metric peaks."""
        return j(await run(ctl.set_focus, mode, drive, af_point, "claude", reason))

    @mcp.tool
    async def set_light(id: str, intensity_pct: float | None = None, cct_k: int | None = None,
                        reason: str | None = None) -> str:
        """Set a light's intensity (0-100) and/or colour temperature (K). id: key|fill.
        Values clamp to the fixture profile. A non-scriptable light (fill) is only recorded."""
        return j(await run(ctl.set_light, id, intensity_pct, cct_k, "claude", reason))

    @mcp.tool
    async def set_movie_mode(resolution: str, fps: int, codec: str | None = None) -> str:
        """resolution: 1080p|4k ; fps: 24|30|60. Validated against the body's capabilities."""
        return j(await run(ctl.set_movie_mode, resolution, fps, codec, "claude"))

    @mcp.tool
    async def record(action: str) -> str:
        """action: start|stop. start is REFUSED unless the user armed recording in the UI
        within the last 60 s. Never call this unprompted."""
        try:
            return j(await run(ctl.record, action, "claude"))
        except PermissionError as e:
            return j({"error": str(e), "armed": False})

    @mcp.tool
    async def pull_clips(since: float | None = None) -> str:
        """Copy new movie files from the card to clips/YYYY-MM-DD/. Never deletes. Returns the list."""
        return j(await run(ctl.pull_clips, since))

    @mcp.tool
    async def extract_frames(clip_id: str, timestamps: list[float]) -> list:
        """Extract frames (seconds) from a pulled clip and analyze them like live captures."""
        out = await run(ctl.extract_frames, clip_id, timestamps)
        content: list = []
        for r in out:
            content.append(Image(data=r.pop("jpeg"), format="jpeg"))
            content.append(j(r))
        return content

    @mcp.tool
    async def save_recipe(name: str, notes: str | None = None) -> str:
        """Snapshot full state as a recipe (preset+scene+lens keyed). Returns pending_confirm;
        the user must confirm in the UI."""
        return j(await run(ctl.save_recipe, name, notes, "claude"))

    @mcp.tool
    async def load_recipe(id: str | None = None, preset: str | None = None, scene: str | None = None,
                          lens: str | None = None) -> str:
        """Load by id, or match {preset, scene, lens} to the most recent recipe and apply it."""
        match = {"preset": preset, "scene": scene, "lens": lens} if preset and scene else None
        try:
            return j(await run(ctl.load_recipe, id, match, "claude"))
        except KeyError as e:
            return j({"error": str(e)})

    @mcp.tool
    async def list_recipes() -> str:
        """All saved recipes."""
        return j(ctl.recipes.list())

    @mcp.tool
    async def list_presets() -> str:
        """Output presets, scene types, lenses, FOV table and target vectors."""
        return j(presets.list_all())

    @mcp.tool
    async def set_context(preset: str | None = None, scene: str | None = None, lens: str | None = None,
                          focal_mm: float | None = None, orientation: str | None = None,
                          roi: list[float] | None = None) -> str:
        """Set what the clip is for and what's in frame. roi = [x,y,w,h] in 0-1 for the subject."""
        return j(await run(ctl.set_context, preset, scene, lens, focal_mm, orientation, roi, False, "claude"))

    @mcp.tool
    async def claude_report(message: str, status: str | None = None, iteration: int | None = None) -> str:
        """Post a one-line explanation of what you changed and why; it shows in the UI.
        status: running|converged|stopped. Set converged when within targets and it looks right."""
        return j(await run(ctl.claude_report, message, status, iteration))

    @mcp.tool
    async def wait_for_request(timeout_s: float = 30) -> str:
        """Block up to timeout_s (max 120) until the user clicks Optimize/Step/Stop or types a
        message in the UI chat line. Returns {request, status, unread_user_messages, timed_out,
        context}. Call this in a loop while idle; reply to messages with claude_report."""
        return j(await run(ctl.wait_for_request, timeout_s))

    @mcp.tool
    async def undo() -> str:
        """Revert the most recent camera or light change."""
        return j(await run(ctl.undo, "claude"))

    return mcp
