import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.analysis import analyze, check_target, diff
from server.camera.mock_backend import MockBackend
from server.presets import target_vector


def _frame(subject="part_dark", **settings):
    m = MockBackend(subject)
    m.connect()
    if settings:
        m.set_settings(settings)
    return m, m.capture_frame().jpeg


def test_basic_fields():
    _, j = _frame()
    a = analyze(j, scene_roi="dark_subject")
    assert a["width"] == 960 and a["orientation"] == "landscape"
    assert 0 < a["luma"]["median"] < 1
    assert len(a["luma"]["hist"]) == 256
    assert "subject" in a["roi"] and a["roi"]["subject"]["bbox"] is not None


def test_dark_subject_found_and_specular_clips():
    _, j = _frame()
    a = analyze(j, scene_roi="dark_subject")
    sub = a["roi"]["subject"]
    assert sub["median"] < 0.2            # the anodized body is dark
    assert a["clip"]["high_pct"] > 0.1    # specular rim clips at default exposure


def test_exposure_moves_luma():
    _, dark = _frame(iso="100")
    _, bright = _frame(iso="1600")
    assert analyze(bright)["luma"]["median"] > analyze(dark)["luma"]["median"]


def test_focus_metric_peaks_in_focus():
    m, _ = _frame()
    m.focus_pos = 0.5
    sharp = analyze(m.capture_frame().jpeg, scene_roi="dark_subject")["focus"]["laplacian_var"]
    m.focus_pos = 0.2
    soft = analyze(m.capture_frame().jpeg, scene_roi="dark_subject")["focus"]["laplacian_var"]
    assert sharp > soft * 1.5


def test_wb_estimate_direction():
    m, _ = _frame(kelvin=5600)
    m.scene_cct = 3200                     # tungsten light, daylight WB -> orange cast
    warm = analyze(m.capture_frame().jpeg)["channels"]["wb_estimate_k"]
    m.scene_cct = 5600
    neutral = analyze(m.capture_frame().jpeg)["channels"]["wb_estimate_k"]
    assert warm < neutral


def test_skin_and_face():
    _, j = _frame("person_portrait")
    a = analyze(j, scene_roi="skin")
    assert "skin_detected" in a["flags"]
    assert 0.3 < a["roi"]["skin"]["median"] < 0.9


def test_user_roi_wins():
    _, j = _frame()
    a = analyze(j, roi=[0.0, 0.0, 0.2, 0.2])
    assert a["roi"]["subject"]["bbox"] == [0.0, 0.0, 0.2, 0.2]


def test_target_check_and_diff():
    _, j = _frame()
    t = target_vector("product_hero", "part_dark")
    a = analyze(j, scene_roi="dark_subject", target=t)
    tc = a["target_check"]
    assert "subject_p1" in tc and "clip_high_pct" in tc and "pass" in tc
    _, j2 = _frame(iso="800")
    d = diff(a, analyze(j2, scene_roi="dark_subject"))
    assert d["luma_median"] > 0


def test_geometry_horizon():
    _, j = _frame()
    g = analyze(j)["geometry"]
    assert g["horizon_deg"] is not None and abs(g["horizon_deg"]) < 2.0
