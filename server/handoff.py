"""Strings: the no-key way to work with any chat (and the compact state Ask Claude sends).

Generate: build_handoff() captures the frame (or, for a clip, a contact sheet), writes
~/Desktop/camlab-handoff/{sample.jpg|sheet.jpg, handoff.md} and returns an 8-line CAMLAB/1
brief, key=value tokens with underscores for spaces in values:

    CAMLAB/1 round=3 sess=a7f2 pic=attached
    clip=reel 9:16 fps=30 lens=35mm(56eq) cam=R50V mode=M
    now cam: iso=250 shutter=1/30 aperture=f/5.6 wb=Color_Temperature kelvin=4300 style=Neutral
    now lights: key=35@4300 fill=47@4300 (0-100%, 2800-6500K)
    want: luma=0.48-0.6 clipH<=2 clipL<=2 K=3800-5200 skin=0.45-0.58 dof=deep
    got: luma=0.729 OFF clipH=0.9 clipL=0 K=4450 skin=-
    last: round=2 you=key=60@4300 fill=40@4300 result=luma 0.81->0.73
    prefs: soft ; Colleen likes soft light

The long explanation (persona, kelvin anchors, feet, behind-the-camera perspective, the
reply formats, the body's choices) goes once per session into preamble.md next to it.
Rounds and the session id persist in <home>/rounds.json (each round's string, the reply,
what was applied and the measurement that followed).

Ingest: parse_reply() reads `CAMLAB/1 reply` / `CAMLAB/1 clipreply` blocks (with or without
a fence, prose around them), and still the old ```camlab block, bare camera:/lights: lines
or JSON. apply_reply() checks round and session, pushes values through the Controller
(source "chat": snapping, clamps, nothing while recording) and, for a clip reply, writes
verdict/trim/caption to a <clip>.json sidecar.
"""
from __future__ import annotations

import base64
import io
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from . import presets

CAMERA_KEYS = ("iso", "shutter", "aperture", "ev_comp", "wb_mode", "kelvin", "picture_style")
ALIASES = {"wb": "wb_mode", "style": "picture_style", "ev": "ev_comp"}
SHORT = {"wb_mode": "wb", "picture_style": "style", "ev_comp": "ev"}
SESSION_IDLE_S = 12 * 3600           # a brief after 12 h of silence starts a new session
SHEET_AT = (0.10, 0.40, 0.70, 0.95)
SHEET_LONG_EDGE = 1024


class WrongRound(Exception):
    """A reply for another round or session. code wrong_round | wrong_session."""
    def __init__(self, code: str, expected: Any, got: Any):
        what = "round" if code == "wrong_round" else "session"
        super().__init__(f"This reply is for {what} {got}, but the current {what} is {expected}. "
                         f"Paste the reply to the latest string, or apply it anyway.")
        self.code, self.expected, self.got = code, expected, got


# ---- small formatting helpers ------------------------------------------------------
def _u(v: Any) -> str:
    """A value as one token: underscores for spaces, '-' for nothing."""
    if v is None or v == "":
        return "-"
    return str(v).strip().replace(" ", "_")


def _g(v: Any, nd: int = 3) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{round(float(v), nd):g}"
    return _u(v)


def _rng(v: Any) -> str:
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return f"{_g(v[0])}-{_g(v[1])}"
    return _g(v)


def _dir(ctl) -> Path:
    d = Path(ctl.cfg["paths"].get("handoff") or "~/Desktop/camlab-handoff").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rounds_path(ctl) -> Path:
    return Path(ctl.cfg["paths"].get("home") or Path(ctl.cfg["paths"]["frames"]).parent) / "rounds.json"


# ---- rounds.json -------------------------------------------------------------------
def _load(ctl) -> dict[str, Any] | None:
    try:
        d = json.loads(_rounds_path(ctl).read_text())
        return d if isinstance(d, dict) and d.get("session") else None
    except (OSError, ValueError):
        return None


def _save(ctl, data: dict[str, Any]) -> None:
    data["updated"] = time.time()
    p = _rounds_path(ctl)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, default=str))


def _fresh() -> dict[str, Any]:
    now = time.time()
    return {"session": secrets.token_hex(2), "started": now, "updated": now, "rounds": []}


def _session(ctl) -> dict[str, Any]:
    d = _load(ctl)
    if d is None or (d.get("rounds") and time.time() - float(d.get("updated") or 0) > SESSION_IDLE_S):
        d = _fresh()
    return d


def new_session(ctl) -> dict[str, Any]:
    """Start over: a new session id, round 0 (the next string is round 1 and comes with the preamble)."""
    d = _fresh()
    _save(ctl, d)
    return {"session": d["session"], "round": 0}


def rounds_summary(ctl) -> dict[str, Any]:
    """Read-only view of rounds.json for the UI's round log (no strings, no replies)."""
    d = _load(ctl) or {"session": None, "rounds": []}
    keep = ("n", "kind", "ts", "clip", "measure", "reply_round", "applied", "next_measure")
    return {"session": d.get("session"),
            "rounds": [{k: r.get(k) for k in keep} | {"replied": bool(r.get("reply"))} for r in d.get("rounds", [])]}


# ---- measurements ------------------------------------------------------------------
_TC = {"luma": "luma_median", "clipH": "clip_high_pct", "clipL": "clip_low_pct", "K": "wb_estimate_k", "skin": "skin_luma"}


def measure(a: dict[str, Any]) -> dict[str, Any]:
    skin = (a.get("roi", {}).get("skin") or a.get("roi", {}).get("face") or {}).get("median")
    tc = a.get("target_check", {}) or {}
    m = {"luma": a.get("luma", {}).get("median"), "clipH": a.get("clip", {}).get("high_pct"),
         "clipL": a.get("clip", {}).get("low_pct"), "K": a.get("channels", {}).get("wb_estimate_k"), "skin": skin}
    m["off"] = [k for k, t in _TC.items() if isinstance(tc.get(t), dict) and tc[t].get("ok") is False]
    return m


def _got_line(ms: list[dict[str, Any]]) -> str:
    """One measurement, or one value per sheet frame joined by commas. OFF after a value outside the target."""
    nd = {"luma": 3, "clipH": 2, "clipL": 2, "K": 0, "skin": 3}
    toks = []
    for k in ("luma", "clipH", "clipL", "K", "skin"):
        each = [_g(m.get(k), nd[k]) for m in ms]
        vals = each[0] if len(set(each)) == 1 else ",".join(each)     # the same on every frame: once
        toks.append(f"{k}={vals}" + (" OFF" if any(k in m.get("off", []) for m in ms) else ""))
    return "got: " + " ".join(toks)


# ---- the brief lines ---------------------------------------------------------------
def _cam_name(model: str | None) -> str:
    return re.sub(r"^(Canon\s+)?(EOS\s+)?", "", str(model or "-")).replace(" ", "") or "-"


def _for(ctx: dict[str, Any], cam: dict[str, Any]) -> str:
    pr = presets.PRESETS.get(ctx["preset"], {})
    fov = ctx.get("lens_fov") or {}
    focal = ctx.get("focal_mm")
    lens = f"{_g(focal)}mm({_g(fov.get('ff_equiv_mm'))}eq)" if focal else "-"
    fps = cam.get("settings", {}).get("movie_fps")
    return (f"{ctx['preset']} {pr.get('frame', '-')} fps={_g(fps)} lens={lens} "
            f"cam={_cam_name(cam.get('model'))} mode={_u(cam.get('mode'))}")


def _cam_line(settings: dict[str, Any]) -> str:
    toks = [f"iso={_u(settings.get('iso'))}", f"shutter={_u(settings.get('shutter'))}",
            f"aperture={_u(settings.get('aperture'))}", f"wb={_u(settings.get('wb_mode'))}",
            f"kelvin={_u(settings.get('kelvin'))}", f"style={_u(settings.get('picture_style'))}"]
    ev = settings.get("ev_comp")
    if ev not in (None, "", "0", "+0", 0, "0.0"):
        toks.append(f"ev={_u(ev)}")
    return "now cam: " + " ".join(toks)


def _lights_line(lights: dict[str, Any]) -> str:
    toks = [f"{lid}={round(float(l.get('intensity_pct') or 0))}@{l.get('cct_k')}" for lid, l in lights.items()]
    if lights:
        ls = list(lights.values())
        lo = min(float(l.get("intensity_min_pct") or 0) for l in ls); hi = max(float(l.get("intensity_max_pct") or 100) for l in ls)
        klo = min(int(l.get("cct_min_k") or 2800) for l in ls); khi = max(int(l.get("cct_max_k") or 6500) for l in ls)
        toks.append(f"({lo:g}-{hi:g}%, {klo}-{khi}K)")
    hand = [lid for lid, l in lights.items() if not l.get("scriptable")]
    if hand:
        toks.append("byhand=" + ",".join(hand))
    return "now lights: " + (" ".join(toks) or "none")


_WANT = [("luma_median", "luma", "="), ("clip_high_pct_max", "clipH", "<="), ("clip_low_pct_max", "clipL", "<="),
         ("cct_k", "K", "="), ("skin_luma", "skin", "="), ("clip_high_pct_face_max", "faceH", "<="),
         ("subject_p1_min", "subP1", ">="), ("subject_median", "sub", "="), ("subject_p99_max", "subP99", "<="),
         ("aperture", "ap", "="), ("skew_deg_max", "skew", "<="), ("uniformity_max", "uneven", "<="), ("dof", "dof", "=")]


def _want_line(target: dict[str, Any]) -> str:
    toks, seen = [], set()
    for key, short, op in _WANT:
        if key in target:
            seen.add(key)
            toks.append(f"{short}{op}{_rng(target[key])}")
    for k, v in target.items():
        if k not in seen and not isinstance(v, bool):
            toks.append(f"{k}={_rng(v)}")
    return "want: " + (" ".join(toks) or "-")


def _applied_tokens(applied: dict[str, Any] | None) -> str:
    if not applied:
        return "-"
    toks = [f"{SHORT.get(k, k)}={_u(v)}" for k, v in (applied.get("camera") or {}).items()]
    toks += [f"{lid}={round(float(v.get('intensity_pct') or 0))}@{v.get('cct_k')}" for lid, v in (applied.get("lights") or {}).items()]
    return " ".join(toks) or "-"


def _last_line(prev: dict[str, Any] | None, now: dict[str, Any] | None) -> str:
    if not prev:
        return "last: none"
    before = (prev.get("measure") or {}).get("luma")
    after = (now or {}).get("luma")
    res = f"luma {_g(before)}->{_g(after)}" if before is not None and after is not None else "-"
    return f"last: round={prev['n']} you={_applied_tokens(prev.get('applied'))} result={res}"


def _prefs_line(text: str) -> str:
    low = text.lower()
    toks = []
    if "soft" in low:
        toks.append("soft")
    if re.search(r"\bcrisp|\bhard\b|\bhard light", low):
        toks.append("crisp")
    k = re.search(r"(\d{4})\s*k\b", low)
    if k:
        toks.append(f"K={k.group(1)}")
    else:
        for w, t in (("warm", "warm"), ("neutral", "neutral"), ("daylight", "daylight"), ("cool", "daylight")):
            if w in low and t not in toks:
                toks.append(t)
    return f"prefs: {' '.join(toks) or '-'} ; {text or 'none'}"


def _prefs(ctl) -> str:
    return str((ctl.cfg.get("chat") or {}).get("preferences") or "").strip()


def still_measure(entry: dict[str, Any]) -> dict[str, Any]:
    """A photo's stored measurement (server/stills.py `measure`) in the brief's own keys."""
    m = entry.get("analysis") or {}
    tc = m.get("target_check", {}) or {}
    return {"luma": m.get("luma_median"), "clipH": m.get("clip_high_pct"), "clipL": m.get("clip_low_pct"),
            "K": m.get("wb_estimate_k"), "skin": m.get("skin_median"),
            "off": [k for k, t in _TC.items() if isinstance(tc.get(t), dict) and tc[t].get("ok") is False]}


def _still_line(entry: dict[str, Any], ctx: dict[str, Any], cam: dict[str, Any]) -> str:
    look = _u(entry.get("look_label") or entry.get("look") or "-")
    return (f"still={_u(entry.get('label'))} look={look} size={entry.get('width')}x{entry.get('height')} "
            f"for={_for(ctx, cam)}")


def state_lines(ctl, a: dict[str, Any] | None = None, sheet: dict[str, Any] | None = None,
                prev: dict[str, Any] | None = None, still: dict[str, Any] | None = None) -> list[str]:
    """Lines 2-8 of the brief (everything but the header). Live: from analysis `a`; clip: from `sheet`;
    photo: from the still's index entry."""
    s = ctl.get_state()
    ctx, cam = s["context"], s["camera"]
    if still:
        first = _still_line(still, ctx, cam)
        ms = [still_measure(still)]
        now = ms[0]
        from .stills import look_recipe
        want = look_recipe(still["look"])["target"] if still.get("look") else (ctx.get("target") or {})
        return [first, _cam_line(cam.get("settings", {})), _lights_line(s["lights"]),
                _want_line(want), _got_line(ms), _last_line(prev, now), _prefs_line(_prefs(ctl))]
    if sheet:
        at = ",".join(_g(t, 2) for t in sheet["frames"])
        first = f"clip={_u(sheet['clip'])} len={sheet['duration_s']:.1f}s at={at} for={_for(ctx, cam)}"
        ms = [m for m in sheet["measures"] if m] or [{}]
        now = {"luma": sum(m.get("luma") or 0 for m in ms) / len(ms)} if any(m.get("luma") is not None for m in ms) else {}
    else:
        first = f"clip={_for(ctx, cam)}"
        ms = [measure(a or {})]
        now = ms[0]
    return [first, _cam_line(cam.get("settings", {})), _lights_line(s["lights"]),
            _want_line(ctx.get("target") or {}), _got_line(ms), _last_line(prev, now), _prefs_line(_prefs(ctl))]


# ---- preamble.md -------------------------------------------------------------------
def preamble(ctl, d: Path) -> str:
    s = ctl.get_state()
    ctx, cam = s["context"], s["camera"]
    pr = presets.PRESETS.get(ctx["preset"], {})
    sc = presets.SCENES.get(ctx["scene"], {})
    caps = cam.get("capabilities", {})
    choices = [f"- {SHORT.get(k, k)}: {', '.join(_u(c) for c in caps[k])}"
               for k in ("iso", "shutter", "aperture", "wb_mode", "picture_style") if caps.get(k)]
    if caps.get("kelvin_range"):
        choices.append(f"- kelvin: {caps['kelvin_range'][0]} to {caps['kelvin_range'][1]}")
    return f"""# camlab: how to read the CAMLAB/1 strings

You are helping a photographer who is NOT a photographer by trade set up one tethered Canon camera
and two Godox lights (key and fill) for a short clip. Keep every explanation to plain sentences.
If a **camlab skill** is available to you, invoke it now and follow it.

## The picture
The frame the camera sees right now is on the photographer's Mac at {d / 'sample.jpg'} (a clip review
uses {d / 'sheet.jpg'}: four frames of the clip with the time burned in). If you can read files, ask
for access to the folder {d} and open it yourself; otherwise ask for it to be attached. Never answer
from the numbers alone: wait until you can see the picture.

## The string camlab sends each round (one line each)
1. `CAMLAB/1 round=N sess=XXXX pic=attached`: round counter and session; your reply quotes both.
2. `clip=`: the output (preset, frame shape, frame rate), lens focal length (full-frame equivalent), body, mode.
3. `now cam:`: current camera settings. `now lights:` brightness percent @ kelvin per light, the ranges,
   `byhand=` lights the photographer sets by hand.
4. `want:`: targets. luma = brightness of the middle of the picture, 0 black to 1 white; clipH/clipL = %
   of the picture pure white/black; K = colour of the light the camera sees; skin = typical skin
   brightness; dof = depth of field wanted.
5. `got:`: what this sample measured; OFF marks a value outside its target.
6. `last:`: what you suggested last round, as applied, and how luma moved.
7. `prefs:`: standing preferences (soft, crisp, K=3200 ...) then the photographer's own words.
Values never contain spaces: an underscore stands for a space.

This shoot: {pr.get('label', ctx['preset'])}, look wanted: {pr.get('look', '-')}. Scene: {sc.get('label', ctx['scene'])};
watch for {', '.join(sc.get('emphasis', [])) or '-'}. Standing preferences: {_prefs(ctl).rstrip('.') or 'none recorded'}.

Camera choices the body accepts (use these exact values):
{chr(10).join(choices) or '- (not reported)'}

## Before the first answer
If `prefs:` has no colour choice (no K=, warm, neutral or daylight), ask one short question first and
nothing else: which colour temperature and how soft. Anchors: warm about 3200 K (tungsten, cosy),
neutral about 4000-4500 K, daylight about 5600 K (cool, clean). Soft = big or close source, bounced or
through a diffuser; crisp = small direct source, more shadow. 4300 K is warm-neutral, not cool.

## How to reply (a short sentence or two, then exactly this block)
```
CAMLAB/1 reply round=N sess=XXXX
camera: iso=250 shutter=1/60 aperture=f/5.6 wb=Color_Temperature kelvin=4300
lights: key=20@4300 fill=30@4300
place: key=left 40deg eye+1ft 5ft soft:bounce ; fill=right 25deg eye 8ft
next: if luma still >0.6, drop fill to 15
```
Only the keys you want to change. Lights are percent@kelvin. Keep video shutter near 1/(2 x frame rate)
and match the lights' kelvin to the camera's unless you want a cast. `place:` one entry per light, from
where the photographer stands behind the camera looking at the subject: left/right of the lens, degrees
off the lens axis, height against the subject's eyes (eye, eye+1ft, eye-1ft), distance in feet, and how
to make it soft (soft:bounce, soft:diffuser, soft:closer). `next:` what to change if the next sample is
still off.

## A clip review (`CAMLAB/1 clip round=N ...`, with `len=` and the sheet's frame times)
```
CAMLAB/1 clipreply round=N sess=XXXX
verdict: keep reason=steady_and_bright_enough
trim: in=1.2 out=16.8
caption: one line for the post
next: iso=400 lights: key=55@4300
```
verdict is keep, retake or maybe; trim in and out in seconds; next are the settings for the next take.
"""


# ---- the contact sheet -------------------------------------------------------------
def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_sheet(ctl, clip: str) -> dict[str, Any]:
    """Four frames at 10/40/70/95 % with the time burned in, 2x2, long edge 1024. Saved next to the
    clip (<stem>_sheet.jpg) and as sheet.jpg in the hand-off folder."""
    from PIL import Image, ImageDraw
    from .clips import probe_duration
    entry = ctl.clips.get(clip)
    if entry is None:
        try:
            ctl.pull_clips()
        except Exception:  # noqa: BLE001  -- recording, no card: just report not found
            pass
        entry = ctl.clips.get(clip)
    if entry is None:
        raise KeyError(f"clip {clip!r} is not on this Mac or the card")
    dur = entry.get("duration_s") or probe_duration(entry["path"])
    if not dur:
        raise ValueError(f"could not read the length of {entry['name']} (is ffprobe installed?)")
    dur = float(dur)
    times = [round(dur * f, 2) for f in SHEET_AT]
    shots: list[tuple[float, bytes | None, dict | None]] = []
    for t in times:
        try:
            r = ctl.extract_frames(entry["name"], [t])[0]
            shots.append((t, r["jpeg"], r["analysis"]))
        except Exception:  # noqa: BLE001  -- a frame past the end: grey tile, no measurement
            shots.append((t, None, None))
    first = next((Image.open(io.BytesIO(j)) for _, j, _ in shots if j), None)
    w, h = first.size if first else (960, 640)
    scale = (SHEET_LONG_EDGE / 2) / max(w, h)
    tw, th = round(w * scale), round(h * scale)
    sheet = Image.new("RGB", (2 * tw, 2 * th), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, th // 14))
    for i, (t, j, _) in enumerate(shots):
        x, y = (i % 2) * tw, (i // 2) * th
        if j:
            sheet.paste(Image.open(io.BytesIO(j)).convert("RGB").resize((tw, th)), (x, y))
        label = f"{t:.1f} s"
        box = draw.textbbox((0, 0), label, font=font)
        pad = 6
        draw.rectangle((x + 8, y + th - 8 - (box[3] - box[1]) - 2 * pad, x + 8 + (box[2] - box[0]) + 2 * pad, y + th - 8), fill=(0, 0, 0))
        draw.text((x + 8 + pad - box[0], y + th - 8 - pad - box[3]), label, fill=(255, 255, 255), font=font)
    buf = io.BytesIO(); sheet.save(buf, "JPEG", quality=85)
    jpeg = buf.getvalue()
    clip_path = Path(entry["path"])
    beside = clip_path.with_name(clip_path.stem + "_sheet.jpg")
    beside.write_bytes(jpeg)
    desk = _dir(ctl) / "sheet.jpg"
    desk.write_bytes(jpeg)
    return {"clip": entry.get("label") or entry["name"], "path": str(beside), "desktop_path": str(desk),
            "jpeg_b64": base64.b64encode(jpeg).decode(), "frames": times, "duration_s": round(dur, 2),
            "measures": [measure(a) if a else None for _, _, a in shots], "jpeg": jpeg}


# ---- Generate ----------------------------------------------------------------------
def build_handoff(ctl, clip: str | None = None, still: str | None = None) -> dict[str, Any]:
    """Write the picture (or the clip's sheet, or the photo) and the string; bump the round. See the module doc."""
    d = _dir(ctl)
    data = _session(ctl)
    rounds = data["rounds"]
    n = (rounds[-1]["n"] if rounds else 0) + 1
    prev = rounds[-1] if rounds else None
    if still:
        entry = ctl.stills.get(still)
        if entry is None:
            raise KeyError(f"photo {still!r} is not on this Mac")
        jpeg, jpg = ctl.stills.jpeg_for_ask(entry["label"]), d / "sample.jpg"
        jpg.write_bytes(jpeg)
        m = still_measure(entry)
        lines = state_lines(ctl, still=entry, prev=prev)
        head = f"CAMLAB/1 still round={n} sess={data['session']} pic=attached"
        kind, clip_name = "still", entry["label"]
    elif clip:
        sh = build_sheet(ctl, clip)
        jpeg, jpg = sh.pop("jpeg"), d / "sheet.jpg"
        lines = state_lines(ctl, sheet=sh, prev=prev)
        m = {"luma": None, "frames": sh["measures"]}
        lums = [x["luma"] for x in sh["measures"] if x and x.get("luma") is not None]
        if lums:
            m["luma"] = sum(lums) / len(lums)
        head = f"CAMLAB/1 clip round={n} sess={data['session']} pic=attached"
        kind, clip_name = "clip", sh["clip"]
    else:
        fr = ctl.capture_frame(analyze=True, save=False, source="chat")
        jpeg, jpg = fr["jpeg"], d / "sample.jpg"
        jpg.write_bytes(jpeg)
        m = measure(fr.get("analysis", {}))
        lines = state_lines(ctl, a=fr.get("analysis", {}), prev=prev)
        head = f"CAMLAB/1 round={n} sess={data['session']} pic=attached"
        kind, clip_name = "live", None
    string = "\n".join([head, *lines])
    if prev is not None:
        prev["next_measure"] = m
    pre_path = d / "preamble.md"
    pre = None
    if data.get("preamble_session") != data["session"] or not pre_path.exists():
        pre = preamble(ctl, d)
        pre_path.write_text(pre)
        data["preamble_session"] = data["session"]
    md = d / "handoff.md"
    md.write_text(string + "\n\n(New chat? It needs preamble.md, next to this file, once: it explains this "
                           "string and the reply format.)\n")
    rounds.append({"n": n, "kind": kind, "ts": time.time(), "clip": clip_name, "string": string, "measure": m,
                   "reply": None, "applied": None, "next_measure": None})
    _save(ctl, data)
    return {"string": string, "text": string, "round": n, "session": data["session"], "kind": kind,
            "jpg_path": str(jpg), "md_path": str(md), "preamble_path": str(pre_path), "preamble": pre,
            "copy": (pre + "\n\n" + string) if pre else string, "dir": str(d),
            "jpeg_b64": base64.b64encode(jpeg).decode(), "stamp": time.strftime("%Y-%m-%d %H:%M:%S")}


# ---- Ingest: parsing ---------------------------------------------------------------
_HEAD = re.compile(r"CAMLAB/1\s+(reply|clipreply)\s+round=(\d+)\s+sess=([0-9A-Za-z]+)", re.I)
_KEYLINE = re.compile(r"^\s*(camera|lights|place|next|verdict|trim|caption)\s*:\s*(.*?)\s*$", re.I)
_FENCE = re.compile(r"```\s*camlab\s*\n(.*?)```", re.S | re.I)
_LINE = re.compile(r"^\s*(camera|lights)\s*:\s*(.+?)\s*$", re.I | re.M)


def _num(v: str) -> Any:
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def _camera_tokens(rest: str, out: dict[str, Any], strict: bool = True) -> list[str]:
    """k=v tokens into out['camera']; returns the words that were not camera tokens."""
    left = []
    for tok in rest.split():
        if "=" not in tok:
            left.append(tok); continue
        k, v = tok.split("=", 1)
        k = ALIASES.get(k.strip().lower(), k.strip().lower())
        if k in CAMERA_KEYS and v.strip():
            v = v.strip().rstrip(",;")
            out["camera"][k] = int(float(v.rstrip("kK"))) if k == "kelvin" and re.fullmatch(r"\d+(\.\d+)?[kK]?", v) else v
        elif strict:
            out["errors"].append(f"unknown camera key {k!r} ignored")
        else:
            left.append(tok)
    return left


def _light_tokens(rest: str, out: dict[str, Any]) -> None:
    for tok in rest.split():
        if "=" not in tok:
            continue
        lid, v = tok.split("=", 1)
        spec: dict[str, Any] = {}
        pct, _, kel = v.rstrip(",;").partition("@")
        if pct.strip():
            try: spec["intensity_pct"] = float(pct.strip().rstrip("%"))
            except ValueError: out["errors"].append(f"bad light value {tok!r}")
        if kel.strip():
            try: spec["cct_k"] = int(float(kel.strip().rstrip("kK")))
            except ValueError: out["errors"].append(f"bad kelvin in {tok!r}")
        if spec:
            out["lights"][lid.strip().lower()] = spec


def _parse_new(text: str, m: re.Match, out: dict[str, Any]) -> dict[str, Any]:
    out["kind"], out["round"], out["session"] = m.group(1).lower(), int(m.group(2)), m.group(3)
    before = text[:m.start()]
    rest = text[m.end():].split("\n")
    used = 1                                         # the header line's remainder
    for line in rest[1:]:
        s = line.strip()
        if not s:
            used += 1; continue
        if s.startswith("```"):
            used += 1; break
        km = _KEYLINE.match(s)
        if not km:
            break
        used += 1
        key, val = km.group(1).lower(), km.group(2)
        if key == "camera":
            _camera_tokens(val, out)
        elif key == "lights":
            _light_tokens(val, out)
        elif key == "place":
            out["place"] = [p.strip() for p in val.split(";") if p.strip()]
        elif key == "next":
            if out["kind"] == "clipreply":
                lm = re.search(r"\blights\s*:", val, re.I)
                cam_part, light_part = (val[:lm.start()], val[lm.end():]) if lm else (val, "")
                left = _camera_tokens(cam_part, out, strict=False)
                _light_tokens(light_part, out)
                out["next"] = " ".join(left).strip()
            else:
                out["next"] = val
        elif key == "verdict":
            vm = re.match(r"(\w+)\s*(?:reason\s*=\s*(.*))?$", val)
            if vm:
                out["verdict"] = vm.group(1).lower()
                out["reason"] = (vm.group(2) or "").replace("_", " ").strip()
            if out["verdict"] not in ("keep", "retake", "maybe"):
                out["errors"].append(f"verdict {val!r} is not keep, retake or maybe")
                out["verdict"] = None
        elif key == "trim":
            tm = re.search(r"in\s*=\s*([\d.]+).*?out\s*=\s*([\d.]+)", val)
            out["trim"] = [float(tm.group(1)), float(tm.group(2))] if tm else None
        elif key == "caption":
            out["caption"] = val
    after = "\n".join(rest[used:])
    out["advice"] = re.sub(r"```\w*", "", (before + "\n" + after)).strip()
    if not (out["camera"] or out["lights"] or out["place"] or out["verdict"] or out["trim"] or out["caption"]):
        out["errors"].append("the CAMLAB/1 block had no usable lines")
    return out


def parse_reply(text: str) -> dict[str, Any]:
    """-> {kind, round, session, camera, lights, place, next, verdict, reason, trim, caption, advice, errors}.
    kind is reply | clipreply for the CAMLAB/1 format, None for the old formats (no round check)."""
    out: dict[str, Any] = {"kind": None, "round": None, "session": None, "camera": {}, "lights": {}, "place": [],
                           "next": "", "verdict": None, "reason": "", "trim": None, "caption": "", "advice": "", "errors": []}
    text = text or ""
    m = _HEAD.search(text)
    if m:
        return _parse_new(text, m, out)
    body, advice = None, text
    fm = _FENCE.search(text)
    if fm:
        body = fm.group(1)
        advice = (text[:fm.start()] + "\n" + text[fm.end():]).strip()
    else:
        try:
            j = json.loads(text.strip())
            if isinstance(j, dict) and ("camera" in j or "lights" in j):
                cam = j.get("camera") or {}
                out["camera"] = {k: (int(v) if k == "kelvin" and str(v).isdigit() else v) for k, v in cam.items() if k in CAMERA_KEYS}
                for lid, lv in (j.get("lights") or {}).items():
                    if isinstance(lv, dict):
                        out["lights"][lid] = {k: _num(str(v)) for k, v in lv.items() if k in ("intensity_pct", "cct_k")}
                out["advice"] = str(j.get("advice") or "")
                out["place"] = [str(p) for p in (j.get("place") or j.get("placement") or [])]
                out["next"] = str(j.get("next") or "")
                return out
        except ValueError:
            pass
        if _LINE.search(text):
            body, advice = text, _LINE.sub("", text).strip()
    if body is None:
        out["errors"].append("no CAMLAB/1 reply, ```camlab block, camera:/lights: lines or JSON found in the reply")
        out["advice"] = advice
        return out
    for key, rest in _LINE.findall(body):
        if key.lower() == "camera":
            _camera_tokens(rest, out)
        else:
            _light_tokens(rest, out)
    out["advice"] = advice
    if not out["camera"] and not out["lights"]:
        out["errors"].append("the block had no usable camera or light values")
    return out


# ---- Ingest: applying --------------------------------------------------------------
def _resolve_choices(ctl, camera: dict[str, Any]) -> dict[str, Any]:
    """Map an underscored token back to the body's own choice (Color_Temperature -> 'Color Temperature')."""
    caps = ctl.state.camera.get("capabilities", {}) or {}
    norm = lambda x: str(x).lower().replace("_", " ").strip()  # noqa: E731
    out = {}
    for k, v in camera.items():
        choices = caps.get(k)
        if isinstance(choices, list) and v not in choices:
            v = next((c for c in choices if norm(c) == norm(v)), v)
        out[k] = v
    return out


def write_sidecar(ctl, clip: str, review: dict[str, Any]) -> str:
    """Merge a review (verdict, reason, trim, caption, ...) into <clip file>.json next to the clip
    (e.g. studio-003.MP4.json), keeping whatever the take sidecar already holds."""
    entry = ctl.clips.get(clip)
    if entry is None:
        raise KeyError(f"clip {clip!r} is not on this Mac")
    side = Path(str(entry["path"]) + ".json")       # same file as the take sidecar (controller auto-pull)
    try:
        cur = json.loads(side.read_text())
    except (OSError, ValueError):
        cur = {}
    cur.update({"clip": entry.get("label") or entry["name"], **review, "ts": time.time()})
    side.write_text(json.dumps(cur, indent=1))
    return str(side)


def apply_reply(ctl, text: str, force: bool = False) -> dict[str, Any]:
    """Apply a reply through the Controller (source 'chat'). Raises PermissionError while recording and
    WrongRound for another round/session unless force. Stores the reply in rounds.json."""
    if ctl.state.recording:
        raise PermissionError("recording: nothing changes while a take rolls")
    r = parse_reply(text)
    data = _load(ctl)
    rec = data["rounds"][-1] if data and data.get("rounds") else None
    if r["round"] is not None and rec is not None and not force:
        if r["session"] != data["session"]:
            raise WrongRound("wrong_session", data["session"], r["session"])
        if r["round"] != rec["n"]:
            raise WrongRound("wrong_round", rec["n"], r["round"])
    out: dict[str, Any] = {"applied_camera": {}, "applied_lights": {}, "place": r["place"], "next": r["next"],
                           "advice": r["advice"], "errors": list(r["errors"]), "kind": r["kind"], "round": r["round"]}
    batch = f"chat-{secrets.token_hex(3)}"           # one undo reverts the whole reply
    out["batch"] = batch
    if r["camera"]:
        try:
            out["applied_camera"] = ctl.set_camera(_resolve_choices(ctl, r["camera"]), source="chat",
                                                   reason="from the chat reply", batch=batch)
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"camera: {e}")
    for lid, spec in r["lights"].items():
        try:
            st = ctl.set_light(lid, spec.get("intensity_pct"), spec.get("cct_k"), source="chat",
                               reason="from the chat reply", batch=batch)
            out["applied_lights"][lid] = {"intensity_pct": st["intensity_pct"], "cct_k": st["cct_k"], "scriptable": st["scriptable"]}
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"light {lid}: {e}")
    if r["kind"] == "clipreply":
        out.update(verdict=r["verdict"], reason=r["reason"], trim=r["trim"], caption=r["caption"], sidecar_path=None)
        clip = (rec or {}).get("clip")
        if not clip:
            out["errors"].append("no clip review string was generated, so the verdict has no clip to go with")
        else:
            try:
                out["sidecar_path"] = write_sidecar(ctl, clip, {
                    "verdict": r["verdict"], "reason": r["reason"], "trim": r["trim"], "caption": r["caption"],
                    "session": r["session"], "round": r["round"], "source": "chat"})
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"sidecar: {e}")
    if rec is not None:
        rec["reply"] = text
        rec["reply_round"] = r["round"]
        rec["applied"] = {"camera": out["applied_camera"],
                          "lights": {k: {kk: v[kk] for kk in ("intensity_pct", "cct_k")} for k, v in out["applied_lights"].items()}}
        _save(ctl, data)
    return out
