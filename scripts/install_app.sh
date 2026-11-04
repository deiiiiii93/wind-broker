#!/usr/bin/env bash
# Install WindBroker.app into /Applications (copy by default, --symlink for dev).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/dist/WindBroker.app"
DEST="/Applications/WindBroker.app"
[ -d "$SRC" ] || "$ROOT/scripts/build_app.sh"

# A running windd imports its Python code from inside the installed bundle, so
# stage the new copy next to the old one and swap by rename: the path is never
# missing, and the running daemon (and its Wind login) is left untouched.
STAGE="$(dirname "$DEST")/.WindBroker.app.installing"
OLD="$(dirname "$DEST")/.WindBroker.app.previous"
rm -rf "$STAGE" "$OLD"
if [ "${1:-}" = "--symlink" ]; then
  ln -s "$SRC" "$STAGE"
else
  ditto "$SRC" "$STAGE"
fi
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  echo "Replacing existing $DEST"
  mv "$DEST" "$OLD"
fi
mv "$STAGE" "$DEST"
rm -rf "$OLD"
if [ -L "$DEST" ]; then echo "Symlinked $DEST -> $SRC"; else echo "Copied to $DEST"; fi
# Refresh Finder/Dock's cached icon for the new bundle.
touch "$DEST"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" >/dev/null 2>&1 || true

# Optional CLI shims on PATH.
BIN="${WIND_BROKER_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN"
ln -sf "$DEST/Contents/Resources/bin/windctl" "$BIN/windctl"
ln -sf "$DEST/Contents/Resources/bin/windd" "$BIN/windd"
echo "Linked windctl/windd into $BIN"
echo "Open it with: open -a WindBroker"
