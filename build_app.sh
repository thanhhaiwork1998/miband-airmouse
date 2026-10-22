#!/bin/zsh
# Builds ~/Applications/MiBand AirMouse.app (a thin launcher for ~/airmouse/app.py).
# build_release.sh reuses it with APP/ARCH/VERSION set and SIGN=0, then adds a Python runtime.
set -e
cd "$(dirname "$0")"
APP="${APP:-$HOME/Applications/MiBand AirMouse.app}"
ARCH="${ARCH:-$(uname -m)}"
VERSION="${VERSION:-1.1.0}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
clang -O2 -fobjc-arc -arch "$ARCH" -mmacosx-version-min=12.0 -framework Cocoa -o "$APP/Contents/MacOS/AirMouse" launcher.m
[ -f AppIcon.icns ] || venv/bin/python make_icon.py
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>MiBand AirMouse</string>
  <key>CFBundleDisplayName</key><string>MiBand AirMouse</string>
  <key>CFBundleIdentifier</key><string>local.miband.airmouse</string>
  <key>CFBundleExecutable</key><string>AirMouse</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>MiBand AirMouse ket noi voi vong Mi Band de doc cam bien chuyen dong va dieu khien chuot.</string>
</dict>
</plist>
PLIST

[ "${SIGN:-1}" = 0 ] || codesign --force --deep --sign - "$APP"
echo "Built: $APP"
