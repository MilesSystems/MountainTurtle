#!/usr/bin/env python3
"""Durable, read-only S3 photo downloads outside Mountain Turtle's disposable cache."""

import argparse
import base64
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
import zlib

import turtle_service as turtle

CHUNK_BYTES = 8 * 1024 * 1024
RESERVE_BYTES = 64 * 1024 * 1024
MAX_ITEMS = 10000
MAX_QUEUE_BYTES = 16 * 1024 * 1024
COMPLETE = {"downloaded", "verified"}
_stop_requested = False


class OfflineError(ValueError):
    pass


class Paused(OfflineError):
    pass


def cancel(*_):
    # Do not interrupt between fsync of a prefix and its durable journal commit.
    global _stop_requested
    _stop_requested = True


def regular_file(path, flags=os.O_RDONLY, mode=0o600, links=1):
    try:
        descriptor = os.open(path, flags | os.O_NOFOLLOW, mode)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise OfflineError("An offline file is an unsafe symbolic link. Its contents were preserved.") from None
        raise
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != links:
        os.close(descriptor)
        raise OfflineError("An offline file has an unsafe type or owner. Its contents were preserved.")
    return os.fdopen(descriptor, "r+b" if flags & (os.O_RDWR | os.O_WRONLY) else "rb")


def private_directory(path, home):
    """Reject symlinks throughout our home-relative subtree, not just its leaf."""
    path, home = Path(path), Path(home)
    try:
        pieces = path.relative_to(home).parts
    except ValueError:
        raise OfflineError("The offline folder must be inside your home folder.") from None
    current = home
    for piece in pieces:
        current /= piece
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OfflineError("The offline folder contains an unsafe link or owner. No files were changed.")
    path.chmod(0o700)


def checked_identity(item):
    if not isinstance(item, dict):
        raise OfflineError("Select valid photos to keep offline.")
    key, etag, size = item.get("key"), item.get("etag"), item.get("size")
    if (not isinstance(key, str) or not key or "\x00" in key or len(key.encode("utf-8")) > 1024
            or not isinstance(etag, str) or not etag.strip().strip('"') or len(etag) > 256
            or any(ord(c) < 32 for c in etag) or type(size) is not int or size < 0 or size > 5 * 1024**4):
        raise OfflineError("A photo's version or size is missing. Refresh the folder and select it again.")
    return {"key": key, "etag": etag, "size": size}


def item_id(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_name(key):
    name = PurePosixPath(key).name or "photo"
    name = re.sub(r"[\x00-\x1f\x7f/:]", "_", name).lstrip(".") or "photo"
    return name.encode("utf-8")[:120].decode("utf-8", "ignore")


def crc32c_table():
    values = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
        values.append(crc)
    return values


CRC32C_TABLE = crc32c_table()


def crc32c_update(crc, data):
    for value in data:
        crc = CRC32C_TABLE[(crc ^ value) & 255] ^ (crc >> 8)
    return crc


def hash_file(path, cancelled=None, need_crc32c=False, links=1):
    sha256, sha1, crc32, crc32c, size = hashlib.sha256(), hashlib.sha1(), 0, 0xffffffff, 0
    with regular_file(path, links=links) as source:
        before = os.fstat(source.fileno())
        while True:
            if cancelled:
                cancelled()
            block = source.read(1024 * 1024)
            if not block:
                break
            sha256.update(block)
            sha1.update(block)
            crc32 = zlib.crc32(block, crc32)
            if need_crc32c:
                crc32c = crc32c_update(crc32c, block)
            size += len(block)
        after = os.fstat(source.fileno())
    current = path.lstat()
    if ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
            != (current.st_ino, current.st_mtime_ns, current.st_ctime_ns)):
        raise OfflineError("The local photo changed during verification. It was preserved; no download replaced it.")
    return {"size": size, "sha256": sha256.hexdigest(), "hasher": sha256, "stat": current,
            "ChecksumSHA256": base64.b64encode(sha256.digest()).decode(),
            "ChecksumSHA1": base64.b64encode(sha1.digest()).decode(),
            "ChecksumCRC32": base64.b64encode((crc32 & 0xffffffff).to_bytes(4, "big")).decode(),
            "ChecksumCRC32C": base64.b64encode((crc32c ^ 0xffffffff).to_bytes(4, "big")).decode()}


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AWSReader:
    def __init__(self, connection, paths, cancelled):
        self.connection, self.paths, self.cancelled = connection, paths, cancelled

    def request(self, operation, key, etag, destination=None, byte_range=None, checksum=True):
        aws = turtle.executable("aws")
        if not aws:
            raise OfflineError("Install AWS CLI v2 to download photos.")
        command = [aws, "s3api", operation, "--bucket=" + self.connection["bucket"], "--key=" + key,
                   "--if-match=" + etag, "--profile", self.connection["profile"],
                   "--region", self.connection["region"], "--output", "json", "--no-cli-pager",
                   "--cli-connect-timeout", "5", "--cli-read-timeout", "30"]
        if checksum:
            command += ["--checksum-mode", "ENABLED"]
        if byte_range:
            command += ["--range=" + byte_range]
        if destination is not None:
            command.append(str(destination))
        env = turtle.environment(self.connection["profile"], self.paths)
        env["AWS_MAX_ATTEMPTS"] = "2"
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + 120
        try:
            while True:
                self.cancelled()
                if time.monotonic() > deadline:
                    raise OfflineError("The download timed out. Retry when the connection is available.")
                try:
                    output, errors = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
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
            text = errors.decode(errors="replace")
            if "PreconditionFailed" in text or "412" in text:
                raise OfflineError("The photo changed in S3. Refresh the folder and select its current version.")
            if turtle.AUTH_ERRORS.search(text):
                raise OfflineError("AWS sign-in expired. Sign in, then retry this download.")
            # KMS profiles can read an object but lack permission to obtain its
            # checksum. A normal conditional HEAD still protects object identity;
            # completion remains explicitly unverified against a remote checksum.
            if operation == "head-object" and checksum and ("AccessDenied" in text or "NotImplemented" in text):
                return self.request(operation, key, etag, checksum=False)
            raise OfflineError("The photo could not be downloaded. Check this connection's access and retry.")
        if len(output) > 1024 * 1024:
            raise OfflineError("S3 returned an unexpected metadata response.")
        try:
            result = json.loads(output)
        except ValueError:
            raise OfflineError("S3 returned invalid photo metadata.") from None
        if not isinstance(result, dict):
            raise OfflineError("S3 returned invalid photo metadata.")
        return result

    def head(self, key, etag):
        return self.request("head-object", key, etag)

    def get(self, key, etag, destination, byte_range):
        return self.request("get-object", key, etag, destination, byte_range, checksum=False)


class OfflineQueue:
    def __init__(self, connection, paths, reader=None, chunk_bytes=CHUNK_BYTES):
        if turtle.connection_backend(connection) != "s3":
            raise OfflineError("Keep offline is available for S3 photos.")
        self.connection, self.paths, self.chunk_bytes = connection, paths, chunk_bytes
        # Credential/profile changes do not move or hide already retained files.
        # A different bucket is a different remote identity, even for the same ID.
        scope = {k: connection.get(k, "") for k in ("id", "bucket", "region")}
        self.base = paths.base / "offline-photos" / item_id(scope)
        self.originals, self.partials = self.base / "originals", self.base / "partials"
        self.queue_path = self.base / "queue.json"
        self.reader = reader or AWSReader(connection, paths, self.check_cancelled)

    def prepare(self):
        for path in (self.base, self.originals, self.partials):
            private_directory(path, self.paths.home)

    @contextlib.contextmanager
    def lock(self, name="queue.lock", blocking=True):
        self.prepare()
        with regular_file(self.base / name, os.O_RDWR | os.O_CREAT) as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield None
                return
            yield handle

    def read(self):
        try:
            with regular_file(self.queue_path) as handle:
                data = handle.read(MAX_QUEUE_BYTES + 1)
            value = json.loads(data)
            if (len(data) > MAX_QUEUE_BYTES or not isinstance(value, dict) or value.get("version") != 1
                    or not isinstance(value.get("items"), list) or len(value["items"]) > MAX_ITEMS
                    or type(value.get("paused")) is not bool):
                raise ValueError()
            for item in value["items"]:
                identity = checked_identity(item)
                if (item.get("id") != item_id(identity) or item.get("state") not in
                        {"queued", "downloading", "paused", "downloaded", "verified", "error"}
                        or type(item.get("bytesDownloaded")) is not int
                        or not 0 <= item["bytesDownloaded"] <= item["size"]
                        or any(item.get(key) is not None and not re.fullmatch(r"[0-9a-f]{64}", str(item[key]))
                               for key in ("sha256", "partialSHA256"))):
                    raise ValueError()
            return value
        except FileNotFoundError:
            return {"version": 1, "paused": False, "items": []}
        except (ValueError, KeyError, TypeError):
            raise OfflineError("The offline queue could not be read. Existing files were preserved.") from None

    def save(self, value):
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode()
        if len(data) > MAX_QUEUE_BYTES:
            raise OfflineError("The offline queue has reached its metadata limit. Its existing photos were preserved.")
        temporary = self.base / (".queue-" + uuid.uuid4().hex)
        try:
            with regular_file(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.queue_path)
            sync_directory(self.base)
        finally:
            temporary.unlink(missing_ok=True)

    def change(self, item_id_value, **fields):
        with self.lock():
            value = self.read()
            item = self.find(value, item_id_value)
            item.update(fields, updatedAt=time.time())
            self.save(value)
            return dict(item)

    @staticmethod
    def find(value, identity):
        for item in value["items"]:
            if item["id"] == identity:
                return item
        raise OfflineError("That photo is no longer in the offline queue.")

    def target(self, item, partial=False):
        return (self.partials / (item["id"] + ".part") if partial
                else self.originals / (item["id"] + "-" + safe_name(item["key"])))

    def worker_running(self):
        with self.lock("worker.lock", blocking=False) as lock:
            return lock is None

    def check_cancelled(self):
        value = self.read()
        if _stop_requested or value["paused"] or value.get("updatePaused") or turtle.Store(self.paths).read().get("updateHandoff"):
            raise Paused("Downloads are paused. Resume when ready.")

    def status(self):
        with self.lock():
            value = self.read()
        running = self.worker_running()
        items = []
        for original in value["items"]:
            item = {key: original.get(key) for key in
                    ("id", "key", "etag", "size", "state", "bytesDownloaded", "error", "sha256", "verification", "verifiedAt")}
            item["name"] = PurePosixPath(item["key"]).name
            item["path"] = None
            if item["state"] in COMPLETE:
                target = self.target(original)
                try:
                    info = target.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                            or info.st_size != item["size"] or info.st_mtime_ns != original.get("localMtimeNs")
                            or info.st_ctime_ns != original.get("localCtimeNs") or info.st_ino != original.get("localInode")):
                        raise OSError()
                    item["path"] = str(target)
                except OSError:
                    item.update(state="error", error="The local photo is missing or changed. Verify it before opening; existing files are preserved.")
            elif item["state"] == "downloading" and not running:
                item["state"] = "paused" if value["paused"] or value.get("updatePaused") else "queued"
            items.append(item)
        return {"ok": True, "paused": value["paused"] or bool(value.get("updatePaused")), "workerRunning": running,
                "items": items, "totals": {"count": len(items), "bytesTotal": sum(i["size"] for i in items),
                "bytesDownloaded": sum(i["bytesDownloaded"] or 0 for i in items),
                "verified": sum(i["state"] == "verified" for i in items),
                "downloaded": sum(i["state"] == "downloaded" for i in items),
                "errors": sum(i["state"] == "error" for i in items)}}

    def enqueue(self, items, start=True):
        if not isinstance(items, list) or not 1 <= len(items) <= 1000:
            raise OfflineError("Select between 1 and 1,000 photos at a time.")
        identities = [checked_identity(item) for item in items]
        with self.lock():
            value = self.read()
            known = {item["id"] for item in value["items"]}
            for identity in identities:
                digest = item_id(identity)
                if digest in known:
                    continue
                if len(value["items"]) >= MAX_ITEMS:
                    raise OfflineError("The offline library is full. It can retain up to 10,000 photos.")
                value["items"].append(dict(identity, id=digest, state="queued", bytesDownloaded=0,
                                           error=None, sha256=None, verification=None, createdAt=time.time()))
                known.add(digest)
            self.save(value)
        if start:
            self.start_worker()
        return self.status()

    def pause(self):
        with self.lock():
            value = self.read()
            value["paused"] = True
            for item in value["items"]:
                if item["state"] == "queued":
                    item["state"] = "paused"
            self.save(value)
        return self.status()

    def resume(self, start=True):
        turtle.assert_no_update(turtle.Store(self.paths).read())
        with self.lock():
            value = self.read()
            value["paused"] = False
            value.pop("updatePaused", None)
            for item in value["items"]:
                if item["state"] == "paused":
                    item["state"] = "queued"
            self.save(value)
        if start:
            self.start_worker()
        return self.status()

    def retry(self, identity, start=True):
        blocked = False
        with self.lock():
            value = self.read()
            item = self.find(value, identity)
            target, partial = self.target(item), self.target(item, partial=True)
            if (item["state"] in COMPLETE or item["state"] == "error") and item.get("sha256"):
                if not target.exists() and not target.is_symlink() and not partial.exists() and not partial.is_symlink():
                    # Explicit Retry may replace a missing copy; never a local edit.
                    item.update(state="error", bytesDownloaded=0, sha256=None, partialSHA256=None,
                                verification=None, verifiedAt=None)
                elif target.exists() or target.is_symlink():
                    try:
                        info = target.lstat()
                        unchanged = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == item["size"]
                                     and info.st_mtime_ns == item.get("localMtimeNs") and info.st_ctime_ns == item.get("localCtimeNs")
                                     and info.st_ino == item.get("localInode"))
                    except OSError:
                        unchanged = False
                    if not unchanged:
                        item.update(state="error", error="The saved photo changed. Choose Check copy to verify it; the existing file will not be replaced.")
                        blocked = True
            if item["state"] == "error" and not blocked:
                item.update(state="paused" if value["paused"] else "queued", error=None)
            self.save(value)
        if start and not blocked:
            self.start_worker()
        return self.status()

    def start_worker(self):
        value = self.read()
        if (value["paused"] or value.get("updatePaused") or turtle.Store(self.paths).read().get("updateHandoff")
                or not any(i["state"] in {"queued", "downloading", "paused"} for i in value["items"])):
            return
        # Ownership is established by flock inside the worker, never by a PID.
        # Concurrent launchers are harmless; only one worker can issue requests.
        subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--resource-dir", str(self.paths.resources),
                          "worker", self.connection["id"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def checked_head(self, item):
        head = self.reader.head(item["key"], item["etag"])
        if (head.get("ContentLength") != item["size"] or head.get("ETag", "").strip('"') != item["etag"].strip('"')):
            raise OfflineError("The photo changed in S3. Refresh the folder and select its current version.")
        return head

    def recover_uncommitted_tail(self, item, partial):
        """Resume a crash between file fsync and journal commit without discarding data."""
        expected = item["bytesDownloaded"]
        if partial.lstat().st_size <= expected:
            return
        if not item.get("partialSHA256"):
            raise OfflineError("The partial download has unrecorded data. It was preserved.")
        if shutil.disk_usage(self.base).free < expected + RESERVE_BYTES:
            raise OfflineError("Free some space to recover the interrupted download. Its contents were preserved.")
        temporary = self.partials / (item["id"] + ".recovery-" + uuid.uuid4().hex)
        preserved = self.partials / (item["id"] + ".interrupted-" + uuid.uuid4().hex)
        try:
            digest, remaining = hashlib.sha256(), expected
            with regular_file(partial) as source, regular_file(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL) as target:
                before = os.fstat(source.fileno())
                while remaining:
                    self.check_cancelled()
                    block = source.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise OfflineError("The partial download changed locally. It was preserved.")
                    remaining -= len(block)
                    digest.update(block)
                    target.write(block)
                after = os.fstat(source.fileno())
                if (digest.hexdigest() != item["partialSHA256"] or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise OfflineError("The partial download changed locally. It was preserved.")
                target.flush()
                os.fsync(target.fileno())
            current = partial.lstat()
            if (current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                raise OfflineError("The partial download changed locally. It was preserved.")
            os.link(partial, preserved, follow_symlinks=False)
            os.replace(temporary, partial)
            sync_directory(self.partials)
            self.change(item["id"], recoveredCopy=str(preserved))
        finally:
            temporary.unlink(missing_ok=True)

    def recover_publication(self, item, target, partial):
        target_info = target.lstat()
        if target_info.st_nlink == 2 and partial.exists() and not partial.is_symlink():
            partial_info = partial.lstat()
            if target_info.st_ino == partial_info.st_ino and target_info.st_dev == partial_info.st_dev:
                hashes = hash_file(target, self.check_cancelled, links=2)
                if hashes["sha256"] != item.get("sha256") or hashes["size"] != item["size"]:
                    raise OfflineError("The local photo changed during an interrupted save. It was preserved.")
                partial.unlink()
                sync_directory(self.partials)
                sync_directory(self.originals)

    @staticmethod
    def verify_remote(head, hashes):
        if head.get("ChecksumType") == "FULL_OBJECT":
            supported = [key for key in ("ChecksumSHA256", "ChecksumSHA1", "ChecksumCRC32", "ChecksumCRC32C") if head.get(key)]
            for key in supported:
                if head[key] != hashes[key]:
                    raise OfflineError("The downloaded photo failed its S3 checksum check. Files were preserved; do not use this copy.")
            if supported:
                return "verified", "S3 full-object checksum and local SHA-256 verified."
        return "downloaded", "Downloaded and locally checked with SHA-256. S3 did not provide a supported full-object checksum."

    def process(self, item):
        self.check_cancelled()
        head = self.checked_head(item)
        target, partial = self.target(item), self.target(item, partial=True)
        expected = item.get("bytesDownloaded", 0)
        # A crash after atomic publication is recoverable only against a recorded
        # SHA-256. Untracked or locally edited files must never be overwritten.
        if target.exists() or target.is_symlink():
            if item.get("sha256") and expected == item["size"]:
                self.recover_publication(item, target, partial)
                self.finish(item, head, target)
                return
            raise OfflineError("An existing local file occupies this photo's destination. It was preserved.")
        if partial.exists() or partial.is_symlink():
            self.recover_uncommitted_tail(item, partial)
            hashes = hash_file(partial, self.check_cancelled)
            if hashes["size"] != expected or hashes["sha256"] != item.get("partialSHA256"):
                raise OfflineError("The partial download changed locally. It was preserved; it cannot be resumed safely.")
            running_sha = hashes["hasher"]
        elif expected:
            raise OfflineError("The partial download is missing. Its recorded progress was preserved.")
        else:
            with regular_file(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL) as handle:
                os.fsync(handle.fileno())
            sync_directory(self.partials)
            item = self.change(item["id"], partialSHA256=hashlib.sha256(b"").hexdigest())
            running_sha = hashlib.sha256()
        last_info = partial.stat()
        item = self.change(item["id"], state="downloading", error=None)
        while expected < item["size"]:
            self.check_cancelled()
            count = min(self.chunk_bytes, item["size"] - expected)
            if shutil.disk_usage(self.base).free < count * 2 + RESERVE_BYTES:
                raise OfflineError("There is not enough free space. Free some space, then retry this download.")
            chunk = self.partials / (item["id"] + ".chunk-" + uuid.uuid4().hex)
            try:
                # The randomly named destination cannot alias an existing file.
                with regular_file(chunk, os.O_RDWR | os.O_CREAT | os.O_EXCL):
                    pass
                metadata = self.reader.get(item["key"], item["etag"], chunk,
                                           f"bytes={expected}-{expected + count - 1}")
                if (chunk.lstat().st_size != count or metadata.get("ContentLength") != count
                        or metadata.get("ContentRange") != f"bytes {expected}-{expected + count - 1}/{item['size']}"
                        or metadata.get("ETag", "").strip('"') != item["etag"].strip('"')):
                    raise OfflineError("S3 returned an incomplete or different photo range. Retry this download.")
                self.check_cancelled()
                with regular_file(chunk) as source, regular_file(partial, os.O_RDWR) as destination:
                    info = os.fstat(destination.fileno())
                    if ((info.st_size, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                            != (expected, last_info.st_ino, last_info.st_mtime_ns, last_info.st_ctime_ns)):
                        raise OfflineError("The partial download changed locally. It was preserved.")
                    destination.seek(0, os.SEEK_END)
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        destination.write(block)
                        running_sha.update(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                    last_info = os.fstat(destination.fileno())
                expected += count
                # Persist the checksum for every committed prefix before issuing
                # another GET, so resume detects edits, not merely byte count.
                item = self.change(item["id"], bytesDownloaded=expected, partialSHA256=running_sha.hexdigest())
            finally:
                chunk.unlink(missing_ok=True)
        self.check_cancelled()
        head_after = self.checked_head(item)
        if (head.get("VersionId") != head_after.get("VersionId")
                or any(head.get(key) != head_after.get(key) for key in
                       ("ChecksumType", "ChecksumSHA256", "ChecksumSHA1", "ChecksumCRC32", "ChecksumCRC32C", "ChecksumCRC64NVME"))):
            raise OfflineError("The photo's S3 metadata changed during download. Retry before using this copy.")
        hashes = hash_file(partial, self.check_cancelled, bool(head.get("ChecksumCRC32C")))
        if hashes["size"] != item["size"] or hashes["sha256"] != item["partialSHA256"]:
            raise OfflineError("The downloaded photo changed during verification. It was preserved.")
        self.verify_remote(head, hashes)
        item = self.change(item["id"], sha256=hashes["sha256"])
        # link() is an atomic no-overwrite publication; rename() could replace a
        # file created by the user while the network request was in flight.
        os.link(partial, target, follow_symlinks=False)
        partial.unlink()
        sync_directory(self.originals)
        sync_directory(self.partials)
        self.finish(item, head, target)

    def finish(self, item, head, target):
        hashes = hash_file(target, self.check_cancelled, bool(head.get("ChecksumCRC32C")))
        if hashes["size"] != item["size"] or hashes["sha256"] != item.get("sha256"):
            raise OfflineError("The local photo changed. It was preserved; no download replaced it.")
        state, verification = self.verify_remote(head, hashes)
        info = hashes["stat"]
        self.change(item["id"], state=state, bytesDownloaded=item["size"], verification=verification, error=None,
                    verifiedAt=time.time(), localMtimeNs=info.st_mtime_ns, localCtimeNs=info.st_ctime_ns, localInode=info.st_ino)

    def verify(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise OfflineError("Select a valid photo from the offline queue.")
        with self.lock(identity + ".lock", blocking=False) as lock:
            if lock is None:
                raise OfflineError("This photo is still downloading. Verify it when its download finishes.")
            with self.lock():
                item = dict(self.find(self.read(), identity))
            target = self.target(item)
            try:
                hashes = hash_file(target)
                if not item.get("sha256") or hashes["size"] != item["size"] or hashes["sha256"] != item["sha256"]:
                    raise OfflineError("The local photo changed. It was preserved; no download replaced it.")
                info = hashes["stat"]
                self.change(identity, state="verified" if item.get("verification", "").startswith("S3 full-object") else "downloaded",
                            error=None, verifiedAt=time.time(), localMtimeNs=info.st_mtime_ns,
                            localCtimeNs=info.st_ctime_ns, localInode=info.st_ino)
            except (OfflineError, OSError) as error:
                self.change(identity, state="error", error=str(error) if isinstance(error, OfflineError) else "The saved photo could not be read.")
        return self.status()

    def open(self, identity):
        result = self.verify(identity)
        item = next(item for item in result["items"] if item["id"] == identity)
        if item["state"] not in COMPLETE or not item["path"]:
            raise OfflineError(item["error"] or "This photo has not finished downloading.")
        return {"ok": True, "path": item["path"], "state": item["state"], "verification": item["verification"]}

    def worker(self):
        with self.lock("worker.lock", blocking=False) as lock:
            if lock is None:
                return
            while True:
                with self.lock():
                    value = self.read()
                    if value["paused"] or value.get("updatePaused") or turtle.Store(self.paths).read().get("updateHandoff"):
                        return
                    item = next((dict(i) for i in value["items"] if i["state"] in {"queued", "downloading", "paused"}), None)
                if item is None:
                    return
                try:
                    with self.lock(item["id"] + ".lock"):
                        self.process(item)
                except Paused:
                    self.change(item["id"], state="paused", error=None)
                    return
                except Exception as error:
                    message = str(error) if isinstance(error, OfflineError) else "The download stopped unexpectedly. Files were preserved; retry when ready."
                    self.change(item["id"], state="error", error=message)


def offline_original(connection, paths, key, etag, size):
    """Read-only metadata lookup for date extraction; never starts a download."""
    queue = OfflineQueue(connection, paths)
    try:
        # Inspect every home-relative ancestor before even opening the manifest.
        current = paths.home
        for piece in queue.originals.relative_to(paths.home).parts:
            current /= piece
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                return None
        identity = item_id(checked_identity({"key": key, "etag": etag, "size": size}))
        item = queue.find(queue.read(), identity)
        if item["state"] not in COMPLETE or not item.get("sha256"):
            return None
        target = queue.target(item)
        info = target.lstat()
        if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and info.st_size == size and info.st_mtime_ns == item.get("localMtimeNs")
                and info.st_ctime_ns == item.get("localCtimeNs") and info.st_ino == item.get("localInode")):
            return target
    except (OfflineError, OSError, KeyError, TypeError):
        pass
    return None


def existing_queues(paths):
    for connection in turtle.Store(paths).read()["connections"]:
        if turtle.connection_backend(connection) == "s3":
            queue = OfflineQueue(connection, paths)
            if queue.queue_path.exists():
                yield queue


def pause_for_update(paths, deadline):
    """An app replacement must not race workers reading its Python resources."""
    queues = list(existing_queues(paths))
    for queue in queues:
        with queue.lock():
            value = queue.read()
            value["updatePaused"] = True
            queue.save(value)
    while any(queue.worker_running() for queue in queues):
        if time.monotonic() >= deadline:
            raise RuntimeError("Offline downloads are still stopping safely. Try updating again in a moment.")
        time.sleep(0.1)


def resume_after_update(paths):
    for queue in existing_queues(paths):
        with queue.lock():
            value = queue.read()
            if not value.pop("updatePaused", False):
                continue
            if not value["paused"]:
                for item in value["items"]:
                    if item["state"] == "paused":
                        item["state"] = "queued"
            queue.save(value)
        queue.start_worker()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--resource-dir")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("status", "enqueue", "pause", "resume", "retry", "verify", "open", "worker"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "enqueue":
            command.add_argument("--items-json", required=True)
        if name in ("retry", "verify", "open"):
            command.add_argument("--item-id", required=True)
    return result


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        args = parser().parse_args()
        paths = turtle.Paths(resources=args.resource_dir)
        connection = turtle.find_connection(turtle.Store(paths).read(), args.id)
        queue = OfflineQueue(connection, paths)
        if args.command == "enqueue":
            response = queue.enqueue(json.loads(args.items_json))
        elif args.command in ("retry", "verify", "open"):
            response = getattr(queue, args.command)(args.item_id)
        elif args.command == "worker":
            queue.worker()
            response = {"ok": True}
        else:
            response = getattr(queue, args.command)()
        print(json.dumps(response, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error) if isinstance(error, (OfflineError, ValueError))
                          else "The offline library could not finish that request. Saved files were preserved."}))
        raise SystemExit(130 if isinstance(error, Paused) else 1)


if __name__ == "__main__":
    main()
