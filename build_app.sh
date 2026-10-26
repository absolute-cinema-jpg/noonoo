#!/bin/bash
# Builds NooNoo.app (native window + bundled engine) and installs it into /Applications.
#   ./build_app.sh                      build for this Mac + install
#   ./build_app.sh --open               ...and launch it
#   ./build_app.sh --arch=x86_64 --no-install --zip
#                                       Intel build, zipped for a release (arm64 | x86_64 | universal)
set -euo pipefail
cd "$(dirname "$0")"

ARCH=$(uname -m); INSTALL=1; OPEN=0; ZIP=0
for arg in "$@"; do
  case "$arg" in
    --open) OPEN=1 ;;
    --no-install) INSTALL=0 ;;
    --zip) ZIP=1 ;;
    --arch=*) ARCH="${arg#--arch=}" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
case "$ARCH" in
  arm64) TARGETS="arm64"; LABEL="Apple-Silicon" ;;
  x86_64) TARGETS="x86_64"; LABEL="Intel" ;;
  universal) TARGETS="arm64 x86_64"; LABEL="Universal" ;;
  *) echo "--arch must be arm64, x86_64 or universal" >&2; exit 2 ;;
esac

BUILD="$PWD/build"
OUT="$BUILD/$ARCH"
APP="$OUT/NooNoo.app"
VERSION=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" app/Info.plist)

echo "Building NooNoo $VERSION ($LABEL)..."
rm -rf "$APP" "$BUILD/NooNoo.iconset"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$BUILD/NooNoo.iconset"
SLICES=()
for t in $TARGETS; do
  swiftc -O -target "$t-apple-macos12" -o "$OUT/NooNoo-$t" app/NooNoo.swift -framework Cocoa -framework WebKit
  SLICES+=("$OUT/NooNoo-$t")
done
lipo -create "${SLICES[@]}" -output "$APP/Contents/MacOS/NooNoo"
rm -f "${SLICES[@]}"
cp app/Info.plist "$APP/Contents/Info.plist"

# app icon: render the SVG once at 1024px, then every size macOS wants
sips -s format png app/icon.svg --out "$BUILD/icon-1024.png" >/dev/null
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon-1024.png" --out "$BUILD/NooNoo.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$BUILD/icon-1024.png" --out "$BUILD/NooNoo.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$BUILD/NooNoo.iconset" -o "$APP/Contents/Resources/NooNoo.icns"

# the engine + UI ride inside the bundle; tools and settings stay in ~/Library/Application Support/NooNoo
rsync -a --exclude __pycache__ noonoo "$APP/Contents/Resources/"
codesign --force --deep --sign - "$APP"
echo "Built $APP ($(lipo -archs "$APP/Contents/MacOS/NooNoo"))"

if [ "$ZIP" = 1 ]; then
  ZIPFILE="$BUILD/NooNoo-$VERSION-macOS-$LABEL.zip"
  rm -f "$ZIPFILE"
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIPFILE"
  echo "Zipped $ZIPFILE"
fi

if [ "$INSTALL" = 1 ]; then
  DEST=/Applications
  [ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
  if pgrep -xq NooNoo; then
    osascript -e 'quit app "NooNoo"' || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -xq NooNoo || break; sleep 0.5; done
  fi
  rm -rf "$DEST/NooNoo.app"
  cp -R "$APP" "$DEST/"
  touch "$DEST/NooNoo.app"
  echo "Installed $DEST/NooNoo.app"
  if [ "$OPEN" = 1 ]; then open "$DEST/NooNoo.app"; fi
fi
