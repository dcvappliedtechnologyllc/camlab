# camlab Mac app (phase 1: runs as an app, unsigned)

One double-clickable `camlab.app` built from this tree. It runs the very same
server as `python -m server.main` inside a WKWebView window (pywebview); the
web version is untouched and both can be built from the same checkout.

No notarization: this is a home-made bundle for the bench. The outer bundle is
signed with a self-signed "camlab dev" certificate (one-time setup:
`./app/make-signing-identity.sh`; `build.sh` falls back to an ad-hoc signature
with a warning when it is missing). If macOS asks "codesign wants to use key
camlab dev" during a build, answer **Always Allow** once; "Allow" asks again on
every build. PyInstaller ad-hoc signs the Python parts;
Canon's `EDSDK.framework` keeps Canon's own signature.

## Build

```sh
cd camlab
./app/build.sh            # installs pyinstaller + pywebview into .venv if missing,
                          # freezes with app/camlab.spec, prints the size,
                          # strips the quarantine flag from dist/camlab.app
open dist/camlab.app
```

Requirements: the project `.venv` (Python 3.14, Homebrew python@3.14), Xcode
command line tools (for `codesign`/`ditto`), `vendor/EDSDK.framework` present.
ffmpeg/ffprobe are **not** bundled: the launcher prepends
`/opt/homebrew/bin:/usr/local/bin` to `PATH`, so Homebrew's ffmpeg is used for
clip frames and durations.

Files:

| file | role |
|---|---|
| `app/launcher.py` | entry point; also runnable from the venv: `CAMLAB_CAMERA=mock .venv/bin/python app/launcher.py` |
| `app/camlab.spec` | PyInstaller spec (one-dir -> `BUNDLE` -> `dist/camlab.app`) |
| `app/build.sh` | build driver (see above) |
| `app/make-signing-identity.sh` | one-time: creates the self-signed "camlab dev" code-signing identity in the login keychain |

Build output: `dist/camlab.app` (and PyInstaller's cache in `build/`). Both are
disposable.

## How the app runs

`server/main.py` already splits startup into `build(cfg)` (controller +
hardware + ASGI app + uvicorn server) and `start_threads()` (uvicorn in a
background thread). The CLI then runs `LOOP.run()` on the main thread; the app
instead lets Cocoa own the main thread:

1. `launcher.py` creates the pywebview window first (a small "starting" page),
   so it appears at once.
2. An `NSTimer` (pyobjc, main run loop, common modes) fires every 20 ms.
   The first tick calls `build(cfg)` on the **main thread** (Canon's EDSDK
   only discovers cameras and delivers events there) and `start_threads()`.
   Every later tick calls `LOOP.tick()`: drains main-thread jobs submitted by
   worker threads and runs the EDSDK event pump.
3. A helper thread waits for the port to answer, then the window loads
   `http://127.0.0.1:<port>/`.
4. Window close or Cmd-Q: `server.should_exit = True`, `ctl.stop()`,
   `LOOP.end_external()`, exit.

While the camera is being discovered (EDSDK: 1-12 s) the main thread is busy,
so the splash does not animate; the window itself is already up. With the
camera asleep the EDSDK backend stays disconnected and reconnects on its own
when the body wakes (expected).

The UI, REST/WebSocket API and the MCP endpoint are exactly the web version's:
`http://<mac>:8765/` for the phone, `http://127.0.0.1:8765/mcp` for Claude.

## Data location

| mode | config + writable data |
|---|---|
| web/CLI (`python -m server.main`) | the repo folder, as before |
| app (`sys.frozen`) | `~/Library/Application Support/camlab/` |
| either, with `CAMLAB_HOME=/some/dir` | that folder |

First app launch seeds `config.toml`, `recipes.json`, `GodoxLight.db`,
`GodoxLight.db.state.json` from the bundle into that folder (never overwrites)
and creates `clips/`, `frames/`, `logs/`. Logs: `logs/camlab.log` (rotating,
5 MB x 3). Edit `~/Library/Application Support/camlab/config.toml` to change
the camera backend, lights, LAN exposure (`[server] host`), etc. The bundle's
own copies in `camlab.app/Contents/Resources/` are read-only defaults.

## Environment overrides

| variable | effect |
|---|---|
| `CAMLAB_CAMERA` | `auto`, `edsdk`, `gphoto2`, `ccapi`, `mock`: overrides `[camera] backend` |
| `CAMLAB_PORT` | TCP port (default from config, 8765) |
| `CAMLAB_HOME` | config/data folder |
| `EDSDK_FRAMEWORK` | path to `EDSDK.framework/EDSDK`; the app sets it to its bundled copy |

From Finder/Dock the app sees no shell environment; use the config file there.
From a shell: `CAMLAB_CAMERA=mock dist/camlab.app/Contents/MacOS/camlab`.

## Hardware grants

- **Bluetooth** (Godox mesh lights): `Info.plist` carries
  `NSBluetoothAlwaysUsageDescription`; macOS asks once. The grant is keyed on
  the app's designated requirement, which with the "camlab dev" signature is
  `identifier "com.dcv.camlab" and certificate leaf = <cert>` and so survives
  rebuilds; an ad-hoc build's requirement is its per-build code hash, so every
  rebuild asks again (and the Godox scan hangs until allowed). Check with
  `codesign -dr - dist/camlab.app`. A shell/tool process without a Bluetooth grant that touches
  CoreBluetooth is killed by macOS, so for shell smoke tests set both lights to
  `backend = "manual"` in a scratch `CAMLAB_HOME`.
- **USB camera**: no TCC grant needed; the EDSDK backend frees the device from
  `ptpcamerad` as in the CLI.

## Known limits (phase 1)

- Self-signed only: on another Mac, `xattr -dr com.apple.quarantine camlab.app` after
  copying. Not for distribution.
- No menu bar items of our own beyond pywebview's defaults, no app icon, no
  keep-awake, no hot-plug reconnect UI (phase 3 in `docs/MAC_APP.md`).
- ffmpeg/ffprobe come from Homebrew, not the bundle.
- The one-dir bundle is large (OpenCV, NumPy, pyobjc, libgphoto2); size is
  printed by `build.sh`.
- Only one process can hold the camera: do not run the CLI server and the app
  at once (or give one of them a different port and expect it to fall back).
- `python -m server.main` and the app share the repo tree but not the data
  folder (the app's lives in Application Support).
