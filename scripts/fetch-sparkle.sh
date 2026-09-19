#!/bin/bash
set -euo pipefail

# Pin both version and bytes. Never silently consume a newer updater framework.
PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
SPARKLE_VERSION=2.10.0
SPARKLE_SHA256=c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c
DEPENDENCY_DIR="$PROJECT_DIR/build/dependencies"
ARCHIVE="$DEPENDENCY_DIR/Sparkle-$SPARKLE_VERSION.tar.xz"
DISTRIBUTION="$DEPENDENCY_DIR/Sparkle-$SPARKLE_VERSION"
mkdir -p -- "$DEPENDENCY_DIR"

if [[ ! -f "$ARCHIVE" ]]; then
    DOWNLOAD=$(mktemp "$DEPENDENCY_DIR/.sparkle-download.XXXXXX")
    trap 'rm -f -- "${DOWNLOAD:-}"' EXIT
    /usr/bin/curl --fail --location --retry 3 --proto '=https' --proto-redir '=https' \
        "https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/Sparkle-$SPARKLE_VERSION.tar.xz" \
        --output "$DOWNLOAD" >&2
    ACTUAL=$(/usr/bin/shasum -a 256 "$DOWNLOAD" | /usr/bin/awk '{print $1}')
    if [[ "$ACTUAL" != "$SPARKLE_SHA256" ]]; then
        echo "Sparkle download failed its pinned SHA-256 check." >&2
        exit 1
    fi
    mv -- "$DOWNLOAD" "$ARCHIVE"
fi
ACTUAL=$(/usr/bin/shasum -a 256 "$ARCHIVE" | /usr/bin/awk '{print $1}')
if [[ "$ACTUAL" != "$SPARKLE_SHA256" ]]; then
    echo "Cached Sparkle archive failed its pinned SHA-256 check: $ARCHIVE" >&2
    exit 1
fi
if [[ ! -d "$DISTRIBUTION/Sparkle.framework" || ! -x "$DISTRIBUTION/bin/generate_appcast" ]]; then
    EXTRACTED=$(mktemp -d "$DEPENDENCY_DIR/.sparkle-extract.XXXXXX")
    trap 'rm -rf -- "${EXTRACTED:-}"; rm -f -- "${DOWNLOAD:-}"' EXIT
    /usr/bin/tar -xJf "$ARCHIVE" -C "$EXTRACTED"
    [[ -d "$EXTRACTED/Sparkle.framework" && -x "$EXTRACTED/bin/generate_appcast" ]]
    rm -rf -- "$DISTRIBUTION"
    mv -- "$EXTRACTED" "$DISTRIBUTION"
fi
printf '%s\n' "$DISTRIBUTION"
