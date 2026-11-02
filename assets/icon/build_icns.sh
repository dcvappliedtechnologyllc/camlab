#!/bin/zsh
# Rebuild assets/icon/camlab.icns from a 1024x1024 SVG (needs rsvg-convert from Homebrew librsvg):
#   ./assets/icon/build_icns.sh path/to/icon.svg
# The shipped camlab.icns is checked in; this only matters if you redraw the icon.
set -euo pipefail
SVG="${1:?svg path}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SET="$HERE/camlab.iconset"
rm -rf "$SET"; mkdir -p "$SET"
render() { rsvg-convert -w "$1" -h "$1" -o "$2" "$SVG"; }
for s in 16 32 128 256 512; do
  render $s "$SET/icon_${s}x${s}.png"
  render $((s*2)) "$SET/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$SET" -o "$HERE/camlab.icns"
rm -rf "$SET"
echo "built $HERE/camlab.icns from $SVG"
