"""Interior / decor setups for Reels: a portrait preset for rooms and a scene for
decor details, with targets that let windows glow but keep verticals straight."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import presets


def test_reel_interior_preset():
    p = presets.PRESETS["reel_interior"]
    assert p["orientation"] == "portrait" and p["frame"] == "9:16"
    t = p["target"]
    assert t["luma_median"][0] >= 0.45 and t["clip_high_pct_max"] >= 1.0      # bright, windows may glow
    assert t["skew_deg_max"] <= 1.0 and t["dof"] == "deep"                       # straight verticals, all in focus
    assert 3800 <= t["cct_k"][0] and t["cct_k"][1] <= 5200


def test_interior_scenes():
    room, decor = presets.SCENES["interior_room"], presets.SCENES["decor_detail"]
    assert room["roi"] == "full" and decor["roi"] == "center"
    assert presets.SCENE_TARGETS["interior_room"]["skew_deg_max"] <= 1.0
    assert presets.SCENE_TARGETS["decor_detail"]["dof"] == "shallow"
    t = presets.target_vector("reel_interior", "interior_room")
    assert t["skew_deg_max"] <= 1.0 and "luma_median" in t
    t2 = presets.target_vector("reel", "decor_detail")
    assert t2["dof"] == "shallow"
