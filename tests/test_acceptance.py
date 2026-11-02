"""Spec §9 acceptance, scripted: from cold start, product_hero + part_dark,
a simple greedy policy (standing in for Claude) must reach
clip.high_pct < 0.5, subject.p1 > 0.05, focus at a local max, within 8 iterations."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import manual_lights
from server import config
from server.controller import Controller

ISO = ["100", "200", "400", "800", "1600", "3200"]


def make_ctl(tmp_path):
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"; cfg["dmx"]["transport"] = "none"; manual_lights(cfg)
    cfg["paths"] = {"frames": str(tmp_path / "f"), "clips": str(tmp_path / "c"), "recipes": str(tmp_path / "r.json")}
    cfg["claude"]["rate_limit_ms"] = 0
    ctl = Controller(cfg); ctl.start()
    return ctl


def policy(ctl: Controller, a: dict) -> str:
    """One iteration of what Claude would do. Returns a one-line reason."""
    key = ctl.state.lights["key"]["intensity_pct"]
    iso = ctl.state.camera["settings"]["iso"]
    sub = a["roi"]["subject"]
    if a["clip"]["high_pct"] >= 0.5:
        if key > 20:
            ctl.set_light("key", key - 10, reason="specular clipping; drop key"); return "key -10"
        i = ISO.index(iso) if iso in ISO else 2
        if i > 0:
            ctl.set_camera({"iso": ISO[i - 1]}, reason="still clipping; pull ISO"); return "iso down"
    if sub["p1"] is not None and sub["p1"] <= 0.05:
        i = ISO.index(iso) if iso in ISO else 2
        if i < len(ISO) - 1:
            ctl.set_camera({"iso": ISO[i + 1]}, reason="shadows crushed; raise ISO"); return "iso up"
    return "hold"


def test_product_hero_part_dark_converges(tmp_path):
    ctl = make_ctl(tmp_path)
    ctl.set_context(preset="product_hero", scene="part_dark")
    cam = ctl.camera
    cam.focus_pos = 0.3                     # start soft, like a cold lens
    # exposure/light loop
    converged = False
    for it in range(8):
        fr = ctl.capture_frame()
        a = fr["analysis"]
        ok = a["clip"]["high_pct"] < 0.5 and (a["roi"]["subject"]["p1"] or 0) > 0.05
        ctl.claude_report(f"iter {it}: clip={a['clip']['high_pct']} p1={a['roi']['subject']['p1']}", "running", it)
        if ok:
            converged = True; break
        policy(ctl, a)
    assert converged, ctl.state.claude.messages
    # focus: hill-climb; stop after two consecutive declines, then step back
    best = ctl.set_focus(drive="far1")["focus_metric"]; declines = 0
    for _ in range(16):
        m = ctl.set_focus(drive="far1")["focus_metric"]
        if m < best:
            declines += 1
            if declines == 2:
                ctl.set_focus(drive="near1"); ctl.set_focus(drive="near1"); break
        else:
            best, declines = m, 0
    assert abs(cam.focus_pos - 0.5) <= 0.03
    ctl.claude_report("converged", "converged")
    assert ctl.state.claude.status == "converged"
    # arm / record / pull / frame from the real clip
    ctl.arm(); ctl.record("start"); ctl.record("stop")
    clip = ctl.pull_clips()[0]
    out = ctl.extract_frames(clip["id"], [0.5])
    assert out[0]["analysis"]["clip"]["high_pct"] < 0.5
    assert ctl.save_recipe("part_dark/product_hero/17-50@35")["status"] == "pending_confirm"
    assert ctl.confirm_recipe()["status"] == "saved"
    ctl.stop()


def test_wait_for_request_sees_user_message(tmp_path):
    import threading, time
    ctl = make_ctl(tmp_path)
    out = {}
    t = threading.Thread(target=lambda: out.update(ctl.wait_for_request(5)))
    t.start(); time.sleep(0.3)
    ctl.claude_message("pull the key down")
    t.join(5)
    assert out["request"] == "message" and out["unread_user_messages"][0]["text"] == "pull the key down"
    assert not out["timed_out"]
    ctl.claude_report("done, key at 60%")
    assert ctl.state.claude.request is None
    assert ctl.wait_for_request(0.3)["timed_out"] is True
    ctl.stop()
