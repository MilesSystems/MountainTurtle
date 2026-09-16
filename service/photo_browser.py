#!/usr/bin/env python3
"""Bounded, read-only S3 photo browsing and local thumbnail generation."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote

import turtle_service as turtle

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".heic", ".heif", ".avif", ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf"}
HEADER_BYTES = 128 * 1024
MAX_THUMB_ORIGINAL = 32 * 1024 * 1024
CACHE_BYTES = 256 * 1024 * 1024
CACHE_FILES = 2048


class BrowserError(Exception):
    pass


class Cancelled(BrowserError):
    pass


def cancel(*_):
    raise Cancelled("Photo request cancelled.")


def run_process(command, env=None, timeout=90):
    """A cancelled CLI owns and reaps its subprocesses, including AWS helpers."""
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    if process.returncode:
        if turtle.AUTH_ERRORS.search(errors.decode(errors="replace")):
            raise BrowserError("AWS sign-in expired. Sign in to this connection's profile and try again.")
        if b"PreconditionFailed" in errors or b"412" in errors:
            raise BrowserError("This photo changed in S3. Refresh the folder before opening it.")
        raise BrowserError("The photo request failed. Check the connection's access and try again.")
    return output


class AWSReader:
    def __init__(self, connection, paths):
        self.connection, self.paths = connection, paths

    def request(self, operation, fields, destination=None):
        aws = turtle.executable("aws")
        if not aws:
            raise BrowserError("Install AWS CLI v2 to browse photos.")
        if operation not in ("list-objects-v2", "get-object"):
            raise BrowserError("Only read operations are supported.")
        payload = dict(fields, Bucket=self.connection["bucket"])
        command = [aws, "s3api", operation]
        if operation == "get-object":
            # AWS CLI's streaming get-object command validates required named
            # options before --cli-input-json can provide them. Keep GET fields
            # explicit; '=' also safely preserves keys beginning with '-'.
            command += ["--bucket=" + payload["Bucket"], "--key=" + payload["Key"]]
            for field, option in (("IfMatch", "--if-match="), ("Range", "--range=")):
                if field in payload:
                    command.append(option + payload[field])
        else:
            command += ["--cli-input-json", json.dumps(payload)]
        command += ["--profile", self.connection["profile"], "--region", self.connection["region"],
                   "--no-paginate", "--output", "json", "--cli-connect-timeout", "5", "--cli-read-timeout", "30"]
        if destination is not None:
            command.append(str(destination))
        env = turtle.environment(self.connection["profile"], self.paths)
        env["AWS_MAX_ATTEMPTS"] = "2"
        result = run_process(command, env, timeout=1800 if destination is not None and "Range" not in payload else 90)
        return json.loads(result)

    def list(self, prefix, cursor, limit):
        fields = {"Prefix": prefix, "Delimiter": "/", "MaxKeys": limit, "EncodingType": "url"}
        if cursor:
            fields["ContinuationToken"] = cursor
        return self.request("list-objects-v2", fields)

    def get(self, key, etag, destination, byte_range=None):
        fields = {"Key": key}
        if etag:
            fields["IfMatch"] = etag
        if byte_range:
            fields["Range"] = byte_range
        return self.request("get-object", fields, destination)


def checked_key(value, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty) or len(value.encode("utf-8")) > 1024 or "\x00" in value:
        raise BrowserError("Invalid S3 object key or prefix.")
    return value


def limited_json(path, limit):
    with path.open("rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Local cache metadata exceeds its size limit.")
    return json.loads(data)


def jpeg_thumbnail(data):
    """Extract only a bounded EXIF IFD1 JPEG; never decode the full photo."""
    if not data.startswith(b"\xff\xd8"):
        return None
    position = 2
    try:
        while position + 4 <= len(data):
            if data[position] != 0xff:
                return None
            marker = data[position + 1]
            if marker in (0xda, 0xd9):
                return None
            if marker == 0xff:
                position += 1
                continue
            size = struct.unpack_from(">H", data, position + 2)[0]
            if size < 2 or position + 2 + size > len(data):
                return None
            segment = data[position + 4:position + 2 + size]
            position += size + 2
            if marker != 0xe1 or not segment.startswith(b"Exif\x00\x00"):
                continue
            tiff = segment[6:]
            order = "<" if tiff[:2] == b"II" else ">" if tiff[:2] == b"MM" else None
            if not order or struct.unpack_from(order + "H", tiff, 2)[0] != 42:
                continue
            offset = struct.unpack_from(order + "I", tiff, 4)[0]
            count = struct.unpack_from(order + "H", tiff, offset)[0]
            if count > 512:
                return None
            second = struct.unpack_from(order + "I", tiff, offset + 2 + count * 12)[0]
            if not second:
                return None
            count = struct.unpack_from(order + "H", tiff, second)[0]
            if count > 512:
                return None
            values = {}
            for index in range(count):
                tag, kind, number, value = struct.unpack_from(order + "HHII", tiff, second + 2 + index * 12)
                if kind == 4 and number == 1 and tag in (0x0201, 0x0202):
                    values[tag] = value
            start, length = values.get(0x0201, 0), values.get(0x0202, 0)
            if not start or not length or length > HEADER_BYTES or start + length > len(tiff):
                return None
            thumbnail = tiff[start:start + length]
            return thumbnail if thumbnail.startswith(b"\xff\xd8") and thumbnail.endswith(b"\xff\xd9") else None
    except (struct.error, IndexError, OverflowError):
        return None
    return None


def render_thumbnail(source, destination, pixels):
    if not Path("/usr/bin/sips").is_file():
        raise BrowserError("Thumbnail rendering requires macOS sips.")
    run_process(["/usr/bin/sips", "--resampleHeightWidthMax", str(pixels), "--setProperty", "format", "jpeg",
                 str(source), "--out", str(destination)], timeout=45)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise BrowserError("This image format could not be previewed.")


class PhotoBrowser:
    def __init__(self, connection, paths, reader=None, renderer=render_thumbnail):
        if connection.get("backend", "s3") != "s3":
            raise BrowserError("The photo browser is available for S3 drives. Open this SFTP drive in Finder to browse its files.")
        self.connection, self.paths = connection, paths
        self.reader = reader or AWSReader(connection, paths)
        self.renderer = renderer
        self.base = paths.cache / "photo-browser"
        self.artifacts = self.base / "artifacts"
        for folder in (self.base, self.artifacts):
            self.safe_path(folder)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def safe_path(self, path):
        try:
            relative = path.relative_to(self.paths.home)
        except ValueError:
            raise BrowserError("Photo files must stay in this user's local folders.")
        if ".." in relative.parts or self.paths.home.is_symlink():
            raise BrowserError("Photo cache and download paths must not use symbolic links.")
        current = self.paths.home
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise BrowserError("Photo cache and download paths must not use symbolic links.")
        return path

    def save_artifact(self, target, value):
        self.safe_path(target)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.artifacts, prefix=".saving-", delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(value, file)
                file.flush()
                self.safe_path(target)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def identity(self, *parts):
        scope = [self.connection[k] for k in ("id", "bucket", "profile", "region")]
        return hashlib.sha256(json.dumps(scope + list(parts), ensure_ascii=False).encode()).hexdigest()

    @contextlib.contextmanager
    def slot(self):
        deadline = time.monotonic() + 90
        while True:
            for number in range(2):
                handle = self.safe_path(self.base / ("worker-%s.lock" % number)).open("a+")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    handle.close()
                return
            if time.monotonic() > deadline:
                raise BrowserError("Two photo downloads are already running. Try again shortly.")
            time.sleep(0.1)

    @contextlib.contextmanager
    def key_lock(self, digest):
        # Fixed stripes keep lock files bounded while collapsing duplicate work.
        with self.safe_path(self.base / ("key-%s.lock" % (int(digest[:4], 16) % 64))).open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def trim(self):
        with self.safe_path(self.base / "trim.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            files = []
            for file in self.artifacts.iterdir():
                try:
                    if not file.name.startswith(".saving-") and file.is_file() and not file.is_symlink():
                        stat = file.stat()
                        files.append((stat.st_mtime, stat.st_size, file))
                except FileNotFoundError:
                    pass
            total = sum(size for _, size, _ in files)
            for index, (_, size, file) in enumerate(sorted(files)):
                if total <= CACHE_BYTES and len(files) - index <= CACHE_FILES:
                    break
                file.unlink(missing_ok=True)
                total -= size

    def list(self, prefix="", cursor=None, limit=100):
        checked_key(prefix, allow_empty=True)
        if not 1 <= limit <= 200 or (cursor and (len(cursor) > 8192 or "\x00" in cursor)):
            raise BrowserError("Invalid page size or continuation token.")
        result = self.reader.list(prefix, cursor, limit)
        decode = unquote if result.get("EncodingType") == "url" else lambda value: value
        folders, photos, hidden = [], [], 0
        for item in result.get("CommonPrefixes", []):
            key = decode(item["Prefix"])
            if key.startswith(prefix) and key != prefix:
                folders.append({"key": key, "name": key[len(prefix):].rstrip("/")})
        for item in result.get("Contents", []):
            key = decode(item["Key"])
            if not key.startswith(prefix) or "/" in key[len(prefix):] or key == prefix:
                continue
            if PurePosixPath(key).suffix.lower() not in PHOTO_EXTENSIONS:
                hidden += 1
                continue
            photos.append({"key": key, "name": key[len(prefix):], "size": item["Size"], "etag": item.get("ETag", "")})
        return {"ok": True, "prefix": prefix, "folders": folders, "photos": photos,
                "nextCursor": result.get("NextContinuationToken") if result.get("IsTruncated") else None,
                "hiddenFileCount": hidden}

    def cached_original(self, key, etag, size):
        downloaded = self.artifacts / (self.identity(key, etag, size) + ".original")
        try:
            info = limited_json(self.safe_path(downloaded), 64 * 1024)
            source = Path(info["path"])
            allowed = self.paths.home / "Downloads/Mountain Turtle" / self.connection["bucket"]
            stat = self.safe_path(source).stat()
            if (source.resolve().is_relative_to(allowed.resolve()) and not source.is_symlink()
                    and stat.st_size == size and stat.st_mtime_ns == info["mtimeNs"]):
                return source
        except (OSError, ValueError, TypeError, KeyError):
            pass
        relative = PurePosixPath(key)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in key.split("/")) or "\\" in key or not etag:
            return None
        root = self.paths.cache / self.connection["id"]
        for remote in (Path("volume"), Path("s3") / self.connection["bucket"]):
            source = root / "vfs" / remote / key
            metadata = root / "vfsMeta" / remote / key
            try:
                self.safe_path(source)
                self.safe_path(metadata)
                if not source.resolve().is_relative_to(root.resolve()) or not metadata.resolve().is_relative_to(root.resolve()):
                    continue
                info = limited_json(metadata, 1024 * 1024)
                if info.get("Dirty") or info.get("Size") != size or source.stat().st_size != size:
                    continue
                if info.get("Fingerprint", "").rsplit(",", 1)[-1] != etag.strip('"'):
                    continue
                covered = 0
                for block in sorted(info.get("Rs", []), key=lambda value: value["Pos"]):
                    if block["Pos"] > covered:
                        break
                    covered = max(covered, block["Pos"] + block["Size"])
                if covered >= size:
                    return source
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return None

    def validate_photo(self, key, etag, size):
        checked_key(key)
        if not isinstance(size, int) or size <= 0 or size > 5 * 1024**4:
            raise BrowserError("The photo size is missing or invalid. Refresh the folder.")
        if not isinstance(etag, str) or not etag.strip('" ') or len(etag) > 256 or any(ord(c) < 32 for c in etag):
            raise BrowserError("The photo version is invalid. Refresh the folder.")

    def thumbnail(self, key, etag, size, pixels=256, allow_original=False, cache_only=False):
        self.validate_photo(key, etag, size)
        if not 64 <= pixels <= 1024:
            raise BrowserError("Thumbnail size must be between 64 and 1024 pixels.")
        digest = self.identity(key, etag, size, pixels)
        target = self.artifacts / (digest + ".jpg")
        unavailable = self.artifacts / (digest + ".unavailable")
        with self.key_lock(digest):
            self.safe_path(target)
            self.safe_path(unavailable)
            if target.is_file():
                os.utime(target, None)
                return {"ok": True, "thumbnailPath": str(target), "source": "thumbnail-cache", "downloadedBytes": 0, "needsOriginal": False}
            source = self.cached_original(key, etag, size)
            downloaded = 0
            origin = "original-cache"
            with self.slot(), tempfile.TemporaryDirectory(prefix="work-", dir=self.base) as work:
                work = Path(work)
                if source is None and not cache_only:
                    if PurePosixPath(key).suffix.lower() in (".jpg", ".jpeg") and not unavailable.exists():
                        header = work / "header.jpg"
                        self.reader.get(key, etag, header, "bytes=0-%s" % (min(size, HEADER_BYTES) - 1))
                        downloaded += header.stat().st_size
                        embedded = jpeg_thumbnail(header.read_bytes())
                        if embedded:
                            source = work / "embedded.jpg"
                            source.write_bytes(embedded)
                            origin = "embedded-jpeg"
                        elif header.stat().st_size == size and size <= HEADER_BYTES:
                            source = header
                            origin = "bounded-jpeg"
                    if source is None and allow_original and size <= MAX_THUMB_ORIGINAL:
                        source = work / ("source" + PurePosixPath(key).suffix)
                        self.reader.get(key, etag, source)
                        downloaded += source.stat().st_size
                        if source.stat().st_size != size:
                            raise BrowserError("The original download was incomplete. Try again.")
                        origin = "downloaded-original"
                if source is None:
                    if not cache_only:
                        self.save_artifact(unavailable, {"needsOriginal": True})
                        self.trim()
                    return {"ok": True, "thumbnailPath": None, "source": "unavailable", "downloadedBytes": downloaded,
                            "needsOriginal": True, "message": "No cached or embedded preview. Open or download the original to preview it."}
                rendered = work / "thumbnail.jpg"
                self.renderer(source, rendered, pixels)
                self.safe_path(target)
                rendered.replace(target)
                unavailable.unlink(missing_ok=True)
                self.trim()
                return {"ok": True, "thumbnailPath": str(target), "source": origin, "downloadedBytes": downloaded, "needsOriginal": False}

    def open_original(self, key, etag, size):
        self.validate_photo(key, etag, size)
        digest = self.identity(key, etag, size)
        name = re.sub(r"[^\w .()-]", "_", PurePosixPath(key).name, flags=re.UNICODE).strip(" .") or "photo"
        suffix = PurePosixPath(name).suffix[:12]
        stem = name[:-len(suffix)] if suffix else name
        name = stem.encode("utf-8")[:140 - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore") + suffix
        directory = self.paths.home / "Downloads/Mountain Turtle" / self.connection["bucket"]
        self.safe_path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (digest[:16] + "-" + name)
        marker = self.artifacts / (digest + ".original")
        with self.key_lock(digest):
            self.safe_path(target)
            self.safe_path(marker)
            source = self.cached_original(key, etag, size)
            if source and source.resolve().is_relative_to(directory.resolve()):
                return {"ok": True, "originalPath": str(source), "source": "download-cache", "downloadedBytes": 0}
            if target.exists():
                # Never replace a user's locally edited or untracked download.
                target = directory / (digest[:16] + "-" + str(time.time_ns()) + "-" + name)
            if shutil.disk_usage(directory).free < size + 64 * 1024 * 1024:
                raise BrowserError("There is not enough free space to download this original.")
            with self.slot(), tempfile.TemporaryDirectory(prefix=".download-", dir=directory) as work:
                temporary = Path(work) / "original"
                if source:
                    shutil.copyfile(source, temporary)
                else:
                    self.reader.get(key, etag, temporary)
                if temporary.stat().st_size != size:
                    raise BrowserError("The original download was incomplete. Try again.")
                self.safe_path(target)
                temporary.replace(target)
                self.save_artifact(marker, {"path": str(target), "mtimeNs": target.stat().st_mtime_ns})
                self.trim()
                return {"ok": True, "originalPath": str(target), "source": "original-cache" if source else "downloaded-original",
                        "downloadedBytes": 0 if source else size}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--resource-dir")
    commands = result.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("id")
    listing.add_argument("--prefix", default="")
    listing.add_argument("--cursor")
    listing.add_argument("--limit", type=int, default=100)
    for operation in ("thumbnail", "open-original"):
        command = commands.add_parser(operation)
        command.add_argument("id")
        command.add_argument("--key", required=True)
        command.add_argument("--etag", required=True)
        command.add_argument("--size", required=True, type=int)
        if operation == "thumbnail":
            command.add_argument("--pixels", type=int, default=256)
            command.add_argument("--allow-original", action="store_true")
            command.add_argument("--cache-only", action="store_true")
    return result


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        args = parser().parse_args()
        paths = turtle.Paths(resources=args.resource_dir)
        connection = turtle.find_connection(turtle.Store(paths).read(), args.id)
        browser = PhotoBrowser(connection, paths)
        if args.command == "list":
            response = browser.list(args.prefix, args.cursor, args.limit)
        elif args.command == "thumbnail":
            response = browser.thumbnail(args.key, args.etag, args.size, args.pixels, args.allow_original, args.cache_only)
        else:
            response = browser.open_original(args.key, args.etag, args.size)
        print(json.dumps(response, ensure_ascii=False))
    except Exception as error:
        message = str(error) if isinstance(error, (BrowserError, ValueError)) else "The photo request could not finish. Try again."
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        raise SystemExit(130 if isinstance(error, Cancelled) else 1)


if __name__ == "__main__":
    main()
