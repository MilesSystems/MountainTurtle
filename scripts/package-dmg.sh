#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
APP_NAME="Mountain Turtle"
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}
CODE_SIGN_IDENTITY=${CODE_SIGN_IDENTITY:-}
BACKGROUND_SOURCE="$PROJECT_DIR/Resources/dmg-background.png"
BACKGROUND_WIDTH=900
BACKGROUND_HEIGHT=600
VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$PROJECT_DIR/build/$APP_NAME.app/Contents/Info.plist" 2>/dev/null || true)
if [[ -z "$VERSION" ]]; then
    "$PROJECT_DIR/scripts/build.sh"
    VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$PROJECT_DIR/build/$APP_NAME.app/Contents/Info.plist")
fi

SOURCE_APP=${SOURCE_APP:-"$PROJECT_DIR/build/$APP_NAME.app"}
OUTPUT_DIR=${OUTPUT_DIR:-"$HOME/Desktop"}
STAMP=${STAMP:-$(date +%Y%m%d-%H%M%S)}
DMG_PATH="$OUTPUT_DIR/$APP_NAME $VERSION-$STAMP.dmg"
RW_DMG="$OUTPUT_DIR/$APP_NAME $VERSION-$STAMP-rw.dmg"
VOLUME_NAME="$APP_NAME $VERSION"

if [[ ! -d "$SOURCE_APP" ]]; then
    echo "Build Mountain Turtle before packaging." >&2
    exit 1
fi
if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3 is required to package Mountain Turtle." >&2
    exit 1
fi
if [[ ! -f "$BACKGROUND_SOURCE" ]]; then
    echo "DMG background is missing: $BACKGROUND_SOURCE" >&2
    exit 1
fi
/usr/bin/codesign --verify --deep --strict "$SOURCE_APP"
/bin/mkdir -p -- "$OUTPUT_DIR"

while IFS= read -r -d '' existing_volume; do
    if [[ -d "$existing_volume" ]]; then
        /usr/bin/hdiutil detach "$existing_volume" >/dev/null 2>&1 || true
    fi
done < <(/usr/bin/find /Volumes -maxdepth 1 \( -name "$VOLUME_NAME" -o -name "$VOLUME_NAME [0-9]*" \) -print0 2>/dev/null)

STAGING_DIR=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/mountainturtle-dmg.XXXXXX")
cleanup() {
    if [[ -n "${MOUNT_POINT:-}" && -d "$MOUNT_POINT" ]]; then
        /usr/bin/hdiutil detach "$MOUNT_POINT" >/dev/null 2>&1 || true
    fi
    /bin/rm -rf -- "$STAGING_DIR"
    if [[ -f "$RW_DMG" ]]; then
        /bin/rm -f -- "$RW_DMG"
    fi
}
trap cleanup EXIT

/usr/bin/ditto --rsrc --extattr "$SOURCE_APP" "$STAGING_DIR/$APP_NAME.app"
/bin/ln -s /Applications "$STAGING_DIR/Applications"
/bin/mkdir -p -- "$STAGING_DIR/.background"
/usr/bin/sips -z "$BACKGROUND_HEIGHT" "$BACKGROUND_WIDTH" "$BACKGROUND_SOURCE" \
    --out "$STAGING_DIR/.background/background.png" >/dev/null
/usr/bin/hdiutil create -volname "$VOLUME_NAME" -srcfolder "$STAGING_DIR" \
    -format UDRW -fs APFS -ov "$RW_DMG"

ATTACH_PLIST="$STAGING_DIR/attach.plist"
/usr/bin/hdiutil attach "$RW_DMG" -readwrite -nobrowse -plist > "$ATTACH_PLIST"
MOUNT_POINT=$("$PYTHON_BIN" - "$ATTACH_PLIST" <<'PY'
import plistlib
import sys
with open(sys.argv[1], "rb") as stream:
    info = plistlib.load(stream)
for entity in info["system-entities"]:
    if "mount-point" in entity:
        print(entity["mount-point"])
        break
PY
)

/usr/bin/osascript <<APPLESCRIPT >/dev/null
tell application "Finder"
  tell disk "$VOLUME_NAME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set bounds of container window to {120, 120, 1020, 720}
    set theViewOptions to the icon view options of container window
    set arrangement of theViewOptions to not arranged
    set icon size of theViewOptions to 112
    set background picture of theViewOptions to file ".background:background.png"
    set position of item "$APP_NAME.app" of container window to {310, 360}
    set position of item "Applications" of container window to {590, 360}
    update without registering applications
    delay 1
    close
  end tell
end tell
APPLESCRIPT

if [[ ! -f "$MOUNT_POINT/.DS_Store" ]]; then
    echo "Finder did not write the DMG layout metadata." >&2
    exit 1
fi

/bin/sync
/usr/bin/hdiutil detach "$MOUNT_POINT" >/dev/null
MOUNT_POINT=
/usr/bin/hdiutil convert "$RW_DMG" -format UDZO -imagekey zlib-level=9 -ov -o "$DMG_PATH" >/dev/null
if [[ -n "$CODE_SIGN_IDENTITY" && "$CODE_SIGN_IDENTITY" != "-" ]]; then
    /usr/bin/codesign --force --sign "$CODE_SIGN_IDENTITY" "$DMG_PATH"
    /usr/bin/codesign --verify --verbose "$DMG_PATH"
fi
/usr/bin/hdiutil verify "$DMG_PATH"
echo "Created: $DMG_PATH"
