#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}
REPO=MilesSystems/MountainTurtle
PREVIEW=false
NOTES_FILE=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --preview) PREVIEW=true; shift ;;
        --notes) NOTES_FILE=${2:?Pass a Markdown release notes file}; shift 2 ;;
        *) echo "Usage: CODE_SIGN_IDENTITY='…' $0 [--preview] [--notes FILE]" >&2; exit 1 ;;
    esac
done
[[ -n "$PYTHON_BIN" ]] || { echo "Python 3 is required." >&2; exit 1; }
[[ -n "${CODE_SIGN_IDENTITY:-}" && "$CODE_SIGN_IDENTITY" != - ]] || {
    echo "Set CODE_SIGN_IDENTITY to the full Apple Development or Developer ID Application certificate name." >&2
    exit 1
}
if [[ "$PREVIEW" == true ]]; then
    [[ "$CODE_SIGN_IDENTITY" == "Apple Development:"* || "$CODE_SIGN_IDENTITY" == "Developer ID Application:"* ]] || {
        echo "Preview releases require an Apple signing certificate." >&2; exit 1;
    }
else
    [[ "$CODE_SIGN_IDENTITY" == "Developer ID Application:"* && -n "${NOTARY_PROFILE:-}" ]] || {
        echo "Public distribution requires Developer ID Application signing and NOTARY_PROFILE. Use --preview explicitly for a labelled, unnotarized preview." >&2
        exit 1
    }
fi
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || {
    echo "Commit all source changes before preparing a release." >&2; exit 1;
}
VERSION=$(cat VERSION)
"$PYTHON_BIN" - "$VERSION" <<'PY'
import re
import sys
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", sys.argv[1]):
    raise SystemExit("VERSION must contain three numeric components, for example 0.7.0.")
PY
COMMIT=$(git rev-parse HEAD)
TAG="v$VERSION"
RELEASE_DIR="$PROJECT_DIR/build/releases/$TAG"
[[ ! -e "$RELEASE_DIR" ]] || { echo "Release already prepared: $RELEASE_DIR. Use a new version; existing assets are not overwritten." >&2; exit 1; }
SPARKLE_DIR=$("$PROJECT_DIR/scripts/fetch-sparkle.sh")
PUBLIC_KEY=$("$SPARKLE_DIR/bin/generate_keys" --account io.mountainturtle.app -p)
[[ "$PUBLIC_KEY" == "$(cat Resources/SparklePublicKey.txt)" ]] || {
    echo "The Sparkle Keychain public key does not match Resources/SparklePublicKey.txt." >&2; exit 1;
}
mkdir -p -- "$PROJECT_DIR/build/releases"
STAGING_DIR=$(mktemp -d "$PROJECT_DIR/build/releases/.prepare-$TAG.XXXXXX")
trap 'rm -rf -- "$STAGING_DIR"' EXIT
OUTPUT="$STAGING_DIR/assets"
mkdir -p -- "$OUTPUT"
BUILD_DIR="$STAGING_DIR/build" TARGET_ARCHS='arm64 x86_64' BUILD_VERSION="$VERSION" \
    "$PROJECT_DIR/scripts/build.sh"
APP="$STAGING_DIR/build/Mountain Turtle.app"
ARCHIVE="$OUTPUT/MountainTurtle-$VERSION.zip"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"
if [[ "$PREVIEW" == false ]]; then
    /usr/bin/xcrun notarytool submit "$ARCHIVE" --keychain-profile "$NOTARY_PROFILE" --wait
    /usr/bin/xcrun stapler staple "$APP"
    /usr/bin/xcrun stapler validate "$APP"
    /usr/sbin/spctl --assess --type execute --verbose "$APP"
    rm -- "$ARCHIVE"
    /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"
fi
/usr/bin/codesign --verify --deep --strict "$APP"
"$PYTHON_BIN" - "$APP" "$OUTPUT" "$VERSION" "$COMMIT" "$PREVIEW" "$NOTES_FILE" <<'PY'
from pathlib import Path
import json
import plistlib
import subprocess
import sys

app, output = map(Path, sys.argv[1:3])
version, commit, preview, notes_file = sys.argv[3:]
preview = preview == "true"
info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
assert info["CFBundleVersion"] == info["CFBundleShortVersionString"] == version
assert info["SUVerifyUpdateBeforeExtraction"] and info["SURequireSignedFeed"]
assert info["SUAllowsAutomaticUpdates"] is False
for binary in [app / "Contents/MacOS/Mountain Turtle",
               app / "Contents/PlugIns/Mountain Turtle Finder.appex/Contents/MacOS/Mountain Turtle Finder",
               *list((app / "Contents/Helpers").iterdir())]:
    arches = subprocess.check_output(["/usr/bin/lipo", "-archs", str(binary)], text=True).split()
    if set(arches) != {"arm64", "x86_64"}:
        raise SystemExit(f"Release executable is not universal: {binary}")
title = f"Mountain Turtle {version}" + (" Preview" if preview else "")
notice = ("**Preview: Apple signed, but not notarized for public distribution.** "
          "macOS may require Privacy & Security → Open Anyway on first installation.\n\n") if preview else ""
if notes_file:
    changes = Path(notes_file).read_text().strip()
else:
    previous = subprocess.run(["git", "describe", "--tags", "--abbrev=0", "--match", "v*", "HEAD^"],
                              text=True, capture_output=True)
    log_range = f"{previous.stdout.strip()}..{commit}" if previous.returncode == 0 else commit
    changes = subprocess.check_output(["git", "log", "--no-merges", "--format=- %s", log_range], text=True).strip()
notes = f"# {title}\n\n{notice}{changes}\n\n" + (
    "Requires macOS 14 or newer; supports Apple silicon and Intel Macs.\n\n"
    "Python 3 and rclone remain separately installed runtime dependencies. "
    "AWS connections also require AWS CLI v2.\n\n"
    "Copy Mountain Turtle.app to Applications before launching. "
    "Use Mountain Turtle → Check for Updates… for future signed updates; "
    "downloads and installation require your choice.\n\n"
    f"Source commit: `{commit}`\n")
(output / f"MountainTurtle-{version}.md").write_text(notes)
(output / "release.json").write_text(json.dumps({
    "repository": "MilesSystems/MountainTurtle", "version": version, "tag": f"v{version}",
    "commit": commit, "preview": preview, "notarized": not preview, "title": title,
    "architectures": ["arm64", "x86_64"], "sparkle_version": "2.10.0"
}, indent=2) + "\n")
PY
"$SPARKLE_DIR/bin/generate_appcast" --account io.mountainturtle.app \
    --download-url-prefix "https://github.com/$REPO/releases/download/$TAG/" \
    --release-notes-url-prefix "https://github.com/$REPO/releases/download/$TAG/" \
    --link "https://github.com/$REPO/releases/tag/$TAG" --embed-release-notes \
    --maximum-deltas 0 "$OUTPUT"
"$SPARKLE_DIR/bin/sign_update" --account io.mountainturtle.app --verify "$OUTPUT/appcast.xml"
"$PYTHON_BIN" - "$OUTPUT" "$VERSION" "$SPARKLE_DIR" <<'PY'
from pathlib import Path
import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET

output, version, sparkle = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
enclosures = ET.parse(output / "appcast.xml").findall("./channel/item/enclosure")
if len(enclosures) != 1:
    raise SystemExit("Expected exactly one release archive in the appcast.")
enclosure = enclosures[0]
archive = output / f"MountainTurtle-{version}.zip"
expected_url = f"https://github.com/MilesSystems/MountainTurtle/releases/download/v{version}/{archive.name}"
if enclosure.attrib["url"] != expected_url or int(enclosure.attrib["length"]) != archive.stat().st_size:
    raise SystemExit("Generated appcast archive URL or size is incorrect.")
signature = enclosure.attrib["{http://www.andymatuschak.org/xml-namespaces/sparkle}edSignature"]
subprocess.run([str(sparkle / "bin/sign_update"), "--account", "io.mountainturtle.app", "--verify", str(archive), signature], check=True)
checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                    for path in sorted(output.iterdir()) if path.is_file())
(output / "SHA256SUMS").write_text(checksums)
PY
[[ "$(git rev-parse HEAD)" == "$COMMIT" && -z "$(git status --porcelain --untracked-files=all)" ]] || {
    echo "Source changed while the release was being prepared; refusing to publish these artifacts." >&2; exit 1;
}
mv -- "$OUTPUT" "$RELEASE_DIR"
echo "Prepared (nothing uploaded): $RELEASE_DIR"
echo "Review its notes and artifacts, then push commit $COMMIT and tag $TAG."
echo "Publish with scripts/publish-release.sh (add --preview for this preview) and the release directory."
