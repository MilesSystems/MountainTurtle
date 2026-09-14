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

# Stage and verify first. Existing installations are moved to a unique backup;
# they are never deleted, and are restored if the final rename fails.
"$PYTHON_BIN" - "$SOURCE_APP" "$DESTINATION_APP" <<'PY'
from datetime import datetime
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

source, destination = map(Path, sys.argv[1:])
stage = Path(tempfile.mkdtemp(prefix=".mountainturtle-install-", dir=destination.parent))
backup = None

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
        backup = destination.with_name(f"Mountain Turtle.backup-{stamp}.app")
        destination.rename(backup)
    try:
        prepared.rename(destination)
    except BaseException:
        if backup is not None and not destination.exists():
            backup.rename(destination)
        raise
    print(f"Installed: {destination}")
    if backup is not None:
        print(f"Previous app preserved: {backup}")
finally:
    shutil.rmtree(stage)
PY

echo "The app has not been launched. Open it from your Applications folder when ready."
echo "Python 3, rclone with nfsmount, and AWS CLI v2 must be installed separately."
