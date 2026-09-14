#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$PROJECT_DIR/build"
APP_NAME="Mountain Turtle"
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}

if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3 is required to package Mountain Turtle." >&2
    exit 1
fi
if ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
    echo "Install Apple's Command Line Tools or Xcode to build Mountain Turtle." >&2
    exit 1
fi
shopt -s nullglob
SOURCES=("$PROJECT_DIR"/Sources/*.swift)
if [[ ${#SOURCES[@]} -eq 0 || ! -f "$PROJECT_DIR/service/turtle_service.py" ]]; then
    echo "Build requires Sources/*.swift and service/turtle_service.py." >&2
    exit 1
fi
if [[ ! -f "$PROJECT_DIR/Resources/AppIcon.icns" || ! -f "$PROJECT_DIR/Resources/icon-overlay/.VolumeIcon.icns" ]]; then
    /usr/bin/xcrun swift "$PROJECT_DIR/scripts/generate-icons.swift" "$PROJECT_DIR/Resources"
fi

mkdir -p -- "$BUILD_DIR"
STAGING_DIR=$(mktemp -d "$BUILD_DIR/.mountainturtle-build.XXXXXX")
trap 'rm -rf -- "$STAGING_DIR"' EXIT
APP_PATH="$STAGING_DIR/$APP_NAME.app"
mkdir -p -- "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"

COMPILE_MODE=()
if "$PYTHON_BIN" - "${SOURCES[@]}" <<'PY'
from pathlib import Path
import re
import sys
sys.exit(0 if any(re.search(r"(?m)^\s*@main\b", Path(p).read_text()) for p in sys.argv[1:]) else 1)
PY
then
    COMPILE_MODE=(-parse-as-library)
fi

echo "Building Mountain Turtle for $(uname -m), macOS 14 or newer…"
/usr/bin/xcrun swiftc -O -swift-version 5 -target "$(uname -m)-apple-macosx14.0" \
    -sdk "$(/usr/bin/xcrun --sdk macosx --show-sdk-path)" \
    -framework AppKit -framework SwiftUI \
    "${COMPILE_MODE[@]}" "${SOURCES[@]}" -o "$APP_PATH/Contents/MacOS/$APP_NAME"

"$PYTHON_BIN" - "$PROJECT_DIR" "$APP_PATH" <<'PY'
from pathlib import Path
import os
import plistlib
import shutil
import sys

project, app = map(Path, sys.argv[1:])
resources = app / "Contents/Resources"

def copy_raw(source, destination):
    source, destination = Path(source), Path(destination)
    destination.write_bytes(source.read_bytes())
    os.chmod(destination, source.stat().st_mode & 0o777)
    return str(destination)

ignore = shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc")
shutil.copytree(project / "Resources", resources, dirs_exist_ok=True,
                copy_function=copy_raw, ignore=ignore)
shutil.copytree(project / "service", resources / "service",
                copy_function=copy_raw, ignore=ignore)
for document in ("LICENSE", "README.md", "PROTOCOL.md", "THIRD_PARTY_NOTICES.md"):
    if (project / document).is_file():
        copy_raw(project / document, resources / document)
if (project / "docs").is_dir():
    shutil.copytree(project / "docs", resources / "docs",
                    copy_function=copy_raw, ignore=ignore)

info = {
    "CFBundleDevelopmentRegion": "en",
    "CFBundleDisplayName": "Mountain Turtle",
    "CFBundleExecutable": "Mountain Turtle",
    "CFBundleIdentifier": "io.mountainturtle.app",
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleName": "Mountain Turtle",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "0.1.0",
    "CFBundleVersion": "0.1.0",
    "CFBundleIconFile": "AppIcon",
    "LSApplicationCategoryType": "public.app-category.utilities",
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": False,
    "NSHighResolutionCapable": True,
    "NSPrincipalClass": "NSApplication",
    "NSHumanReadableCopyright": "Copyright © 2026 Mountain Turtle contributors. MIT License.",
}
with (app / "Contents/Info.plist").open("wb") as stream:
    plistlib.dump(info, stream, sort_keys=True)
(app / "Contents/PkgInfo").write_bytes(b"APPL????")
PY

/usr/bin/codesign --force --sign - --timestamp=none "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
/usr/bin/plutil -lint "$APP_PATH/Contents/Info.plist"
if [[ -e "$BUILD_DIR/$APP_NAME.app" ]]; then
    rm -rf -- "$BUILD_DIR/$APP_NAME.app"
fi
mv -- "$APP_PATH" "$BUILD_DIR/$APP_NAME.app"
echo "Built: $BUILD_DIR/$APP_NAME.app"
echo "Runtime dependencies: Python 3, rclone with nfsmount, and AWS CLI v2. These are not bundled."
