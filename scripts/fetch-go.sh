#!/bin/bash
set -euo pipefail
PROJECT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=1.27.1
CACHE="$PROJECT_DIR/build/toolchain"
case "$(uname -m)" in
    arm64) GO_ARCH=arm64; SHA=ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12 ;;
    x86_64) GO_ARCH=amd64; SHA=8f8f52c6649542cf027bbc9b9c68d1ec042f9f34808a40413f0b8b3f66f3caa4 ;;
    *) echo 'The mount engine build requires a Mac.' >&2; exit 1 ;;
esac
if [[ ! -x "$CACHE/go/bin/go" ]] || [[ "$("$CACHE/go/bin/go" version)" != "go version go$VERSION darwin/$GO_ARCH" ]]; then
    mkdir -p "$CACHE"
    ARCHIVE="$CACHE/go$VERSION.darwin-$GO_ARCH.tar.gz"
    if [[ ! -f "$ARCHIVE" ]]; then
        /usr/bin/curl --fail --location --retry 3 "https://go.dev/dl/go$VERSION.darwin-$GO_ARCH.tar.gz" -o "$ARCHIVE.download"
        mv "$ARCHIVE.download" "$ARCHIVE"
    fi
    ACTUAL=$(/usr/bin/shasum -a 256 "$ARCHIVE" | /usr/bin/awk '{print $1}')
    [[ "$ACTUAL" = "$SHA" ]] || { echo 'Go archive checksum mismatch.' >&2; exit 1; }
    /usr/bin/tar -xzf "$ARCHIVE" -C "$CACHE"
fi
printf '%s\n' "$CACHE/go/bin/go"
