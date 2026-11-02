# camlab as a Mac desktop app — spec

Status: proposal, 2026-10-25. Written against the code as it stands (FastAPI +
FastMCP server, single-file UI, EDSDK primary camera backend, Godox mesh
lights over Bluetooth). Nothing here is built yet.

## 1. Goal

One double-clickable `camlab.app` that:

- starts the server and shows the UI in its own window (no browser, no
  terminal, no `.venv`),
- owns the hardware grants the terminal currently has to lend it (Bluetooth,
  USB camera), so it runs the same from the Dock, from Spotlight, or at login,
- keeps every existing surface: the UI, the REST/WebSocket API on 8765 for the
  phone, the MCP endpoint for Claude,
- installs with a drag to /Applications and passes Gatekeeper.

Non-goals for v1: a native (SwiftUI) rewrite of the UI, Mac App Store
distribution (the USB SDK and `pkill ptpcamerad` rule out sandboxing), Windows.

## 2. Recommended architecture: Python app bundle with a WKWebView shell

Keep the whole Python server as-is and wrap it:

```
camlab.app/Contents/
  MacOS/camlab              launcher (frozen Python, PyInstaller or py2app)
  Frameworks/EDSDK.framework   Canon EDSDK (already in vendor/)
  Resources/
    ui/index.html, assets/   the existing UI
    bin/ffmpeg, bin/ffprobe  static builds
    python runtime + site-packages (fastapi, fastmcp, numpy, cv2, Pillow,
      gphoto2 wheel with libgphoto2 inside, bleak, pyobjc, pywebview, requests)
  Info.plist                 bundle id, usage strings, LSUIElement no
  embedded.provisionprofile  not needed (Developer ID, not App Store)
```

The window is a `pywebview` window (a WKWebView) pointed at
`http://127.0.0.1:8765` served by the in-process server. The UI needs no
changes: it already talks to the server over HTTP/WebSocket. The phone keeps
using the same server over the LAN.

Why not the alternatives:

- **Electron** doubles the bundle (Chromium) and still needs the Python
  server as a helper process. No gain for a one-page UI.
- **Tauri / Swift shell + Python helper**: a nicer native shell, but the
  helper process has to be the one holding the camera, and spawning it from
  the app re-creates the "which process has the grant" problem. Doable
  later; not for v1.
- **Full Swift rewrite**: the analysis (NumPy/OpenCV), MCP server and both
  light protocols would all have to be reimplemented. Months, not days.

## 3. The main-thread rule (the one thing that shapes the design)

Bench-verified 2026-10-25: Canon's EDSDK on macOS discovers cameras and
delivers events only on the process main thread. Today `server/main.py`
runs uvicorn in a background thread and keeps the main thread as a job loop
(`server/mainloop.py`).

In a Cocoa app the main thread belongs to `NSApplication`'s run loop (which
pywebview starts with `webview.start()`). That is exactly the loop EDSDK
wants, so the fit is natural:

- replace `LOOP.run()` with an `NSTimer` (via pyobjc) on the main run loop
  that drains the `MainLoop` job queue and calls `EdsGetEvent()` every
  ~20 ms. `MainLoop.submit()` keeps working unchanged from worker threads;
- `webview.start()` takes over the main thread after the server threads are
  up; the timer fires from inside it.

`MainLoop` gets a second mode (`attach_to_runloop()`) for this; the terminal
mode stays for the CLI.

## 4. Bundling details

- **Freezer**: PyInstaller 6.x (one-dir mode, then wrapped as `.app`). Python
  3.14 support must be confirmed on day one; if it isn't there yet, build the
  app against a bundled 3.13 (nothing in camlab needs 3.14). py2app is the
  fallback.
- **Size**: the venv is 467 MB, 119 MB of it OpenCV. Trim with
  `opencv-python-headless` (already) and PyInstaller excludes; expect a
  ~250 MB app. Acceptable for a bench tool.
- **ffmpeg/ffprobe**: ship static universal builds in `Resources/bin` and
  prepend that directory to `PATH` at startup (`clips.py` finds them by
  name). Homebrew's ffmpeg must not be assumed.
- **libgphoto2**: comes inside the `gphoto2` wheel; keep as fallback backend.
- **EDSDK**: `vendor/EDSDK.framework` goes to `Contents/Frameworks`;
  `EdsDriver` already honours `$EDSDK_FRAMEWORK`, so the launcher sets it to
  the bundle path. Canon's developer licence permits redistribution inside
  your own app; the headers and reference text do not ship.
- **Data**: move all writable state out of the bundle:
  `~/Library/Application Support/camlab/{config.toml,recipes.json,clips/,frames/,GodoxLight.db,logs/}`.
  `config.load()` grows a search order: `$CAMLAB_HOME`, then that folder,
  then the repo (dev). First launch seeds config from the bundled default.
- **Bluetooth**: `bleak` uses CoreBluetooth, which needs
  `NSBluetoothAlwaysUsageDescription` in Info.plist; the app then gets its
  own TCC grant on first scan (the prompt the terminal gave us, now owned by
  the app). This removes the "run from the right terminal" rule for good.
- **USB camera**: no TCC entitlement exists for PTP; EDSDK just needs the
  device free. The app keeps the `pkill -9 ptpcamerad` step (user-owned
  daemon, no privilege needed). A tidier option for later: `EdsSetCameraAddedHandler`
  to react to plug-in instead of polling.
- **Serial/DMX**: `pyserial` works unchanged; no entitlement.

## 5. Signing and notarization

- Apple Developer ID (the $99/yr account), `Developer ID Application`
  certificate.
- Hardened runtime **on**, with these entitlements:
  `com.apple.security.cs.disable-library-validation` (Canon's framework is
  signed by Canon's team, not ours), `com.apple.security.cs.allow-unsigned-executable-memory`
  only if the frozen Python needs it (test; PyInstaller usually doesn't),
  `com.apple.security.device.bluetooth` is not required outside the sandbox.
- Sign inside-out: every `.so`/`.dylib` in the bundle, the ffmpeg binaries,
  the EDSDK framework (re-sign with our identity, which is allowed with
  library validation disabled), then the app. `codesign --deep` is not
  enough for PyInstaller output; use a script that walks the tree.
- `notarytool submit` + `stapler staple`. Ship as a DMG (`create-dmg`).
- Without signing (internal use only): `xattr -dr com.apple.quarantine`
  after copying works, but every Mac it lands on needs that step. Fine for
  the bench, not for sharing.

## 6. App behaviour

- **Launch**: start the server threads (uvicorn on 0.0.0.0:8765 by default
  now; make the LAN exposure a setting with a menu toggle), open the window,
  connect the camera in the background with a status line ("looking for
  camera…") instead of blocking the window on the 1–12 s EDSDK discovery.
- **Menu bar**: File (Pull clips, Open clips folder, Open frames folder),
  Camera (backend select mirroring the UI, Reconnect, Keep awake), Server
  (LAN on/off, copy LAN URL, copy MCP URL), Help (README, logs).
- **Keep awake**: EDSDK has `kEdsCameraCommand_ExtendShutDownTimer`; send it
  every minute while connected so the R50 V stops auto-powering off
  mid-session (the sleep that stalled last night's bench).
- **Camera hot-plug**: register `EdsSetCameraAddedHandler`; on plug-in,
  reconnect without a restart. On disconnect (state event
  `kEdsStateEvent_Shutdown`), show "camera off" in the pill and fall back to
  no camera rather than mock.
- **Quit**: stop recording if rolling, close the EDSDK session (it already
  does), stop lights' BLE connection, then exit. Cmd-Q and window close both.
- **Claude Desktop**: the app can write the MCP entry into
  `~/Library/Application Support/Claude/claude_desktop_config.json` on
  request (menu item), pointing at `http://127.0.0.1:8765/mcp`; the `--stdio`
  mode stays available via `camlab.app/Contents/MacOS/camlab --stdio` for
  those who prefer it.
- **Logs**: rotate under Application Support/camlab/logs; the existing
  logging config already writes to stderr, so add a file handler.
- **Updates**: v1 manual (download new DMG). Sparkle is the standard answer
  later and works with PyInstaller apps via pyobjc.

## 7. Work plan and estimate

| Phase | What | Effort |
|---|---|---|
| 1. Runs as an app | `MainLoop.attach_to_runloop`, pywebview window, config/data relocation, ffmpeg/EDSDK path wiring, PyInstaller spec, unsigned `.app` that passes the full bench (EDSDK camera, both lights, pull, extract) | 2–3 days |
| 2. Ships | Developer ID signing script, hardened runtime entitlements, notarization, DMG, Bluetooth usage string, first-launch setup | 1–2 days |
| 3. Feels like an app | menu bar, keep-awake, hot-plug reconnect, LAN toggle, log files, Claude Desktop registration, icon | 2–4 days |

Phase 1 is the gate: if PyInstaller/py2app cannot freeze this stack on
Python 3.14 cleanly, the fallback is the same bundle on 3.13, which costs
half a day. Everything in the current codebase stays; the only source
changes are in `main.py`, `mainloop.py` and `config.py` plus a new
`app/` folder for the launcher, Info.plist, entitlements and build scripts.

## 8. Risks and open questions

- **Freezer vs Python 3.14**: unknown until tried (day 1 of phase 1).
- **Canon framework under hardened runtime**: needs library validation off;
  expected to work, confirm on the first signed build.
- **WKWebView and the live-view loop**: the UI polls frames at 3 fps over
  WebSocket; fine in WKWebView, but confirm the window keeps rendering when
  occluded (macOS throttles background WebViews; the server keeps running
  regardless).
- **Two processes, one camera**: if someone also runs the CLI server, the
  second one gets no camera. The app should show the error plainly rather
  than silently falling to mock (today's `auto` behaviour).
- **R50 V auto power-off**: keep-awake (phase 3) is the real fix; until
  then the app must reconnect cleanly after the body sleeps, which the
  reopen path already does once the body is awake.
- **Bundle size** (~250 MB) and OpenCV 5 on 3.14 lacking `CascadeClassifier`
  (already a known gap; unchanged by packaging).

## Keeping it alive (added 2026-10-28)

Every Python / camlab crash report on the bench Mac was the same thing: macOS kills any process
that touches Bluetooth without an `NSBluetoothAlwaysUsageDescription` in its bundle. The signed
`dist/camlab.app` has one; a bare `.venv/bin/python -m server.main` never will. So:

- Run camlab only from the bundle. The server now checks this before touching Bluetooth and, from
  bare Python, logs a warning and falls back to manual lights instead of dying
  (`CAMLAB_BLUETOOTH_OK=1` skips the check; `scripts/bringup.command` sets it, Terminal.app shows the prompt).
- `scripts/launchagent.sh install` writes `~/Library/LaunchAgents/com.dcv.camlab.plist`: camlab starts at
  login and relaunches within about 5 s after a crash. Cmd-Q or closing the window stays quit
  (`KeepAlive.SuccessfulExit = false`). `remove` undoes it, `status` shows pid and last exit code.
- `app/build.sh` unloads the agent for the build and reloads it afterwards.
