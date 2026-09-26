"""Cancellable remote-folder download and local ZIP creation, without NFS traversal."""
import argparse
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile

import turtle_service as service


class Cancelled(Exception):
    pass


def emit(phase, **values):
    print(json.dumps(dict(phase=phase, **values)), flush=True)


def check_space(directory):
    if shutil.disk_usage(directory).free < 1024 ** 3:
        raise ValueError("Less than 1 GB of free space remains. Choose a disk with room for the downloaded folder and ZIP.")


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def download(command, env, directory, report=emit):
    report("downloading", files=0, bytes=0)
    child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    selector.register(child.stderr, selectors.EVENT_READ)
    pending = b""
    try:
        while True:
            check_space(directory)
            for key, _ in selector.select(timeout=0.5):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        stats = json.loads(line).get("stats")
                        if stats:
                            report("downloading", files=stats.get("transfers", 0),
                                   bytes=stats.get("bytes", 0), speed=stats.get("speed", 0))
                    except (ValueError, AttributeError):
                        pass
                if len(pending) > 1024 * 1024:
                    pending = b""
            if child.poll() is not None and not selector.get_map():
                break
        if child.returncode:
            raise ValueError("The folder could not be downloaded completely. Check the drive connection and available disk space, then retry. No ZIP was saved.")
    finally:
        stop_child(child)
        selector.close()
        child.stderr.close()


def make_zip(source, output, report=emit):
    files, size, last = 0, 0, 0
    report("compressing", files=0, bytes=0)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True, strict_timestamps=False) as archive:
        for directory, folders, names in os.walk(source, followlinks=False):
            base = Path(directory)
            if base.is_symlink() or any((base / name).is_symlink() for name in folders + names):
                raise ValueError("The downloaded folder contains a symbolic link. No ZIP was saved.")
            archive.write(base, base.relative_to(source.parent).as_posix() + "/")
            for name in names:
                path = base / name
                info = zipfile.ZipInfo.from_file(path, path.relative_to(source.parent).as_posix(), strict_timestamps=False)
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        writer.write(chunk)
                        size += len(chunk)
                        if time.monotonic() - last >= 0.25:
                            check_space(output.parent)
                            report("compressing", files=files, bytes=size)
                            last = time.monotonic()
                files += 1
    report("compressing", files=files, bytes=size)


def archive_folder(paths, requested, destination, report=emit):
    state = service.Store(paths).read()
    connection, relative = service._folder_cache_target(paths, state["connections"], requested)
    if not relative:
        raise ValueError("Choose a folder inside the drive, rather than the entire drive.")
    if service.pending_writes(dict(connection, readOnly=False), paths):
        raise ValueError("This drive has changes waiting to upload. Let uploads finish before compressing, and keep the source folder unchanged until completion.")
    destination = Path(destination).expanduser()
    if not destination.is_absolute() or destination.suffix.lower() != ".zip":
        raise ValueError("Choose an absolute local path ending in .zip.")
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    # Resolve parent symlinks before checking: never stage or archive back onto NFS.
    for mount in service.mount_table():
        mount_root = Path(mount).resolve()
        if parent == mount_root or mount_root in parent.parents:
            raise ValueError("Save the ZIP on a local disk. You can copy the finished ZIP to the drive afterward.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("A file already exists at this location. Choose a new ZIP name.")
    rclone = service.executable("rclone")
    if not rclone:
        raise ValueError("Install rclone before compressing a remote folder.")
    check_space(parent)
    config, remote = service.connection_config(connection, paths)
    env = service.mount_environment(connection, paths, rclone)
    with tempfile.TemporaryDirectory(prefix=".mountainturtle-compress-", dir=parent) as temporary:
        temporary = Path(temporary)
        config_path = temporary / "remote.conf"
        config_path.write_text(config)
        config_path.chmod(0o600)
        source = temporary / "download" / Path(relative).name
        source.mkdir(parents=True)
        remote = remote.rstrip("/") + ("" if remote.endswith(":") else "/") + relative
        command = [rclone, "copy", remote, str(source), "--config", str(config_path),
                   "--create-empty-src-dirs", "--transfers", "4", "--checkers", "8",
                   "--stats", "1s", "--stats-log-level", "NOTICE", "--use-json-log",
                   "--contimeout", "10s", "--timeout", "1m", "--retries", "2"]
        download(command, env, temporary, report)
        if service.pending_writes(dict(connection, readOnly=False), paths):
            raise ValueError("Files on this drive changed during the download. Wait for uploads to finish, then retry. No ZIP was saved.")
        output = temporary / "archive.zip"
        make_zip(source, output, report)
        output.chmod(0o600)
        # Atomic no-clobber publication: an existing destination is never replaced.
        os.link(output, destination)
    report("complete", path=str(destination))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-dir", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    def cancel(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise Cancelled()
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    os.umask(0o077)
    try:
        archive_folder(service.Paths(resources=args.resource_dir), args.path, args.destination)
    except Cancelled:
        emit("cancelled")
    except Exception as error:
        emit("failed", message=str(error) if isinstance(error, (ValueError, OSError)) else "Compression failed. No ZIP was saved.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
