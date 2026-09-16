#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE_APP="$PROJECT_DIR/build/Mountain Turtle.app"
APPLICATIONS_DIR="$HOME/Applications"
DESTINATION_APP="$APPLICATIONS_DIR/Mountain Turtle.app"
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}

if /usr/bin/pgrep -x "Mountain Turtle" >/dev/null 2>&1; then
    echo "Quit Mountain Turtle before installing. Your current app has not been changed." >&2
    exit 1
fi
if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3 is required to install Mountain Turtle." >&2
    exit 1
fi
if [[ ! -d "$SOURCE_APP" ]]; then
    "$PROJECT_DIR/scripts/build.sh"
fi
/usr/bin/codesign --verify --deep --strict "$SOURCE_APP"
mkdir -p -- "$APPLICATIONS_DIR"

# Stage and verify first. Archive existing installations so macOS does not
# register backup copies as competing app identities. Keep the old directory
# available for rollback until the final rename succeeds.
"$PYTHON_BIN" - "$SOURCE_APP" "$DESTINATION_APP" <<'PY'
from datetime import datetime
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

source, destination = map(Path, sys.argv[1:])
stage = Path(tempfile.mkdtemp(prefix=".mountainturtle-install-", dir=destination.parent))
backup_directory = Path.home() / "Library/Application Support/Mountain Turtle/Backups"
backup = None
previous = None

def copy_raw(src, dst):
    src, dst = Path(src), Path(dst)
    dst.write_bytes(src.read_bytes())
    os.chmod(dst, src.stat().st_mode & 0o777)
    return str(dst)

try:
    prepared = stage / destination.name
    shutil.copytree(source, prepared, copy_function=copy_raw, symlinks=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(prepared)], check=True)
    if subprocess.run(["/usr/bin/pgrep", "-x", "Mountain Turtle"], stdout=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError("Mountain Turtle started during installation. Quit it and run this installer again.")
    if destination.exists() or destination.is_symlink():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_directory / f"Mountain Turtle.backup-{stamp}.zip"
        pending = stage / "previous.zip"
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(destination), str(pending)], check=True)
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip() is not None or destination.name + "/Contents/Info.plist" not in archive.namelist():
                raise RuntimeError("Could not verify the previous app archive. The installed app has not been changed.")
        pending.chmod(0o600)
        pending.rename(backup)
        # Archiving takes time. Check again before touching the running app.
        if subprocess.run(["/usr/bin/pgrep", "-x", "Mountain Turtle"], stdout=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError("Mountain Turtle started during backup. Quit it and run this installer again.")
        previous = stage / "previous.app"
        destination.rename(previous)
    try:
        prepared.rename(destination)
    except BaseException:
        if previous is not None and not destination.exists():
            previous.rename(destination)
        raise
    print(f"Installed: {destination}")
    if backup is not None:
        print(f"Previous app preserved: {backup}")
finally:
    shutil.rmtree(stage)

# Refresh only the installed app after the old bundle leaves staging. macOS
# network privacy can otherwise retain the previous executable's build UUID.
try:
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                    "-f", str(destination)], check=True, timeout=15,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
except (OSError, subprocess.SubprocessError):
    print("Warning: The app was installed, but macOS app registration could not be refreshed. Open the installed app from Applications and check Local Network access in System Settings if needed.", file=sys.stderr)
PY

echo "The app has not been launched. Open it from your Applications folder when ready."
echo "Python 3, rclone with nfsmount, and AWS CLI v2 must be installed separately."
