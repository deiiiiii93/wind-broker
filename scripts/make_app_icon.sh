#!/usr/bin/env bash
# Regenerate the app icon sizes from macos/Assets/AppIcon-1024.png:
#   - Assets.xcassets/AppIcon.appiconset/icon_*.png (used when actool is available)
#   - AppIcon.icns (fallback used by build_app.sh without actool)
# AppIcon-1024.png should already sit on Apple's icon grid: an ~824 px rounded
# square centered on a transparent 1024 px canvas. Uses only sips + iconutil.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSETS="$ROOT/macos/Assets"
SRC="$ASSETS/AppIcon-1024.png"
SET="$ASSETS/Assets.xcassets/AppIcon.appiconset"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"

for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done

cp "$ICONSET"/icon_*.png "$SET/"
iconutil -c icns "$ICONSET" -o "$ASSETS/AppIcon.icns"
echo "Updated $SET and $ASSETS/AppIcon.icns"
