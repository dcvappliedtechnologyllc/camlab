"""Ask Claude: one API call per message, a proposal back, the human applies it.
A fake client stands in for the API (no network); the camera is the mock."""
import shutil
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from fastapi.testclient import TestClient

from conftest import manual_lights
from server import config
from server.api import build as build_api
from server.ask import AskError
from server.controller import Controller
from test_autopilot import FakeClient, reply, text, tool

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")

PROP = {"sentence": "Her face is a little dark, so turn the key light up and lift the ISO a step.",
        "camera": {"iso": "800"}, "lights": {"key": {"intensity_pct": 65, "cct_k": 4300}},
        "placement": ["Key light: to your left of the lens, 40 degrees off, a foot above her eyes, 5 feet away, "
                      "bounced off a white card so it is soft."],
        "verdict": None, "trim": None}


def propose(**over):
    return reply(text("Looking at the frame."), tool("propose", PROP | over))


def make_ctl(tmp, client, **ai):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"; manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp / "frames"), "clips": str(tmp / "clips"), "recipes": str(tmp / "recipes.json"),
                    "context": str(tmp / "context.json"), "home": str(tmp), "handoff": str(tmp / "Desktop" / "camlab-handoff")}
    cfg["claude"]["rate_limit_ms"] = 0
    cfg["chat"] = {"preferences": "Colleen likes soft light."}
    cfg["ai"] = {"api_key": "sk-test", "model": "claude-opus-5-5", "cost_cap_usd": 1.0, "daily_cap_usd": 5.0,
                 "effort": "medium", "max_run_s": 60} | ai
    ctl = Controller(cfg, client_factory=(lambda: client) if client else None)
    ctl.start()
    ctl.set_context(preset="reel", scene="person_portrait")
    return ctl


@pytest.fixture
def made(tmp_path):
    made = []
    yield lambda client, **ai: made.append(make_ctl(tmp_path, client, **ai)) or made[-1]
    for c in made:
        c.stop()


def test_ask_returns_sentence_and_diff_and_applies_nothing(made):
    client = FakeClient([propose()])
    ctl = made(client)
    out = ctl.ask.ask("is she too dark?")
    assert out["id"] and out["sentence"].startswith("Her face is a little dark")
    assert {"what": "ISO", "from": "400", "to": "800"} in out["diff"]
    assert {"what": "key light", "from": "80 %", "to": "65 %"} in out["diff"]
    assert {"what": "key light colour", "from": "5600 K", "to": "4300 K"} in out["diff"]
    assert out["placement"] == PROP["placement"] and out["camera"] == {"iso": "800"}
    assert out["lights"] == {"key": {"intensity_pct": 65, "cct_k": 4300}} and out["verdict"] is None and out["trim"] is None
    assert ctl.state.camera["settings"]["iso"] == "400" and ctl.state.lights["key"]["intensity_pct"] == 80
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["output_config"] == {"effort": "medium"}
    assert "thinking" not in req and "temperature" not in req and "tool_choice" not in req
    assert [t["name"] for t in req["tools"]] == ["propose"]
    last = req["messages"][-1]["content"]
    assert any(b.get("type") == "image" for b in last)
    words = " ".join(b.get("text", "") for b in last)
    assert "is she too dark?" in words and "Colleen likes soft light" in words and "now cam: iso=400" in words
    assert "skin-first" in words                                      # the preset look
    assert "feet" in req["system"] and "behind the camera" in req["system"]


def test_cost_metered_and_daily_cap(made):
    ctl = made(FakeClient([propose(), propose()]), daily_cap_usd=0.03)
    out = ctl.ask.ask("hi")
    assert out["cost_usd"] == pytest.approx(0.022) and ctl.autopilot.spent_today == pytest.approx(0.022)
    ctl.ask.ask("again")
    with pytest.raises(AskError) as e:
        ctl.ask.ask("third")
    assert e.value.code == "daily_cap" and "daily" in str(e.value)


def test_apply_goes_through_guardrails_refuses_while_recording_and_one_undo_reverts(made):
    ctl = made(FakeClient([propose(lights={"key": {"intensity_pct": 140, "cct_k": 4300}, "fill": {"intensity_pct": 20}})]))
    p = ctl.ask.ask("help")
    ctl.arm(); ctl.record("start")
    with pytest.raises(AskError) as e:
        ctl.ask.apply(p["id"])
    assert e.value.status == 409
    ctl.record("stop")
    out = ctl.ask.apply(p["id"])
    assert out["applied_camera"] == {"iso": "800"}
    assert out["applied_lights"]["key"]["intensity_pct"] == 100                     # clamped
    assert out["applied_lights"]["fill"]["intensity_pct"] == 20
    ask_entries = [h for h in ctl.state.history if h["source"] == "ask"]
    assert len(ask_entries) == 3 and {h["batch"] for h in ask_entries} == {p["id"]}
    u = ctl.undo()
    assert u["undone"] == p["id"]
    assert ctl.state.camera["settings"]["iso"] == "400"
    assert ctl.state.lights["key"]["intensity_pct"] == 80 and ctl.state.lights["key"]["cct_k"] == 5600
    assert ctl.state.lights["fill"]["intensity_pct"] == 30
    with pytest.raises(AskError) as e2:
        ctl.ask.apply("nope")
    assert e2.value.status == 404


def test_single_undo_still_works(made):
    ctl = made(FakeClient())
    ctl.set_camera({"iso": "800"}, source="ui")
    ctl.undo()
    assert ctl.state.camera["settings"]["iso"] == "400"


def test_follow_ups_keep_memory_and_reset_clears(made):
    client = FakeClient([propose() for _ in range(9)])
    ctl = made(client)
    first = ctl.ask.ask("too dark?")
    ctl.ask.apply(first["id"])
    ctl.ask.ask("a bit warmer")
    msgs = client.requests[1]["messages"]
    assert len(msgs) == 3 and msgs[1]["role"] == "assistant"
    tu = next(b for b in msgs[1]["content"] if b["type"] == "tool_use")
    tr = next(b for b in msgs[2]["content"] if b.get("type") == "tool_result")
    assert tr["tool_use_id"] == tu["id"] and "applied" in str(tr["content"])
    assert sum(1 for m in msgs for b in (m["content"] if isinstance(m["content"], list) else [])
               if isinstance(b, dict) and b.get("type") == "image") == 1     # only the newest picture is sent
    for i in range(6):
        ctl.ask.ask(f"more {i}")
    assert len(client.requests[-1]["messages"]) == 13                         # 6 earlier exchanges + this one
    assert client.requests[-1]["messages"][0]["role"] == "user"
    ctl.ask.reset()
    ctl.ask.ask("fresh")
    assert len(client.requests[-1]["messages"]) == 1


def test_refusal_and_text_only_become_sentences(made):
    ctl = made(FakeClient([reply(stop="refusal"), reply(text("That already looks right to me."), stop="end_turn")]))
    r = ctl.ask.ask("x")
    assert r["id"] is None and r["error"] == "refusal" and "Traceback" not in r["sentence"] and r["sentence"]
    assert r["diff"] == [] and r["cost_usd"] > 0
    t = ctl.ask.ask("y")
    assert t["id"] is None and t["sentence"] == "That already looks right to me." and t["error"] == "no_proposal"


def test_api_error_is_a_plain_sentence(made):
    ctl = made(FakeClient(raise_=RuntimeError("boom at line 3")))
    with pytest.raises(AskError) as e:
        ctl.ask.ask("x")
    assert e.value.status == 502 and "Claude" in str(e.value) and "Traceback" not in str(e.value)


def test_no_key(made, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ctl = made(None, api_key="")
    with pytest.raises(AskError) as e:
        ctl.ask.ask("x")
    assert e.value.code == "no_key" and e.value.status == 409


@needs_ffmpeg
def test_ask_about_a_clip_sends_the_sheet_and_returns_verdict(made):
    client = FakeClient([propose(verdict="retake", trim=[0.2, 1.8], sentence="Her face goes dark at the end; take it again.")])
    ctl = made(client)
    ctl.arm(); ctl.record("start"); ctl.record("stop")
    name = (ctl.pull_clips() or ctl.clips.index)[-1]["name"]
    out = ctl.ask.ask("keep this one?", clip=name)
    assert out["verdict"] == "retake" and out["trim"] == [0.2, 1.8]
    words = " ".join(b.get("text", "") for b in client.requests[0]["messages"][-1]["content"])
    assert f"clip={name}" in words and "CAMLAB/1 clip" in words
    ctl.ask.apply(out["id"])
    side = Path(ctl.clips.get(name)["path"] + ".json")
    assert side.exists() and '"retake"' in side.read_text()


def test_api_routes(made):
    client = FakeClient([propose(), propose()])
    ctl = made(client)
    with TestClient(build_api(ctl, ctl.cfg)) as c:
        p = c.post("/api/ask", json={"text": "too dark?"}).json()
        assert set(p) >= {"id", "sentence", "diff", "placement", "camera", "lights", "verdict", "trim", "cost_usd"}
        ctl.arm(); ctl.record("start")
        r = c.post("/api/ask/apply", json={"id": p["id"]})
        assert r.status_code == 409 and "recording" in r.json()["detail"]
        ctl.record("stop")
        a = c.post("/api/ask/apply", json={"id": p["id"]}).json()
        assert a["applied_camera"] == {"iso": "800"} and a["id"] == p["id"]
        assert c.post("/api/undo").json()["undone"] == p["id"]
        assert c.post("/api/ask/reset").json() == {"reset": True}
        assert c.post("/api/ask", json={"text": " "}).status_code == 400


def test_api_error_codes(made, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ctl = made(None, api_key="")
    with TestClient(build_api(ctl, ctl.cfg)) as c:
        r = c.post("/api/ask", json={"text": "hello"})
        assert r.status_code == 409 and r.json()["code"] == "no_key" and "key" in r.json()["detail"]
