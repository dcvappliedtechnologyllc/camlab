# camlab — Claude-in-the-loop camera & lighting optimizer

Handoff spec for a local MCP server + browser front end that lets a Claude session
see what the camera sees, drive camera and light settings, and converge on a
target look for a chosen output format. Written for the DCV gun-room bench rig
and Colleen's Reels workflow; one codebase, two presets.

Status: spec only. No code exists yet. Build order is in §9.

---

## 1. Hardware this targets

| Role | v1 (owned / ordered) | v2 (planned) |
|---|---|---|
| Camera | Canon EOS 77D, USB 2.0 Micro-B, tethered | Canon EOS R50 V (or R7), USB-C, CCAPI over WiFi |
| Lenses | Sigma 17-50 f/2.8 (APS-C), Tamron 70-200 f/2.8 | + Tokina 11-16 f/2.8 (optional) |
| Key light | Godox Litemons LE300Bi, 315 W bicolor COB, Bowens, **wired DMX/RDM** | — |
| Fill light | Godox LP800Bi, 85 W bicolor panel, **Bluetooth app only (not scriptable)** | swap to a DMX panel if fill needs to be in the loop |
| Modifier | Godox EazyFlow 24x36 in gridded softbox on the COB | + 12x47 strip |
| Support | CF tripod + horizontal arm, L-bracket, JerkStopper tether clip | fixed overhead arm off Gallowtech wall |
| Host | Mac (desktop), gphoto2 via Homebrew | Raspberry Pi Zero 2 W on the boom as the server host (optional) |
| DMX interface | USB-DMX (Enttec Open DMX USB or FTDI clone) **or** ESP32 Art-Net node | — |
| Power | ACK-E18 dummy battery; lights on AC | — |

Body-specific facts the code must respect:

- **77D**: no CCAPI. Control is PTP over USB via `gphoto2`. Live view is ~960x640
  JPEG at a few fps. WiFi is disabled in movie mode, so USB is mandatory. Records
  1080p60 to card only; the USB live view is *not* a recording path.
- **R50 V**: CCAPI (HTTP/JSON over WiFi, works in movie mode), plus USB UVC
  for a clean 4K30 stream. 4K60 adds ~1.5x crop. Vertical tripod socket exists;
  orientation is still read from EXIF.
- Both are 1.6x crop. Lens FOV table in §6.

---

## 2. What the system does

1. User picks an **output preset** (what the clip is for) and a **scene type**
   (what's in frame).
2. Server grabs a live-view frame + current camera/light state.
3. Claude (via MCP) analyzes the frame — exposure, clipping, focus, white
   balance, composition hints — and proposes new settings.
4. Server applies them. Loop until Claude declares "converged" or user stops.
5. User records to card. Server pulls the clip, Claude can review real frames.
6. Converged settings are saved as a **named recipe** keyed by preset + scene +
   lens, so next time the same shot starts from the last good state.

The human stays in control of: starting/stopping recording, physical framing,
lens changes, and approving any recipe save. Claude never records autonomously.

---

## 3. Architecture

```
┌─────────────┐   MCP (stdio)   ┌──────────────────┐   HTTP/WS    ┌──────────────┐
│ Claude      │◄───────────────►│ camlab-server    │◄────────────►│ camlab-ui    │
│ (desktop/   │                 │ Python, FastMCP  │              │ single-page  │
│  code)      │                 │ + FastAPI        │              │ web app      │
└─────────────┘                 └───┬──────┬───┬───┘              └──────────────┘
                                    │      │   │
                          gphoto2 / │  DMX │   │ ffmpeg / Pillow / numpy
                          CCAPI     │      │   │ (frame analysis)
                                    ▼      ▼   ▼
                               camera   lights  frames/, clips/, recipes.json
```

- **One Python process** exposes both the MCP tool surface (for Claude) and a
  REST + WebSocket API (for the UI). Single source of truth for state.
- **Camera backends** behind one interface: `Gphoto2Backend` (77D) and
  `CCAPIBackend` (R50 V). Selected by config; auto-detect on startup.
- **Light backends**: `DMXLight` (LE300Bi now; any DMX fixture later). A
  `ManualLight` stub records what the user set by hand so recipes stay complete.
- **Analysis** is done server-side with numpy/Pillow and returned as numbers.
  Claude also receives the JPEG itself so it can judge things numbers miss
  (specular hot spots on a barrel, catchlight shape, framing).

Suggested tree:

```
camlab/
  server/
    main.py            # FastMCP + FastAPI mount, startup detection
    camera/
      base.py          # CameraBackend ABC
      gphoto2_backend.py
      ccapi_backend.py
    lights/
      base.py
      dmx.py           # pyserial → Enttec Open DMX / FTDI, or Art-Net UDP
      manual.py
    analysis.py        # histogram, clipping, focus metric, WB estimate, levels
    recipes.py         # load/save/match recipes.json
    presets.py         # output presets + scene types (§5)
    state.py           # single in-memory state + WS broadcast
  ui/
    index.html         # single-file app (no build step), connects to WS
  recipes.json
  config.toml
  README.md            # this file
```

---

## 4. MCP tool surface (what Claude calls)

All tools return JSON. Frames are returned as base64 JPEG with a `path` on disk.

| Tool | Args | Returns | Notes |
|---|---|---|---|
| `get_state` | — | camera model, lens, mode, all current settings, light levels, active preset/scene, orientation | call first |
| `capture_frame` | `analyze: bool = true` | `{path, jpeg_b64, width, height, analysis}` | live-view grab; analysis per §7 |
| `set_exposure` | `iso?, shutter?, aperture?, ev_comp?` | applied values (camera may snap to nearest step) | strings as the camera names them, e.g. `"1/125"`, `"f/5.6"` |
| `set_white_balance` | `mode?` (`auto|daylight|tungsten|kelvin`), `kelvin?` | applied | 77D kelvin range 2500–10000 |
| `set_picture_style` | `style` (`neutral|standard|portrait|faithful|clog3…`) | applied | body-dependent list from `get_state` |
| `set_focus` | `mode` (`af_one_shot|af_servo|mf`), `drive?` (`near3..far3`), `af_point?` (x,y 0–1) | applied + new focus metric | MF drive is relative on 77D |
| `set_light` | `id` (`key|fill|…`), `intensity_pct?`, `cct_k?` | applied | DMX channels per fixture profile in config |
| `set_movie_mode` | `resolution` (`1080p|4k`), `fps` (`24|30|60`), `codec?` | applied | validates against body capability |
| `record` | `action` (`start|stop`) | clip id / status | **only when UI shows user armed recording**; server refuses otherwise |
| `pull_clips` | `since?` | list of pulled files with paths | copies card → `clips/YYYY-MM-DD/` |
| `extract_frames` | `clip_id, timestamps[]` | frames + analysis | for post-take review |
| `save_recipe` | `name, notes?` | recipe id | snapshots full state; requires UI confirm |
| `load_recipe` | `id` or `match: {preset, scene, lens}` | applied state | |
| `list_presets` | — | §5 tables | |
| `set_context` | `preset, scene, lens?, orientation?` | ack | UI usually sets this; tool exists for voice/chat flows |

Guardrails (server-enforced, not left to the model):

- `record start` only succeeds if `state.armed == true` (user clicked ARM in UI
  within the last 60 s).
- Setting changes are rate-limited to one batch per 500 ms and logged to
  `state.history` for undo.
- `set_light` clamps to fixture profile min/max.
- Nothing deletes from the card. Ever.

---

## 5. Presets and scene types (what the UI selects)

### 5.1 Output presets — "what is this clip for"

| id | Frame | Res / fps | Orientation | Target look | Notes |
|---|---|---|---|---|---|
| `reel` | 9:16 | 1080p60 (77D) / 4K30 (R50 V) | portrait | skin-first: mid-tones 45–55 % luma, no clipped highlights on face, 3200–4300 K warm-neutral | Colleen's default |
| `product_hero` | 16:9 | 4K30 or 1080p30 | landscape | low key, black parts hold detail at 8–15 % luma, specular edge highlights allowed, 5600 K | DCV marketing shots |
| `bench_doc` | 16:9 | 1080p30 | landscape | flat, even, high-DOF (f/8–11), 5000 K, no shadows in fastener recesses | assembly / how-to |
| `overhead` | 16:9 or 1:1 | 1080p30 | landscape | as `bench_doc` but camera pointed down; checks horizon/skew | boom or wall arm |
| `talking_head` | 16:9 | 1080p30 | landscape | as `reel` look, landscape framing, eyes in upper third | interviews, explainers |

### 5.2 Scene types — "what's in frame"

| id | Analysis emphasis | Default lens |
|---|---|---|
| `person_fullbody` | skin ROI, background separation, feet not cut | 17-50 @ 17–24 |
| `person_portrait` | skin ROI, catchlight, hair separation | 17-50 @ 35–50 or 70-200 @ 70–100 |
| `part_dark` | shadow detail on black/parkerized/anodized, specular control | 17-50 @ 35–50, f/5.6–8 |
| `part_bright` | highlight clipping on stainless/brass, reflection management | 17-50 @ 35–50, f/8 |
| `assembly` | DOF covers depth of work area, hands not blown out | 17-50 @ 24–35, f/8–11 |
| `flat_doc` | uniformity across frame, skew < 1°, text legibility | overhead |

Each preset × scene pair has a **target vector** (luma median, clip %, focus
floor, CCT, DOF hint) in `presets.py`. Claude's job is to drive state toward it,
then apply taste the numbers can't encode.

---

## 6. Lens / FOV reference (1.6x crop, for composition hints)

| Lens | FF-equiv | iPhone "x" | Use |
|---|---|---|---|
| 11 mm (Tokina) | 18 | 0.7x | room / doorway |
| 17 mm | 27 | 1.1x | full body indoors |
| 24 mm | 38 | 1.6x | bench wide |
| 35 mm | 56 | 2.3x | portrait / part |
| 50 mm | 80 | 3.3x | tight portrait / part |
| 70 mm | 112 | 4.7x | compressed portrait |
| 200 mm | 320 | 13x | isolated detail |

R50 V 4K60 multiplies all by ~1.5. Server exposes this via `get_state.lens_fov`.

---

## 7. Frame analysis (server computes, returns in `analysis`)

```json
{
  "luma": {"median": 0.47, "p1": 0.03, "p99": 0.94, "hist": [256 bins]},
  "clip": {"high_pct": 0.4, "low_pct": 1.8},
  "channels": {"r_mean":..., "g_mean":..., "b_mean":..., "wb_estimate_k": 4100},
  "focus": {"laplacian_var": 812.3, "roi": [x,y,w,h]},
  "roi": {"skin": {...same fields...}, "subject": {...}},
  "geometry": {"horizon_deg": -0.4, "skew_deg": 0.2},
  "orientation": "portrait",
  "flags": ["face_detected", "highlight_clip_face"]
}
```

- Skin ROI: simple YCbCr skin mask; good enough for a single subject under
  controlled light. Face detection via OpenCV Haar or mediapipe if installed.
- Subject ROI for parts: largest connected dark/bright blob vs. background, or
  user-drawn rectangle from the UI (preferred; persists per recipe).
- Focus: variance of Laplacian on the ROI; relative, not absolute. Claude
  iterates `set_focus drive` and watches it peak.
- Horizon/skew: Hough lines on the bench edge or a calibration card.

---

## 8. Front end (`ui/index.html`)

Single HTML file, no build step, talks to the server over WebSocket. Dark
theme by default (it lives next to a monitor in a gun room). Layout:

```
┌──────────────────────────────────────────────────────────────────────┐
│ camlab    [77D ● USB]  [LE300Bi ● DMX]  [LP800Bi ○ manual]   ⚙ config │
├──────────────────────────────┬───────────────────────────────────────┤
│                              │ OUTPUT                                │
│                              │  ( ) reel  (•) product_hero  ( ) …    │
│      LIVE VIEW               │ SCENE                                 │
│   (frame + ROI overlay +     │  ( ) person_portrait (•) part_dark …  │
│    histogram strip under)    │ LENS   [17-50 ▾]  @ [35] mm           │
│                              │ ORIENT [landscape ▾]                  │
│                              ├───────────────────────────────────────┤
│                              │ CAMERA                                │
│                              │  ISO [400▾] Shutter [1/60▾] f/[5.6▾]  │
│                              │  WB [5600K]  Style [neutral▾]         │
│                              │  Focus [AF one-shot▾] [◄◄][◄][►][►►]  │
│                              │ LIGHTS                                │
│                              │  key  ████████░░ 80%   CCT [5600]     │
│                              │  fill ███░░░░░░░ 30%   (manual)       │
├──────────────────────────────┴───────────────────────────────────────┤
│ CLAUDE                                                               │
│  [ Optimize ]  [ Step once ]  [ Stop ]    status: iter 3 — converged │
│  ▸ "Dropped key to 70%, pulled ISO to 200; barrel flutes now hold    │
│     detail at p1=0.06 with no specular clip. Try 2° more rotation    │
│     on the part toward key for edge definition."                     │
├──────────────────────────────────────────────────────────────────────┤
│ [ ARM ] [ ● REC ]  00:00:00   last clip: 2026-10-19_0007.MP4  [pull] │
│ RECIPES  [ save as… ]  [ part_dark/product_hero/17-50@35 ▾ load ]    │
└──────────────────────────────────────────────────────────────────────┘
```

Behaviors:

- **Every control is two-way.** User changes a dial → server applies → Claude
  sees it on next `get_state`. Claude changes something → UI updates live and
  the change is appended to the history strip with a one-line reason.
- **Optimize** sends `set_context` + a canned prompt to the Claude session
  ("optimize for current preset/scene; stop when within targets or after 8
  iterations; explain each change in one line"). **Step once** does one
  iteration. Implementation: the UI doesn't call the model directly; it writes
  a `request` into server state that the MCP-connected Claude session polls
  via `get_state` (or the user just types "optimize" in chat — both work).
- **ROI draw**: click-drag on live view sets subject ROI; stored with recipe.
- **ARM** is a 60-second window; REC is disabled otherwise. Both are UI-only.
- **Delta view** toggle: shows previous vs current frame side by side with
  histogram diff — the fastest way to see whether an iteration helped.
- **Recipe match banner**: on preset/scene/lens change, if a recipe matches,
  show "load last good: …" with one-click apply.
- **Orientation**: read from camera; if preset is `reel` and camera reports
  landscape, warn (not block).
- **Config panel** (⚙): camera backend, DMX port/universe, fixture channel map,
  clip pull directory, Claude iteration cap.

Keyboard: `space` capture frame, `o` optimize, `s` step, `r` arm, `shift+r` rec,
`[`/`]` focus near/far, `-`/`=` key intensity.

---

## 9. Build order

1. **gphoto2 backend + `capture_frame` + `get_state`** against the 77D. Prove
   live view and setting reads/writes over USB in movie mode. (Quit EOS Utility
   and Image Capture first; they grab the device.)
2. **analysis.py** on captured frames. Unit-test on a few stills of a black
   upper and a face.
3. **MCP surface** via FastMCP. Verify from a Claude desktop session: get
   state, capture, change ISO, capture, compare.
4. **DMX backend** for the LE300Bi. Fixture profile = `{intensity: ch1,
   cct: ch2}` (confirm channel map in the LE300Bi manual; it's 2-ch in basic
   mode). Enttec Open DMX via pyserial at 250 kbaud, or Art-Net UDP to an ESP32.
5. **FastAPI + WS + ui/index.html**. State broadcast, controls, live view
   refresh at 2–4 fps.
6. **Presets/recipes.** Hard-code the five presets and six scenes; save/load.
7. **record / pull_clips / extract_frames.**
8. **CCAPI backend** when the R50 V lands. Same interface; swap in config.
9. Optional: Pi Zero 2 W host on the boom; `set_level` tool for a printed
   ESP32/IMU pan-tilt head.

Acceptance for v1: from a cold start, user selects `product_hero` +
`part_dark`, clicks Optimize, and within 8 iterations the frame has
`clip.high_pct < 0.5`, `roi.subject.p1 > 0.05`, focus metric at a local max,
and Claude's last message says "converged." User arms, records, pulls, and
Claude can report on a frame from the actual clip.

---

## 10. Dependencies

- Python 3.11+: `fastmcp`, `fastapi`, `uvicorn`, `websockets`, `numpy`,
  `Pillow`, `opencv-python-headless` (optional, for face/Hough), `pyserial`
  (DMX), `requests` (CCAPI), `tomli`.
- System: `gphoto2` (`brew install gphoto2`), `ffmpeg`.
- Hardware prerequisites: tether cable + JerkStopper, ACK-E18, USB-DMX dongle
  or ESP32 Art-Net node, XLR-3 DMX cable to the LE300Bi.

---

## 11. Open questions

- LE300Bi DMX channel map and whether it exposes CCT as one channel or two
  (confirm from manual before writing `dmx.py`).
- Whether to replace the LP800Bi with a DMX panel so fill is in the loop.
  v1: leave manual, record its setting in recipes by hand.
- Face detection library choice (Haar is enough; mediapipe is nicer and heavier).
- Does Claude drive the loop by polling `get_state.request`, or does the UI
  post into the chat? Decide at step 5; the tool surface supports both.
