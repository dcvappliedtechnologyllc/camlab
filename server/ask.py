"""Ask Claude: one API call per message, a proposal back, the human applies it.

ask(text, clip=None) sends the current frame (or the clip's contact sheet), the compact
CAMLAB/1 state lines, the preset's look, the body's choices, the standing preferences and the
owner's words. The model answers with one `propose` tool call; the server applies nothing.
apply(id) pushes a proposal's camera then lights through the Controller (source "ask", one
batch id, so a single Controller.undo() reverts the whole proposal). The last MEMORY exchanges
are kept per process so follow-ups ("a bit warmer") work; reset() clears them. Price metering
and the daily cap are shared with AI Auto (autopilot.cost_usd, Autopilot.spent_today).
Errors come back as AskError(sentence, status, code), never a traceback.
"""
from __future__ import annotations

import logging
import secrets
import threading
from collections import OrderedDict
from typing import Any, Callable

from . import handoff, presets
from .autopilot import MAX_TOKENS, Autopilot, cost_usd, create_with_fallback

log = logging.getLogger("camlab.ask")

MEMORY = 6              # earlier exchanges sent with each new message
KEEP_PROPOSALS = 30

TOOL: dict[str, Any] = {
    "name": "propose",
    "description": "Your whole answer: one plain sentence for the owner, the camera and light values you suggest "
                   "(only the ones that should change), and where to put each light. camlab shows it as a card; "
                   "the owner decides whether to apply it.",
    "input_schema": {"type": "object", "properties": {
        "sentence": {"type": "string", "description": "one plain sentence for a non-photographer: what you see and what the change does"},
        "camera": {"type": "object", "properties": {
            "iso": {"type": "string"}, "shutter": {"type": "string"}, "aperture": {"type": "string"},
            "kelvin": {"type": "integer"}}, "additionalProperties": False},
        "lights": {"type": "object", "properties": {
            lid: {"type": "object", "properties": {"intensity_pct": {"type": "number"}, "cct_k": {"type": "integer"}},
                  "additionalProperties": False} for lid in ("key", "fill")}, "additionalProperties": False},
        "placement": {"type": "array", "items": {"type": "string"},
                      "description": "one sentence per light that should move; empty if none should"},
        "verdict": {"type": ["string", "null"], "enum": ["keep", "retake", None], "description": "clips only, else null"},
        "trim": {"type": ["array", "null"], "items": {"type": "number"}, "description": "clips only: [in_s, out_s], else null"}},
        "required": ["sentence", "camera", "lights", "placement", "verdict", "trim"], "additionalProperties": False},
}

SYSTEM = """You are camlab's Ask Claude. camlab tethers one Canon camera and two Godox lights (key and fill) for short
clips. The owner is not a photographer: they ask a question in their own words, you look at the picture and the
state, and you answer with exactly ONE call to the tool `propose`. Nothing is changed until the owner presses Apply,
so propose only what you would actually do next.

The state comes as CAMLAB/1 lines: `now cam:` and `now lights:` (percent@kelvin) are the current values, `want:` the
targets (luma = brightness of the middle of the picture, 0 black to 1 white; clipH/clipL = % of the picture pure
white/black; K = colour the camera sees; skin = typical skin brightness), `got:` what the picture measures (OFF marks
a miss), `prefs:` standing preferences. Underscores in values stand for spaces. A clip comes as a contact sheet:
four frames with the time burned in. A still photo comes as a `still=` line with the look the owner picked (the
look's sentence is the look wanted); for photos the video shutter rule does not apply, and keep/retake is a
verdict on the photo.

Rules for `propose`:
- sentence: one plain sentence, no jargon left unexplained.
- camera and lights: only the values that should change, picked from the choices listed; lights are clamped to their
  range. Prefer moving the lights over raising ISO on a person. Keep video shutter near 1/(2 x frame rate). Match the
  lights' kelvin to the camera's unless you want a deliberate cast. Kelvin anchors: warm about 3200 K (tungsten,
  cosy), neutral about 4000-4500 K, daylight about 5600 K (cool, clean); 4300 K is warm-neutral, not cool.
- placement: one sentence per light that should move, from where the photographer stands behind the camera looking
  at the subject: left or right of the lens, degrees off the lens axis, height against the subject's eyes, distance
  in feet (never metres), and how to make it soft (bounce card, diffuser, closer and bigger) when soft light is wanted. Empty when
  nothing should move.
- verdict and trim: only for a clip (keep or retake; trim [in_s, out_s] in seconds); otherwise null.
- If the subject is missing from the picture or the camera is on an auto mode that would undo your changes, say so in
  the sentence and propose no values. If the picture already looks right, say so and propose nothing."""

CAMERA_LABEL = {"iso": "ISO", "shutter": "shutter", "aperture": "aperture", "kelvin": "camera colour"}


class AskError(Exception):
    """A plain sentence for the owner, the HTTP status the API should answer with and a short code."""
    def __init__(self, sentence: str, status: int = 400, code: str = "error"):
        super().__init__(sentence)
        self.status, self.code = status, code


def _plain_api_error(e: Exception) -> str:
    name, status = type(e).__name__, getattr(e, "status_code", None)
    if status == 401 or name == "AuthenticationError":
        return "Claude refused the API key. Check api_key under [ai] in config.toml."
    if status == 429 or name == "RateLimitError":
        return "Claude is busy right now (rate limit). Wait a minute and ask again."
    if status in (500, 502, 503, 529) or name in ("InternalServerError", "OverloadedError"):
        return "Claude is overloaded or having trouble right now. Try again in a minute."
    if "Timeout" in name or "Connection" in name:
        return "camlab could not reach Claude (no internet, or it took too long). Check the connection and ask again."
    return f"Claude could not answer this time ({status or name}). Try again; the camlab log has the details."


def _block(b: Any) -> dict[str, Any] | None:
    """An SDK content block as a plain dict that can be sent back next turn."""
    t = getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")
    g = (lambda k: b.get(k)) if isinstance(b, dict) else (lambda k: getattr(b, k, None))  # noqa: E731
    if t == "text":
        return {"type": "text", "text": g("text") or ""} if (g("text") or "").strip() else None
    if t == "tool_use":
        return {"type": "tool_use", "id": g("id"), "name": g("name"), "input": dict(g("input") or {})}
    if t == "thinking":
        return {"type": "thinking", "thinking": g("thinking") or "", "signature": g("signature") or ""}
    if t == "redacted_thinking":
        return {"type": "redacted_thinking", "data": g("data")}
    return None


class Ask:
    def __init__(self, ctl, cfg: dict[str, Any], client_factory: Callable[[], Any] | None = None):
        self.ctl, self.cfg, self.client_factory = ctl, cfg, client_factory
        self.exchanges: list[dict[str, Any]] = []
        self.proposals: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._plain = False
        self._lock = threading.Lock()

    # ---- plumbing ----------------------------------------------------------
    def configured(self) -> bool:
        return self.ctl.autopilot.configured()

    def _client(self):
        if self.client_factory:
            return self.client_factory()
        import anthropic
        return anthropic.Anthropic(api_key=self.ctl.autopilot._key())

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self.exchanges.clear()
        return {"reset": True}

    def _state_text(self, text: str, lines: list[str], clip: str | None, still: str | None = None) -> str:
        s = self.ctl.get_state()
        ctx, caps = s["context"], s["camera"].get("capabilities", {})
        pr = presets.PRESETS.get(ctx["preset"], {})
        sc = presets.SCENES.get(ctx["scene"], {})
        if still or s.get("stills_mode"):
            from .stills import LOOKS
            lk = LOOKS.get(s.get("stills_look") or "", {})
            if lk:
                pr = {"label": f"Still photo, look {lk['label']}", "look": lk["sentence"]}
                sc = presets.SCENES.get(lk["scene"], sc)
        choices = [f"{k}={','.join(map(str, caps[k]))}" for k in ("iso", "shutter", "aperture") if caps.get(k)]
        if caps.get("kelvin_range"):
            choices.append(f"kelvin={caps['kelvin_range'][0]}-{caps['kelvin_range'][1]}")
        head = "CAMLAB/1 photo review" if still else "CAMLAB/1 clip review" if clip else "CAMLAB/1 live"
        return "\n".join([
            head,
            f"setup: {pr.get('label', ctx['preset'])}, look wanted: {pr.get('look', '-')}; scene {sc.get('label', ctx['scene'])}",
            *[l for l in lines if not l.startswith("last:")],
            "choices: " + " ".join(choices),
            f"owner: {text}"])

    def _messages(self, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hist = self.exchanges[-MEMORY:]
        msgs: list[dict[str, Any]] = []
        for i, ex in enumerate(hist):
            content: list[dict[str, Any]] = []
            if i > 0 and hist[i - 1].get("tool_id"):
                content.append(self._result(hist[i - 1]))
            content.append({"type": "text", "text": ex["text"] + "\n(picture from then not repeated)"})
            msgs.append({"role": "user", "content": content})
            msgs.append({"role": "assistant", "content": ex["assistant"]})
        lead = [self._result(hist[-1])] if hist and hist[-1].get("tool_id") else []
        msgs.append({"role": "user", "content": lead + current})
        return msgs

    @staticmethod
    def _result(ex: dict[str, Any]) -> dict[str, Any]:
        said = "applied this proposal" if ex.get("applied") else "has not applied this proposal"
        return {"type": "tool_result", "tool_use_id": ex["tool_id"], "content": f"Shown to the owner, who {said}."}

    # ---- ask -----------------------------------------------------------------
    def ask(self, text: str, clip: str | None = None, still: str | None = None) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise AskError("Type a question first.", 400, "empty")
        if not self.configured():
            raise AskError("Ask Claude needs an API key: add api_key under [ai] in config.toml, or use the "
                           "Generate / Ingest strings with any chat instead.", 409, "no_key")
        try:
            self.ctl.autopilot.check_daily()
        except RuntimeError as e:
            raise AskError(f"Today's spending cap is reached: {e}.", 429, "daily_cap") from e
        with self._lock:
            return self._ask(text[:2000], clip, still)

    def _ask(self, text: str, clip: str | None, still: str | None = None) -> dict[str, Any]:
        ctl = self.ctl
        try:
            if still:
                entry = ctl.stills.get(still)
                if entry is None:
                    raise KeyError(f"photo {still!r} is not on this Mac")
                jpeg = ctl.stills.jpeg_for_ask(entry["label"])
                lines = handoff.state_lines(ctl, still=entry)
                still = entry["label"]
            elif clip:
                sheet = handoff.build_sheet(ctl, clip)
                jpeg = sheet.pop("jpeg")
                lines = handoff.state_lines(ctl, sheet=sheet)
                clip = sheet["clip"]
            else:
                fr = ctl.capture_frame(analyze=True, save=False, source="ask")
                jpeg = fr["jpeg"]
                lines = handoff.state_lines(ctl, a=fr.get("analysis", {}))
        except KeyError as e:
            raise AskError(str(e).strip("'\""), 404, "no_clip") from e
        except Exception as e:  # noqa: BLE001
            log.warning("ask: no picture: %s", e)
            raise AskError(f"There is no picture to show Claude right now: {e}", 409, "no_picture") from e
        state_text = self._state_text(text, lines, clip, still)
        current = [Autopilot._image_block(jpeg), {"type": "text", "text": state_text}]
        ai = self.cfg.get("ai", {})
        model = ai.get("model", "claude-opus-5-5")
        kw = dict(model=model, max_tokens=MAX_TOKENS, system=SYSTEM, tools=[TOOL], messages=self._messages(current),
                  output_config={"effort": ai.get("effort", "medium")},
                  timeout=max(5.0, min(float(ai.get("max_run_s", 240)), 120.0)))
        try:
            resp, self._plain = create_with_fallback(self._client(), self._plain, **kw)
        except Exception as e:  # noqa: BLE001
            log.warning("ask: API call failed: %s: %s", type(e).__name__, e)
            raise AskError(_plain_api_error(e), 502, "api") from e
        cost = cost_usd(getattr(resp, "model", None) or model, getattr(resp, "usage", None))
        ctl.autopilot.spent_today += cost
        stop = getattr(resp, "stop_reason", None)
        blocks = [x for x in (_block(b) for b in (getattr(resp, "content", None) or [])) if x]
        base = {"id": None, "sentence": "", "diff": [], "placement": [], "camera": {}, "lights": {}, "verdict": None,
                "trim": None, "cost_usd": round(cost, 4), "clip": clip, "still": still, "error": None}
        if stop == "refusal":
            return base | {"sentence": "Claude declined to answer this one. Try asking it a different way.", "error": "refusal"}
        tu = next((b for b in blocks if b["type"] == "tool_use" and b["name"] == "propose"), None)
        if tu is None:
            said = " ".join(b["text"].strip() for b in blocks if b["type"] == "text").strip()
            if blocks:
                self._remember(state_text, blocks, None, None)
            if stop == "max_tokens":
                return base | {"sentence": "Claude's answer ran too long and was cut off. Ask again, a bit more specifically.",
                               "error": "max_tokens"}
            return base | {"sentence": said or "Claude did not suggest anything this time.", "error": "no_proposal"}
        inp = tu["input"]
        pid = secrets.token_hex(4)
        prop = base | {"id": pid, "sentence": str(inp.get("sentence") or "").strip(),
                       "camera": self._clean_camera(inp.get("camera")), "lights": self._clean_lights(inp.get("lights")),
                       "placement": [str(p) for p in (inp.get("placement") or []) if str(p).strip()],
                       "verdict": inp.get("verdict") if inp.get("verdict") in ("keep", "retake") else None,
                       "trim": self._clean_trim(inp.get("trim"))}
        prop["diff"] = self._diff(prop)
        self.proposals[pid] = prop
        while len(self.proposals) > KEEP_PROPOSALS:
            self.proposals.popitem(last=False)
        self._remember(state_text, blocks, tu["id"], pid)
        return dict(prop)

    def _remember(self, text: str, blocks: list[dict[str, Any]], tool_id: str | None, pid: str | None) -> None:
        self.exchanges.append({"text": text, "assistant": blocks, "tool_id": tool_id, "proposal": pid, "applied": False})
        del self.exchanges[:-MEMORY]

    # ---- shaping -------------------------------------------------------------
    @staticmethod
    def _clean_camera(cam: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in (cam or {}).items() if isinstance(cam, dict) else []:
            if k in CAMERA_LABEL and v not in (None, ""):
                if k == "kelvin":
                    try: out[k] = int(float(v))
                    except (TypeError, ValueError): continue
                else:
                    out[k] = str(v)
        return out

    def _clean_lights(self, lights: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for lid, spec in (lights or {}).items() if isinstance(lights, dict) else []:
            if lid not in self.ctl.lights or not isinstance(spec, dict):
                continue
            s = {}
            if spec.get("intensity_pct") is not None:
                s["intensity_pct"] = spec["intensity_pct"]
            if spec.get("cct_k") is not None:
                s["cct_k"] = int(spec["cct_k"])
            if s:
                out[lid] = s
        return out

    @staticmethod
    def _clean_trim(trim: Any) -> list[float] | None:
        try:
            if isinstance(trim, (list, tuple)) and len(trim) == 2:
                return [float(trim[0]), float(trim[1])]
        except (TypeError, ValueError):
            pass
        return None

    def _diff(self, p: dict[str, Any]) -> list[dict[str, str]]:
        st = self.ctl.get_state()
        cur = st["camera"].get("settings", {})
        out = []
        for k, v in p["camera"].items():
            was = cur.get(k)
            if str(was) != str(v):
                f = (lambda x: f"{x} K" if x is not None else "-") if k == "kelvin" else (lambda x: "-" if x is None else str(x))  # noqa: E731
                out.append({"what": CAMERA_LABEL[k], "from": f(was), "to": f(v)})
        for lid, spec in p["lights"].items():
            now = st["lights"].get(lid, {})
            if "intensity_pct" in spec and round(float(now.get("intensity_pct") or 0)) != round(float(spec["intensity_pct"])):
                out.append({"what": f"{lid} light", "from": f"{round(float(now.get('intensity_pct') or 0))} %",
                            "to": f"{round(float(spec['intensity_pct']))} %"})
            if "cct_k" in spec and now.get("cct_k") != spec["cct_k"]:
                out.append({"what": f"{lid} light colour", "from": f"{now.get('cct_k')} K", "to": f"{spec['cct_k']} K"})
        return out

    # ---- apply ---------------------------------------------------------------
    def apply(self, pid: str) -> dict[str, Any]:
        p = self.proposals.get(pid or "")
        if p is None:
            raise AskError("That suggestion is no longer available. Ask again.", 404, "unknown_proposal")
        ctl = self.ctl
        if ctl.state.recording:
            raise AskError("recording: nothing changes while a take rolls. Stop recording, then apply.", 409, "recording")
        out: dict[str, Any] = {"id": pid, "applied_camera": {}, "applied_lights": {}, "errors": [], "sidecar_path": None}
        reason = f"Ask Claude: {p['sentence'][:80]}"
        try:
            if p["camera"]:
                try:
                    out["applied_camera"] = ctl.set_camera(dict(p["camera"]), source="ask", reason=reason, batch=pid)
                except PermissionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"camera: {e}")
            for lid, spec in p["lights"].items():
                try:
                    st = ctl.set_light(lid, spec.get("intensity_pct"), spec.get("cct_k"), source="ask", reason=reason, batch=pid)
                    out["applied_lights"][lid] = {"intensity_pct": st["intensity_pct"], "cct_k": st["cct_k"],
                                                  "scriptable": st["scriptable"]}
                except PermissionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"light {lid}: {e}")
        except PermissionError as e:
            raise AskError(f"{e}. Stop recording, then apply.", 409, "recording") from e
        if p.get("clip") and (p.get("verdict") or p.get("trim")):
            try:
                out["sidecar_path"] = handoff.write_sidecar(ctl, p["clip"], {
                    "verdict": p["verdict"], "trim": p["trim"], "reason": p["sentence"], "source": "ask"})
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"sidecar: {e}")
        for ex in self.exchanges:
            if ex.get("proposal") == pid:
                ex["applied"] = True
        return out
