#!/usr/bin/env bash
# Build dist/WindBroker.app with swiftc (no Xcode project needed).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/dist/WindBroker.app"
ARCH="${ARCH:-$(uname -m)}"
MIN_MACOS="${MIN_MACOS:-14.0}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/python" "$APP/Contents/Resources/bin"

echo "==> swiftc ($ARCH, macOS $MIN_MACOS+)"
xcrun swiftc \
  -O -parse-as-library -swift-version 5 \
  -target "$ARCH-apple-macos$MIN_MACOS" \
  -sdk "$(xcrun --sdk macosx --show-sdk-path)" \
  -framework SwiftUI -framework AppKit \
  "$ROOT"/macos/Sources/*.swift \
  -o "$APP/Contents/MacOS/WindBroker"

cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"
# App icon: compiled asset catalog (Assets.car + AppIcon.icns) when Xcode's actool
# works; otherwise (Command Line Tools only, or Xcode not first-launched) use the
# prebuilt .icns.
if ! { xcrun --find actool >/dev/null 2>&1 && \
       xcrun actool "$ROOT/macos/Assets/Assets.xcassets" --compile "$APP/Contents/Resources" \
         --platform macosx --minimum-deployment-target "$MIN_MACOS" --app-icon AppIcon \
         --output-partial-info-plist "$ROOT/dist/.actool-info.plist" >/dev/null 2>&1; }; then
  echo "    (actool unavailable; using prebuilt AppIcon.icns)"
  cp "$ROOT/macos/Assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi
printf 'APPL????' > "$APP/Contents/PkgInfo"

echo "==> bundling Python daemon + SDK"
rsync -a --delete --exclude '__pycache__' "$ROOT/src/wind_broker" "$APP/Contents/Resources/python/"
cp "$ROOT/src/wind_broker_client.py" "$APP/Contents/Resources/python/"

# Shell shims so `windd` / `windctl` work from the bundle without pip install.
for tool in windd:wind_broker.daemon windctl:wind_broker.cli; do
  name="${tool%%:*}"; module="${tool#*:}"
  cat > "$APP/Contents/Resources/bin/$name" <<SHIM
#!/bin/sh
# Uses python_executable from ~/.wind-broker/config.json when set.
# Follow symlinks (e.g. ~/.local/bin/windctl) back into the app bundle.
SELF="\$0"
while [ -L "\$SELF" ]; do
  LINK="\$(readlink "\$SELF")"
  case "\$LINK" in /*) SELF="\$LINK" ;; *) SELF="\$(dirname "\$SELF")/\$LINK" ;; esac
done
HERE="\$(cd "\$(dirname "\$SELF")/../python" && pwd)"
CONFIG="\${WIND_BROKER_HOME:-\$HOME/.wind-broker}/config.json"
PY=""
if [ -f "\$CONFIG" ]; then
  PY="\$(/usr/bin/plutil -extract python_executable raw -o - "\$CONFIG" 2>/dev/null || true)"
fi
[ -x "\$PY" ] || PY="\$(command -v python3)"
# Keep bytecode out of the signed bundle (writing __pycache__ breaks its seal).
export PYTHONPYCACHEPREFIX="\${PYTHONPYCACHEPREFIX:-\${WIND_BROKER_HOME:-\$HOME/.wind-broker}/pycache}"
PYTHONPATH="\$HERE\${PYTHONPATH:+:\$PYTHONPATH}" exec "\$PY" -m $module "\$@"
SHIM
  chmod +x "$APP/Contents/Resources/bin/$name"
done

echo "==> ad-hoc codesign"
codesign --force --deep --sign - "$APP" >/dev/null

file "$APP/Contents/MacOS/WindBroker"
echo "Built $APP"
