#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$PROJECT_DIR/build"
APP_NAME="Mountain Turtle"
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}
CODE_SIGN_IDENTITY=${CODE_SIGN_IDENTITY:--}

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
if [[ ! -f "$PROJECT_DIR/Resources/AppIcon.icns" || ! -f "$PROJECT_DIR/Resources/icon-overlay-assets/VolumeIcon.icns" ]]; then
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
    -framework AppKit -framework SwiftUI -framework FinderSync \
    "${COMPILE_MODE[@]}" "${SOURCES[@]}" -o "$APP_PATH/Contents/MacOS/$APP_NAME"

EXTENSION_PATH="$APP_PATH/Contents/PlugIns/Mountain Turtle Finder.appex"
mkdir -p -- "$EXTENSION_PATH/Contents/MacOS"
/usr/bin/xcrun swiftc -O -swift-version 5 -target "$(uname -m)-apple-macosx14.0" \
    -sdk "$(/usr/bin/xcrun --sdk macosx --show-sdk-path)" \
    -application-extension -parse-as-library -module-name MountainTurtleFinder \
    -framework AppKit -framework FinderSync -Xlinker -e -Xlinker _NSExtensionMain \
    "$PROJECT_DIR/Sources/FinderSync/FinderSync.swift" \
    -o "$EXTENSION_PATH/Contents/MacOS/Mountain Turtle Finder"

# SharedFileList remains the macOS compatibility API for native Locations items.
# Keep it in a bounded helper so sidebar integration cannot block drive service.
HELPER_PATH="$APP_PATH/Contents/Helpers/Mountain Turtle Sidebar"
mkdir -p -- "$(dirname -- "$HELPER_PATH")"
/usr/bin/xcrun clang -O2 -fobjc-arc -mmacosx-version-min=14.0 \
    -Wno-deprecated-declarations -framework Foundation -framework CoreServices \
    "$PROJECT_DIR/Sources/SidebarMounts/main.m" -o "$HELPER_PATH"

CREDENTIALS_PATH="$APP_PATH/Contents/Helpers/Mountain Turtle Credentials"
/usr/bin/xcrun swiftc -O -swift-version 5 -target "$(uname -m)-apple-macosx14.0" \
    -framework Foundation -framework Security \
    "$PROJECT_DIR/Sources/Credentials/main.swift" -o "$CREDENTIALS_PATH"

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
    "CFBundleShortVersionString": "0.4.0",
    "CFBundleVersion": "0.4.0",
    "CFBundleURLTypes": [{"CFBundleURLName": "io.mountainturtle.app.actions",
                          "CFBundleURLSchemes": ["mountainturtle"],
                          "CFBundleTypeRole": "Viewer"}],
    "CFBundleIconFile": "AppIcon",
    "LSApplicationCategoryType": "public.app-category.utilities",
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": False,
    "NSHighResolutionCapable": True,
    "NSNetworkVolumesUsageDescription": "Mountain Turtle accesses your connected drives to show them directly in Finder's sidebar.",
    "NSPrincipalClass": "NSApplication",
    "NSHumanReadableCopyright": "Copyright © 2026 Mountain Turtle contributors. MIT License.",
}
with (app / "Contents/Info.plist").open("wb") as stream:
    plistlib.dump(info, stream, sort_keys=True)
(app / "Contents/PkgInfo").write_bytes(b"APPL????")

extension = app / "Contents/PlugIns/Mountain Turtle Finder.appex/Contents"
extension_info = {
    "CFBundleDevelopmentRegion": "en",
    "CFBundleDisplayName": "Mountain Turtle Finder Status",
    "CFBundleExecutable": "Mountain Turtle Finder",
    "CFBundleIdentifier": "io.mountainturtle.app.findersync",
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleName": "Mountain Turtle Finder",
    "CFBundlePackageType": "XPC!",
    "CFBundleShortVersionString": info["CFBundleShortVersionString"],
    "CFBundleVersion": info["CFBundleVersion"],
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": True,
    "NSHighResolutionCapable": True,
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    "NSExtension": {
        "NSExtensionPointIdentifier": "com.apple.FinderSync",
        "NSExtensionPrincipalClass": "MountainTurtleFinderSync",
        "NSExtensionAttributes": {},
    },
}
with (extension / "Info.plist").open("wb") as stream:
    plistlib.dump(extension_info, stream, sort_keys=True)
(extension / "PkgInfo").write_bytes(b"XPC!????")
PY

/usr/bin/codesign --force --sign "$CODE_SIGN_IDENTITY" --timestamp=none \
    --entitlements "$PROJECT_DIR/Sources/FinderSync/Entitlements.plist" "$EXTENSION_PATH"
/usr/bin/codesign --force --sign "$CODE_SIGN_IDENTITY" --timestamp=none "$HELPER_PATH"
/usr/bin/codesign --force --sign "$CODE_SIGN_IDENTITY" --timestamp=none "$CREDENTIALS_PATH"
/usr/bin/codesign --force --sign "$CODE_SIGN_IDENTITY" --timestamp=none "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
/usr/bin/plutil -lint "$APP_PATH/Contents/Info.plist"
if [[ -e "$BUILD_DIR/$APP_NAME.app" ]]; then
    rm -rf -- "$BUILD_DIR/$APP_NAME.app"
fi
mv -- "$APP_PATH" "$BUILD_DIR/$APP_NAME.app"
echo "Built: $BUILD_DIR/$APP_NAME.app"
echo "Runtime dependencies: Python 3, rclone with nfsmount, and AWS CLI v2. These are not bundled."
