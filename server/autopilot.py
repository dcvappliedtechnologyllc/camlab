"""AI Auto: the server calls the Claude API itself, shows the model the live
frame plus the analysis, and lets it drive camera and lights through the
Controller (so every guardrail stays where it is: rate limit, clamps, snapping
to the body's lists, no record/pull/delete tools).

One run = look -> adjust -> look ... until the model calls `done`, the look
cap (claude.iteration_cap), the per-run cost cap, the wall clock, a Stop, or a
REC start. Reasoning goes to the Claude timeline; tool calls land in history
with source "ai". A manual tool-use loop (not the SDK's tool runner) because
tool results carry image blocks and usage is metered per response.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from typing import Any, Callable

from . import presets

log = logging.getLogger("camlab.ai")

# $ per million tokens (input, output); cache reads are 5 % of input. Unknown models use Opus prices.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
CACHE_READ_FRACTION = 0.05
FRAME_LONG_EDGE = 768
MAX_TOKENS = 4096
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def cost_usd(model: str, usage: Any) -> float:
    inp, out = PRICES.get(model, PRICES["claude-opus-5-5"])
    g = lambda k: float(getattr(usage, k, 0) or 0)  # noqa: E731
    return (g("input_tokens") * inp + g("cache_creation_input_tokens") * inp * 1.25
            + g("cache_read_input_tokens") * inp * CACHE_READ_FRACTION + g("output_tokens") * out) / 1e6


def create_with_fallback(client, plain: bool, **kw) -> tuple[Any, bool]:
    """Opus 5.5 and friends: server-side refusal fallback when the SDK and the API accept it;
    otherwise the plain call (a TypeError from the SDK, or a 400 from the API). Returns
    (response, plain) so the caller remembers to skip the beta next time."""
    if not plain:
        try:
            return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw), False
        except TypeError:
            plain = True
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) == 400 or type(e).__name__ == "BadRequestError":
                log.warning("fallback beta rejected (%s); continuing without it", e)
                plain = True
            else:
                raise
    return client.messages.create(**kw), plain


TOOLS: list[dict[str, Any]] = [
    {"name": "look", "description": "Capture a fresh live-view frame after your changes. Returns the picture plus "
                                    "the measurements and target_check, and the delta against the previous look. "
                                    "Each look is one round against the cap.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "set_camera", "description": "Change camera settings. Values are the camera's own strings (iso \"800\", "
                                          "shutter \"1/60\", aperture \"f/4\"); the body snaps to its nearest legal step "
                                          "and the applied values are returned. For video keep the shutter near 1/(2*fps).",
     "input_schema": {"type": "object", "properties": {
         "iso": {"type": "string"}, "shutter": {"type": "string"}, "aperture": {"type": "string"},
         "ev_comp": {"type": "string"}, "wb_mode": {"type": "string"}, "kelvin": {"type": "integer"},
         "picture_style": {"type": "string"},
         "reason": {"type": "string", "description": "one line, shown to the operator"}},
         "additionalProperties": False}},
    {"name": "set_light", "description": "Set a light's brightness (percent) and/or colour temperature (kelvin). "
                                         "Values are clamped to the fixture's range; a light marked not scriptable is "
                                         "only recorded.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "key or fill"},
         "intensity_pct": {"type": "number"}, "cct_k": {"type": "integer"},
         "reason": {"type": "string"}},
         "required": ["id"], "additionalProperties": False}},
    {"name": "focus", "description": "Nudge focus (drive near1/near2/far1/far2) or set the focus mode. Returns the "
                                     "sharpness metric and its change.",
     "input_schema": {"type": "object", "properties": {
         "drive": {"type": "string", "enum": ["near1", "near2", "far1", "far2"]},
         "mode": {"type": "string"}, "reason": {"type": "string"}},
         "additionalProperties": False}},
    {"name": "done", "description": "End the run. status converged when target_check passes and the picture looks "
                                    "right to you; stopped when you cannot get there within the caps. The summary is "
                                    "shown to the operator.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["converged", "stopped"]},
         "summary": {"type": "string"}},
         "required": ["status", "summary"], "additionalProperties": False}},
]

TARGET_WORDS = {
    "luma_median": "brightness of the middle of the whole picture, 0-1",
    "clip_high_pct_max": "max % of the picture that is pure white",
    "clip_low_pct_max": "max % of the picture that is pure black",
    "cct_k": "colour of the light as the camera sees it, kelvin",
    "subject_p1_min": "floor for the darkest 1 % of the subject (black parts keep detail)",
    "subject_median": "typical brightness of the subject",
    "subject_p99_max": "ceiling for the brightest 1 % of the subject",
    "skin_luma": "typical brightness of skin",
    "clip_high_pct_face_max": "max % of the face that is pure white",
    "skew_deg_max": "max tilt of the bench/page in degrees",
    "uniformity_max": "max unevenness of the lighting across the frame",
    "dof": "depth of field the output wants",
    "aperture": "f-stop range the output wants",
}

SYSTEM = """You are camlab's AI Auto: you set up a tethered camera and two lights for a specific output, before a take.
The operator is not a photographer; explain each change in one plain sentence.

How to work: you are given the current frame, its measurements and the targets. Change one or two things with
set_camera / set_light / focus, giving a one-line reason, then call look to see the result and the delta. Repeat.
Call done(status="converged") when target_check passes AND the picture looks right to your eye (no blown
highlights on skin or specular edges, catchlight present on a person, the subject readable). Call
done(status="stopped") if the targets cannot be reached within the caps, saying what the operator should change
by hand (move a light, reposition). Never spend more than the look cap. Prefer light changes over ISO for
brightness on a person; keep video shutter near 1/(2*fps); keep kelvin inside the target band and set wb_mode
to kelvin when you change kelvin. Do not ask questions; act."""


CHECK_WORDS = {
    "luma_median": "brightness", "clip_high_pct": "pure white", "clip_low_pct": "pure black",
    "wb_estimate_k": "colour", "subject_p1": "darkest part of the subject", "subject_median": "subject brightness",
    "subject_p99": "brightest part of the subject", "skin_luma": "skin brightness",
    "face_clip_high_pct": "white on the face", "skew_deg": "tilt", "uniformity": "unevenness",
}


def _num(v: Any) -> str:
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.2f}" if abs(v) < 10 else f"{v:.0f}"
    return str(v)


def plain_check(tc: dict[str, Any] | None) -> str:
    """One line a non-photographer can read: what is off target and which way.
    'brightness 0.72 is above 0.48-0.60; colour 6250 K is above 3800-5200 K' or 'all targets met'."""
    if not tc:
        return "no measurements"
    off = []
    for key, r in tc.items():
        if key == "pass" or not isinstance(r, dict) or r.get("ok", True):
            continue
        word = CHECK_WORDS.get(key, key)
        unit = " K" if key == "wb_estimate_k" else ("%" if "pct" in key else ("°" if key == "skew_deg" else ""))
        v = r.get("value")
        if v is None:
            off.append(f"{word}: nothing to measure (no face/subject found)")
        elif "range" in r:
            lo, hi = r["range"]
            side = "below" if v < lo else "above"
            off.append(f"{word} {_num(v)}{unit} is {side} {_num(lo)}-{_num(hi)}{unit}")
        elif "max" in r:
            off.append(f"{word} {_num(v)}{unit} is over the {_num(r['max'])}{unit} limit")
        elif "min" in r:
            off.append(f"{word} {_num(v)}{unit} is under the {_num(r['min'])}{unit} floor")
    return "all targets met" if not off else "; ".join(off)


class Autopilot:
    def __init__(self, ctl, cfg: dict[str, Any], client_factory: Callable[[], Any] | None = None):
        self.ctl, self.cfg = ctl, cfg
        self.client_factory = client_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.run_id = 0
        self.spent_today = 0.0          # across runs, this process, resets at midnight
        self._spent_day = time.strftime("%Y-%m-%d")

    # ---- config ------------------------------------------------------------
    def _key(self) -> str:
        return (self.cfg.get("ai", {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()

    def configured(self) -> bool:
        return bool(self._key())

    @property
    def running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def status(self) -> dict[str, Any]:
        cl = self.ctl.state.claude
        return {"running": self.running, "status": cl.status, "iteration": cl.iteration, "cost_usd": cl.cost_usd,
                "run_id": self.run_id}

    def check_daily(self) -> None:
        """The per-day spend cap, shared by AI Auto runs and Ask Claude messages (this process)."""
        today = time.strftime("%Y-%m-%d")
        if today != self._spent_day:
            self._spent_day, self.spent_today = today, 0.0
        daily = float(self.cfg.get("ai", {}).get("daily_cap_usd", 5.0))
        if self.spent_today >= daily:
            raise RuntimeError(f"daily cap ${daily:.2f} reached (${self.spent_today:.2f} spent today); raise ai.daily_cap_usd or wait for tomorrow")

    # ---- control -----------------------------------------------------------
    def start(self, mode: str = "optimize") -> dict[str, Any]:
        if mode not in ("optimize", "step"):
            raise ValueError("mode must be optimize|step")
        with self._lock:
            if self.ctl.state.recording:
                raise RuntimeError("recording: AI Auto adjusts the picture before a take, not during one")
            if self.running:
                raise RuntimeError("running: AI Auto is already on a run; Stop it first")
            self.check_daily()
            self._stop.clear()
            self.run_id += 1
            with self.ctl.lock:
                cl = self.ctl.state.claude
                cl.request, cl.status, cl.iteration, cl.mode, cl.cost_usd = None, "running", 0, "ai", 0.0
                cl.run_id = self.run_id
                self._say("ui", f"AI Auto {mode} for {self.ctl.state.context.preset}/{self.ctl.state.context.scene}")
            self._thread = threading.Thread(target=self._run, args=(mode, self.run_id), name="ai-auto", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()

    # ---- the loop ----------------------------------------------------------
    def _say(self, role: str, text: str) -> None:
        cl = self.ctl.state.claude
        cl.messages.append({"ts": time.time(), "role": role, "text": text, "iteration": cl.iteration, "status": cl.status})
        self.ctl.state.broadcast({"type": "claude", "claude": self.ctl.get_state()["claude"]})

    def _finish(self, status: str, note: str, rounds: int, cost: float) -> None:
        with self.ctl.lock:
            cl = self.ctl.state.claude
            cl.status, cl.request, cl.cost_usd = status, None, round(cost, 4)
            self._say("claude", f"AI Auto {status} after {rounds} round{'s' if rounds != 1 else ''}, ${cost:.2f}"
                                + (f": {note}" if note else ""))
        log.info("ai auto %s after %d rounds $%.3f %s", status, rounds, cost, note)

    def _client(self):
        if self.client_factory:
            return self.client_factory()
        import anthropic
        return anthropic.Anthropic(api_key=self._key())

    def _create(self, client, **kw):
        resp, self._plain = create_with_fallback(client, self._plain, **kw)
        return resp

    @staticmethod
    def _image_block(jpeg: bytes) -> dict[str, Any]:
        from PIL import Image
        im = Image.open(io.BytesIO(jpeg))
        im.thumbnail((FRAME_LONG_EDGE, FRAME_LONG_EDGE))
        buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=80)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(buf.getvalue()).decode()}}

    def _frame_text(self, fr: dict[str, Any]) -> str:
        a = fr.get("analysis", {})
        return json.dumps({"analysis": fr.get("analysis_summary"), "target_check": a.get("target_check"),
                           "roi": {k: {kk: vv for kk, vv in v.items() if kk != "bbox"} for k, v in a.get("roi", {}).items()},
                           "delta": fr.get("delta"), "orientation": fr.get("orientation")}, default=str)

    def _brief(self) -> str:
        s = self.ctl.get_state()
        ctx, cam = s["context"], s["camera"]
        pr = presets.PRESETS.get(ctx["preset"], {})
        target = {k: {"want": v, "means": TARGET_WORDS.get(k, "")} for k, v in (ctx.get("target") or {}).items()}
        lights = {lid: {k: l.get(k) for k in ("model", "intensity_pct", "cct_k", "intensity_min_pct", "intensity_max_pct",
                                              "cct_min_k", "cct_max_k", "scriptable")} for lid, l in s["lights"].items()}
        caps = {k: cam["capabilities"].get(k) for k in ("iso", "shutter", "aperture", "wb_mode", "picture_style", "kelvin_range", "focus_drive")}
        return json.dumps({"output": {"preset": ctx["preset"], "look": pr.get("look"), "scene": ctx["scene"],
                                      "orientation": ctx["orientation"]},
                           "target": target, "camera": {"model": cam["model"], "settings": cam["settings"], "choices": caps},
                           "lights": lights, "look_cap": self.ctl.state.claude.cap}, default=str)

    def _dispatch(self, name: str, inp: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """Run one tool. Returns (result blocks, is_error)."""
        ctl = self.ctl
        try:
            if name == "look":
                fr = ctl.capture_frame(analyze=True, save=True, source="ai")
                self._say("ui", f"Look {ctl.state.claude.iteration}: {self._look_line(fr)}")
                return [self._image_block(fr["jpeg"]), {"type": "text", "text": self._frame_text(fr)}], False
            if name == "set_camera":
                reason = inp.pop("reason", None)
                applied = ctl.set_camera(inp, source="ai", reason=reason)
                what = ", ".join(f"{k} {v}" for k, v in (applied or inp).items() if k != "reason") or "camera"
                self._say("ui", f"Changed {what}" + (f" — {reason}" if reason else ""))
                return [{"type": "text", "text": json.dumps({"applied": applied})}], False
            if name == "set_light":
                reason = inp.get("reason")
                st = ctl.set_light(inp["id"], inp.get("intensity_pct"), inp.get("cct_k"), source="ai", reason=reason)
                self._say("ui", f"Set {inp['id']} light to {_num(float(st.get('intensity_pct') or 0)):s}% at {st.get('cct_k')} K"
                                + (f" — {reason}" if reason else "") + ("" if st.get("scriptable", True) else " (set it by hand)"))
                return [{"type": "text", "text": json.dumps({k: st.get(k) for k in ("id", "intensity_pct", "cct_k", "scriptable", "note")})}], False
            if name == "focus":
                out = ctl.set_focus(mode=inp.get("mode"), drive=inp.get("drive"), source="ai", reason=inp.get("reason"))
                self._say("ui", f"Focus {inp.get('drive') or inp.get('mode')}: sharpness now {_num(out.get('focus_metric'))}"
                                + (f" — {inp['reason']}" if inp.get("reason") else ""))
                return [{"type": "text", "text": json.dumps({k: out.get(k) for k in ("drive", "focus_metric", "focus_delta")}, default=str)}], False
            return [{"type": "text", "text": f"unknown tool {name}"}], True
        except Exception as e:  # noqa: BLE001  -- the model gets the error and can try something else
            self._say("ui", f"{name} did not apply ({e}); Claude will try something else")
            return [{"type": "text", "text": f"{type(e).__name__}: {e}"}], True

    def _look_line(self, fr: dict[str, Any]) -> str:
        a = fr.get("analysis") or {}
        tc = a.get("target_check")
        s = fr.get("analysis_summary") or {}
        head = f"brightness {_num(s.get('luma_median'))}, colour {_num(s.get('wb_estimate_k'))} K"
        return f"{head}. {plain_check(tc)}" if tc is not None else head

    def _run(self, mode: str, run_id: int) -> None:
        cfg = self.cfg.get("ai", {})
        cap_usd = float(cfg.get("cost_cap_usd", 1.0))
        max_s = float(cfg.get("max_run_s", 240))
        model = cfg.get("model", "claude-opus-5-5")
        effort = cfg.get("effort", "medium")
        look_cap = int(self.ctl.state.claude.cap)
        call_cap = 6 * look_cap
        cost, looks, calls, changed = 0.0, 1, 0, False      # the opening frame is round 1
        t0 = time.time()
        self._plain = False
        try:
            client = self._client()
            fr = self.ctl.capture_frame(analyze=True, save=True, source="ai")
            intro = ("Mode: one round only — make your change(s), look once, then done." if mode == "step"
                     else "Mode: optimize until converged or the caps.")
            self._say("ui", f"Targets for {self.ctl.state.context.preset}/{self.ctl.state.context.scene}; "
                            f"Claude ({model}) may look up to {look_cap} times, cost cap ${cap_usd:.2f}, {int(max_s)} s")
            messages: list[dict[str, Any]] = [{"role": "user", "content": [
                self._image_block(fr["jpeg"]),
                {"type": "text", "text": f"{intro}\nSetup: {self._brief()}\nCurrent frame: {self._frame_text(fr)}"}]}]
            with self.ctl.lock:
                self.ctl.state.claude.iteration = looks
                self._say("ui", f"Look 1: {self._look_line(fr)}")
            while True:
                if self._stop.is_set() or self.ctl.state.recording:
                    return self._finish("stopped", "", looks, cost)
                if cost >= cap_usd:
                    return self._finish("cap", f"cost cap ${cap_usd:.2f} reached", looks, cost)
                if looks >= look_cap or calls >= call_cap:
                    return self._finish("cap", f"look cap {look_cap} reached", looks, cost)
                remaining = max_s - (time.time() - t0)
                if remaining <= 0:
                    return self._finish("cap", f"time cap {int(max_s)} s reached", looks, cost)
                with self.ctl.lock:
                    self._say("ui", f"Claude is thinking (round {looks} of {look_cap}, ${cost:.2f} so far)")
                resp = self._create(client, model=model, max_tokens=MAX_TOKENS, system=SYSTEM, tools=TOOLS,
                                    messages=messages, output_config={"effort": effort},
                                    timeout=max(5.0, min(remaining, 120.0)))
                step_cost = cost_usd(getattr(resp, "model", None) or model, getattr(resp, "usage", None))
                cost += step_cost
                self.spent_today += step_cost
                with self.ctl.lock:
                    self.ctl.state.claude.cost_usd = round(cost, 4)
                if getattr(resp, "stop_reason", None) == "refusal":
                    return self._finish("error", "the API declined this request (refusal)", looks, cost)
                if self._stop.is_set() or self.ctl.state.recording:
                    return self._finish("stopped", "", looks, cost)
                content = list(getattr(resp, "content", []) or [])
                messages.append({"role": "assistant", "content": content})
                tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
                for b in content:
                    if getattr(b, "type", None) == "text" and b.text.strip():
                        with self.ctl.lock:
                            self._say("claude", b.text.strip())
                if not tool_uses:
                    return self._finish("stopped", "the model ended without calling done", looks, cost)
                results: list[dict[str, Any]] = []
                finish: tuple[str, str] | None = None
                for tu in tool_uses:
                    calls += 1
                    inp = dict(tu.input or {})
                    if tu.name == "done":
                        finish = (inp.get("status") or "stopped", inp.get("summary") or "")
                        results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "ok"})
                        continue
                    if self._stop.is_set() or self.ctl.state.recording:      # a REC between two tool calls
                        return self._finish("stopped", "", looks, cost)
                    if tu.name == "look":
                        if looks >= look_cap:
                            return self._finish("cap", f"look cap {look_cap} reached", looks, cost)
                        looks += 1
                        with self.ctl.lock:
                            self.ctl.state.claude.iteration = looks
                    elif tu.name in ("set_camera", "set_light", "focus"):
                        changed = True
                    blocks, err = self._dispatch(tu.name, inp)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": blocks, "is_error": err})
                    if tu.name == "look" and mode == "step" and changed and finish is None:
                        finish = ("stopped", "one round done")
                messages.append({"role": "user", "content": results})
                if finish:
                    return self._finish(finish[0], finish[1], looks, cost)
        except Exception as e:  # noqa: BLE001
            log.exception("ai auto run failed")
            self._finish("error", f"{type(e).__name__}: {e}", looks, cost)
