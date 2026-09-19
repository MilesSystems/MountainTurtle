#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}
REPO=MilesSystems/MountainTurtle
PREVIEW=false
if [[ "${1:-}" == --preview ]]; then PREVIEW=true; shift; fi
[[ $# -eq 1 && -n "$PYTHON_BIN" ]] || { echo "Usage: $0 [--preview] build/releases/vVERSION" >&2; exit 1; }
RELEASE_DIR=$(cd -- "$1" && pwd)
command -v gh >/dev/null || { echo "GitHub CLI (gh) is required." >&2; exit 1; }
WORK=$(mktemp -d "$PROJECT_DIR/build/.publish-release.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
"$PYTHON_BIN" - "$RELEASE_DIR" "$PREVIEW" > "$WORK/metadata" <<'PY'
from pathlib import Path
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET

directory, preview = Path(sys.argv[1]), sys.argv[2] == "true"
manifest = json.loads((directory / "release.json").read_text())
version = manifest["version"]
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("Invalid release version.")
if manifest["repository"] != "MilesSystems/MountainTurtle" or manifest["tag"] != f"v{version}":
    raise SystemExit("Unexpected release repository or tag.")
if not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
    raise SystemExit("Invalid source commit.")
if manifest["preview"] != preview or manifest["notarized"] == preview:
    raise SystemExit("Pass --preview only for an explicitly prepared, unnotarized preview.")
expected = {"release.json", "appcast.xml", f"MountainTurtle-{version}.zip", f"MountainTurtle-{version}.md"}
if {p.name for p in directory.iterdir()} != expected | {"SHA256SUMS"}:
    raise SystemExit("Release directory has missing or unexpected assets.")
seen = set()
for line in (directory / "SHA256SUMS").read_text().splitlines():
    digest, name = line.split("  ", 1)
    if name not in expected or name in seen or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("Invalid checksum manifest.")
    path = directory / name
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"Release asset checksum mismatch: {name}")
    seen.add(name)
if seen != expected:
    raise SystemExit("Checksum manifest does not cover every release asset.")
namespace = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
items = ET.parse(directory / "appcast.xml").findall("./channel/item")
if len(items) != 1 or items[0].findtext(namespace + "version") != version:
    raise SystemExit("Appcast version does not match the release.")
enclosure = items[0].find("enclosure")
archive = directory / f"MountainTurtle-{version}.zip"
if enclosure is None or enclosure.get("url") != f"https://github.com/MilesSystems/MountainTurtle/releases/download/v{version}/{archive.name}":
    raise SystemExit("Appcast URL does not match this release.")
if int(enclosure.get("length", "0")) != archive.stat().st_size or not enclosure.get(namespace + "edSignature"):
    raise SystemExit("Appcast has an invalid archive size or missing signature.")
print(manifest["tag"])
print(manifest["commit"])
print(manifest["title"])
print(enclosure.get(namespace + "edSignature"))
PY
TAG=$(sed -n '1p' "$WORK/metadata")
COMMIT=$(sed -n '2p' "$WORK/metadata")
TITLE=$(sed -n '3p' "$WORK/metadata")
SIGNATURE=$(sed -n '4p' "$WORK/metadata")
[[ "$(git rev-parse HEAD)" == "$COMMIT" && -z "$(git status --porcelain --untracked-files=all)" ]] || {
    echo "Publish from the clean commit used to prepare this release: $COMMIT" >&2; exit 1;
}
case "$(git remote get-url origin)" in
    git@github.com:MilesSystems/MountainTurtle.git|https://github.com/MilesSystems/MountainTurtle.git|https://github.com/MilesSystems/MountainTurtle) ;;
    *) echo "origin must be the official MilesSystems/MountainTurtle GitHub repository." >&2; exit 1 ;;
esac
git ls-remote --tags origin "refs/tags/$TAG" "refs/tags/$TAG^{}" > "$WORK/remote-tag"
"$PYTHON_BIN" - "$WORK/remote-tag" "$TAG" "$COMMIT" <<'PY'
from pathlib import Path
import sys
refs = dict(line.split()[::-1] for line in Path(sys.argv[1]).read_text().splitlines())
tag = "refs/tags/" + sys.argv[2]
if refs.get(tag + "^{}", refs.get(tag)) != sys.argv[3]:
    raise SystemExit("Push the exact release commit and version tag before publishing. Remote tag is missing or points to another commit.")
PY
SPARKLE_DIR=$("$PROJECT_DIR/scripts/fetch-sparkle.sh")
[[ "$("$SPARKLE_DIR/bin/generate_keys" --account io.mountainturtle.app -p)" == "$(cat Resources/SparklePublicKey.txt)" ]] || {
    echo "Sparkle Keychain public key differs from the committed public key." >&2; exit 1;
}
"$SPARKLE_DIR/bin/sign_update" --account io.mountainturtle.app --verify "$RELEASE_DIR/appcast.xml"
"$SPARKLE_DIR/bin/sign_update" --account io.mountainturtle.app --verify "$RELEASE_DIR/MountainTurtle-${TAG#v}.zip" "$SIGNATURE"
gh release list --repo "$REPO" --limit 1000 --json tagName > "$WORK/releases.json"
"$PYTHON_BIN" - "$WORK/releases.json" "$TAG" <<'PY'
from pathlib import Path
import json
import re
import sys
releases = json.loads(Path(sys.argv[1]).read_text())
if any(item["tagName"] == sys.argv[2] for item in releases):
    raise SystemExit("This GitHub release already exists. Inspect it before recovery; published assets are never replaced by this script.")
current = tuple(map(int, sys.argv[2][1:].split(".")))
for item in releases:
    tag = item["tagName"]
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) and tuple(map(int, tag[1:].split("."))) >= current:
        raise SystemExit(f"Release {tag} already has an equal or newer version. Increase VERSION before updating the latest feed.")
PY

# Upload as a draft, verify every downloaded byte, then expose the complete release.
# A failure after creation deliberately leaves a draft for explicit recovery.
gh release create "$TAG" "$RELEASE_DIR"/* --repo "$REPO" --verify-tag --draft \
    --title "$TITLE" --notes-file "$RELEASE_DIR/MountainTurtle-${TAG#v}.md"
mkdir "$WORK/downloaded"
gh release download "$TAG" --repo "$REPO" --dir "$WORK/downloaded"
"$PYTHON_BIN" - "$RELEASE_DIR" "$WORK/downloaded" <<'PY'
from pathlib import Path
import sys
source, downloaded = map(Path, sys.argv[1:])
if {p.name for p in source.iterdir()} != {p.name for p in downloaded.iterdir()}:
    raise SystemExit("Uploaded release asset names differ; draft remains unpublished.")
for asset in source.iterdir():
    if asset.read_bytes() != (downloaded / asset.name).read_bytes():
        raise SystemExit(f"Uploaded asset differs: {asset.name}; draft remains unpublished.")
PY
# GitHub excludes prereleases from /releases/latest. Preview status is disclosed
# in title/notes; it remains a normal release to serve the configured update feed.
gh release edit "$TAG" --repo "$REPO" --draft=false --latest
"$PYTHON_BIN" -B "$PROJECT_DIR/scripts/verify-published-release.py" "$RELEASE_DIR" --attempts 6
gh release view "$TAG" --repo "$REPO" --json url --jq .url
