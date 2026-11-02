#!/bin/zsh
# Double-click (or `open scripts/bringup.command`) to run the Godox mesh
# bring-up in Terminal.app, where macOS shows the Bluetooth permission prompt.
# Python started from a sandboxed shell gets no prompt and is killed with
# signal 134 instead. Usage:
#   bringup.command [export-file] [extra bringup flags, e.g. --light LP800Bi --camera]
# Without an export argument it uses the first of godox_mesh.json,
# GodoxLight.db, or any GodoxLight.db one folder down (the dragged-out app folder).
# Output also goes to logs/bringup-<timestamp>.log so a Claude session can
# read the PASS/FAIL table without anyone copying it.
set -u
cd "$(dirname "$0")/.." || exit 1
EXPORT="${1:-}"
if [[ -n "$EXPORT" && "$EXPORT" == -* ]]; then EXPORT=""; else shift $(( $# > 0 ? 1 : 0 )); fi
if [[ -z "$EXPORT" ]]; then
  for cand in godox_mesh.json GodoxLight.db */GodoxLight.db; do
    if [[ -f "$cand" ]]; then EXPORT="$cand"; break; fi
  done
fi
if [[ -z "$EXPORT" || ! -f "$EXPORT" ]]; then
  echo "No mesh export found. Put godox_mesh.json or GodoxLight.db in $(pwd) or pass its path."
  echo "Press any key to close."; read -k1; exit 2
fi
mkdir -p logs
LOG="logs/bringup-$(date +%Y%m%d-%H%M%S).log"
echo "camlab bring-up  export=$EXPORT  log=$LOG"
echo "If macOS asks to allow Bluetooth for Terminal, click Allow."
CAMLAB_BLUETOOTH_OK=1 .venv/bin/python -m server.lights.godox_mesh bringup --export "$EXPORT" "$@" 2>&1 | tee "$LOG"
RC=${pipestatus[1]}
echo "exit code $RC  (0 = all steps passed)   log: $LOG" | tee -a "$LOG"
if (( RC == 134 )); then
  echo "Signal 134 = macOS refused Bluetooth. System Settings > Privacy & Security > Bluetooth: enable Terminal, then run again." | tee -a "$LOG"
fi
echo "Press any key to close."; read -k1
exit $RC
