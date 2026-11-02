"""AI Auto: the server-side Claude tool-use loop. Runs against a fake API
client with scripted responses; the camera is the mock backend, so every tool
call lands on real Controller guardrails."""
import copy
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import manual_lights
from server import config
from server.controller import Controller


# ---- fake Claude API --------------------------------------------------------
def usage(inp=3000, out=500):
    return NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text(t):
    return NS(type="text", text=t)


def tool(name, inp, tid=None):
    return NS(type="tool_use", id=tid or f"tu_{name}_{time.time_ns()}", name=name, input=inp)


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, usage=usage(), model="claude-opus-5-5")


class FakeClient:
    """Pops scripted responses; records every request. `gate` blocks each
    request until set (for Stop tests); `raise_` raises instead of answering."""
    def __init__(self, script=None, gate=None, raise_=None, loop_tool=None):
        self.script, self.gate, self.raise_, self.loop_tool = list(script or []), gate, raise_, loop_tool
        self.requests = []
        self.messages = NS(create=self.create)
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.raise_:
            raise self.raise_
        if self.script:
            return self.script.pop(0)
        if self.loop_tool:
            return reply(text("again"), tool(*self.loop_tool))
        return reply(text("nothing left"), stop="end_turn")


# ---- fixtures ----------------------------------------------------------------
def make_ctl(tmp, client, **ai):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"
    cfg["dmx"]["transport"] = "none"
    manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp / "frames"), "clips": str(tmp / "clips"), "recipes": str(tmp / "recipes.json"),
                    "context": str(tmp / "context.json")}
    cfg["claude"]["rate_limit_ms"] = 0
    cfg["ai"] = {"api_key": "sk-test", "model": "claude-opus-5-5", "cost_cap_usd": 1.0, "effort": "medium", "max_run_s": 60} | ai
    ctl = Controller(cfg, client_factory=(lambda: client) if client else None)
    ctl.start()
    return ctl


def wait_done(ctl, timeout=10):
    t0 = time.time()
    while ctl.autopilot.running and time.time() - t0 < timeout:
        time.sleep(0.02)
    assert not ctl.autopilot.running, "run did not finish"


# ---- tests -------------------------------------------------------------------
def test_tools_offer_no_forbidden_names():
    from server.autopilot import TOOLS
    assert {t["name"] for t in TOOLS} == {"look", "set_camera", "set_light", "focus", "done"}


def test_cost_table():
    from server.autopilot import cost_usd
    assert cost_usd("claude-opus-5-5", usage(1_000_000, 0)) == pytest.approx(4.0)
    assert cost_usd("claude-sonnet-5-5", usage(0, 1_000_000)) == pytest.approx(10.0)
    assert cost_usd("something-new", usage(1_000_000, 1_000_000)) == pytest.approx(24.0)


def test_run_converges_and_logs(tmp_path):
    client = FakeClient([
        reply(text("Too dark; raising ISO."), tool("set_camera", {"iso": "800", "reason": "frame under target"})),
        reply(tool("look", {})),
        reply(text("Within targets now."), tool("done", {"status": "converged", "summary": "ISO 800 lands luma in band"})),
    ])
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        s = ctl.get_state()
        assert ctl.state.camera["settings"]["iso"] == "800"
        assert any(h["source"] == "ai" and h["kind"] == "camera" for h in ctl.state.history)
        assert s["claude"]["status"] == "converged" and s["claude"]["mode"] == "ai"
        assert s["claude"]["cost_usd"] > 0
        texts = [m["text"] for m in s["claude"]["messages"] if m["role"] == "claude"]
        assert any("Too dark" in t for t in texts) and any("ISO 800 lands" in t for t in texts)
        first = client.requests[0]["messages"][0]["content"]
        assert any(b.get("type") == "image" for b in first)
        assert client.requests[0]["model"] == "claude-opus-5-5"
        assert "tool_choice" not in client.requests[0] and "thinking" not in client.requests[0]
    finally:
        ctl.stop()


def test_cost_cap_ends_run(tmp_path):
    client = FakeClient(loop_tool=("look", {}))
    ctl = make_ctl(tmp_path, client, cost_cap_usd=0.0001)
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        assert ctl.get_state()["claude"]["status"] == "cap"
        assert len(client.requests) <= 2
    finally:
        ctl.stop()


def test_iteration_cap(tmp_path):
    client = FakeClient(loop_tool=("look", {}))
    ctl = make_ctl(tmp_path, client)
    ctl.cfg["claude"]["iteration_cap"] = 2
    ctl.state.claude.cap = 2
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        assert ctl.get_state()["claude"]["status"] == "cap"
        looks = sum(1 for r in client.requests for m in r["messages"] if m["role"] == "user"
                    for b in (m["content"] if isinstance(m["content"], list) else []) if b.get("type") == "tool_result")
        assert looks <= 3
    finally:
        ctl.stop()


def test_step_does_one_round(tmp_path):
    client = FakeClient([
        reply(tool("set_light", {"id": "key", "intensity_pct": 60, "reason": "bring highlights down"})),
        reply(tool("look", {})),
        reply(tool("set_light", {"id": "key", "intensity_pct": 50})),
        reply(tool("look", {})),
    ])
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("step")
        wait_done(ctl)
        assert ctl.state.lights["key"]["intensity_pct"] == 60
        assert ctl.get_state()["claude"]["status"] == "stopped"
    finally:
        ctl.stop()


def test_stop_ends_run(tmp_path):
    gate = threading.Event()
    client = FakeClient(loop_tool=("look", {}), gate=gate)
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("optimize")
        time.sleep(0.2)
        assert ctl.autopilot.running
        ctl.claude_request("stop")
        gate.set()
        wait_done(ctl)
        assert ctl.get_state()["claude"]["status"] == "stopped"
    finally:
        ctl.stop()


def test_autopilot_refuses_while_recording(tmp_path):
    client = FakeClient(loop_tool=("look", {}))
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.arm(); ctl.record("start")
        with pytest.raises(RuntimeError):
            ctl.autopilot.start("optimize")
        ctl.record("stop")
    finally:
        ctl.stop()


def test_record_start_stops_autopilot(tmp_path):
    gate = threading.Event()
    client = FakeClient(loop_tool=("look", {}), gate=gate)
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("optimize")
        time.sleep(0.2)
        ctl.arm(); ctl.record("start")
        gate.set()
        wait_done(ctl)
        assert ctl.get_state()["claude"]["status"] == "stopped"
        ctl.record("stop")
    finally:
        ctl.stop()


def test_autopilot_api_error_ends_run(tmp_path):
    client = FakeClient(raise_=RuntimeError("boom"))
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        s = ctl.get_state()["claude"]
        assert s["status"] == "error"
        assert "boom" in [m for m in s["messages"] if m["role"] == "claude"][-1]["text"]
    finally:
        ctl.stop()


def test_refusal_ends_run(tmp_path):
    client = FakeClient([reply(stop="refusal")])
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        assert ctl.get_state()["claude"]["status"] == "error"
    finally:
        ctl.stop()


def test_unconfigured_falls_back_to_mcp(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ctl = make_ctl(tmp_path, None, api_key="")
    try:
        assert not ctl.autopilot.configured()
        s = ctl.claude_request("optimize")
        assert s["request"] == "optimize" and s["mode"] == "mcp"
        assert any("MCP" in m["text"] for m in s["messages"] if m["role"] == "ui")
    finally:
        ctl.stop()


# ---- review fixes ---------------------------------------------------------------
def test_ai_changes_refused_while_recording(tmp_path):
    ctl = make_ctl(tmp_path, FakeClient())
    try:
        ctl.arm(); ctl.record("start")
        with pytest.raises(PermissionError):
            ctl.set_camera({"iso": "800"}, source="ai")
        with pytest.raises(PermissionError):
            ctl.set_light("key", 40, source="ai")
        assert ctl.set_camera({"iso": "800"}, source="ui") == {"iso": "800"}   # the human may still turn a dial
        ctl.record("stop")
    finally:
        ctl.stop()


def test_rec_between_two_tool_calls_blocks_the_second(tmp_path):
    client = FakeClient([reply(tool("set_light", {"id": "key", "intensity_pct": 60}), tool("set_camera", {"iso": "800"}))])
    ctl = make_ctl(tmp_path, client)
    real = ctl.set_light

    def set_light_then_rec(*a, **kw):
        out = real(*a, **kw)
        ctl.arm(); ctl.record("start")
        return out
    ctl.set_light = set_light_then_rec
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        assert ctl.state.lights["key"]["intensity_pct"] == 60
        assert ctl.state.camera["settings"]["iso"] == "400"
        assert ctl.get_state()["claude"]["status"] == "stopped"
        ctl.record("stop")
    finally:
        ctl.stop()


class BadRequestError(Exception):
    status_code = 400


def test_bad_request_on_beta_retries_plain(tmp_path):
    client = FakeClient([reply(tool("done", {"status": "converged", "summary": "fine"}))])
    calls = []

    def beta_create(**kw):
        calls.append("beta"); raise BadRequestError("unknown beta")
    client.beta = NS(messages=NS(create=beta_create))
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        assert calls == ["beta"] and len(client.requests) == 1
        assert ctl.get_state()["claude"]["status"] == "converged"
        assert "betas" not in client.requests[0]
    finally:
        ctl.stop()


def test_daily_cap_refuses_new_runs(tmp_path):
    client = FakeClient(loop_tool=("look", {}))
    ctl = make_ctl(tmp_path, client, daily_cap_usd=0.03)
    try:
        ctl.claude_request("optimize"); wait_done(ctl)           # one $0.022 call then look cap... spend > 0.03 after 2 calls
        assert ctl.autopilot.spent_today >= 0.02
        with pytest.raises(ValueError, match="daily"):
            ctl.claude_request("optimize")
    finally:
        ctl.stop()


def test_create_gets_a_timeout(tmp_path):
    client = FakeClient([reply(tool("done", {"status": "converged", "summary": "ok"}))])
    ctl = make_ctl(tmp_path, client, max_run_s=50)
    try:
        ctl.claude_request("optimize"); wait_done(ctl)
        assert 0 < client.requests[0]["timeout"] <= 50
    finally:
        ctl.stop()


def test_controller_stop_stops_run(tmp_path):
    gate = threading.Event()
    client = FakeClient(loop_tool=("look", {}), gate=gate)
    ctl = make_ctl(tmp_path, client)
    ctl.claude_request("optimize")
    time.sleep(0.2)
    ctl.stop()
    gate.set()
    wait_done(ctl)
    assert ctl.get_state()["claude"]["status"] == "stopped"


def test_rounds_never_exceed_cap(tmp_path):
    client = FakeClient(loop_tool=("look", {}))
    ctl = make_ctl(tmp_path, client)
    ctl.state.claude.cap = 2
    try:
        ctl.claude_request("optimize"); wait_done(ctl)
        assert ctl.state.claude.iteration <= 2
        assert "after 2 rounds" in ctl.get_state()["claude"]["messages"][-1]["text"]
    finally:
        ctl.stop()


def test_chat_with_ai_mode_does_not_look_running(tmp_path):
    ctl = make_ctl(tmp_path, FakeClient())
    try:
        s = ctl.claude_message("hello")
        assert s["status"] != "requested" and s["request"] is None
    finally:
        ctl.stop()


def test_run_narrates_every_step(tmp_path):
    """The operator must never see a silent run: every look, change and think
    shows up as a plain line on the timeline, not only the model's prose."""
    from server.autopilot import plain_check
    client = FakeClient([
        reply(tool("set_light", {"id": "key", "intensity_pct": 60, "reason": "too bright on the subject"})),
        reply(tool("look", {})),
        reply(tool("done", {"status": "converged", "summary": "fine"})),
    ])
    ctl = make_ctl(tmp_path, client)
    try:
        ctl.claude_request("optimize")
        wait_done(ctl)
        ui = [m["text"] for m in ctl.get_state()["claude"]["messages"] if m["role"] == "ui"]
        assert any(t.startswith("Look 1:") and "brightness" in t for t in ui)
        assert any(t.startswith("Claude is thinking (round 1") for t in ui)
        assert any(t.startswith("Set key light to 60%") and "too bright" in t for t in ui)
        assert any(t.startswith("Look 2:") for t in ui)
        assert any("may look up to" in t and "cost cap" in t for t in ui)
    finally:
        ctl.stop()
    assert plain_check({"luma_median": {"value": 0.72, "range": [0.48, 0.6], "ok": False}, "pass": False}) \
        == "brightness 0.72 is above 0.48-0.60"
    assert plain_check({"luma_median": {"value": 0.5, "range": [0.48, 0.6], "ok": True}, "pass": True}) == "all targets met"
