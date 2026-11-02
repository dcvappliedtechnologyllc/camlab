#!/bin/zsh
# Build the camlab Mac app: dist/camlab.app (self-signed with the "camlab dev" identity when present)
#   ./app/build.sh            full build (removes build/ and dist/camlab* first)
#   ./app/build.sh --keep     keep PyInstaller's build/ cache
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
PY="$REPO/.venv/bin/python"
AGENT=com.dcv.camlab; AGENT_LOADED=0
if launchctl print "gui/$(id -u)/$AGENT" >/dev/null 2>&1; then
  echo "stopping the $AGENT LaunchAgent for the build (restarted at the end)"
  launchctl bootout "gui/$(id -u)/$AGENT" 2>/dev/null || true; AGENT_LOADED=1; sleep 2
fi
if pgrep -f "$REPO/dist/camlab.app/Contents/MacOS/camlab" >/dev/null; then
  echo "camlab.app is running; quit it first (the build replaces dist/camlab.app)"; exit 1
fi
[[ -x "$PY" ]] || { echo "no .venv at $REPO/.venv (python3.14 -m venv .venv && .venv/bin/pip install -r requirements)"; exit 1; }

# 1. build tools into the venv (idempotent)
need=()
"$PY" -c "import PyInstaller" 2>/dev/null || need+=(pyinstaller)
"$PY" -c "import webview"     2>/dev/null || need+=(pywebview)
if (( ${#need[@]} )); then
  echo "installing: ${need[*]}"
  "$REPO/.venv/bin/pip" install -q "${need[@]}"
fi

# 2. freeze
if [[ "${1:-}" != "--keep" ]]; then
  rm -rf "$REPO/build/camlab" "$REPO/dist/camlab" "$REPO/dist/camlab.app"
fi
"$PY" -m PyInstaller --noconfirm --clean --distpath "$REPO/dist" --workpath "$REPO/build" "$REPO/app/camlab.spec"

APP="$REPO/dist/camlab.app"
[[ -d "$APP" ]] || { echo "build failed: $APP missing"; exit 1; }

# 3. restore Canon's EDSDK.framework byte-for-byte (keeps Canon's code signature; PyInstaller's ad-hoc
#    deep signing may have re-signed the copy inside the bundle). Symlinks and bundles are preserved by ditto.
FW="$APP/Contents/Frameworks/EDSDK.framework"
rm -rf "$FW"
ditto "$REPO/vendor/EDSDK.framework" "$FW"
find "$FW" -name .DS_Store -delete
echo "EDSDK.framework signature:"; codesign -dv "$FW" 2>&1 | grep -E "Identifier|TeamIdentifier|Authority" | head -3 || true
# (Contents/Resources/EDSDK.framework is PyInstaller's symlink to this copy.)

# 3b. sign the outer bundle, NOT deep: PyInstaller's own `codesign --deep` fails on Canon's framework
#     ("bundle format is ambiguous") and would re-sign it anyway. The nested code keeps its signatures
#     (Canon's for EDSDK, PyInstaller's ad-hoc for the Python .so/.dylib files).
#     Identity: the self-signed "camlab dev" certificate from app/make-signing-identity.sh. macOS privacy
#     (TCC) keys the Bluetooth grant on the designated requirement; with a certificate it is
#     "identifier com.dcv.camlab and certificate leaf = <cert>" and survives rebuilds, whereas an ad-hoc
#     signature's requirement is the per-build code hash, so every rebuild had to be allowed again.
IDENTITY="${CAMLAB_SIGN_IDENTITY:-camlab dev}"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$IDENTITY\""; then
  codesign --force --timestamp=none -s "$IDENTITY" "$APP" 2>&1 | grep -v "replacing existing signature" || true
  echo "signed with: $IDENTITY"
else
  echo "WARNING: no code-signing identity \"$IDENTITY\" in the login keychain -> ad-hoc signature."
  echo "         Bluetooth must be allowed again after every build. One-time fix: ./app/make-signing-identity.sh"
  codesign --force -s - "$APP" 2>&1 | grep -v "replacing existing signature" || true
fi
codesign -vv "$APP" 2>&1 | tail -1
codesign -dr - "$APP" 2>&1 | grep "^designated" | sed 's/^/requirement: /'

# 4. the one-dir folder beside the .app is a by-product; the .app is the deliverable
rm -rf "$REPO/dist/camlab"

# 5. no quarantine flag on our own build (not notarized, bench use)
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo
echo "built: $APP"
du -sh "$APP" | awk '{print "size:  " $1}'
echo "run:   open \"$APP\"        (or: CAMLAB_CAMERA=mock \"$APP/Contents/MacOS/camlab\")"
echo "data:  ~/Library/Application Support/camlab/"
if (( AGENT_LOADED )); then
  echo "restarting the $AGENT LaunchAgent"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$AGENT.plist" && echo "camlab is back up (kept alive by launchd)"
fi
