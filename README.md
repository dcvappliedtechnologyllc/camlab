# camlab — Claude-in-the-loop camera & lighting optimizer

A DCV Applied Technology × CollTakesChagrin tool. One Python process tethers a
Canon body over USB and two Godox lights over Bluetooth Mesh, and exposes
them three ways at once: a single-file web UI (also usable from a phone on the
same Wi-Fi), a REST + WebSocket API, and an MCP tool surface for a Claude
session. Claude sees what the camera sees, drives camera and light settings,
and converges on a target look for a chosen output preset (a portrait Reel, a
product hero shot, a bench how-to clip). Full design: [SPEC.md](SPEC.md).

The camera and light control layers are usable on their own: see
[Reusable components](#reusable-components-camera-and-light-control).

## What you need

Hardware this was built and verified on:

| Part | Role | Notes |
|---|---|---|
| Canon EOS R50 V | camera, over USB | driven through Canon's EDSDK; python-gphoto2 is the fallback; CCAPI over Wi-Fi is written but unverified |
| Godox LE300Bi | key light | Bluetooth Mesh (or 2-channel DMX through an Enttec Open DMX / FTDI cable) |
| Godox LP800Bi | fill light | Bluetooth Mesh |
| An iPhone with the Godox Light app | one-time | pairs the lights and provides the mesh export (below) |
| A USB tether | | active cables are directional: the boosted end goes to the Mac |

Other Canon bodies will mostly work over EDSDK or gphoto2 (property tables
come from Canon's reference, lens limits from `server/presets.py`), and any
Godox light the Godox Light app can drive speaks the same mesh protocol. Only
the hardware above has been exercised. No hardware at all is needed to try
it: `--camera mock` gives a synthetic camera and the lights fall back to
"manual" (recorded, not driven).

Software: macOS 14 or later (Apple Silicon or Intel), Python 3.14 and ffmpeg
from Homebrew, Xcode command line tools (`codesign`, `ditto`) for the Mac app.

## Install

```bash
brew install python@3.14 ffmpeg
git clone https://github.com/dcv-at/camlab.git && cd camlab
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml        # then edit: lights, LAN exposure, API key
```

**Canon EDSDK (for the real camera).** Canon does not allow the SDK to be
redistributed, so it is not in this repository. Register on
[Canon's developer site](https://developercommunity.usa.canon.com/) (or the
Canon developer programme for your region), download the macOS EDSDK (v13.20
or later), and copy `EDSDK.framework` into `vendor/`, so that
`vendor/EDSDK.framework/Versions/A/EDSDK` exists. The header files
(`EDSDK.h`, `EDSDKTypes.h`, `EDSDKErrors.h`) can go in `vendor/EDSDK_headers/`
for reference; the backend loads the framework over ctypes and needs no
bindings package. `vendor/` is gitignored. Without it, `backend = "auto"`
tries python-gphoto2 (bundled with the `gphoto2` wheel), then CCAPI if
`ccapi_host` is set, then the mock camera.

Before plugging in the camera: quit EOS Utility, Image Capture and Photos.
macOS claims the body with `ptpcamerad`; the backends kill it and retry on
their own, and you will see "freed camera from macOS PTP daemon(s)" in the
log. Put the body's photo/video switch on **video** for clips.

**Godox lights (mesh export).** The Godox Light phone app talks to its lights
over encrypted Bluetooth SIG Mesh; camlab speaks the same protocol from the
Mac and needs the network and app keys the phone app holds. Once:

1. Pair both lights in the **Godox Light** app on the phone as usual, and
   name them (`LE300Bi`, `LP800Bi`; the names go in `config.toml`).
2. Get the mesh configuration out of the app. iPhone: plug it into the Mac,
   Finder → the phone → **Files** tab → drag the Godox Light app's folder
   out; it contains `GodoxLight.db`. If your app version has a share/export
   network option, that JSON file works too. Either file is accepted.
3. Copy it into the camlab folder (`GodoxLight.db` or `godox_mesh.json`) and
   check it parses: `.venv/bin/python -m server.lights.godox_mesh export GodoxLight.db`
   lists the lights it found (the phone's provisioner entry is skipped).
4. In `config.toml`, `[lights.key]` and `[lights.fill]` point `mesh_export`
   at that file and `mesh_address` at each light (name, MAC or unicast
   address). `config.example.toml` shows both.
5. Close the Godox app on the phone (a light accepts one connection at a
   time).

The export holds the mesh keys; it is gitignored and never leaves the
machine. camlab never provisions or touches device keys, so the lights stay
paired with the phone.

**Bluetooth permission.** macOS kills a process that touches CoreBluetooth
without a grant (signal 134) instead of prompting it, unless the process is a
real app or a terminal that can prompt. Run the web version from
**Terminal.app** the first time and click Allow when asked, or double-click
`scripts/bringup.command`, which runs the light bring-up in Terminal.app and
explains what to do if macOS refused. The Mac app (below) carries its own
Bluetooth usage description and asks once.

## Run

```bash
.venv/bin/python -m server.main                  # UI http://127.0.0.1:8765, MCP at http://127.0.0.1:8765/mcp
.venv/bin/python -m server.main --camera mock    # synthetic camera, no hardware
.venv/bin/python -m pytest tests -q              # the test suite needs no hardware (fakes for EDSDK, CCAPI, BLE)
```

With no `config.toml` the defaults apply: mock camera (auto with no SDK and
no body), key light on DMX with no port, fill light manual, UI on
127.0.0.1 only. `[server] host = "0.0.0.0"` opens the UI to the LAN (there is
no login; the per-day AI spend cap bounds what anyone on the LAN can cost
you). Clips, frames, recipes, logs and the saved context live next to the
config (`$CAMLAB_HOME` moves them).

## Hook up Claude

**Claude Code** (HTTP transport, server must already be running):

```bash
claude mcp add --transport http camlab http://127.0.0.1:8765/mcp
```

**Claude Desktop** (stdio; Desktop launches the server itself, UI comes up on
:8765 as a side effect). Copy `claude_desktop_config.example.json` with your
own path filled in and merge it into
`~/Library/Application Support/Claude/claude_desktop_config.json`.

Then in chat: "optimize for product_hero / part_dark" — or click **Optimize**
in the UI, or type into the chat line under the log. Both set `claude.request`;
the model sees it on `get_state`, or blocks for it with `wait_for_request`
(long-poll, up to 120 s) and answers with `claude_report`, which is the only
channel that shows in the UI. Tell the Claude session once: "run camlab: loop
on wait_for_request and act on what comes back."

**No MCP, no API key** (any chat): the UI's Strings mode writes a picture and
an 8-line `CAMLAB/1` string to a hand-off folder; paste the reply back and it
is applied through the same guardrails. `skills/camlab/SKILL.md` is a Claude
skill that answers those strings.

## The Mac app, signing and keep-alive

The same tree builds a double-clickable `camlab.app` (pywebview window
around the same server; `app/README.md` has the details). It is self-signed
for home use, not notarized.

```bash
./app/make-signing-identity.sh     # once: a self-signed "camlab dev" code-signing identity in the login keychain
./app/build.sh                     # PyInstaller build -> dist/camlab.app (needs vendor/EDSDK.framework)
open dist/camlab.app
scripts/launchagent.sh install     # optional: start at login and relaunch within seconds if it crashes
scripts/launchagent.sh status      # or: remove
```

The certificate matters for one thing: macOS keys the Bluetooth grant on the
app's code-signing requirement. Signed with a certificate the grant survives
rebuilds; ad-hoc signed, every rebuild asks again. `make-signing-identity.sh`
may show two prompts (trust the certificate; let `codesign` use the key: click
**Always Allow**). Nothing leaves the machine. The app keeps its config and
data in `~/Library/Application Support/camlab/`, seeded from the repo's
`config.toml` on first launch. The LaunchAgent (`com.dcv.camlab`) relaunches
the app after a crash but respects a clean quit. `scripts/morning_test.command`
runs the tests and then the app with a bench checklist.

## Reusable components: camera and light control

Both hardware layers are plain synchronous Python with no dependency on the
rest of camlab, so they can be lifted into another project.

### Camera control (`server/camera/`)

`CameraBackend` (`server/camera/base.py`) is the interface; the server wraps
it in a thread executor. Values are strings exactly as the camera names them
(`"1/125"`, `"f/5.6"`, `"400"`, `"5600"`), so settings are portable between
bodies and backends.

| Method | Does |
|---|---|
| `connect() / disconnect() / info()` | session; `CameraInfo` carries model, lens, mode and a `capabilities` dict (legal ISO/shutter/aperture lists, kelvin range, movie modes, which optional methods work) |
| `get_settings() / set_settings({...})` | iso, shutter, aperture, ev_comp, wb_mode, kelvin, picture_style, focus_mode, movie_resolution, movie_fps; `set_settings` returns what was actually applied after the body snaps to a legal step |
| `capture_frame()` | one live-view JPEG (`Frame`: bytes, size, orientation) |
| `set_exposure_mode("manual")` | best effort; the top dial decides on most bodies |
| `focus_drive("near1".."far3")`, `set_af_point(x, y)` | optional |
| `record("start" / "stop")` | returns `{"recording", "clip"}`; the clip name arrives when the body announces it |
| `list_clips() / pull_clip(clip, dest_dir)` | the card, read-only: nothing in any backend deletes |
| `still_mode() / set_image_quality(raw) / take_picture(dest_dir)` | stills: JPEG or RAW+JPEG, copied off the card |

Backends:

- `edsdk_backend.py`: Canon EDSDK over ctypes, no bindings package. Property
  codes and value tables are in `edsdk_tables.py` (generated from Canon's API
  reference). Handles the macOS main-thread rule (the SDK discovers cameras
  and delivers events only on the main thread; `server/mainloop.py` is the
  job loop that services it), keep-awake, reconnect on wake or re-plug, and
  the `ptpcamerad` fight. Verified on the R50 V: live view, every setting,
  focus drive, record, listing and pulling clips, stills.
- `gphoto2_backend.py`: python-gphoto2, one persistent PTP session; live
  view, settings, record. On the R50 V it cannot see the clips it recorded
  (a body limitation, see Known gaps).
- `ccapi_backend.py`: Canon's HTTP Camera Control API over Wi-Fi, the whole
  surface including the contents API. Written against the reference and
  tested against an in-process fake; not yet run against a body.
- `mock_backend.py`: a synthetic scene that responds to exposure and colour
  settings, for the UI and the tests.

Standalone use:

```python
from server.camera.edsdk_backend import EdsdkBackend      # or Gphoto2Backend(), CCAPIBackend(host), MockBackend()
cam = EdsdkBackend(framework="vendor/EDSDK.framework")      # default: the framework under vendor/ or $EDSDK_FRAMEWORK
info = cam.connect()                                        # CameraInfo; info.capabilities["iso"] lists the legal values
cam.set_settings({"iso": "400", "shutter": "1/60", "kelvin": "4300"})
frame = cam.capture_frame()                                 # frame.jpeg
cam.record("start"); ...; clip = cam.record("stop")["clip"]
cam.pull_clip(cam.list_clips()[-1], "clips/")
cam.disconnect()
```

EDSDK calls must come from the main thread on macOS; in a script that is
simply the case. `python -m server.camera.ccapi_backend probe <ip>` is a
small diagnostic for the Wi-Fi backend.

### Light control (`server/lights/`)

`LightBackend` (`server/lights/base.py`): `connect()`, `set(intensity_pct, cct_k)`
(clamped to the fixture profile, then `apply()`), `state()` → `LightState`
(id, model, connected, intensity, Kelvin, limits, `scriptable`, and
`last_ack` for backends that hear the fixture answer).

- `godox_mesh.py` is the one worth lifting: a complete, dependency-light
  Bluetooth SIG Mesh client for Godox lights (bleak for the radio,
  `cryptography` for AES-CCM/CMAC). Layers, lowest first: `godox_cct()` /
  `godox_power()` build the 8-byte vendor frame (opcode 0x0211F0, CRC-8);
  `NetKey` / `AppKey` / `encrypt_network_pdu()` / `encrypt_access()` are
  Mesh Profile 1.0 §3.8 crypto, kept line-for-line from `ha-godox-ble-mesh`
  and checked against `godox-tl`'s captured wire bytes in the tests;
  `MeshNetwork` holds the sequence counter (persisted next to the export, with
  a probe for the replay floor the lights enforce) and builds PDUs;
  `load_mesh_export()` reads the phone app's `GodoxLight.db` (SQLite) or JSON
  export; `GodoxMeshClient` is one GATT proxy connection that drives every
  light in the mesh and decrypts their status replies; `GodoxMeshLight` is
  the camlab backend. A CLI covers `export`, `scan`, `probe`, `set` and a
  full PASS/FAIL `bringup`:

  ```bash
  .venv/bin/python -m server.lights.godox_mesh export GodoxLight.db
  .venv/bin/python -m server.lights.godox_mesh set --export GodoxLight.db --light LP800Bi 50 4500
  .venv/bin/python -m server.lights.godox_mesh bringup --export GodoxLight.db
  ```

  Standalone:

  ```python
  from server.lights.godox_mesh import GodoxMeshLight
  fill = GodoxMeshLight("fill", {"model": "Godox LP800Bi", "mesh_export": "GodoxLight.db", "mesh_address": "LP800Bi"})
  fill.connect()                      # scans for a proxy node on this mesh, probes the sequence floor
  fill.set(60, 4300)                  # 60 %, 4300 K; the light's own status record lands in fill.state().last_ack
  fill.close()
  ```

  Several lights sharing one export share one Bluetooth connection and one
  sequence counter (`shared_client()`). The light's radio answers a status
  request within about 60 ms, so a change can be confirmed without looking
  at the panel (`query_status()`, `wait_ack()`). Details in
  "Lights over Bluetooth Mesh" below.
- `dmx.py`: a 2-channel (intensity, CCT) DMX fixture over an Enttec Open DMX
  / FTDI serial cable or Art-Net.
- `manual.py`: a light set by hand; camlab records the values and the UI
  shows them, nothing is sent.

## Layout

```
server/
  main.py          entry: FastAPI + FastMCP in one process (--stdio adds stdio MCP)
  controller.py    the one object both surfaces call; guardrails live here
  mcp_tools.py     spec §4 tools (frames returned as image content + JSON)
  api.py           REST + /ws (mirrors the tools 1:1) and the live-view loop
  state.py         in-memory state, history (undo), broadcast
  presets.py       presets, scenes, target vectors, lens FOV
  analysis.py      luma/clip/WB/focus/skin/subject/geometry → JSON
  recipes.py       recipes.json: snapshot keyed by preset+scene+lens
  clips.py         pull from card (never deletes), ffmpeg frame extraction
  stills.py        Stills page: the LOOKS table, one-press capture, photo list (never deletes)
  mainloop.py      main-thread job runner (Canon EDSDK only works on the main thread; uvicorn runs in a thread)
  camera/          base.py, edsdk_backend.py (Canon EDSDK over USB, primary; R50 V verified), edsdk_tables.py (generated from Canon's reference), gphoto2_backend.py (python-gphoto2, one persistent PTP session; fallback), ccapi_backend.py (Canon CCAPI over Wi-Fi, unverified), mock_backend.py
vendor/EDSDK.framework  Canon's EDSDK, downloaded by you (not in the repo, see Install)
  lights/          base.py, dmx.py (Open DMX serial or Art-Net), godox_mesh.py (Bluetooth Mesh, LP800Bi/LE300Bi, echo listener, bringup CLI), manual.py
scripts/bringup.command  double-click: runs the mesh bring-up in Terminal.app (Bluetooth prompt), logs to logs/
ui/index.html      single-file app, dark, joint DCV × CollTakesChagrin lockup
assets/            wordmarks (SVG), brand.css, preview.html
tests/             pytest tests, all mock-based (EDSDK and CCAPI run against fakes) (the mesh tests use a fake BLE node that echoes like the firmware)
config.toml        your copy of config.example.toml: camera backend, lights, paths, AI caps, LAN exposure
app/               the Mac app: launcher, PyInstaller spec, build and signing scripts
scripts/launchagent.sh  install/remove the keep-alive LaunchAgent for the app
```

## The UI: portrait first, plain words, AI Auto

`ui/index.html` (one file, both builds) lays itself out from the live frame: a
portrait frame (Reel) puts the viewfinder in the middle at full window height
with ARM / REC / timer / last clip on its bottom edge and the controls in
drawers that slide in from the edges (Setup on the left: output, scene, lens,
targets; Controls on the right: camera, lights, AI Auto, recipes; Claude's log
and chat along the bottom). A landscape frame pins the same drawers open as
columns. Drawer and section state is remembered per browser (`localStorage`),
`f` is focus mode (picture only), `1 2 3` toggle the drawers, `?` lists every
key, and on a phone the drawers are bottom sheets. The app window opens taller
when the last context was portrait; the context (preset, scene, lens, focal
length, orientation) is saved to `<home>/context.json` and restored at start.

Readouts on the picture are plain sentences, not numbers: "Too dark: raise key
or ISO", "Face too bright", "Sharp", "Too warm for Reel", each with a tooltip
that says what to do; the live loop measures the frame every
`liveview_hud_s` seconds (3) for them. The numbers live in the Targets
section, with a definition on hover for every target.

**AI Auto** (`o`, or the button) has the server call the Claude API itself:
it captures a frame, sends the picture, the measurements and the targets,
and the model adjusts camera and lights through the same `Controller`
methods the UI uses (rate limit, clamps, snapping, no record/pull/delete
tools), looks again, and repeats until it reports converged, the look cap
(`claude.iteration_cap`), the per-run cost cap (`ai.cost_cap_usd`, default
$1.00; a run is usually $0.30-0.60 on Opus 5.5), the wall clock
(`ai.max_run_s`), Stop (`x`) or a REC start. It never runs while a take rolls:
the controller refuses any change from source `ai` while recording, and a
per-day cap (`ai.daily_cap_usd`, default $5.00, this process) bounds what
anyone on the LAN can spend.
Its reasoning shows in the timeline; the final line reports status, rounds and
cost. Config in `config.toml` `[ai]`: `api_key` (or `ANTHROPIC_API_KEY`),
`model` (`claude-opus-5-5`; `claude-sonnet-5-5` is about half the cost),
`effort`. With no key, AI Auto hands the request to an MCP-connected Claude
session as before and says so in the log. The app icon is `assets/icon/camlab.icns` (`build_icns.sh` rebuilds it from an SVG).

### Shoot mode (default) and Studio

The header switch picks **Shoot** or **Studio** (remembered per browser; Shoot
is the default). Shoot is one viewfinder and one card that walks a take in six
steps in plain words: what are you making (four tiles: Portrait Reel ("Colleen Reel" in the UI, after the
CollTakesChagrin persona it was built for), Interior Reel, Product shot, Bench how-to, each setting output, scene and orientation,
with a "turn the camera upright" hint until the frame comes up portrait); get
in position; set it up for me (AI Auto, narrated in sentences, or "load last
good setup"); check (three verdicts: bright enough, the subject line, colour
natural, each with one fix: key light up/down, "fix it for me", or a sentence);
record (one button arms and starts; space records and stops); keep this setup
(a recipe named after the tile). Every step has a "why?" that opens Studio at
the matching panel. Esc goes back a step. Studio is the full desk described
below.

### Stills: a look, not settings

The **Video | Stills** switch at the top of the panel turns the same screen into
a still-photo page. Instead of settings you pick a **look**, each with one
sentence saying what it does and when to use it: **High bokeh** (the lens as
wide as it goes, one thing stands out), **Everything sharp** (f/8, parts and
assemblies in focus front to back), **Product on black** (low key, fixed ISO
100, specular edges), **Clean portrait** (skin first, 4300 K, soft key) and
**Flat document / overhead** (even light, square-on). A look writes aperture,
shutter, ISO (Auto where the look says "clean exposure", a fixed value
otherwise), colour temperature, picture style and AF mode through the usual
controller path, clamped to the lens: the body's own aperture list first (the
Sigma 17-50 reads f/2.8 at any focal length), the lens table in `presets.py`
second. Each look also shows where to put the key and fill light, in words.
The table is `LOOKS` in `server/stills.py`; a look borrows its target band from
the matching preset, so a photo is judged the way a clip would be.

**Shoot** (the gold button, or space) takes one photo over EDSDK: shutter
pressed fully and released, the body announces the new file the way it
announces a clip, and it is copied into `<home>/stills/<date>/<tag>-NNN.JPG`
with a sidecar (settings, look, measurements). No ARM: that guardrail is for
a rolling take, and the readiness list says so. The card is never written to
or cleared. The photo is analysed like a capture (luma, clipping, focus,
skin or subject region) and listed under **Photos** with plain chips
("Too bright", "Sharp", "Face good"), Send to my phone (QR to
`/stills/<label>`), Show in Finder and Ask about this photo. **RAW too**
appears when the body lists a RAW+JPEG quality; it is off by default and
keeps the CR3 next to the JPEG.

Readiness in Stills: camera, **photo dial**, lights, subject. Canon's EDSDK
cannot move the Mode dial from movie to photo (its MovieSelectSw commands only
work with the dial already on a photo position), so the page reads the body's
movie flag (`kEdsPropID_FixedMovie`) and asks for the dial by hand: "Turn the
top dial to a photo position (M)", with a Show-me-how entry (`photo_dial`).
Looks need M in the photo range so the body does not take the values back.

Ask Claude and the strings work with a photo: `POST /api/ask {text, still}` and
`POST /api/handoff {still}` send a downscaled copy of the JPEG with a `still=`
line (label, look, size) and the look's own target band; the reply's verdict
is keep/retake for the photo. Routes: `GET /api/stills/looks`,
`GET /api/stills/ready`, `POST /api/stills/mode {mode: stills|video}`,
`POST /api/stills/look {look}`, `POST /api/stills/shoot {raw}`,
`GET /api/stills` (today's photos with LAN urls), `POST /api/stills/send
{label}`, `GET /stills/<label>`. State carries `stills_mode`, `stills_look`,
`stills`, `last_still`; the mode and look persist in `context.json`.

### Strings and Ask Claude: advice you apply yourself

**Ask Claude** (needs `[ai] api_key`): type a question; camlab makes ONE API call with the current
frame (or a clip's contact sheet), a compact state (the preset's look, camera, lights, targets, what
the frame measures, `[chat] preferences`) and your words. Claude answers with one `propose` tool
call: a plain sentence, the camera/light values, where to put each light (from behind the camera,
degrees, height against the eyes, feet, how to soften), and for a clip keep/retake and a trim.
Nothing changes until you press Apply (`/api/ask/apply`, source `ask`, refused while recording); one
Undo reverts the whole applied proposal. The last 6 exchanges are remembered so "a bit warmer"
works; `/api/ask/reset` forgets them. Each message is metered with AI Auto's price table and counts
against `ai.daily_cap_usd`. `server/ask.py`.

**Strings** (no key, any chat): Generate (`/api/handoff`) writes the picture to
`~/Desktop/camlab-handoff/sample.jpg` (a clip review: `sheet.jpg`) and an 8-line `CAMLAB/1` string to
`handoff.md`; the long explanation (persona, kelvin anchors, feet, behind-the-camera placement, the
reply formats, the body's choices) goes once per session into `preamble.md`. Ingest
(`/api/handoff/apply`) takes the chat's `CAMLAB/1 reply` (or `clipreply`) block with or without a fence
and prose around it, refuses a reply for an old round or another session (409, `force` applies anyway),
applies it through the controller (source `chat`, one undo reverts it) and stores it with the next
measurement in `<home>/rounds.json`. A clip reply's verdict, trim and caption go into the clip's
sidecar `<clip>.json`. The old fenced ```camlab block, bare `camera:`/`lights:` lines and JSON are
still accepted. `/api/clips/sheet` builds the contact sheet (4 frames at 10/40/70/95 %, time burned
in, 2×2, long edge 1024). `server/handoff.py`; the `camlab` skill in `skills/camlab/SKILL.md` answers
the strings.

### Save to…

After a take, **Save to…** (Shoot step 5, and next to pull in Studio) opens
the Mac's save panel for the last clip; the app pulls it from the card if it
has not yet, copies it where you chose, remembers that folder for next time
(`<home>/export.json`) and shows it in Finder. Pulled clips still land in
`<home>/clips/<date>/` either way. The web version has no native dialogs, so
there the button reports the local copy's path instead
(`POST /api/clips/export {clip}`).

### Take delivery (auto-pull, send to phone)

When you press Stop, the clip is pulled from the card automatically in a background
thread (never while recording; the card is never written to), named `<tag>-NNN.MP4`
when a tag is set, and announced on the WebSocket (`{"type":"take","take":{...}}`).
A failed pull is announced as `{"type":"take","error":"..."}` and retried once after 5 s.
Next to each clip is `<clip>.MP4.json`: camera and light settings at Stop, tag, preset,
scene, plus empty `verdict` and `notes` for a later review. The remote page (`/remote`)
shows the newest take with **Save to phone** (opens `GET /clips/<label>`; the phone's
share sheet then saves it to Photos), **Play**, and today's older takes.
`GET /api/takes` lists today's takes with absolute LAN `url`s; `POST /api/takes/send
{label}` returns `{url, qr_svg}` for a one-clip QR. `POST /api/takes/airdrop {label}`
only reveals the file in Finder (`{"method":"finder"}`): macOS has no command-line
AirDrop, so Share > AirDrop is one click from there. `/clips/` serves only video files
inside the clips folder, with range requests.

### Camera how-to (R50 V)

Some settings can't be changed over USB (Manual movie exposure on the Mode dial, a fixed ISO, the
auto power-off, and so on). `GET /api/howto` and `GET /api/howto/<id>` return plain-language
numbered steps for each, paraphrased from Canon's EOS R50 V Advanced User Guide, with a link to the
manual page; `by_readiness` maps the readiness items (`dial`, `camera`, ...) to the entries to show
behind "Show me how". The same text is in `docs/r50v-howto.md`; the source is `server/howto.py`.

### Clip tags and the phone remote

**Tag** (Shoot step 1, Studio's Output section, the remote): a name for the
location or shoot, saved with the context. With a tag set, pulled clips are
saved as `<tag>-001.MP4`, `<tag>-002.MP4` … numbering per tag and continuing
across sessions (the card's own name stays in `clips/index.json`); Save to…
suggests that name. Tags are lower-cased and hyphenated (`Front Room` →
`front-room`).

**Phone remote**: `http://<mac-ip>:8765/remote` is a one-screen page for a
phone on the same Wi-Fi: live view, a big Record/Stop (arms and starts in one
tap), the timer, AI Auto and Stop, Save to… (the panel opens on the Mac), and
the tag. It shares the server with the main UI; nothing else to run.

**Remote QR code**: a small QR tile sits in the bottom-right corner of the
viewfinder in Studio and Shoot (dim until hovered; hidden in focus mode and on
phones). Scan it with the phone and the remote opens; click it for a large code
with the address and a Copy button. The Mac's LAN address is found locally (no
external calls) by `GET /api/remote_url`; `GET /api/remote_qr.svg` draws the code
(pure-Python `segno`).

### Launch: the window first, the hardware after

The Mac app no longer blocks on the camera and the lights before showing
anything. The first timer tick builds state and server only, the page loads
within about a second, and a strip under the header reports the rest:
"Looking for the camera…" (an 8 s first EDSDK look on the main thread, then
the backend's own reconnect path keeps looking: half-press the shutter if
the body is asleep) and "Connecting lights…" (the Bluetooth scans, in a
worker thread). Until the camera answers, calls that need it return 409
"camera not connected yet" and the live view stays quiet. The web version
(`python -m server.main`) still connects everything before it listens;
`build(cfg, deferred=True)` is the app's path (`Controller.start(deferred=True)`,
`connect_camera()`, `connect_lights()`).

### Interior and decor setups for Reels

Output **Reel: interior** (`reel_interior`, portrait): bright and airy rooms,
mid-tones 48-60 %, windows may glow (up to 2 % pure white), verticals within
1°, everything in focus at f/5.6-8, 3800-5200 K. Scenes **Interior, whole
room** (`interior_room`: full-frame metering, even light, straight verticals)
and **Decor detail** (`decor_detail`: centre subject, shallow depth of field,
no hot reflections, true fabric and wood colours). They combine with the
other outputs too (a decor detail on the plain Reel preset gets the shallow
DOF target).

## Guardrails (server-enforced)

- `record start` refuses unless ARM was clicked in the UI within 60 s.
- Setting changes are throttled to one batch per 500 ms and logged for undo.
- `set_light` clamps to the fixture profile (DMX and Bluetooth Mesh alike); non-scriptable lights are recorded only.
- `save_recipe` from Claude is *pending* until confirmed in the UI.
- No code path deletes from the card; every gphoto2 session call goes through one funnel that refuses any operation or argument containing "delete", and the session class exposes no delete method.

## Lights over Bluetooth Mesh (Godox LE300Bi and LP800Bi)

The Godox Light phone app talks to its lights over encrypted Bluetooth SIG
Mesh. `server/lights/godox_mesh.py` speaks the same protocol from the Mac
(ported from the MIT-licensed `ha-godox-ble-mesh` and `godox-tl` projects,
whose captured wire bytes the tests reproduce exactly), so the fill panel can
be scripted like the DMX key light. It never provisions or touches device
keys, so the panel stays paired with the phone.

The one-time setup (pair in the app, export the mesh database, point
`config.toml` at it, allow Bluetooth) is under Install above. When it works,
the light pills in the UI read **BLE** with a green dot and the sliders and
CCT boxes drive the panels.

The key light can go the same way: the LE300Bi is a node in the same app
(its firmware is in the same database, same frame, same range), so pairing
it in the Godox app, re-exporting, and setting `[lights.key]` to
`backend = "godox_mesh"` with `mesh_address = "LE300Bi"` drives both lights
over one Bluetooth connection and no USB-DMX adapter is needed. All mesh
lights that share an export share one connection and one sequence counter.
The LE300Bi ignores Bluetooth while a DMX signal is present, so pick one.

Notes: the first connection probes for a working mesh sequence number (the
lights drop replayed numbers) and persists it next to the export in
`<export>.state.json`; keep that file. Our provisioner address defaults to
0x0500 (`mesh_src`), distinct from the app's 0x0100. Any construction failure
(bleak not installed, export unreadable) logs a warning and the fill falls
back to manual so the server still starts; a radio failure keeps the BLE
backend and retries on the next change.

Diagnostics: `python -m server.lights.godox_mesh scan --export godox_mesh.json`
lists every BLE advert and marks mesh proxies on our network;
`... probe <address>` dumps a device's services and characteristics;
`... set --export godox_mesh.json 50 4500` sends one command without the server.

### Bring-up without watching the light

A Godox light answers on vendor opcode 0x0211F1 when asked: the status
request `FD 01 FF FF FF FF A0 crc` (sent on the normal command opcode) makes
the light's radio chip reply within ~60 ms with its last colour-temperature
record `A0 <bri> <K/100> ...`. Neither of our lights echoes commands
unprompted (the LE300Bi's C1 MCU family never does), so the request is the
readback. `GodoxMeshClient` decrypts those replies, logs them at INFO
(`godox mesh: echo from 0x0202 ...`), keeps the newest per node in
`last_ack`, and offers `query_status(node)`, `wait_ack(node, timeout)` and
`send_frame(..., ack_timeout=)`. The light's state dict (`/api/state`,
`get_state`) carries `last_ack` (timestamp, age, decoded brightness/Kelvin)
so a Claude session can confirm a change landed without anyone looking at
the panel. Verified 2026-10-21 on the real LE300Bi and LP800Bi: both answer
to our address 0x0500 with exactly the brightness and Kelvin just sent
(LE300Bi record `A0 64 41 FF 00 00 00`, LP800Bi `A0 64 41 32 FF FF FF`).

One command does the whole first-contact check and prints a PASS/FAIL table:

```bash
.venv/bin/python -m server.lights.godox_mesh bringup --export godox_mesh.json [--light LP800Bi --light LE300Bi] [--camera] [--ble <gateway>]
```

It lists the nodes in the export, scans and prints which ones advertise our
Network ID with RSSI, connects, probes the sequence floor, then per node
sends power on, 30 %/5600 K, 100 %/2800 K, 100 %/6500 K, 0 % and the
power-off frame, reading the light's status record after each one. Exit code 0 only when every step passed; on any
failure it dumps the full scan and the gateway's GATT services for the
bring-up log. `--camera` additionally captures a frame with the light off
and at 100 % through the tethered camera and requires the luma median
to rise, which catches a light that acks but does not change output.

A shell without a Bluetooth grant gets no prompt: macOS kills Python with
signal 134 instead. Run the bring-up from Terminal.app: double-click
**`scripts/bringup.command`** (or
`open scripts/bringup.command`). It cd's into camlab, finds
`godox_mesh.json`/`GodoxLight.db`, runs `bringup`, tees the output to
`logs/bringup-<timestamp>.log`, and explains what to do if macOS answered
with signal 134. Extra flags pass through:
`scripts/bringup.command godox_mesh.json --light LP800Bi --camera`.

## Camera stays awake and reconnects (EDSDK)

The R50 V auto-powers off after a few idle minutes. While connected, the
EDSDK backend sends `kEdsCameraCommand_ExtendShutDownTimer` every 60 s (and
at once when the body reports `WillSoonShutDown`); log line "extend shutdown
timer (keep-awake)"; `keep_awake = False` on the backend turns it off. If the
session is lost anyway (shutdown event, USB unplugged), the camera pill goes
red, calls fail fast with "camera off or asleep", and the backend reconnects
by itself: every 10 s with a 3 s discovery window, or immediately when the
SDK's camera-added handler fires on plug-in or wake. Starting the server with
the body asleep leaves the EDSDK backend waiting in that state rather than
falling back to the mock camera. Bench 2026-10-25 morning: the camera-added
handler fired on wake, keep-awake sent its first extend exactly 60 s after
connect, record/pull worked through the web version and through the app.
Still to try by hand: leaving the body idle 5+ minutes (should stay on) and
unplugging/replugging USB (should reconnect within ~10 s).

Note on the app: the EDSDK backend registers its reconnect hook only after a
successful open (or when deliberately waiting for an absent body), and a
reconnect attempt is bounded to a 3 s discovery and one open call. Before
those two fixes a failed first open could leave a discarded backend retrying
from the Cocoa main thread, which wedged the app's window (seen once,
2026-10-25 09:11).

## Camera over USB with Canon's EDSDK (primary backend)

`server/camera/edsdk_backend.py` drives the body through Canon's own SDK
(`vendor/EDSDK.framework`, loaded over ctypes; no bindings package). Verified
on the R50 V 2026-10-25, from the backend class and from the UI:

- connect in ~1-12 s (device discovery arrives through the SDK's event pump;
  it has taken anywhere from 0.5 s to 11 s on the bench); the lens name
  reads blank because the glass is a **Sigma 17-50mm f/2.8 EX DC OS HSM**,
  an EF-S lens on an EF-RF adapter (identified from the clips' Canon maker
  note with `exiftool`: LensID, 17-50 mm, f/2.8-22, IS on); mode reads from
  the AE-mode property;
- live view 1024x576 at ~65 ms per frame (`EdsDownloadEvfImage`);
- ISO / shutter / aperture / exposure comp / WB / kelvin / picture style /
  AF mode through Canon's property codes (`edsdk_tables.py`, generated from
  the API reference), snapped to the list the body reports via
  `EdsGetPropertyDesc`; kelvin implies WB "Color Temperature";
- focus drive through `kEdsCameraCommand_DriveLensEvf`;
- record = `kEdsPropID_SaveTo` camera, `kEdsPropID_Record` 4 / 0; the body
  announces the finalized clip with `kEdsObjectEvent_DirItemCreated` about
  1 s after stop and the backend returns its name;
- `list_clips` walks the card through the SDK (all XFVC/REEL_0001 takes
  show, which PTP never listed), `pull_clip` streams with `EdsDownload`
  (13 MB in 0.3 s). The UI's pull fetched 17 clips in one go.

Threading: EDSDK on macOS discovers cameras and delivers events only on the
process **main thread**, so `server/main.py` runs uvicorn (and MCP stdio) in
background threads and keeps the main thread as a job loop
(`server/mainloop.py`) that services SDK calls and pumps `EdsGetEvent`
between them. Standalone scripts just call the backend from the main thread.
`ptpcamerad` is killed before `EdsInitializeSDK` (the SDK enumerates USB at
init and never picks up a device that was claimed at that moment), and
`EdsOpenSession` is retried a few times, since the first open after another
process lets go can fail with an internal error. The driver binds no delete
or format function; the call funnel refuses anything mentioning delete.

Switching: `auto` tries EDSDK first when `vendor/EDSDK.framework` exists,
then gphoto2, then CCAPI if `ccapi_host` is set, then mock. The backend
select next to the camera pill in the UI switches at runtime
(`POST /api/camera/backend`). gphoto2 and EDSDK cannot hold the camera at
the same time; the switch disconnects one before connecting the other.

## Camera over Wi-Fi (Canon CCAPI) and the USB/WiFi toggle

`server/camera/ccapi_backend.py` implements the whole `CameraBackend`
surface against Canon's Camera Control API (HTTP/JSON): live view
(`shooting/liveview/flip`), exposure / WB / kelvin / picture style / AF mode,
focus drive and AF point, movie quality (`moviequality` parsed into
resolution + fps), movie record (movie mode on, shutter button toggles), and
the **contents API** for listing and downloading card files, which is the
reason it exists: the R50 V never exports PTP-recorded clips over PTP (see
Known gaps), and CCAPI enumerates the card on its own. The module uses no
DELETE anywhere and refuses the method if asked.

Setup (hands on the body, once):

1. Wireless settings → Wi-Fi → **connect to a Wi-Fi network** (infrastructure
   mode, your LAN; not the camera-as-access-point mode). Give it a DHCP
   reservation on the router.
2. Activate CCAPI on the body with Canon's free **CCAPI activation tool**
   (developer site registration). Until then port 8080 answers nothing.
3. Probably take the USB tether off: EOS bodies switch Wi-Fi off while an
   interface cable is attached.
4. `.venv/bin/python -m server.camera.ccapi_backend probe <ip>` prints device
   info, ability lists, one live-view frame and the card's video files. Put
   the address in `config.toml` as `ccapi_host`.

Switching: `--camera ccapi` on the command line, `backend = "ccapi"` in
`config.toml`, or at runtime the **USB / WiFi** button next to the camera
pill in the UI (`POST /api/camera/backend {"backend": "ccapi"|"gphoto2"}`).
The switch disconnects the current backend, connects the other, and falls
back to the previous one if that fails. Nothing in the CCAPI backend has been
exercised on hardware yet; its tests run against an in-process fake of the
API.

## Portrait: rotating the live view

Live-view JPEGs are sensor-landscape pixels with no orientation tag, so a
body on an L-bracket shows up sideways. Set **Orient = portrait** (it follows
the preset, e.g. Reel) and the server rotates every frame 90° before
analysis, saving and the UI; the small **turn cw / ccw** control next to it
picks the direction for whichever way the grip went. The gphoto2 and CCAPI
backends both go through this; the mock camera rotates itself.

## Verified on hardware (bench notes)

- **Canon EOS R50 V over USB** (python-gphoto2, one persistent PTP session,
  re-verified 2026-10-24 after the rework): connect in ~1 s, live view at
  1024x576 at ~80 ms per frame (was ~1 s per frame with a CLI process per
  frame), ISO / shutter / aperture / kelvin writes, manual focus drive,
  ARM + REC + stop, pull (30.7 MB clip streamed to disk) and frame
  extraction, all from the UI. The backend flips the body to Manual over PTP
  on connect (Auto reports one choice per setting and refuses writes). Kelvin
  writes also set WB to "Color Temperature", which Canon requires. Lens name
  reads blank on this body.
- **One persistent PTP session**: `gphoto2_backend.py` uses the python-gphoto2
  bindings (`gphoto2` 2.6.4 wheel, libgphoto2 2.5.34 bundled, Python 3.14
  arm64; the `gphoto2` CLI is no longer required) and holds a single
  `gp.Camera()` from connect to disconnect. Frames come from
  `capture_preview()`, settings from the widget tree, clips from
  `folder_list_files`/`file_get` (streamed to disk), events from
  `wait_for_event`. The session stays open across record start and stop. If
  it drops (I/O error), the next call reopens it once.
- **macOS claims the camera** with `ptpcamerad` (user-owned, ignores SIGTERM,
  respawns on hot-plug and re-grabs the body within a fraction of a second of
  any session close). The backend SIGKILLs it and retries whenever a claim
  fails or the body reads as "no camera detected"; you'll see "freed camera
  from macOS PTP daemon(s)" in the log. With a persistent session this
  happens once at connect, not on every call. Quit Photos if it auto-opened.
  The old `killall PTPCamera` advice is for pre-Sequoia systems. Only one
  process can hold the camera: stop any older camlab server before starting
  a new one (the symptom is a claim error that survives the daemon kill).
- **Tether cable**: Tether Tools active cables are directional; boosted end to the Mac.
- **Run the server from a terminal with USB/Bluetooth grants** (the Claude
  desktop terminal panel or Terminal.app). The Claude Code tool shell sees no
  USB devices at all.

## Known gaps / to verify on hardware

- **Record on the R50 V** (bench, 2026-10-24, persistent session): rolls only
  with the body's photo/video switch on **video**; start = `capturetarget=1`,
  `viewfinder=1`, `movierecordtarget=Card` in the open session, stop =
  `movierecordtarget=None` (the "Busy" error while idle just means "not
  recording"). The takes do land on the card: free space drops ~13 MB per 3 s
  (storage info and the shot counter both move) and the body's playback
  shows them. **But the body never exposes them over PTP.** Tried in the open
  session and in fresh ones: `wait_for_event` (only StorageInfoChanged and
  property events, never FILE_ADDED/ObjectAdded), re-listing after the
  camera's own `CTGInfoCheckComplete`, `exit()`+`init()` and listing again,
  toggling `capturetarget` and `eosmoviemode`, turning live view off. The
  XFVC folder keeps listing only the clip recorded on the body itself.
  Listing files is also refused while recording (libgphoto2 error -1), so the
  backend snapshots the card before start. After stop it still tries FILE_ADDED
  (~6 s), a re-list, then a session re-init; the log says "new clip found by
  relist|reinit" if one ever does, else "record stopped but no new clip is
  visible over PTP". A power cycle of the body with USB attached does not
  help either: afterwards the card still lists only the body-recorded clip
  while free space is down ~70 MB across the day's PTP takes. Conclusion: the
  R50 V does not export clips it recorded under PTP remote control through
  the generic PTP object list libgphoto2 uses. **Canon's own EDSDK does see
  them** (bench, 2026-10-24 17:52, EDSDK v13.20.21 for macOS, driven from
  Python over ctypes): it lists every PTP-recorded take under
  `XFVC/REEL_0001/A_0001Cnnn..._CANON.MP4` with full names and sizes, movie
  start/stop works through `kEdsPropID_Record` (4 / 0) after
  `kEdsPropID_SaveTo = camera`, the body announces the finalized file with
  `kEdsObjectEvent_DirItemCreated` ~2 s after stop, and `EdsDownload` copied
  an 18 MB clip in 0.4 s. This
  became the EDSDK backend (see "Camera over USB with Canon's EDSDK"), now
  the primary backend: ARM + REC + stop from the UI returns the clip name,
  pull copies it, frame extraction works on it. gphoto2 stays as the
  fallback for live view and settings; its record path still cannot see its
  own clips. The server, by the way,
  survived the power cycle unattended: the live-view loop hit a USB error,
  the backend reopened the session, and the UI carried on.
- **Clip names**: EOS R stores clips extensionless under `XFVC/` (e.g.
  `A_0001C001A2`, `video/mp4`); `list_clips` keys on MIME type and adds `.mp4`.
- **Exposure mode resets to P when the switch moves to video**; the backend
  flips it back to Manual only on connect. Reconnect (restart) after flipping.
- **Movie size/fps** are not exposed over PTP; set them on the body.
- **Canon EOS 77D**: never connected; gphoto2's `/main/actions/movie` path is kept as a fallback for older bodies.


- **Face detection**: the OpenCV 5 headless wheel on Python 3.14 has no
  `CascadeClassifier`, so faces are found via the YCbCr skin mask only. Warm
  wood benches can trip it; draw an ROI for parts and it's ignored.
- **R50 V / CCAPI**: full backend written against the CCAPI reference, not yet run against the body; the open question is whether its contents API lists PTP-recorded clips.
- `frames/` and `clips/` grow without bound; prune by hand.

## Licence and notices

camlab is released under the [MIT License](LICENSE), Copyright (c) 2026 DCV
Applied Technology.

- The Godox Bluetooth Mesh implementation in `server/lights/godox_mesh.py`
  is ported from [ha-godox-ble-mesh](https://github.com/SegulaLabs/ha-godox-ble-mesh)
  (SegulaLabs) and [godox-tl](https://github.com/TheAlexLichter/godox-tl)
  (Alex Lichter), both MIT, with protocol notes from
  [godox-ul60bi-bt](https://github.com/mattharrison/godox-ul60bi-bt) and
  [ha-godox-mesh](https://github.com/binary-person/ha-godox-mesh) (MIT).
  Their copyright notices apply to that file.
- **Canon EDSDK is not included.** It is Canon's proprietary SDK, licensed to
  registered developers only. Download it from Canon's developer site and
  place `EDSDK.framework` in `vendor/` (gitignored). `server/camera/edsdk_tables.py`
  contains property codes transcribed from Canon's public API reference.
- The DCV Applied Technology and CollTakesChagrin logos and wordmarks in
  `assets/` (and the app icon built from them) are trademarks of their
  owners and are **not** covered by the MIT licence. Replace them if you
  redistribute a modified build.
- Camera how-to text in `docs/r50v-howto.md` paraphrases Canon's EOS R50 V
  Advanced User Guide, which remains Canon's.
