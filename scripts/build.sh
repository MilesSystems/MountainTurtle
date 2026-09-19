#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR=${BUILD_DIR:-"$PROJECT_DIR/build"}
APP_NAME="Mountain Turtle"
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}
CODE_SIGN_IDENTITY=${CODE_SIGN_IDENTITY:--}
TARGET_ARCHS=${TARGET_ARCHS:-$(uname -m)}
VERSION=$(cat "$PROJECT_DIR/VERSION")
BUILD_VERSION=${BUILD_VERSION:-$VERSION}

if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3 is required to package Mountain Turtle." >&2
    exit 1
fi
"$PYTHON_BIN" - "$VERSION" "$BUILD_VERSION" "$PROJECT_DIR/Resources/SparklePublicKey.txt" <<'PY'
import base64
from pathlib import Path
import re
import sys
for value in sys.argv[1:3]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise SystemExit("VERSION and BUILD_VERSION must be three numeric components (for example, 0.7.0).")
try:
    key = base64.b64decode(Path(sys.argv[3]).read_text().strip(), validate=True)
except (ValueError, OSError) as error:
    raise SystemExit(f"A valid Resources/SparklePublicKey.txt is required: {error}")
if len(key) != 32:
    raise SystemExit("Sparkle public key must decode to 32 bytes.")
PY
read -r -a ARCHS <<< "$TARGET_ARCHS"
if [[ ${#ARCHS[@]} -eq 0 || ${#ARCHS[@]} -gt 2 ]]; then
    echo "TARGET_ARCHS must be arm64, x86_64, or 'arm64 x86_64'." >&2
    exit 1
fi
for arch in "${ARCHS[@]}"; do
    if [[ "$arch" != arm64 && "$arch" != x86_64 ]]; then
        echo "Unsupported target architecture: $arch" >&2
        exit 1
    fi
done
if ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
    echo "Install Apple's Command Line Tools or Xcode to build Mountain Turtle." >&2
    exit 1
fi
SPARKLE_DIR=$("$PROJECT_DIR/scripts/fetch-sparkle.sh")
shopt -s nullglob
SOURCES=("$PROJECT_DIR"/Sources/*.swift)
if [[ ${#SOURCES[@]} -eq 0 || ! -f "$PROJECT_DIR/service/turtle_service.py" ]]; then
    echo "Build requires Sources/*.swift and service/turtle_service.py." >&2
    exit 1
fi
if [[ ! -f "$PROJECT_DIR/Resources/AppIcon.icns" || ! -f "$PROJECT_DIR/Resources/icon-overlay-assets/VolumeIcon.icns" || ! -f "$PROJECT_DIR/Resources/icon-overlay-assets-sftp/VolumeIcon.icns" ]]; then
    /usr/bin/xcrun swift "$PROJECT_DIR/scripts/generate-icons.swift" "$PROJECT_DIR/Resources"
fi

mkdir -p -- "$BUILD_DIR"
STAGING_DIR=$(mktemp -d "$BUILD_DIR/.mountainturtle-build.XXXXXX")
trap 'rm -rf -- "$STAGING_DIR"' EXIT
APP_PATH="$STAGING_DIR/$APP_NAME.app"
mkdir -p -- "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources" "$APP_PATH/Contents/Frameworks"
# ditto preserves the framework's versioned symbolic links and executable bits.
/usr/bin/ditto "$SPARKLE_DIR/Sparkle.framework" "$APP_PATH/Contents/Frameworks/Sparkle.framework"

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

EXTENSION_PATH="$APP_PATH/Contents/PlugIns/Mountain Turtle Finder.appex"
mkdir -p -- "$EXTENSION_PATH/Contents/MacOS" "$APP_PATH/Contents/Helpers"
HELPER_PATH="$APP_PATH/Contents/Helpers/Mountain Turtle Sidebar"
CREDENTIALS_PATH="$APP_PATH/Contents/Helpers/Mountain Turtle Credentials"
PHOTO_DATES_PATH="$APP_PATH/Contents/Helpers/Mountain Turtle Photo Dates"

for arch in "${ARCHS[@]}"; do
ARCH_DIR="$STAGING_DIR/$arch"
mkdir -p -- "$ARCH_DIR"
echo "Building Mountain Turtle $VERSION ($BUILD_VERSION) for $arch, macOS 14 or newer…"
/usr/bin/xcrun swiftc -O -swift-version 5 -target "$arch-apple-macosx14.0" \
    -sdk "$(/usr/bin/xcrun --sdk macosx --show-sdk-path)" \
    -framework AppKit -framework SwiftUI -framework FinderSync \
    -F "$SPARKLE_DIR" -framework Sparkle \
    -Xlinker -rpath -Xlinker '@executable_path/../Frameworks' \
    "${COMPILE_MODE[@]}" "${SOURCES[@]}" -o "$ARCH_DIR/app"

/usr/bin/xcrun swiftc -O -swift-version 5 -target "$arch-apple-macosx14.0" \
    -sdk "$(/usr/bin/xcrun --sdk macosx --show-sdk-path)" \
    -application-extension -parse-as-library -module-name MountainTurtleFinder \
    -framework AppKit -framework FinderSync -Xlinker -e -Xlinker _NSExtensionMain \
    "$PROJECT_DIR/Sources/FinderSync/FinderSync.swift" \
    -o "$ARCH_DIR/finder"

# SharedFileList remains the macOS compatibility API for native Locations items.
# Keep it in a bounded helper so sidebar integration cannot block drive service.
/usr/bin/xcrun clang -O2 -fobjc-arc -arch "$arch" -mmacosx-version-min=14.0 \
    -Wno-deprecated-declarations -framework Foundation -framework CoreServices \
    "$PROJECT_DIR/Sources/SidebarMounts/main.m" -o "$ARCH_DIR/sidebar"

/usr/bin/xcrun swiftc -O -swift-version 5 -target "$arch-apple-macosx14.0" \
    -framework Foundation -framework Security \
    "$PROJECT_DIR/Sources/Credentials/main.swift" -o "$ARCH_DIR/credentials"

/usr/bin/xcrun swiftc -O -swift-version 5 -target "$arch-apple-macosx14.0" \
    -framework Foundation -framework ImageIO \
    "$PROJECT_DIR/Sources/PhotoDates/main.swift" -o "$ARCH_DIR/photo-dates"
done

combine_binary() {
    local name=$1 destination=$2
    local inputs=()
    for arch in "${ARCHS[@]}"; do inputs+=("$STAGING_DIR/$arch/$name"); done
    /usr/bin/lipo -create "${inputs[@]}" -output "$destination"
}
combine_binary app "$APP_PATH/Contents/MacOS/$APP_NAME"
combine_binary finder "$EXTENSION_PATH/Contents/MacOS/Mountain Turtle Finder"
combine_binary sidebar "$HELPER_PATH"
combine_binary credentials "$CREDENTIALS_PATH"
combine_binary photo-dates "$PHOTO_DATES_PATH"

"$PYTHON_BIN" - "$PROJECT_DIR" "$APP_PATH" "$VERSION" "$BUILD_VERSION" <<'PY'
from pathlib import Path
import os
import plistlib
import shutil
import sys

project, app = map(Path, sys.argv[1:3])
version, build_version = sys.argv[3:5]
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
    "CFBundleShortVersionString": version,
    "CFBundleVersion": build_version,
    "SUFeedURL": "https://github.com/MilesSystems/MountainTurtle/releases/latest/download/appcast.xml",
    "SUPublicEDKey": (project / "Resources/SparklePublicKey.txt").read_text().strip(),
    "SUEnableAutomaticChecks": True,
    "SUAutomaticallyUpdate": False,
    "SUAllowsAutomaticUpdates": False,
    "SUVerifyUpdateBeforeExtraction": True,
    "SURequireSignedFeed": True,
    "CFBundleURLTypes": [{"CFBundleURLName": "io.mountainturtle.app.actions",
                          "CFBundleURLSchemes": ["mountainturtle"],
                          "CFBundleTypeRole": "Viewer"}],
    "CFBundleDocumentTypes": [{"CFBundleTypeName": "Mountain Turtle Connection",
                               "CFBundleTypeRole": "Viewer",
                               "LSHandlerRank": "Owner",
                               "CFBundleTypeIconFile": "AppIcon",
                               "LSItemContentTypes": ["io.mountainturtle.setup", "io.mountainturtle.connection"]}],
    "UTExportedTypeDeclarations": [{"UTTypeIdentifier": "io.mountainturtle.connection",
                                    "UTTypeDescription": "Mountain Turtle Connection",
                                    "UTTypeConformsTo": ["public.json"],
                                    "UTTypeTagSpecification": {"public.filename-extension": ["mountainturtle"]}},
                                   {"UTTypeIdentifier": "io.mountainturtle.setup",
                                    "UTTypeDescription": "Mountain Turtle Connection",
                                    "UTTypeConformsTo": ["public.json"],
                                    "UTTypeTagSpecification": {"public.filename-extension": ["turtle"]}}],
    "CFBundleIconFile": "AppIcon",
    "LSApplicationCategoryType": "public.app-category.utilities",
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": False,
    "NSHighResolutionCapable": True,
    "NSLocalNetworkUsageDescription": "Mountain Turtle connects to SFTP servers you choose on your local network and reads their storage capacity.",
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

SIGN_FLAGS=(--force --sign "$CODE_SIGN_IDENTITY" --timestamp=none)
if [[ "$CODE_SIGN_IDENTITY" != - ]]; then
    SIGN_FLAGS+=(--options runtime)
fi
if [[ "$CODE_SIGN_IDENTITY" == "Developer ID Application:"* ]]; then
    SIGN_FLAGS=(--force --sign "$CODE_SIGN_IDENTITY" --options runtime --timestamp)
fi
SPARKLE_FRAMEWORK="$APP_PATH/Contents/Frameworks/Sparkle.framework"
# Sign from the inside out. Downloader alone needs its sandbox entitlements.
# https://sparkle-project.org/documentation/sandboxing/#code-signing
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$SPARKLE_FRAMEWORK/Versions/B/XPCServices/Installer.xpc"
/usr/bin/codesign "${SIGN_FLAGS[@]}" --preserve-metadata=entitlements \
    "$SPARKLE_FRAMEWORK/Versions/B/XPCServices/Downloader.xpc"
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$SPARKLE_FRAMEWORK/Versions/B/Autoupdate"
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$SPARKLE_FRAMEWORK/Versions/B/Updater.app"
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$SPARKLE_FRAMEWORK"
/usr/bin/codesign "${SIGN_FLAGS[@]}" \
    --entitlements "$PROJECT_DIR/Sources/FinderSync/Entitlements.plist" "$EXTENSION_PATH"
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$HELPER_PATH"
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$CREDENTIALS_PATH"
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$PHOTO_DATES_PATH"
/usr/bin/codesign "${SIGN_FLAGS[@]}" "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
/usr/bin/plutil -lint "$APP_PATH/Contents/Info.plist"
if [[ -e "$BUILD_DIR/$APP_NAME.app" ]]; then
    rm -rf -- "$BUILD_DIR/$APP_NAME.app"
fi
mv -- "$APP_PATH" "$BUILD_DIR/$APP_NAME.app"
echo "Built: $BUILD_DIR/$APP_NAME.app"
echo "Runtime dependencies: Python 3, rclone with nfsmount, and AWS CLI v2. These are not bundled."
