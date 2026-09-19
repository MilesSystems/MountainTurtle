#!/usr/bin/env python3
"""Verify the public updater URLs against the prepared, signed release assets."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request


RELEASES = "https://github.com/MilesSystems/MountainTurtle/releases"


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def remote_digest(url, expected_size):
    request = urllib.request.Request(url, headers={"User-Agent": "MountainTurtle-Release-Verification"})
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=30) as response:
        if not response.geturl().startswith("https://"):
            raise RuntimeError("Release download redirected outside HTTPS.")
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > expected_size:
                raise RuntimeError("Public release asset is larger than the prepared asset.")
            digest.update(block)
    if size != expected_size:
        raise RuntimeError("Public release download is incomplete.")
    return digest.hexdigest()


def verify(directory, fetch=remote_digest):
    """Local hashes are the trust anchor; never trust a downloaded hash manifest."""
    manifest = json.loads((directory / "release.json").read_text())
    version = manifest.get("version", "")
    if (not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or manifest.get("repository") != "MilesSystems/MountainTurtle"
            or manifest.get("tag") != "v" + version):
        raise ValueError("Invalid prepared release manifest.")
    expected_names = {"release.json", "appcast.xml", f"MountainTurtle-{version}.zip",
                      f"MountainTurtle-{version}.md"}
    checksums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if (name not in expected_names or name in checksums
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid prepared checksum manifest.")
        checksums[name] = digest
    if checksums.keys() != expected_names:
        raise ValueError("Prepared checksum manifest is incomplete.")
    for name, digest in checksums.items():
        path = directory / name
        if path.is_symlink() or digest_file(path) != digest:
            raise ValueError(f"Prepared asset changed: {name}")

    # Verify the actual URL embedded in installed apps first. A tag-only check
    # would miss a prerelease or a newer release accidentally owning /latest.
    feed = directory / "appcast.xml"
    latest_url = RELEASES + "/latest/download/appcast.xml"
    if fetch(latest_url, feed.stat().st_size) != checksums[feed.name]:
        raise RuntimeError("The public update feed does not match this prepared release.")
    for name in sorted(expected_names):
        path = directory / name
        url = RELEASES + f"/download/v{version}/{name}"
        if fetch(url, path.stat().st_size) != checksums[name]:
            raise RuntimeError(f"Public release asset differs from the prepared asset: {name}")
    return RELEASES + f"/tag/v{version}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--attempts", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.attempts <= 6:
        parser.error("Use between one and six attempts.")
    for attempt in range(args.attempts):
        try:
            url = verify(args.directory)
            print(f"Public download and installed-app update feed verified: {url}")
            return 0
        except (urllib.error.URLError, RuntimeError) as error:
            if attempt + 1 == args.attempts:
                print(f"Public release delivery is not verified: {error}", file=sys.stderr)
                print("Do not replace published assets. Retry this verification after GitHub finishes serving them.", file=sys.stderr)
                return 1
            time.sleep(5)
        except (OSError, ValueError, KeyError) as error:
            print(f"Cannot verify prepared release: {error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
