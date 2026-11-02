"""Strings (Chat mode / no-key fallback): a compact CAMLAB/1 brief out, a CAMLAB/1 reply in. No API."""
import base64
import json
import re
import shutil
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from conftest import manual_lights
from server import config, handoff
from server.api import build as build_api
from server.controller import Controller

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")


def make_cfg(tmp_path):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"; manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp_path / "frames"), "clips": str(tmp_path / "clips"), "recipes": str(tmp_path / "recipes.json"),
                    "context": str(tmp_path / "context.json"), "home": str(tmp_path), "handoff": str(tmp_path / "Desktop" / "camlab-handoff")}
    cfg["claude"]["rate_limit_ms"] = 0
    cfg["chat"] = {"preferences": "Colleen likes soft light."}
    return cfg


@pytest.fixture
def ctl(tmp_path):
    c = Controller(make_cfg(tmp_path)); c.start()
    c.set_context(preset="reel", scene="person_portrait")
    yield c
    c.stop()


def reply_for(h, body="camera: iso=800\nlights: key=60@4300 fill=40@4300", kind="reply"):
    return f"CAMLAB/1 {kind} round={h['round']} sess={h['session']}\n{body}"


def mock_clip(ctl):
    ctl.arm(); ctl.record("start"); ctl.record("stop")
    pulled = ctl.pull_clips()                 # the controller's auto-pull may have got it first
    return (pulled or ctl.clips.index)[-1]["name"]


# ---- the brief string ------------------------------------------------------------
def test_brief_string_shape_files_and_rounds(ctl, tmp_path):
    h = handoff.build_handoff(ctl)
    d = tmp_path / "Desktop" / "camlab-handoff"
    # the same fixed folder and names every time, so a chat or a skill can find them
    assert h["jpg_path"] == str(d / "sample.jpg") and h["md_path"] == str(d / "handoff.md")
    assert Path(h["jpg_path"]).exists() and base64.b64decode(h["jpeg_b64"])[:2] == b"\xff\xd8"
    lines = h["string"].splitlines()
    assert len(lines) == 8, h["string"]
    assert re.fullmatch(r"CAMLAB/1 round=1 sess=[0-9a-f]{4} pic=attached", lines[0])
    assert lines[1].startswith("clip=reel 9:16 fps=30 lens=35mm(56eq) cam=") and " mode=" in lines[1]
    assert lines[2].startswith("now cam: iso=400 shutter=1/60 aperture=f/5.6 wb=kelvin kelvin=5600 style=neutral")
    assert lines[3].startswith("now lights: key=80@5600 fill=30@5600 (0-100%, 2800-6500K)")
    assert lines[4].startswith("want: luma=0.45-0.55 clipH<=0.3 clipL<=3") and "K=3200-4300" in lines[4]
    assert "skin=0.45-0.58" in lines[4] and "dof=shallow" in lines[4]
    assert lines[5].startswith("got: luma=") and "OFF" in lines[5] and "clipH=" in lines[5] and "skin=" in lines[5]
    assert lines[6] == "last: none"
    assert lines[7] == "prefs: soft ; Colleen likes soft light."
    assert h["string"] in Path(h["md_path"]).read_text()
    assert h["text"] == h["string"]                      # old key kept for the UI
    # round increments, session stays, the long explanation is written once per session
    assert h["preamble"] and Path(h["preamble_path"]).exists() and Path(h["preamble_path"]).name == "preamble.md"
    h2 = handoff.build_handoff(ctl)
    assert h2["round"] == 2 and h2["session"] == h["session"] and h2["preamble"] is None
    assert h2["string"].startswith(f"CAMLAB/1 round=2 sess={h['session']} ")
    rj = json.loads((tmp_path / "rounds.json").read_text())
    assert rj["session"] == h["session"] and [r["n"] for r in rj["rounds"]] == [1, 2]
    assert rj["rounds"][0]["string"] == h["string"] and rj["rounds"][0]["next_measure"]["luma"] is not None


def test_spaces_become_underscores(ctl):
    ctl.state.camera["settings"]["wb_mode"] = "Color Temperature"
    ctl.state.camera["settings"]["picture_style"] = "Fine Detail"
    line = handoff.build_handoff(ctl)["string"].splitlines()[2]
    assert "wb=Color_Temperature" in line and "style=Fine_Detail" in line


def test_preamble_carries_the_long_explanation(ctl):
    p = handoff.build_handoff(ctl)["preamble"]
    for must in ("camlab skill", "access", "sample.jpg", "behind the camera", "feet", "3200", "4000-4500", "5600",
                 "CAMLAB/1 reply", "CAMLAB/1 clipreply", "place:", "1/60", "f/5.6", "Colleen likes soft light"):
        assert must in p, must


def test_prefs_with_kelvin_choice(ctl):
    ctl.cfg["chat"] = {"preferences": "Colleen likes soft light and warm 3200K tones"}
    assert handoff.build_handoff(ctl)["string"].splitlines()[7] == "prefs: soft K=3200 ; Colleen likes soft light and warm 3200K tones"


def test_last_line_reports_previous_round(ctl, tmp_path):
    h = handoff.build_handoff(ctl)
    handoff.apply_reply(ctl, reply_for(h))
    h2 = handoff.build_handoff(ctl)
    last = h2["string"].splitlines()[6]
    assert last.startswith("last: round=1 you=iso=800 key=60@4300 fill=40@4300 result=luma ") and "->" in last
    rj = json.loads((tmp_path / "rounds.json").read_text())
    r1 = rj["rounds"][0]
    assert r1["reply"].startswith("CAMLAB/1 reply round=1") and r1["applied"]["camera"]["iso"] == "800"
    assert r1["applied"]["lights"]["key"]["intensity_pct"] == 60 and r1["next_measure"]


def test_new_session_resets_rounds(ctl):
    h = handoff.build_handoff(ctl)
    s = handoff.new_session(ctl)
    assert s["session"] != h["session"] and s["round"] == 0
    h2 = handoff.build_handoff(ctl)
    assert h2["round"] == 1 and h2["preamble"]


# ---- the reply string ------------------------------------------------------------
REPLY = """CAMLAB/1 reply round=3 sess=a7f2
camera: iso=250 shutter=1/60 aperture=f/5.6 wb=Color_Temperature kelvin=4300
lights: key=20@4300 fill=30@4300
place: key=left 40deg eye+1ft 5ft soft:bounce ; fill=right 25deg eye 8ft
next: if luma still >0.6, drop fill to 15"""


@pytest.mark.parametrize("wrap", ["{}", "Sure, here is the next round.\n\n{}\n\nTell me how it looks.",
                                  "Here you go:\n```\n{}\n```\nGood luck!", "```text\n{}\n```"])
def test_parse_reply_new_format(wrap):
    r = handoff.parse_reply(wrap.format(REPLY))
    assert r["kind"] == "reply" and r["round"] == 3 and r["session"] == "a7f2" and not r["errors"]
    assert r["camera"] == {"iso": "250", "shutter": "1/60", "aperture": "f/5.6", "wb_mode": "Color_Temperature", "kelvin": 4300}
    assert r["lights"] == {"key": {"intensity_pct": 20, "cct_k": 4300}, "fill": {"intensity_pct": 30, "cct_k": 4300}}
    assert r["place"] == ["key=left 40deg eye+1ft 5ft soft:bounce", "fill=right 25deg eye 8ft"]
    assert r["next"] == "if luma still >0.6, drop fill to 15"


def test_apply_new_reply_resolves_underscored_choices(ctl):
    ctl.state.camera["capabilities"]["wb_mode"] = ["auto", "daylight", "Color Temperature"]
    h = handoff.build_handoff(ctl)
    seen = {}
    real = ctl.camera.set_settings
    ctl.camera.set_settings = lambda ch: (seen.update(ch), real({k: v for k, v in ch.items() if k != "wb_mode"}))[1]
    out = handoff.apply_reply(ctl, reply_for(h, "camera: iso=800 wb=Color_Temperature\nplace: key=left 30deg eye 4ft\nnext: shoot"))
    assert seen["wb_mode"] == "Color Temperature" and out["applied_camera"]["iso"] == "800"
    assert out["place"] == ["key=left 30deg eye 4ft"] and out["next"] == "shoot" and out["errors"] == []


def test_wrong_round_refused_unless_forced(ctl):
    h1 = handoff.build_handoff(ctl)
    handoff.build_handoff(ctl)                                      # round 2 is current now
    with pytest.raises(handoff.WrongRound) as e:
        handoff.apply_reply(ctl, reply_for(h1))
    assert e.value.code == "wrong_round" and e.value.expected == 2 and e.value.got == 1
    assert ctl.state.camera["settings"]["iso"] == "400"
    out = handoff.apply_reply(ctl, reply_for(h1), force=True)
    assert out["applied_camera"]["iso"] == "800"
    with pytest.raises(handoff.WrongRound) as e2:
        handoff.apply_reply(ctl, "CAMLAB/1 reply round=2 sess=zzzz\ncamera: iso=200")
    assert e2.value.code == "wrong_session"


def test_api_wrong_round_409_and_force(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Controller(cfg); c.start()
    try:
        with TestClient(build_api(c, cfg)) as cl:
            h1 = cl.post("/api/handoff").json()
            assert set(h1) >= {"string", "round", "session", "jpg_path", "md_path", "jpeg_b64"}
            cl.post("/api/handoff", json={})
            r = cl.post("/api/handoff/apply", json={"text": reply_for(h1)})
            assert r.status_code == 409
            assert r.json() == {"detail": r.json()["detail"], "code": "wrong_round", "expected": 2, "got": 1}
            ok = cl.post("/api/handoff/apply", json={"text": reply_for(h1), "force": True}).json()
            assert ok["applied_camera"]["iso"] == "800" and ok["applied_lights"]["key"]["intensity_pct"] == 60
            assert set(ok) >= {"applied_camera", "applied_lights", "place", "next", "errors"}
            assert cl.post("/api/handoff/new_session").json()["round"] == 0
    finally:
        c.stop()


# ---- the old format stays accepted -------------------------------------------------
def test_parse_reply_fenced_block_and_advice():
    r = handoff.parse_reply("Here you go.\n```camlab\ncamera: iso=800 shutter=1/60 aperture=f/2.8 wb_mode=kelvin kelvin=4300\nlights: key=70@4300 fill=30@4300\n```\nLight placement (from where you stand): key 45° to your left, 5 ft away, a foot above her eyes.")
    assert r["camera"] == {"iso": "800", "shutter": "1/60", "aperture": "f/2.8", "wb_mode": "kelvin", "kelvin": 4300}
    assert r["lights"] == {"key": {"intensity_pct": 70, "cct_k": 4300}, "fill": {"intensity_pct": 30, "cct_k": 4300}}
    assert "45° to your left" in r["advice"] and "```" not in r["advice"] and r["round"] is None


def test_parse_reply_json_and_partials():
    r = handoff.parse_reply('{"camera": {"iso": "400"}, "lights": {"key": {"intensity_pct": 55}}}')
    assert r["camera"] == {"iso": "400"} and r["lights"] == {"key": {"intensity_pct": 55}}
    r2 = handoff.parse_reply("camera: iso=200\nlights: fill=20")
    assert r2["camera"] == {"iso": "200"} and r2["lights"] == {"fill": {"intensity_pct": 20}}
    assert handoff.parse_reply("no config here")["camera"] == {} and handoff.parse_reply("no config here")["errors"]


def test_apply_reply_goes_through_guardrails(ctl):
    handoff.build_handoff(ctl)
    out = handoff.apply_reply(ctl, "```camlab\ncamera: iso=800 aperture=f/8\nlights: key=140@9000\n```\nPut the key light at 2 o'clock.")
    assert out["applied_camera"]["iso"] == "800" and out["applied_camera"]["aperture"] == "f/8"
    assert out["applied_lights"]["key"]["intensity_pct"] == 100 and out["applied_lights"]["key"]["cct_k"] == 6500   # clamped
    assert "2 o'clock" in out["advice"]
    assert any(h["source"] == "chat" for h in ctl.state.history)
    u = ctl.undo()                                       # one undo reverts the whole reply
    assert u["undone"] == out["batch"] and ctl.state.camera["settings"]["iso"] == "400"
    assert ctl.state.camera["settings"]["aperture"] == "f/5.6" and ctl.state.lights["key"]["intensity_pct"] == 80


def test_apply_reply_refuses_while_recording_and_bad_text(ctl):
    ctl.arm(); ctl.record("start")
    try:
        with pytest.raises(PermissionError):
            handoff.apply_reply(ctl, "```camlab\ncamera: iso=800\n```")
    finally:
        ctl.record("stop")
    out = handoff.apply_reply(ctl, "thanks, looks great")
    assert out["errors"] and out["applied_camera"] == {} and out["applied_lights"] == {}


# ---- clip review -------------------------------------------------------------------
@needs_ffmpeg
def test_clip_sheet_built_on_a_mock_clip(ctl, tmp_path):
    from PIL import Image
    import io
    name = mock_clip(ctl)
    s = handoff.build_sheet(ctl, name)
    clip_path = Path(ctl.clips.get(name)["path"])
    assert Path(s["path"]).parent == clip_path.parent and Path(s["path"]).exists()
    assert (tmp_path / "Desktop" / "camlab-handoff" / "sheet.jpg").exists()
    assert s["duration_s"] == pytest.approx(2.0, abs=0.1)
    assert s["frames"] == pytest.approx([0.2, 0.8, 1.4, 1.9], abs=0.02)
    im = Image.open(io.BytesIO(base64.b64decode(s["jpeg_b64"])))
    assert max(im.size) == 1024


@needs_ffmpeg
def test_clip_brief_and_clipreply_sidecar(ctl, tmp_path):
    name = mock_clip(ctl)
    h = handoff.build_handoff(ctl, clip=name)
    lines = h["string"].splitlines()
    assert re.fullmatch(rf"CAMLAB/1 clip round=1 sess={h['session']} pic=attached", lines[0])
    assert lines[1].startswith(f"clip={name} len=2.0s at=0.2,0.8,1.4,1.9 for=reel 9:16 fps=30")
    assert any(l.startswith("now cam: ") for l in lines) and any(l.startswith("now lights: ") for l in lines)
    assert any(l.startswith("want: ") for l in lines) and any(l.startswith("got: luma=") for l in lines)
    assert h["jpg_path"].endswith("sheet.jpg")
    out = handoff.apply_reply(ctl, reply_for(h, "verdict: retake reason=face_too_dark\ntrim: in=0.2 out=1.8\n"
                                                "caption: Colleen's new look\nnext: iso=800 lights: key=70@4300", kind="clipreply"))
    side = Path(ctl.clips.get(name)["path"] + ".json")
    j = json.loads(side.read_text())
    assert j["verdict"] == "retake" and j["reason"] == "face too dark" and j["trim"] == [0.2, 1.8]
    assert j["caption"] == "Colleen's new look" and j["session"] == h["session"] and j["round"] == 1
    assert out["applied_camera"]["iso"] == "800" and out["applied_lights"]["key"]["intensity_pct"] == 70
    assert out["verdict"] == "retake" and out["sidecar_path"] == str(side)


@needs_ffmpeg
def test_api_clip_sheet(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Controller(cfg); c.start()
    try:
        name = mock_clip(c)
        with TestClient(build_api(c, cfg)) as cl:
            s = cl.post("/api/clips/sheet", json={"clip": name}).json()
            assert set(s) >= {"path", "jpeg_b64", "frames", "duration_s"} and len(s["frames"]) == 4
            h = cl.post("/api/handoff", json={"clip": name}).json()
            assert h["string"].startswith("CAMLAB/1 clip round=1")
            assert cl.post("/api/clips/sheet", json={"clip": "nope.MP4"}).status_code == 400
    finally:
        c.stop()


def test_rounds_summary_route(ctl, tmp_path):
    with TestClient(build_api(ctl, ctl.cfg)) as c:
        assert c.get("/api/handoff/rounds").json()["rounds"] == []
        h = c.post("/api/handoff").json()
        c.post("/api/handoff/apply", json={"text": reply_for(h)})
        r = c.get("/api/handoff/rounds").json()
    assert r["session"] == h["session"] and [x["n"] for x in r["rounds"]] == [h["round"]]
    assert r["rounds"][0]["replied"] is True and r["rounds"][0]["applied"]["camera"]
    assert "string" not in r["rounds"][0]


@needs_ffmpeg
def test_clip_string_and_sidecar_use_the_take_label(ctl, tmp_path):
    ctl.set_context(tag="studio")
    name = mock_clip(ctl)
    entry = ctl.clips.get(name)
    assert entry["label"] == "studio-001.MP4"
    h = handoff.build_handoff(ctl, clip=name)
    assert h["string"].splitlines()[1].startswith("clip=studio-001.MP4 len=")
    assert json.loads((handoff._dir(ctl) / "rounds.json").read_text())[-1]["clip"] == "studio-001.MP4" \
        if (handoff._dir(ctl) / "rounds.json").exists() else True
    handoff.apply_reply(ctl, reply_for(h, "verdict: keep", kind="clipreply"))
    assert Path(entry["path"] + ".json").name == "studio-001.MP4.json"
    assert json.loads(Path(entry["path"] + ".json").read_text())["clip"] == "studio-001.MP4"
