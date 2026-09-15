#!/usr/bin/env python3
"""Mountain Turtle's local JSON CLI and native macOS NFS supervisor."""

import argparse
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

LABEL = "com.mountainturtle.service"
SERVICE = Path(__file__).resolve()
CACHE_DEFAULTS = {"cacheMaxSizeMiB": 2048, "cacheMaxAgeHours": 24}
SIDEBAR_PERMISSION_TIMEOUT = 60
HOMEBREW_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
ICON_ASSETS = {
    "VolumeIcon.icns": ".VolumeIcon.icns",
    "root-finder-info.ad": "._.",
    "volume-icon-finder-info.ad": "._.VolumeIcon.icns",
}
SIDEBAR_FALLBACK = ("The drive stays connected. In Finder, choose Go → Computer, select the drive, "
                    "then File → Add to Sidebar.")
AUTH_ERRORS = re.compile(r"expiredtoken|token.{0,30}expir|sso.{0,50}(invalid|fail|expir)|"
                         r"refresh cached credentials|tokenretrievalerror|invalidgrant|"
                         r"unauthorizedexception|aws sso login", re.I)


class Paths:
    def __init__(self, home=None, resources=None):
        self.home = Path(home) if home else Path.home()
        self.base = self.home / "Library/Application Support/Mountain Turtle"
        self.cache = self.home / "Library/Caches/MountainTurtle"
        self.logs = self.home / "Library/Logs/MountainTurtle"
        self.mounts = self.home / "Mountain Turtle"
        self.remotes = self.base / "remotes"
        self.resources = Path(resources).resolve() if resources else SERVICE.parent.parent
        self.plist = self.home / "Library/LaunchAgents" / (LABEL + ".plist")

    def prepare(self):
        for path in (self.base, self.cache, self.logs, self.remotes):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def executable(name):
    candidates = {"rclone": ("/opt/homebrew/bin/rclone", "/usr/local/bin/rclone"),
                  "aws": ("/usr/local/bin/aws", "/opt/homebrew/bin/aws"),
                  "brew": ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")}.get(name, ())
    for candidate in (*candidates, shutil.which(name)):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def checked_command(command, timeout=5):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        return result.returncode == 0, output[:2000]
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def application_path(paths):
    resources = paths.resources
    if resources.name == "Resources" and resources.parent.name == "Contents":
        bundle = resources.parent.parent
        if bundle.name.endswith(".app"):
            return bundle
    return None


def installed_application(paths):
    bundle = application_path(paths)
    if not bundle:
        return False
    try:
        bundle = bundle.resolve()
        expected = [(paths.home / "Applications/Mountain Turtle.app").resolve(),
                    Path("/Applications/Mountain Turtle.app").resolve()]
    except OSError:
        return False
    return bundle in expected


def privacy_status(runtime, mounted):
    live_connections = runtime.values()
    if any(item.get("sidebarItemID") for item in live_connections):
        return {"privacyState": "approved", "privacyMessage": "Finder sidebar access is approved."}
    for item in live_connections:
        error = item.get("sidebarError", "")
        if "Network Volumes" in error or "Files and Folders" in error or "Privacy & Security" in error:
            return {"privacyState": "needsApproval", "privacyMessage": "Allow Network Volumes for Mountain Turtle in macOS Privacy & Security."}
    if mounted:
        return {"privacyState": "checking", "privacyMessage": "Mountain Turtle will confirm Finder access after the first mounted drive appears in Locations."}
    return {"privacyState": "unknown", "privacyMessage": "macOS asks for Network Volumes access when Mountain Turtle first adds a drive to Finder."}


def dependencies(paths=None, runtime=None, mounted=None):
    brew, rclone, aws = executable("brew"), executable("rclone"), executable("aws")
    aws_ok, aws_version = checked_command([aws, "--version"]) if aws else (False, "")
    rclone_ok, rclone_version = checked_command([rclone, "version"]) if rclone else (False, "")
    nfsmount_ok, _ = checked_command([rclone, "nfsmount", "--help"]) if rclone else (False, "")
    rclone_first_line = rclone_version.splitlines()[0] if rclone_version else ""
    result = {"rclone": rclone, "aws": aws, "python": sys.executable, "brew": brew,
              "awsVersion": aws_version.splitlines()[0] if aws_version else "",
              "awsCliV2": aws_ok and aws_version.lower().startswith("aws-cli/2"),
              "rcloneVersion": rclone_first_line,
              "rcloneNfsmount": rclone_ok and nfsmount_ok}
    if paths:
        app = application_path(paths)
        result.update(appPath=str(app) if app else "", appInstalled=installed_application(paths))
        result.update(privacy_status(runtime or {}, mounted or set()))
    return result


def profiles(paths):
    # Read profile section names only. Never read or output credential values.
    names = set()
    for filename, prefix in (("config", "profile "), ("credentials", "")):
        try:
            for line in (paths.home / ".aws" / filename).read_text().splitlines():
                if line.startswith("[") and line.endswith("]"):
                    name = line[1:-1]
                    if filename == "config":
                        if name == "default":
                            names.add(name)
                        elif name.startswith(prefix):
                            names.add(name[len(prefix):])
                    else:
                        names.add(name)
        except FileNotFoundError:
            pass
    return sorted(names, key=str.casefold)


def environment(profile, paths):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("RCLONE_") or key in (
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
            "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
            "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3",
        ):
            env.pop(key)
    env.update(AWS_PROFILE=profile, AWS_SDK_LOAD_CONFIG="1", AWS_PAGER="",
               AWS_EC2_METADATA_DISABLED="true", AWS_CLI_AUTO_PROMPT="off")
    return env


def mount_table():
    result = subprocess.run(["/sbin/mount", "-t", "nfs"], text=True, capture_output=True,
                            timeout=5, check=True)
    return {line.split(" on ", 1)[1].rsplit(" (", 1)[0] for line in result.stdout.splitlines()
            if " on " in line and " (" in line}


def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def service_running(paths):
    try:
        with (paths.base / "service.lock").open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False


def validate_fields(name, bucket, profile, region):
    name = name.strip()
    if (not name or name in (".", "..") or name.startswith(".") or len(name.encode()) > 180
            or any(ord(c) < 32 or c in "/:\\" for c in name)):
        raise ValueError("Choose a visible drive name without slashes, colons, or control characters")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Enter a valid S3 bucket name, without s3:// or a folder path")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", profile):
        raise ValueError("Select a valid AWS profile")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", region):
        raise ValueError("Enter an AWS region such as us-east-1")
    return name


def cache_settings(connection, size=None, age=None):
    values = {key: connection.get(key, default) for key, default in CACHE_DEFAULTS.items()}
    if size is not None:
        values["cacheMaxSizeMiB"] = size
    if age is not None:
        values["cacheMaxAgeHours"] = age
    if not 64 <= values["cacheMaxSizeMiB"] <= 1048576:
        raise ValueError("Choose a cache limit between 64 and 1048576 MiB")
    if not 1 <= values["cacheMaxAgeHours"] <= 8760:
        raise ValueError("Choose a cache age between 1 and 8760 hours")
    return values


class Store:
    def __init__(self, paths):
        self.paths = paths

    def read(self):
        return read_json(self.paths.base / "connections.json",
                         {"version": 1, "launchAtLogin": False, "shutdown": False, "connections": []})

    @contextlib.contextmanager
    def update(self):
        self.paths.prepare()
        with (self.paths.base / "state.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            value = self.read()
            yield value
            write_json(self.paths.base / "connections.json", value)

    def runtime(self):
        return read_json(self.paths.base / "runtime.json", {"connections": {}})


def find_connection(state, connection_id):
    for connection in state["connections"]:
        if connection["id"] == connection_id:
            return connection
    raise ValueError("This saved connection no longer exists")


def assert_disconnected(connection, runtime, mounts, paths):
    live = runtime.get("connections", {}).get(connection["id"], {})
    if (connection.get("desiredConnected") or str(paths.mounts / connection["name"]) in mounts
            or process_alive(live.get("pid"))):
        raise ValueError("Disconnect this drive before editing or removing it")


def materialized_icon_overlay(paths):
    assets = paths.resources / "icon-overlay-assets"
    if not all((assets / name).is_file() for name in ICON_ASSETS):
        return None
    overlay = paths.base / "icon-overlay"
    overlay.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source_name, target_name in ICON_ASSETS.items():
        source = assets / source_name
        target = overlay / target_name
        data = source.read_bytes()
        if not target.exists() or target.read_bytes() != data:
            temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
            temporary.write_bytes(data)
            temporary.chmod(0o600)
            temporary.replace(target)
    return overlay if all((overlay / name).is_file() for name in ICON_ASSETS.values()) else None


def connection_config(connection, paths):
    config = ("[s3]\ntype = s3\nprovider = AWS\nenv_auth = true\n"
              f'profile = {connection["profile"]}\nregion = {connection["region"]}\n'
              "no_check_bucket = true\ndirectory_markers = false\n")
    overlay = materialized_icon_overlay(paths)
    if overlay:
        # The resource path is local application data. Quote without enabling a shell.
        quoted = str(overlay).replace("\\", "\\\\").replace('"', '\\"')
        config += (f'\n[volume]\ntype = union\nupstreams = "{quoted}:ro" s3:{connection["bucket"]}\n'
                   "action_policy = epall\ncreate_policy = ff\nsearch_policy = epall\n")
        remote = "volume:"
    else:
        remote = "s3:" + connection["bucket"]
    return config, remote


def mount_command(connection, paths, rclone, remote):
    identity = connection["id"]
    cache = cache_settings(connection)
    command = [rclone, "nfsmount", remote, str(paths.mounts / connection["name"]),
               "--config", str(paths.remotes / (identity + ".conf")), "--addr", "127.0.0.1:0",
               "-o", "nfsvers=3", "-o", "noresvport", "-o", "nolocks", "-o", "readahead=0",
               "--no-modtime", "--noappledouble", "--noapplexattr", "--umask", "077",
               "--file-perms", "0600", "--dir-perms", "0700",
               "--filter", "+ /._.", "--filter", "+ /._.VolumeIcon.icns",
               "--filter", "- .DS_Store", "--filter", "- ._*",
               "--filter", "- .Spotlight-V100/**", "--filter", "- .Trashes/**",
               "--vfs-cache-mode", "full", "--cache-dir", str(paths.cache / identity),
               "--vfs-cache-max-size", f'{cache["cacheMaxSizeMiB"]}Mi', "--vfs-cache-min-free-space", "20Gi",
               "--vfs-cache-max-age", f'{cache["cacheMaxAgeHours"]}h', "--vfs-write-back", "5s",
               "--dir-cache-time", "30m", "--poll-interval", "0", "--buffer-size", "0",
               "--vfs-read-ahead", "0", "--vfs-read-chunk-streams", "0",
               "--vfs-read-chunk-size", "1Mi", "--vfs-read-chunk-size-limit", "1Mi",
               "--contimeout", "10s", "--timeout", "1m", "--transfers", "2",
               "--log-level", "NOTICE", "--log-file", str(paths.logs / (identity + ".log")),
               "--log-file-max-size", "2Mi", "--log-file-max-backups", "2"]
    if connection["readOnly"]:
        command.append("--read-only")
    return command


def tail_error(connection, paths):
    path = paths.logs / (connection["id"] + ".log")
    cutoff = max(connection.get("lastLoginAt", 0), connection.get("lastMountAt", 0))
    try:
        if path.stat().st_mtime < cutoff:
            return ""
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 4096))
            lines = handle.read().decode(errors="replace").splitlines()
        for line in reversed(lines):
            if cutoff:
                try:
                    stamp = time.mktime(time.strptime(line[:19], "%Y/%m/%d %H:%M:%S"))
                    if stamp < int(cutoff):
                        continue
                except ValueError:
                    continue
            if AUTH_ERRORS.search(line):
                return "AWS sign-in expired. Choose Sign In to renew this profile."
            if "ERROR" in line or "CRITICAL" in line:
                # Avoid echoing arbitrary credential-provider output into the UI.
                return "The drive reported an error. Check the local connection log."
    except FileNotFoundError:
        pass
    return ""


def pending_writes(connection, paths):
    """Inspect only rclone's local metadata, never the mounted or remote tree."""
    if connection["readOnly"]:
        return False
    metadata = paths.cache / connection["id"] / "vfsMeta"
    if not metadata.exists():
        return False
    for directory, subdirectories, files in os.walk(metadata, followlinks=False):
        subdirectories[:] = [name for name in subdirectories if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            try:
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    return True
                item = json.loads(path.read_text())
                if item.get("Dirty", False):
                    return True
            except (OSError, ValueError):
                # A concurrent update is not proof that all uploads are complete.
                return True
    return False


def cache_info(connection, paths, max_entries=10000, max_seconds=0.25):
    """Bounded local disk usage; sparse file logical sizes are not downloaded bytes."""
    root = paths.cache / connection["id"]
    result = {"ok": True, "usedBytes": 0, "files": 0, "partial": False}
    if paths.cache.is_symlink() or root.is_symlink():
        raise ValueError("The cache folder must not be a symbolic link")
    deadline, visited, directories = time.monotonic() + max_seconds, 0, [root]
    while directories:
        directory = directories.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if visited >= max_entries or time.monotonic() >= deadline:
                        result["partial"] = True
                        return result
                    visited += 1
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        result["usedBytes"] += entry.stat(follow_symlinks=False).st_blocks * 512
                        if "vfs" in Path(entry.path).relative_to(root).parts[:-1]:
                            result["files"] += 1
        except FileNotFoundError:
            pass
        except OSError:
            result["partial"] = True
    return result


def clear_cache(connection, paths):
    root = paths.cache / connection["id"]
    if paths.cache.is_symlink() or root.is_symlink():
        raise ValueError("The cache folder must not be a symbolic link")
    # A previous writable session may have pending data even after a settings change.
    if pending_writes(dict(connection, readOnly=False), paths):
        raise ValueError("Cached changes still need uploading. Reconnect this drive before clearing its cache")
    if root.exists():
        shutil.rmtree(root)


class ExistingProcess:
    """Adopt an exact matching orphan after a supervisor crash; never duplicate its cache."""
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None if process_alive(self.pid) else 0

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def send_signal(self, sig):
        os.kill(self.pid, sig)


class Supervisor:
    def __init__(self, paths):
        self.paths, self.store = paths, Store(paths)
        self.children, self.ejections, self.runtime = {}, {}, {}
        self.retry, self.failures, self.blocked = {}, {}, {}
        self.revisions = {}
        self.stop_requested = False
        self.badge_connections = []
        self.sidebar_processes, self.sidebar_results = {}, {}

    def record(self, connection, state, message="", pid=None):
        self.runtime[connection["id"]] = {"state": state, "message": message, "pid": pid,
                                          "updatedAt": time.time(), **self.sidebar_results.get(connection["id"], {})}

    def start_sidebar(self, connection, child):
        """Once per confirmed mount generation, including adopted rclone processes."""
        # The native helper updates one shared Favorites registry. Queue other
        # connections until its bounded operation completes, without extra attempts.
        if child.get("sidebarAttempted") or self.sidebar_processes:
            return
        child["sidebarAttempted"] = True
        identity = connection["id"]
        self.sidebar_results.pop(identity, None)
        helper = self.paths.resources.parent / "Helpers/Mountain Turtle Sidebar"
        try:
            if not helper.is_file() or not os.access(helper, os.X_OK):
                raise FileNotFoundError(str(helper))
            process = subprocess.Popen([str(helper), "ensure", identity, str(self.paths.mounts / connection["name"])],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.sidebar_processes[identity] = {"process": process, "started": time.monotonic(),
                                                 "child": child, "path": str(self.paths.mounts / connection["name"])}
        except OSError as error:
            logging.warning("Could not start sidebar helper for %s: %s", identity, type(error).__name__)
            self.sidebar_failure(identity, "Could not add the drive to Finder's sidebar. In Finder, choose Go → Computer, select the drive, then File → Add to Sidebar.")

    def sidebar_failure(self, identity, message):
        self.sidebar_results[identity] = {"sidebarError": message}
        logging.warning("Sidebar update for %s: %s", identity, message)

    @staticmethod
    def stop_sidebar_process(process):
        """Reap only this owned helper; mounted rclone processes are independent."""
        try:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=0.2)
        except ProcessLookupError:
            process.wait(timeout=0.2)

    def poll_sidebars(self, connections, mounts):
        by_id = {connection["id"]: connection for connection in connections}
        for identity, job in list(self.sidebar_processes.items()):
            process = job["process"]
            connection = by_id.get(identity, {})
            cancelled = (self.stop_requested or not connection.get("desiredConnected")
                         or connection.get("reconnectRequested") or job["path"] not in mounts
                         or self.children.get(identity) is not job["child"])
            running = process.poll() is None
            # macOS may be waiting for the user's Network Volumes permission.
            # Poll without blocking drive supervision while that prompt is open.
            timed_out = running and time.monotonic() - job["started"] >= SIDEBAR_PERMISSION_TIMEOUT
            if running and not cancelled and not timed_out:
                continue
            self.sidebar_processes.pop(identity)
            try:
                if cancelled or timed_out:
                    self.stop_sidebar_process(process)
                    if timed_out and not cancelled:
                        self.sidebar_failure(identity, "Finder sidebar update timed out. In System Settings → Privacy & Security → "
                                             "Files and Folders → Mountain Turtle, allow Network Volumes, then reconnect to retry. "
                                             + SIDEBAR_FALLBACK)
                    continue
                output, _ = process.communicate(timeout=0.2)
                result = json.loads(output) if len(output) <= 16384 else {}
                item = result.get("itemID")
                if process.returncode or result.get("ok") is not True or type(item) is not int or not 0 <= item <= 0xffffffff:
                    detail = str(result.get("error", "Finder sidebar update did not finish."))[:256]
                    self.sidebar_failure(identity, detail + " " + SIDEBAR_FALLBACK)
                    continue
                self.sidebar_results[identity] = {"sidebarItemID": item}
                if result.get("warnings"):
                    self.sidebar_results[identity]["sidebarError"] = "The drive was added to Finder, but an older sidebar entry may need to be removed manually."
                    warnings = result["warnings"]
                    detail = " ".join(value[:160] for value in warnings[:4] if isinstance(value, str))[:512] if isinstance(warnings, list) else "Unexpected helper warning format"
                    logging.warning("Sidebar update for %s: %s", identity, detail)
                logging.info("Finder sidebar updated for %s (item %s)", identity, item)
            except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
                self.sidebar_failure(identity, "Finder sidebar helper returned an invalid response; the drive stays connected.")

    def stop_sidebars(self):
        for identity, job in list(self.sidebar_processes.items()):
            try:
                self.stop_sidebar_process(job["process"])
            except (OSError, subprocess.TimeoutExpired):
                logging.warning("Could not reap sidebar helper for %s", identity)
        self.sidebar_processes.clear()

    def publish(self):
        write_json(self.paths.base / "runtime.json",
                   {"pid": os.getpid(), "updatedAt": time.time(), "connections": self.runtime})

    def recover(self, connections):
        previous = self.store.runtime().get("connections", {})
        for connection in connections:
            pid = previous.get(connection["id"], {}).get("pid")
            if not pid or not process_alive(pid):
                continue
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                    text=True, capture_output=True, timeout=3)
            command = result.stdout.strip()
            expected = str(self.paths.remotes / (connection["id"] + ".conf"))
            if "rclone nfsmount " in command and expected in command:
                self.children[connection["id"]] = {"process": ExistingProcess(pid), "started": time.time(),
                                                    "seenMounted": str(self.paths.mounts / connection["name"]) in mount_table()}

    def start_mount(self, connection):
        identity = connection["id"]
        with self.store.update() as state:
            current = find_connection(state, identity)
            if (not current.get("desiredConnected") or current.get("reconnectRequested")
                    or current.get("revision") != connection.get("revision")):
                return
            deps = dependencies()
            if not deps["rclone"]:
                self.record(connection, "error", "Install the open-source rclone command to mount drives.")
                self.retry[identity] = time.time() + 30
                return
            path = self.paths.mounts / connection["name"]
            if self.paths.mounts.is_symlink() or path.is_symlink():
                raise ValueError("The mount root and drive folders must not be symbolic links")
            self.paths.mounts.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.mkdir(exist_ok=True, mode=0o700)
            if any(path.iterdir()):
                raise ValueError("The drive folder contains local files; move them before connecting")
            config, remote = connection_config(connection, self.paths)
            config_path = self.paths.remotes / (identity + ".conf")
            config_path.write_text(config)
            config_path.chmod(0o600)
            (self.paths.cache / identity).mkdir(parents=True, exist_ok=True, mode=0o700)
            started = time.time()
            with (self.paths.logs / (identity + ".log")).open("ab", buffering=0) as error_log:
                process = subprocess.Popen(mount_command(connection, self.paths, deps["rclone"], remote),
                                           env=environment(connection["profile"], self.paths),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_log)
            # Logs span reconnects; keep errors from an earlier process out of
            # the new connection's status, including after supervisor recovery.
            current["lastMountAt"] = connection["lastMountAt"] = started
            current.pop("refreshRequested", None)
            self.children[identity] = {"process": process, "started": started, "seenMounted": False}
            self.record(connection, "connecting", "Connecting to S3…", process.pid)
            self.publish()

    def eject(self, connection):
        identity = connection["id"]
        if pending_writes(connection, self.paths):
            child = self.children.get(identity, {}).get("process")
            self.record(connection, "disconnecting", "Waiting for pending S3 uploads; cached changes are preserved.",
                        child.pid if child else None)
            return
        process = subprocess.Popen(["/sbin/umount", str(self.paths.mounts / connection["name"])],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.ejections[identity] = {"process": process, "started": time.time(),
                                    "revision": connection.get("revision", 0)}
        child = self.children.get(identity, {}).get("process")
        self.record(connection, "disconnecting", "Ejecting safely; waiting for open files…",
                    child.pid if child else None)

    def tick(self):
        state = self.store.read()
        if self.stop_requested and not state.get("shutdown"):
            with self.store.update() as state:
                state["shutdown"] = True
                for connection in state["connections"]:
                    connection["desiredConnected"] = False
                    connection["reconnectRequested"] = False
                    connection["revision"] = connection.get("revision", 0) + 1
        mounts = mount_table()
        now = time.time()
        self.poll_sidebars(state["connections"], mounts)
        for connection in state["connections"]:
            identity = connection["id"]
            reconnecting = connection.get("reconnectRequested", False) and connection.get("desiredConnected", False)
            desired = connection.get("desiredConnected", False) and not reconnecting
            if self.revisions.get(identity) != connection.get("revision", 0):
                self.retry[identity] = 0
                self.revisions[identity] = connection.get("revision", 0)
            path = str(self.paths.mounts / connection["name"])
            mounted = path in mounts
            if not mounted:
                self.sidebar_results.pop(identity, None)
            child = self.children.get(identity)
            ejecting = self.ejections.get(identity)
            if child and mounted:
                child["seenMounted"] = True
            # A disappearing live mount, or its clean server exit, is Finder eject.
            # Preserve that user choice; only failures should automatically reconnect.
            if (desired and child and child.get("seenMounted") and not child.get("expectedStop") and not ejecting
                    and ((not mounted and child["process"].poll() is None) or child["process"].poll() == 0)):
                with self.store.update() as current_state:
                    current = find_connection(current_state, identity)
                    if current.get("revision", 0) == connection.get("revision", 0):
                        current["desiredConnected"] = False
                        current["revision"] = current.get("revision", 0) + 1
                        connection = dict(current)
                        desired = False
            if ejecting:
                eject_process = ejecting["process"]
                timed_out = now - ejecting["started"] >= 65
                if eject_process.poll() is None and not timed_out:
                    continue
                if timed_out and eject_process.poll() is None:
                    eject_process.terminate()  # Stop the unmount helper, never force-detach the filesystem.
                self.ejections.pop(identity)
                if mounted:
                    self.blocked[identity] = ejecting["revision"]
                    self.record(connection, "connected", "Drive is busy. Close its open files, then disconnect again.",
                                child["process"].pid if child else None)
                    continue
                if child and child["process"].poll() is None and not pending_writes(connection, self.paths):
                    child["expectedStop"] = True
                    child["process"].terminate()  # NFS has already been safely detached.
            if child and child["process"].poll() is not None:
                self.children.pop(identity)
                child = None
                count = self.failures.get(identity, 0) + 1
                self.failures[identity] = count
                self.retry[identity] = now + min(300, 5 * (2 ** min(count - 1, 6)))
                message = tail_error(connection, self.paths)
                self.record(connection, "needsLogin" if "sign-in expired" in message else "error",
                            message or "Connection stopped; retrying shortly." if desired else "Disconnected.")
            if not desired:
                if mounted:
                    if self.blocked.get(identity) != connection.get("revision", 0):
                        self.eject(connection)
                elif child:
                    if pending_writes(connection, self.paths):
                        self.record(connection, "disconnecting", "Waiting for pending S3 uploads; cached changes are preserved.", child["process"].pid)
                    else:
                        child["expectedStop"] = True
                        child["process"].terminate()
                        self.record(connection, "disconnecting", "Finishing disconnection…", child["process"].pid)
                else:
                    if reconnecting:
                        with self.store.update() as current_state:
                            current = find_connection(current_state, identity)
                            if current.get("revision", 0) == connection.get("revision", 0):
                                current["reconnectRequested"] = False
                        self.retry[identity] = 0
                        self.record(connection, "connecting", "Reconnecting safely…")
                    else:
                        self.record(connection, "disconnected")
                continue
            if mounted and child:
                if not child.get("expectedStop"):
                    self.start_sidebar(connection, child)
                if connection.get("refreshRequested") and now - child["started"] >= 5:
                    # SIGHUP only invalidates directory listings; it does not fetch file data.
                    try:
                        child["process"].send_signal(signal.SIGHUP)
                    except ProcessLookupError:
                        continue  # The next tick handles the process exit and normal backoff.
                    with self.store.update() as current_state:
                        current = find_connection(current_state, identity)
                        if current.get("revision", 0) == connection.get("revision", 0):
                            current["refreshRequested"] = False
                message = tail_error(connection, self.paths)
                self.record(connection, "needsLogin" if "sign-in expired" in message else "connected",
                            message, child["process"].pid)
                self.failures[identity] = 0
                continue
            if mounted:
                if self.blocked.get(identity) != connection.get("revision", 0):
                    self.eject(connection)
                else:
                    self.record(connection, "error", "A previous mount is busy. Close its open files and reconnect.")
                continue
            if child:
                if now - child["started"] > 60 and not pending_writes(connection, self.paths):
                    child["expectedStop"] = True
                    child["process"].terminate()  # No mount exists; failed startup only.
                    self.record(connection, "error", "Connection took too long; retrying shortly.", child["process"].pid)
                continue
            if now >= self.retry.get(identity, 0):
                try:
                    self.start_mount(connection)
                except Exception as error:
                    self.record(connection, "error", str(error))
                    self.retry[identity] = now + 30
        self.badge_connections = [dict(connection,
            mountPath=str(self.paths.mounts / connection["name"]),
            state=self.runtime.get(connection["id"], {}).get("state", "disconnected"),
            mounted=str(self.paths.mounts / connection["name"]) in mounts)
            for connection in state["connections"]]
        self.publish()
        return not (state.get("shutdown") and not self.children and not self.ejections and not self.sidebar_processes
                    and not any(str(self.paths.mounts / c["name"]) in mounts for c in state["connections"]))

    def serve(self, at_login=False):
        self.paths.prepare()
        with (self.paths.base / "service.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            logging.basicConfig(level=logging.INFO, handlers=[logging.handlers.RotatingFileHandler(
                self.paths.logs / "service.log", maxBytes=2 * 1024**2, backupCount=2)])
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop_requested", True))
            signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop_requested", True))
            if at_login:
                with self.store.update() as state:
                    state["shutdown"] = False
                    for connection in state["connections"]:
                        connection["desiredConnected"] = connection.get("autoConnect", False)
            self.recover(self.store.read()["connections"])
            from finder_badges import BadgeBridge
            bridge = BadgeBridge(self.paths, lambda: self.badge_connections)
            try:
                bridge.start()
            except (OSError, ValueError):
                logging.exception("Finder badge service could not start; drives remain available")
            try:
                while self.tick():
                    time.sleep(1)
            finally:
                self.stop_sidebars()
                bridge.stop()


def ensure_service(paths):
    if service_running(paths):
        return
    paths.prepare()
    with (paths.logs / "launcher.log").open("ab", buffering=0) as output:
        process = subprocess.Popen([sys.executable, str(SERVICE), "--resource-dir", str(paths.resources), "serve"],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    for _ in range(30):
        if service_running(paths):
            return
        if process.poll() is not None:
            raise RuntimeError("The local service could not start. Check the launcher log.")
        time.sleep(0.1)
    raise RuntimeError("The local service is still starting. Try again shortly.")


def status(paths):
    store = Store(paths)
    saved, runtime = store.read(), store.runtime().get("connections", {})
    running, mounted = service_running(paths), mount_table()
    connections = []
    for connection in saved["connections"]:
        item = {key: connection[key] for key in ("id", "name", "bucket", "profile", "region", "readOnly", "autoConnect")}
        item.update(cache_settings(connection))
        live = runtime.get(connection["id"], {})
        item.update(desiredConnected=connection.get("desiredConnected", False),
                    state=live.get("state", "disconnected") if running else "disconnected",
                    message=live.get("message", "") if running else "", mountPath=str(paths.mounts / connection["name"]),
                    updatedAt=live.get("updatedAt", connection.get("updatedAt", 0)))
        item["mounted"] = item["mountPath"] in mounted
        # Finder can eject between supervisor ticks. Only describe sidebar state
        # for a volume that is still present in the current kernel mount table.
        if running and item["mounted"]:
            item.update({key: live[key] for key in ("sidebarItemID", "sidebarError") if key in live})
        if item["mountPath"] in mounted and item["state"] == "disconnected":
            item.update(state="connected", message="Drive remains attached; connect to resume supervision.")
        connections.append(item)
    return {"ok": True, "serviceRunning": running, "launchAtLogin": saved.get("launchAtLogin", False),
            "dependencies": dependencies(paths, runtime, mounted), "profiles": profiles(paths), "connections": connections}


def set_autostart(paths, enabled):
    paths.plist.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    if enabled:
        paths.plist.write_bytes(plistlib.dumps({
            "Label": LABEL, "ProgramArguments": [sys.executable, str(SERVICE), "--resource-dir", str(paths.resources),
                                                   "serve", "--at-login"],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "ExitTimeOut": 180, "ProcessType": "Background", "Umask": 0o077,
            "StandardOutPath": str(paths.logs / "launcher.log"), "StandardErrorPath": str(paths.logs / "launcher.log"),
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        }))
        paths.plist.chmod(0o600)
        subprocess.run(["/bin/launchctl", "enable", domain + "/" + LABEL], capture_output=True, timeout=10, check=True)
        loaded = subprocess.run(["/bin/launchctl", "print", domain + "/" + LABEL], capture_output=True, timeout=10)
        if loaded.returncode:
            subprocess.run(["/bin/launchctl", "bootstrap", domain, str(paths.plist)], capture_output=True, timeout=10, check=True)
    else:
        # disable prevents future starts but does not kill currently attached drives.
        subprocess.run(["/bin/launchctl", "disable", domain + "/" + LABEL], capture_output=True, timeout=10, check=True)
        paths.plist.unlink(missing_ok=True)
    with Store(paths).update() as state:
        state["launchAtLogin"] = enabled


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    result = JsonArgumentParser(description=__doc__)
    result.add_argument("--resource-dir")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    for command in ("add", "edit"):
        operation = commands.add_parser(command)
        if command == "edit":
            operation.add_argument("id")
        for field in ("name", "bucket", "profile", "region"):
            operation.add_argument("--" + field, required=True)
        operation.add_argument("--read-only", action="store_true")
        operation.add_argument("--auto-connect", action="store_true")
        operation.add_argument("--cache-max-size-mib", type=int)
        operation.add_argument("--cache-max-age-hours", type=int)
    for command in ("remove", "connect", "disconnect", "login", "refresh", "reconnect", "cache-info", "clear-cache"):
        commands.add_parser(command).add_argument("id")
    rename = commands.add_parser("rename")
    rename.add_argument("id")
    rename.add_argument("--name", required=True)
    settings = commands.add_parser("settings")
    settings.add_argument("id")
    settings.add_argument("--cache-max-size-mib", type=int)
    settings.add_argument("--cache-max-age-hours", type=int)
    commands.add_parser("autostart").add_argument("setting", choices=("on", "off"))
    commands.add_parser("serve").add_argument("--at-login", action="store_true")
    commands.add_parser("shutdown")
    return result


def action(args, paths):
    store = Store(paths)
    if args.command == "status":
        return status(paths)
    if args.command == "cache-info":
        return cache_info(find_connection(store.read(), args.id), paths)
    paths.prepare()
    if args.command == "serve":
        Supervisor(paths).serve(args.at_login)
        return {"ok": True}
    if args.command == "autostart":
        set_autostart(paths, args.setting == "on")
        return {"ok": True, "message": "Login startup updated; active drives stay connected."}
    if args.command == "login":
        connection = find_connection(store.read(), args.id)
        aws = executable("aws")
        if not aws:
            raise ValueError("Install the AWS command-line tools to sign in")
        try:
            # Device authorization finishes on AWS's page, without a temporary
            # localhost callback that stops working when a browser tab is revisited.
            result = subprocess.run([aws, "sso", "login", "--use-device-code", "--profile", connection["profile"]],
                                    env=environment(connection["profile"], paths), capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise ValueError("AWS sign-in timed out after five minutes. Close the previous sign-in tab, then choose Sign in to AWS to start a fresh request.")
        if result.returncode:
            raise ValueError("AWS sign-in did not complete. Close the previous sign-in tab, then choose Sign in to AWS to start a fresh request. If it fails again, check this profile's SSO configuration.")
        with store.update() as state:
            connection = find_connection(state, args.id)
            connection["lastLoginAt"] = time.time()
            connection["revision"] = connection.get("revision", 0) + 1
        return {"ok": True, "message": "AWS sign-in completed."}
    if args.command == "shutdown":
        with store.update() as state:
            state["shutdown"] = True
            for connection in state["connections"]:
                connection["desiredConnected"] = False
                connection["reconnectRequested"] = False
                connection["revision"] = connection.get("revision", 0) + 1
        busy = [c["name"] for c in status(paths)["connections"] if c["state"] == "connected"]
        if busy and not service_running(paths):
            ensure_service(paths)
        return {"ok": True, "message": "Safe disconnection requested; busy drives remain attached.", "pendingMounts": busy}
    mounted = mount_table()
    with store.update() as state:
        if args.command in ("add", "edit"):
            name = validate_fields(args.name, args.bucket, args.profile, args.region)
            identity = args.id if args.command == "edit" else str(uuid.uuid4())
            if any(c["name"].casefold() == name.casefold() and c["id"] != identity for c in state["connections"]):
                raise ValueError("Another saved drive already uses this name")
            if args.command == "edit":
                connection = find_connection(state, identity)
                assert_disconnected(connection, store.runtime(), mounted, paths)
                if pending_writes(dict(connection, readOnly=False), paths):
                    raise ValueError("Upload pending cached changes before editing this connection")
            else:
                connection = {"id": identity}
                state["connections"].append(connection)
            connection.update(name=name, bucket=args.bucket, profile=args.profile, region=args.region,
                              readOnly=args.read_only, autoConnect=args.auto_connect, desiredConnected=False,
                              updatedAt=time.time(), revision=connection.get("revision", 0) + 1)
            connection.update(cache_settings(connection, args.cache_max_size_mib, args.cache_max_age_hours))
        else:
            connection = find_connection(state, args.id)
            identity = args.id
            if args.command == "remove":
                assert_disconnected(connection, store.runtime(), mounted, paths)
                state["connections"].remove(connection)
            elif args.command in ("rename", "settings", "clear-cache"):
                assert_disconnected(connection, store.runtime(), mounted, paths)
                if args.command == "rename":
                    name = validate_fields(args.name, connection["bucket"], connection["profile"], connection["region"])
                    if any(c["name"].casefold() == name.casefold() and c["id"] != identity for c in state["connections"]):
                        raise ValueError("Another saved drive already uses this name")
                    connection["name"] = name
                elif args.command == "settings":
                    connection.update(cache_settings(connection, args.cache_max_size_mib, args.cache_max_age_hours))
                else:
                    clear_cache(connection, paths)
                connection["updatedAt"] = time.time()
                connection["revision"] = connection.get("revision", 0) + 1
            elif args.command in ("connect", "disconnect", "reconnect", "refresh"):
                if args.command == "refresh":
                    if str(paths.mounts / connection["name"]) not in mounted:
                        raise ValueError("Connect this drive before refreshing its folders")
                    connection["refreshRequested"] = True
                    connection["desiredConnected"] = True
                else:
                    connection["desiredConnected"] = args.command != "disconnect"
                    connection["reconnectRequested"] = args.command == "reconnect"
                connection["revision"] = connection.get("revision", 0) + 1
                state["shutdown"] = False
    if args.command in ("connect", "reconnect", "refresh") or (args.command == "disconnect" and str(paths.mounts / connection["name"]) in mounted):
        ensure_service(paths)
    return {"ok": True, "id": identity}


def main():
    os.umask(0o077)
    try:
        args = parser().parse_args()
        result = action(args, Paths(resources=args.resource_dir))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        # Command/provider output is deliberately excluded from error responses.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "Local service error: " + type(error).__name__
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
