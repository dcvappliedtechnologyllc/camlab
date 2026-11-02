#!/bin/zsh
# camlab morning test (2026-10-25): wake the R50 V first (half-press the shutter or power-cycle it),
# then double-click this file. It runs the two builds back to back and prints a checklist.
cd "$(dirname "$0")/.." || exit 1
echo "camlab morning test  --  $(date)"
echo
echo "== 0. hardware"
pkill -9 ptpcamerad 2>/dev/null
sleep 1
if system_profiler SPUSBDataType 2>/dev/null | grep -qi canon; then echo "   camera on USB: yes"; else echo "   camera on USB: NO -> wake it (half-press shutter / power cycle), then rerun"; fi
echo
echo "== 1. tests (no hardware)"
.venv/bin/python -m pytest tests -q --deselect tests/test_godox_mesh.py 2>&1 | tail -1
echo
echo "== 2. the Mac app (the only supported way to run camlab: bare python has no Bluetooth"
echo "      usage description and macOS kills it on first Bluetooth use)"
if [ ! -d dist/camlab.app ]; then
  echo "   dist/camlab.app is missing: run ./app/build.sh first."; exit 1
fi
if launchctl print "gui/$(id -u)/com.dcv.camlab" >/dev/null 2>&1; then
  echo "   LaunchAgent is installed; kickstarting it (relaunches if it was down)"
  launchctl kickstart "gui/$(id -u)/com.dcv.camlab"
else
  open dist/camlab.app
fi
echo "   UI: http://$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1):8765  (also from the phone)"
echo "   CHECK: camera pill says R50 V / USB-EDSDK (not mock); live view moves; ISO change sticks;"
echo "          ARM -> REC -> stop shows a new 'last clip'; pull copies it; frames extract."
echo "   CHECK (keep-awake): log line 'extend shutdown timer' about once a minute; leave the body"
echo "          idle 5+ min, it must NOT power off; if it does, wake it and the pill must recover."
echo "   tailing the app log; Ctrl-C when done."
tail -n 5 -f "$HOME/Library/Application Support/camlab/logs/camlab.log" | grep --line-buffered -v DEBUG
echo
