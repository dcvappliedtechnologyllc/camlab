"""Output presets, scene types, target vectors, lens FOV table (spec §5, §6)."""
from __future__ import annotations

PRESETS: dict[str, dict] = {
    "reel": {
        "label": "Reel", "frame": "9:16", "orientation": "portrait",
        "resolution": {"gphoto2": ("1080p", 60), "ccapi": ("4k", 30), "mock": ("1080p", 60)},
        "look": "skin-first: mid-tones 45-55% luma, no clipped highlights on face, 3200-4300K warm-neutral",
        "notes": "Colleen's default",
        "target": {"luma_median": [0.45, 0.55], "clip_high_pct_max": 0.3,
                   "clip_low_pct_max": 3.0, "cct_k": [3200, 4300], "dof": "shallow"},
    },
    "reel_interior": {
        "label": "Reel: interior", "frame": "9:16", "orientation": "portrait",
        "resolution": {"gphoto2": ("1080p", 30), "ccapi": ("4k", 30), "mock": ("1080p", 30)},
        "look": "bright and airy room: mid-tones 48-60% luma, windows may glow but walls and furniture keep "
                "detail, verticals straight (walk-through / pan), everything in focus, 4000-5000K clean neutral",
        "notes": "rooms, decor, staging walk-throughs for Reels",
        "target": {"luma_median": [0.48, 0.60], "clip_high_pct_max": 2.0,
                   "clip_low_pct_max": 2.0, "cct_k": [3800, 5200], "dof": "deep",
                   "aperture": ["f/5.6", "f/8"], "skew_deg_max": 1.0},
    },
    "product_hero": {
        "label": "Product hero", "frame": "16:9", "orientation": "landscape",
        "resolution": {"gphoto2": ("1080p", 30), "ccapi": ("4k", 30), "mock": ("1080p", 30)},
        "look": "low key, black parts hold detail at 8-15% luma, specular edge highlights allowed, 5600K",
        "notes": "DCV marketing shots",
        "target": {"luma_median": [0.18, 0.35], "subject_p1_min": 0.05,
                   "clip_high_pct_max": 0.5, "cct_k": [5400, 5800], "dof": "medium"},
    },
    "bench_doc": {
        "label": "Bench doc", "frame": "16:9", "orientation": "landscape",
        "resolution": {"gphoto2": ("1080p", 30), "ccapi": ("1080p", 30), "mock": ("1080p", 30)},
        "look": "flat, even, high-DOF (f/8-11), 5000K, no shadows in fastener recesses",
        "notes": "assembly / how-to",
        "target": {"luma_median": [0.45, 0.60], "clip_high_pct_max": 0.5,
                   "clip_low_pct_max": 1.0, "cct_k": [4800, 5200], "dof": "deep",
                   "aperture": ["f/8", "f/11"]},
    },
    "overhead": {
        "label": "Overhead", "frame": "16:9", "orientation": "landscape",
        "resolution": {"gphoto2": ("1080p", 30), "ccapi": ("1080p", 30), "mock": ("1080p", 30)},
        "look": "as bench_doc but camera pointed down; checks horizon/skew",
        "notes": "boom or wall arm",
        "target": {"luma_median": [0.45, 0.60], "clip_high_pct_max": 0.5,
                   "clip_low_pct_max": 1.0, "cct_k": [4800, 5200], "dof": "deep",
                   "skew_deg_max": 1.0},
    },
    "talking_head": {
        "label": "Talking head", "frame": "16:9", "orientation": "landscape",
        "resolution": {"gphoto2": ("1080p", 30), "ccapi": ("1080p", 30), "mock": ("1080p", 30)},
        "look": "as reel look, landscape framing, eyes in upper third",
        "notes": "interviews, explainers",
        "target": {"luma_median": [0.45, 0.55], "clip_high_pct_max": 0.3,
                   "cct_k": [3200, 4300], "dof": "shallow", "eyes_upper_third": True},
    },
}

SCENES: dict[str, dict] = {
    "person_fullbody": {"label": "Person, full body",
                        "emphasis": ["skin ROI", "background separation", "feet not cut"],
                        "default_lens": "17-50 @ 17-24", "roi": "skin"},
    "person_portrait": {"label": "Person, portrait",
                        "emphasis": ["skin ROI", "catchlight", "hair separation"],
                        "default_lens": "17-50 @ 35-50 or 70-200 @ 70-100", "roi": "skin"},
    "interior_room": {"label": "Interior, whole room",
                      "emphasis": ["windows not blown out", "walls and floor keep detail", "verticals straight",
                                   "even light across the room"],
                      "default_lens": "17-50 @ 17-24, f/5.6-8", "roi": "full"},
    "decor_detail": {"label": "Decor detail",
                     "emphasis": ["subject sharp, background soft", "no hot reflections on glass/metal",
                                  "true colours of fabric and wood"],
                     "default_lens": "17-50 @ 35-50, f/2.8-4", "roi": "center"},
    "part_dark": {"label": "Part, dark finish",
                  "emphasis": ["shadow detail on black/parkerized/anodized", "specular control"],
                  "default_lens": "17-50 @ 35-50, f/5.6-8", "roi": "dark_subject"},
    "part_bright": {"label": "Part, bright finish",
                    "emphasis": ["highlight clipping on stainless/brass", "reflection management"],
                    "default_lens": "17-50 @ 35-50, f/8", "roi": "bright_subject"},
    "assembly": {"label": "Assembly",
                 "emphasis": ["DOF covers work area depth", "hands not blown out"],
                 "default_lens": "17-50 @ 24-35, f/8-11", "roi": "center"},
    "flat_doc": {"label": "Flat document",
                 "emphasis": ["uniformity across frame", "skew < 1 deg", "text legibility"],
                 "default_lens": "overhead", "roi": "full"},
}

# Scene-specific target overrides merged on top of preset target.
SCENE_TARGETS: dict[str, dict] = {
    "person_fullbody": {"skin_luma": [0.40, 0.58]},
    "person_portrait": {"skin_luma": [0.45, 0.58], "clip_high_pct_face_max": 0.1},
    "interior_room": {"skew_deg_max": 1.0, "uniformity_max": 0.35, "clip_high_pct_max": 2.0},
    "decor_detail": {"dof": "shallow", "subject_median": [0.35, 0.60], "subject_p99_max": 0.97},
    "part_dark": {"subject_p1_min": 0.05, "subject_median": [0.08, 0.20]},
    "part_bright": {"clip_high_pct_max": 0.2, "subject_p99_max": 0.97},
    "assembly": {"focus_floor_rel": 0.8},
    "flat_doc": {"uniformity_max": 0.12, "skew_deg_max": 1.0},
}

LENSES: dict[str, dict] = {
    "tokina_11_16": {"label": "Tokina 11-16 f/2.8", "min_mm": 11, "max_mm": 16, "max_aperture": "f/2.8"},
    # Identified from the clips' Canon maker note (exiftool LensID), 2026-10-25: Sigma 17-50mm f/2.8 EX DC OS HSM,
    # an EF-S lens on an EF-RF adapter, so the body reports no lens name over USB (LensModel blank, RF lens n/a).
    "sigma_17_50": {"label": "Sigma 17-50 f/2.8 EX DC OS HSM", "min_mm": 17, "max_mm": 50, "max_aperture": "f/2.8",
                    "min_aperture": "f/22", "stabilized": True, "mount": "EF-S via adapter"},
    "tamron_70_200": {"label": "Tamron 70-200 f/2.8", "min_mm": 70, "max_mm": 200, "max_aperture": "f/2.8"},
    # Bought 2026-10-28 (B&H). Native RF-mount APS-C lens, so unlike the adapted Sigma 17-50 the body should
    # report its name over USB. 72mm filter thread; close focus ~4.3 in (wide) for near-macro bench shots.
    "sigma_10_18": {"label": "Sigma 10-18 f/2.8 DC DN Contemporary", "min_mm": 10, "max_mm": 18,
                    "max_aperture": "f/2.8", "min_aperture": "f/22", "stabilized": False, "mount": "RF (native)",
                    "filter_mm": 72},
}

CROP = 1.6

FOV_TABLE = [  # focal_mm, ff_equiv, iphone_x, use
    (10, 16, 0.7, "whole room / tight-space wide"),
    (11, 18, 0.7, "room / doorway"),
    (17, 27, 1.1, "full body indoors"),
    (24, 38, 1.6, "bench wide"),
    (35, 56, 2.3, "portrait / part"),
    (50, 80, 3.3, "tight portrait / part"),
    (70, 112, 4.7, "compressed portrait"),
    (200, 320, 13.0, "isolated detail"),
]


def lens_fov(focal_mm: float | None, extra_crop: float = 1.0) -> dict | None:
    if not focal_mm:
        return None
    ff = focal_mm * CROP * extra_crop
    # nearest table row for the "use" hint
    row = min(FOV_TABLE, key=lambda r: abs(r[0] - focal_mm))
    return {"focal_mm": focal_mm, "ff_equiv_mm": round(ff), "iphone_x": round(ff / 24, 1),
            "hint": row[3], "crop": CROP * extra_crop}


def target_vector(preset: str, scene: str) -> dict:
    t = dict(PRESETS.get(preset, {}).get("target", {}))
    t.update(SCENE_TARGETS.get(scene, {}))
    return t


def list_all() -> dict:
    return {"presets": PRESETS, "scenes": SCENES, "lenses": LENSES,
            "fov_table": [dict(zip(("focal_mm", "ff_equiv_mm", "iphone_x", "use"), r)) for r in FOV_TABLE]}
