#!/bin/bash
# Build BrainPoint.app: a release build of the menu-bar agent, wrapped in an app bundle with an
# Info.plist (LSUIElement, the Accessibility usage text) and signed ad hoc with a stable
# identifier, so the Accessibility permission survives rebuilds as far as macOS allows.
#
#     clients/macos/scripts/build-app.sh                 # -> clients/macos/build/BrainPoint.app
#     SCRATCH=.build-release clients/macos/scripts/build-app.sh
#     open clients/macos/build/BrainPoint.app --args --server ws://127.0.0.1:8765/ws
set -euo pipefail

cd "$(dirname "$0")/.."
BUNDLE_ID="ai.brainsync.brainpoint"
VERSION="0.1.0"
SCRATCH="${SCRATCH:-.build}"
APP="build/BrainPoint.app"

swift build -c release --product BrainPoint --scratch-path "$SCRATCH"
BIN="$(swift build -c release --product BrainPoint --scratch-path "$SCRATCH" --show-bin-path)/BrainPoint"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/BrainPoint"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleName</key><string>BrainPoint</string>
  <key>CFBundleDisplayName</key><string>BrainPoint</string>
  <key>CFBundleExecutable</key><string>BrainPoint</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAccessibilityUsageDescription</key>
  <string>BrainPoint reads the buttons of the app in front and presses the one you pick with your eyes through the BrainSync headset.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"
codesign --verify --verbose=1 "$APP"
echo "built $(pwd)/$APP"
