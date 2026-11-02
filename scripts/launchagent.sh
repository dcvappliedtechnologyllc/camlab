#!/bin/zsh
# Keep camlab alive: a per-user LaunchAgent that starts dist/camlab.app at login and
# relaunches it within seconds if it crashes. A clean quit (Cmd-Q / window close) stays quit.
#   scripts/launchagent.sh install     write ~/Library/LaunchAgents/com.dcv.camlab.plist and start it
#   scripts/launchagent.sh remove      stop it and delete the plist
#   scripts/launchagent.sh status
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.dcv.camlab
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
EXE="$REPO/dist/camlab.app/Contents/MacOS/camlab"
TARGET="gui/$(id -u)"
case "${1:-status}" in
  install)
    [[ -x "$EXE" ]] || { echo "no app at $EXE (run ./app/build.sh)"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Application Support/camlab/logs"
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$EXE</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Application Support/camlab/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Application Support/camlab/logs/launchd.err.log</string>
</dict></plist>
PL
    launchctl bootout "$TARGET/$LABEL" 2>/dev/null || true
    # a copy started by hand (open dist/camlab.app) would hold port 8765: stop it first
    pkill -f "$EXE" 2>/dev/null && sleep 2 || true
    launchctl bootstrap "$TARGET" "$PLIST"
    echo "installed $PLIST and started camlab (relaunches on crash; Cmd-Q stays quit)"
    ;;
  remove)
    launchctl bootout "$TARGET/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed; camlab is no longer kept alive"
    ;;
  status)
    if launchctl print "$TARGET/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit"; then :; else echo "not installed"; fi
    ;;
  *) echo "usage: $0 install|remove|status"; exit 2 ;;
esac
