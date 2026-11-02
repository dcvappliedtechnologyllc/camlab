# camlab UI rework: portrait-first shell, guided HUD, AI Auto — design

Date: 2026-10-25. Agreed with the owner in the brainstorming session that started this
work (answers recorded below). Status: approved for planning and build.

## 1. What the owner told us (the brief)

- **Shoot flow:** The owner runs the whole portrait Reel shoot **from the Mac**. Colleen
  is in frame. The phone is a fallback, not the primary surface.
- **While rolling** he wants exposure (clipping and luma), focus confirmation and the
  Reels safe-area guides, but he is **not a photographer**: readouts must be plain
  language and guided, never bare numbers.
- **Deterministic Auto is dropped.** The only automation is **AI Auto**.
- **AI Auto cost model (explained and accepted):** runs *before* REC, never while a
  take rolls. One run = up to 8 look/adjust rounds, about 1-2 minutes, roughly
  $0.30-0.60 on Opus 5.5. The cap is **per run**, default **$1.00**, in
  `config.toml`. A 10 s take costs nothing; a 30 min shoot with three runs is ~$1.50.
- Ask once, then leave him alone. The only remaining question is the icon pick.

## 2. Scope

One tree, two builds (web `python -m server.main`, Mac app `dist/camlab.app`) share
`ui/index.html`, `server/`, `assets/`. Everything below lands in both.

1. Portrait-first layout with drawers (landscape keeps a pinned-column layout).
2. Guided HUD over the live frame, tooltips on every control, plain Targets
   definitions, `?` key-map overlay, and the stale orientation banner fixed.
3. AI Auto: server-side agent on the Claude API with full camera and light control.
4. App icon / lockup mashup: 2-3 candidates, `.icns` wired into `app/camlab.spec`.

Not in scope: deterministic Auto, a native rewrite, signing/notarization, CCAPI.

## 3. Layout

### 3.1 Shell

```
header: lockup · pills (camera/key/fill) · [?] [⚙]        (thin; hidden in focus mode)
stage:  rail-L | viewfinder (centered, full stage height) | rail-R
        drawer-L (Output, Scene, Lens, Targets)   drawer-R (Camera, Lights, AI, Recipes)
        drawer-B (Claude timeline + chat), under the stage
overlays on the viewfinder: HUD chips (top-left), guides canvas, ARM/REC bar (bottom)
```

- **Mode** is decided by the frame: `body.portrait` when the live image is taller than
  wide (or, before the first frame, when `context.orientation == "portrait"`);
  otherwise `body.landscape`.
- **Portrait mode:** the viewfinder is centered at full stage height (9:16 column).
  Drawers slide in from the left/right over the stage edges; the bottom drawer slides
  up. Each drawer has a rail tab (icon + label) on its edge; open/closed state and
  each section's collapsed state persist in `localStorage` (`camlab.ui.v1`).
- **Landscape mode:** the same drawers are **pinned open** as columns (left column,
  viewfinder, right column) with the Claude timeline under the viewfinder: the
  current information density, nothing hidden. Rail tabs still collapse a column.
- **Focus mode (`F`):** header collapses to a 28 px strip (pills only), drawers and
  rails hide, HUD stays. `F` or `Esc` leaves it.
- **Phone (`max-width: 820px`):** viewfinder full width at the top, drawers become
  bottom sheets opened from a bottom tab bar; the ARM/REC bar stays on the frame.
- **ARM/REC bar** sits on the bottom edge of the viewfinder over a scrim: ARM
  (countdown), REC/STOP, timer, last clip, pull. Recipes move to the right drawer.
- **Window size (app):** the launcher opens 1100×1000 when the last context was
  portrait, else 1280×860. Context (preset/scene/lens/focal/orientation/rotation) is
  persisted to `<home>/context.json` on every change and restored at start, so Reel
  stays selected between launches.

### 3.2 Visual language

Unchanged brand rules: dark, flat, hairline borders, gold (`--brand-gold`) the only
accent, Inter for UI, Bodoni Moda small caps for the Coll Takes Chagrin wordmark.
Drawers are surfaces one step lighter than the stage with a hairline edge; no
shadows beyond a 1 px edge. Chips use the existing `.chip` style.

## 4. Guided HUD, tooltips, key map, banner

### 4.1 HUD chips (plain language)

Chips render top-left on the viewfinder, each one sentence, colour = state
(`good` green / warn gold / `clip` red), each with a tooltip that explains the term
and what to do. Clicking a chip opens the Targets section.

| Signal | Source | Text (examples) |
|---|---|---|
| Exposure | live histogram (median → luma, end bins → clip %) vs `context.target.luma_median` | "Exposure good" / "Too dark: raise key or ISO" / "Too bright" / "Highlights clipping" / "Shadows crushed" |
| Face / skin | last analysis (`skin_luma`, `face_clip`) | "Face too bright" / "Face good" / "No face found" |
| Focus | last analysis `focus.laplacian_var` vs session best | "Sharp" / "Soft: nudge ◄ ►" |
| Colour | `wb_estimate_k` vs `target.cct_k` | "Too warm for Reel" / "Colour good" |
| Age | timestamp of the analysis used | "from capture 12 s ago" |

The live loop runs a **light analysis every 3 s** (no save, no history, no delta;
`server.liveview_hud_s`) and includes it in the `live` message as `hud`, so chips stay
fresh without the user pressing Capture. Exposure chips also update on every live
histogram.

### 4.2 Tooltips and definitions

Every control gets a `title`/`data-tip`. Targets rows get a plain definition in a
`TDEF` map ("luma median: how bright the middle of the picture is, 0-1; Reel wants
skin around half"). The Targets table shows the definition on hover and the preset's
"look" sentence under it, as now.

### 4.3 Key map overlay

`?` opens a dialog listing every key: space capture · o AI Auto · s step · x stop ·
r arm · R rec · [ ] focus · - = key light · u undo · d delta · g guides · F focus
mode · / chat · ? this. The old "Keys" section is removed.

### 4.4 Orientation banner fix

Today the banner compares the preset's orientation to a stale
`camera.settings.orientation`. New rule, UI side: show the banner only when the
preset wants portrait **and** `context.orientation` is landscape ("Reel is portrait:
turn the body on the bracket, then [set Orient to portrait]" with a one-click
button). Server side: `set_context` also broadcasts the camera state so
`settings.orientation` is current. Once Orient is portrait the server rotates every
frame and the banner is gone.

## 5. AI Auto

### 5.1 Behaviour

- Button **AI Auto** (key `o`) in the AI section and in the Claude timeline header.
  **Stop** (key `x`) ends a run at the next tool boundary. **Step once** does one
  look/adjust round.
- A run: capture → send frame + analysis + targets + current camera/light state →
  the model calls tools (`look`, `set_camera`, `set_light`, `focus`, `done`) → the
  server applies them through the existing `Controller` methods → repeat until
  `done`, the iteration cap (`claude.iteration_cap`, 8 looks), the cost cap, Stop,
  or REC starting.
- Every assistant text block becomes a Claude message in the timeline (reasoning,
  one or two lines per step). Tool calls are history entries with `source: "ai"`.
- Status line: `AI Auto · round 3/8 · $0.21`. Final line: converged / stopped / cap
  reached / error, with the run's cost.
- **Fallback:** with no API key configured, AI Auto posts the existing MCP
  `optimize` request and logs "no API key in config.toml: handing the request to the
  MCP-connected Claude session".

### 5.2 Guardrails (all kept, none bypassed)

- Runs only when **not recording**; a REC start cancels the run.
- No `record`, `pull`, `delete`, `save_recipe` tools exposed to the model.
- Camera changes go through `Controller.set_camera` (rate limit 500 ms, snapped to
  the body's lists); light changes through `Controller.set_light` (profile clamps).
- Per-run caps: looks ≤ `iteration_cap`, tool calls ≤ 6 × cap, cost ≤
  `ai.cost_cap_usd`, wall clock ≤ `ai.max_run_s` (default 240 s).
- Cost is accumulated from `response.usage` with a price table keyed by model
  (Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 per MTok, cache reads
  at 5 %); unknown models use Opus prices.
- `stop_reason == "refusal"` or any API error ends the run with the error in the log.

### 5.3 Implementation

- New `server/autopilot.py`: `Autopilot(ctl, cfg, client_factory=None)` with
  `start(mode="optimize"|"step")`, `stop()`, `status()`. Runs in its own thread; the
  model loop is a **manual tool-use loop** on `client.messages.create` (needed to
  return image blocks in tool results and to meter usage per response). Model
  `claude-opus-5-5`, adaptive thinking (default on), `output_config.effort` from
  config (default `medium`), `max_tokens` 4096, `fallbacks: "default"` with the
  server-side-fallback beta when the SDK accepts it.
- Frames sent to the model are downscaled to 768 px long edge JPEG (≈0.4 K tokens).
- Config `[ai]`: `api_key` (or `ANTHROPIC_API_KEY`), `model`, `cost_cap_usd = 1.0`,
  `effort = "medium"`, `max_run_s = 240`.
- API: `POST /api/claude {request: optimize|step|stop}` routes to the autopilot when
  configured, else to the MCP request path. `state.claude` gains `mode`, `cost_usd`,
  `run_id`.
- Dependency: `anthropic` added to the venv and to `app/camlab.spec`
  (`collect_all("anthropic")`).
- Tests: a fake client with scripted responses covers a converging run, Stop,
  cost cap, the recording guard, the no-key fallback, and that no forbidden tool is
  offered.

## 6. Icon and lockup

- `assets/icon/`: three SVG candidates. The DCV mark is reproduced from
  `assets/dcv-logo-white.svg` (brand-authentic, untouched geometry); Coll Takes
  Chagrin is set in Bodoni Moda small caps in an editorial treatment. Candidates:
  A "stacked" (DCV mark above CTC rule), B "monogram" (DCV mark with a gold hairline
  frame and a CTC initial), C "split" (two-tone field, mark left, CTC right).
- Rendered to PNG at 1024 px (`qlmanage`/`rsvg-convert`, whichever is present),
  `iconutil` builds `assets/icon/camlab.icns`; `app/camlab.spec` `BUNDLE(icon=...)`.
- The in-app lockup is updated to match the chosen candidate. the owner picks the
  candidate from rendered PNGs; the build ships the recommended one until then.

## 7. Testing and acceptance

- `pytest tests -q` green throughout (89 now; new tests for autopilot, context
  persistence, HUD payload, set_context broadcast).
- UI checked in the browser pane on the mock camera: portrait and landscape modes,
  drawers persist, focus mode, phone width, HUD chips, `?` overlay, banner logic.
- Bench on the R50 V in both builds from the desktop terminal panel: live view in
  portrait, ARM/REC/stop with clip name, AI Auto run with cost line, Stop works,
  lights respond (Godox mesh, if on).
- App rebuilt with `./app/build.sh` at the end and launched once.

## 8. Order of work

1. Layout and drawers (+ context persistence, window size).
2. HUD, tooltips, key map, banner fix.
3. AI Auto (server, API, UI, tests).
4. Icon candidates + `.icns` + spec wiring; in-app lockup.
5. Tests, rebuild, bench; then the one question to the owner: icon pick.
