"""Frame analysis (spec §7). Pure numpy/Pillow; OpenCV optional for face
detection and Hough lines. Everything returns plain JSON-able numbers."""
from __future__ import annotations

import io
import math
from typing import Any

import numpy as np
from PIL import Image

try:  # optional
    import cv2  # type: ignore
    _HAS_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None
    _HAS_CV2 = False

_FACE = None


def _face_cascade():
    global _FACE
    if _FACE is None:
        _FACE = False
        if _HAS_CV2 and hasattr(cv2, "CascadeClassifier"):
            try:
                path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                c = cv2.CascadeClassifier(path)
                _FACE = c if not c.empty() else False
            except Exception:
                _FACE = False
    return _FACE or None


def _luma(rgb: np.ndarray) -> np.ndarray:
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def _stats(l: np.ndarray, bins: int = 256) -> dict[str, Any]:
    flat = l.ravel()
    if flat.size == 0:
        return {"median": None, "p1": None, "p99": None, "mean": None, "hist": [0] * bins}
    p1, med, p99 = np.percentile(flat, [1, 50, 99])
    hist, _ = np.histogram(flat, bins=bins, range=(0.0, 1.0))
    return {"median": round(float(med), 4), "p1": round(float(p1), 4), "p99": round(float(p99), 4),
            "mean": round(float(flat.mean()), 4), "hist": hist.astype(int).tolist()}


def _clip(l: np.ndarray) -> dict[str, float]:
    n = l.size or 1
    return {"high_pct": round(100.0 * float((l >= 0.98).sum()) / n, 3),
            "low_pct": round(100.0 * float((l <= 0.02).sum()) / n, 3)}


def _laplacian_var(gray: np.ndarray) -> float:
    """Variance of Laplacian; relative sharpness metric."""
    g = gray.astype(np.float32)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return round(float(lap.var() * 1e4), 2)


def _wb_estimate(rgb: np.ndarray) -> int:
    """Rough CCT from grey-world R/B ratio. Calibrated so R/B == 1 → ~5500K.
    Direction: more red than blue → scene lit warmer than the camera WB."""
    r, b = float(rgb[..., 0].mean()) + 1e-6, float(rgb[..., 2].mean()) + 1e-6
    ratio = r / b
    k = 5500 / (ratio ** 1.6)
    return int(max(2000, min(12000, round(k / 50) * 50)))


def _skin_mask(rgb8: np.ndarray) -> np.ndarray:
    """YCbCr skin mask; fine for one subject under controlled light."""
    r, g, b = rgb8[..., 0].astype(np.float32), rgb8[..., 1].astype(np.float32), rgb8[..., 2].astype(np.float32)
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return (cb > 77) & (cb < 127) & (cr > 133) & (cr < 173)


def _bbox(mask: np.ndarray) -> list[float] | None:
    ys, xs = np.nonzero(mask)
    if xs.size < 50:
        return None
    h, w = mask.shape
    return [round(xs.min() / w, 4), round(ys.min() / h, 4),
            round((xs.max() - xs.min() + 1) / w, 4), round((ys.max() - ys.min() + 1) / h, 4)]


def _crop(arr: np.ndarray, roi: list[float] | None) -> np.ndarray:
    if not roi:
        return arr
    h, w = arr.shape[:2]
    x, y, rw, rh = roi
    x0, y0 = int(max(0, x) * w), int(max(0, y) * h)
    x1, y1 = int(min(1, x + rw) * w), int(min(1, y + rh) * h)
    if x1 <= x0 or y1 <= y0:
        return arr
    return arr[y0:y1, x0:x1]


def _subject_mask(l: np.ndarray, kind: str) -> np.ndarray:
    """Largest dark (or bright) blob vs background, for parts."""
    if kind == "bright_subject":
        m = l > min(0.85, float(np.percentile(l, 90)))
    else:
        m = l < max(0.12, float(np.percentile(l, 12)))
    if _HAS_CV2:
        m8 = m.astype(np.uint8)
        m8 = cv2.morphologyEx(m8, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(m8, 8)
        if n > 1:
            biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            return labels == biggest
    return m


def _geometry(gray8: np.ndarray) -> dict[str, Any]:
    """Horizon/skew from the dominant near-horizontal line (bench edge)."""
    out: dict[str, Any] = {"horizon_deg": None, "skew_deg": None}
    if not _HAS_CV2:
        return out
    edges = cv2.Canny(gray8, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=80,
                            minLineLength=gray8.shape[1] * 0.35, maxLineGap=12)
    if lines is None:
        return out
    angs, lens = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if a > 90: a -= 180
        if a < -90: a += 180
        if abs(a) < 15:
            angs.append(a); lens.append(math.hypot(x2 - x1, y2 - y1))
    if angs:
        a = float(np.average(angs, weights=lens))
        out["horizon_deg"] = round(a, 2)
        out["skew_deg"] = round(abs(a), 2)
    return out


def _faces(gray8: np.ndarray) -> list[list[float]]:
    c = _face_cascade()
    if c is None:
        return []
    h, w = gray8.shape
    found = c.detectMultiScale(gray8, 1.15, 5, minSize=(max(24, w // 16),) * 2)
    return [[round(x / w, 4), round(y / h, 4), round(fw / w, 4), round(fh / h, 4)] for x, y, fw, fh in found]


def analyze(jpeg: bytes | Image.Image, *, roi: list[float] | None = None,
            scene_roi: str = "dark_subject", target: dict | None = None) -> dict[str, Any]:
    """Analyze a frame. `roi` is a user-drawn [x,y,w,h] in 0..1 (preferred);
    `scene_roi` picks the auto method when none is drawn: skin | dark_subject |
    bright_subject | center | full. `target` (from presets.target_vector)
    turns on pass/fail flags."""
    img = jpeg if isinstance(jpeg, Image.Image) else Image.open(io.BytesIO(jpeg))
    exif = img.getexif()
    orient = exif.get(274, 1)
    rgb8 = np.asarray(img.convert("RGB"))
    h, w = rgb8.shape[:2]
    orientation = "portrait" if (h > w) ^ (orient in (5, 6, 7, 8)) else "landscape"
    rgb = rgb8.astype(np.float32) / 255.0
    l = _luma(rgb)
    gray8 = (l * 255).astype(np.uint8)

    out: dict[str, Any] = {
        "width": w, "height": h, "orientation": orientation,
        "luma": _stats(l),
        "clip": _clip(l),
        "channels": {"r_mean": round(float(rgb[..., 0].mean()), 4), "g_mean": round(float(rgb[..., 1].mean()), 4),
                     "b_mean": round(float(rgb[..., 2].mean()), 4), "wb_estimate_k": _wb_estimate(rgb)},
        "focus": {"laplacian_var": _laplacian_var(gray8), "roi": roi or [0.25, 0.25, 0.5, 0.5]},
        "roi": {},
        "geometry": _geometry(gray8),
        "flags": [],
    }

    # uniformity (flat_doc): std of a coarse 8x8 luma grid relative to mean
    gh, gw = h // 8, w // 8
    grid = l[:gh * 8, :gw * 8].reshape(8, gh, 8, gw).mean(axis=(1, 3))
    out["uniformity"] = round(float(grid.std() / (grid.mean() + 1e-6)), 4)

    # ---- skin / face ----
    faces = _faces(gray8)
    skin = _skin_mask(rgb8)
    if faces:
        out["flags"].append("face_detected")
        fx, fy, fw, fh = faces[0]
        face_l = _crop(l, [fx, fy, fw, fh])
        s = _stats(face_l); s.pop("hist")
        s.update(_clip(face_l)); s["bbox"] = faces[0]
        out["roi"]["face"] = s
        if s["high_pct"] > 0.1:
            out["flags"].append("highlight_clip_face")
    if skin.sum() > 0.005 * h * w:
        s = _stats(l[skin]); s.pop("hist"); s.update(_clip(l[skin])); s["bbox"] = _bbox(skin)
        s["coverage_pct"] = round(100 * float(skin.mean()), 2)
        out["roi"]["skin"] = s
        out["flags"].append("skin_detected")

    # ---- subject ----
    if roi:
        sub_l = _crop(l, roi)
        sub_gray = _crop(gray8, roi)
        bbox = roi
    elif scene_roi in ("dark_subject", "bright_subject"):
        m = _subject_mask(l, scene_roi)
        sub_l = l[m] if m.any() else l
        bbox = _bbox(m)
        sub_gray = _crop(gray8, bbox) if bbox else gray8
    elif scene_roi == "skin" and "skin" in out["roi"]:
        bbox = out["roi"]["skin"]["bbox"]
        sub_l, sub_gray = _crop(l, bbox), _crop(gray8, bbox)
    elif scene_roi == "center":
        bbox = [0.25, 0.25, 0.5, 0.5]
        sub_l, sub_gray = _crop(l, bbox), _crop(gray8, bbox)
    else:
        bbox, sub_l, sub_gray = None, l, gray8
    s = _stats(sub_l); s.pop("hist"); s.update(_clip(sub_l)); s["bbox"] = bbox
    out["roi"]["subject"] = s
    out["focus"]["laplacian_var"] = _laplacian_var(sub_gray if sub_gray.ndim == 2 and sub_gray.size > 400 else gray8)
    out["focus"]["roi"] = bbox or out["focus"]["roi"]

    # ---- flags vs target ----
    if out["clip"]["high_pct"] > 0.5: out["flags"].append("highlight_clip")
    if out["clip"]["low_pct"] > 5: out["flags"].append("shadow_crush")
    if s["p1"] is not None and s["p1"] < 0.03 and scene_roi == "dark_subject": out["flags"].append("subject_shadow_crush")
    if out["geometry"]["skew_deg"] and out["geometry"]["skew_deg"] > 1.0: out["flags"].append("skewed")
    if target:
        out["target_check"] = check_target(out, target)
        if out["target_check"]["pass"]:
            out["flags"].append("within_targets")
    return out


def check_target(a: dict, t: dict) -> dict[str, Any]:
    """Compare analysis against a target vector; return per-key results."""
    res: dict[str, Any] = {}
    def rng(key, val, lo_hi):
        if val is None: res[key] = {"value": None, "ok": False}; return
        lo, hi = lo_hi; res[key] = {"value": val, "range": [lo, hi], "ok": lo <= val <= hi}
    def mx(key, val, limit):
        if val is None: res[key] = {"value": None, "ok": True}; return
        res[key] = {"value": val, "max": limit, "ok": val <= limit}
    def mn(key, val, limit):
        if val is None: res[key] = {"value": None, "ok": False}; return
        res[key] = {"value": val, "min": limit, "ok": val >= limit}
    sub, skin = a["roi"].get("subject", {}), a["roi"].get("skin") or a["roi"].get("face") or {}
    if "luma_median" in t: rng("luma_median", a["luma"]["median"], t["luma_median"])
    if "clip_high_pct_max" in t: mx("clip_high_pct", a["clip"]["high_pct"], t["clip_high_pct_max"])
    if "clip_low_pct_max" in t: mx("clip_low_pct", a["clip"]["low_pct"], t["clip_low_pct_max"])
    if "cct_k" in t: rng("wb_estimate_k", a["channels"]["wb_estimate_k"], t["cct_k"])
    if "subject_p1_min" in t: mn("subject_p1", sub.get("p1"), t["subject_p1_min"])
    if "subject_median" in t: rng("subject_median", sub.get("median"), t["subject_median"])
    if "subject_p99_max" in t: mx("subject_p99", sub.get("p99"), t["subject_p99_max"])
    if "skin_luma" in t: rng("skin_luma", skin.get("median"), t["skin_luma"])
    if "clip_high_pct_face_max" in t: mx("face_clip_high_pct", skin.get("high_pct"), t["clip_high_pct_face_max"])
    if "skew_deg_max" in t: mx("skew_deg", a["geometry"].get("skew_deg"), t["skew_deg_max"])
    if "uniformity_max" in t: mx("uniformity", a.get("uniformity"), t["uniformity_max"])
    res["pass"] = all(v.get("ok", True) for k, v in res.items() if isinstance(v, dict))
    return res


def diff(prev: dict | None, cur: dict) -> dict[str, Any]:
    """Delta between two analyses, for the 'did that iteration help' view."""
    if not prev:
        return {}
    def d(a, b):
        return None if a is None or b is None else round(b - a, 4)
    return {"luma_median": d(prev["luma"]["median"], cur["luma"]["median"]),
            "clip_high_pct": d(prev["clip"]["high_pct"], cur["clip"]["high_pct"]),
            "clip_low_pct": d(prev["clip"]["low_pct"], cur["clip"]["low_pct"]),
            "focus": d(prev["focus"]["laplacian_var"], cur["focus"]["laplacian_var"]),
            "wb_estimate_k": d(prev["channels"]["wb_estimate_k"], cur["channels"]["wb_estimate_k"]),
            "subject_p1": d(prev["roi"].get("subject", {}).get("p1"), cur["roi"].get("subject", {}).get("p1"))}
