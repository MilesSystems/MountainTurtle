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
                  "aws": ("/usr/local/bin/aws", "/opt/homebrew/bin/aws")}.get(name, ())
    for candidate in (*candidates, shutil.which(name)):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def dependencies():
    return {"rclone": executable("rclone"), "aws": executable("aws"), "python": sys.executable}


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


def connection_config(connection, paths):
    config = ("[s3]\ntype = s3\nprovider = AWS\nenv_auth = true\n"
              f'profile = {connection["profile"]}\nregion = {connection["region"]}\n'
              "no_check_bucket = true\ndirectory_markers = false\n")
    overlay = paths.resources / "icon-overlay"
    if all((overlay / name).is_file() for name in (".VolumeIcon.icns", "._.", "._.VolumeIcon.icns")):
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
    command = [rclone, "nfsmount", remote, str(paths.mounts / connection["name"]),
               "--config", str(paths.remotes / (identity + ".conf")), "--addr", "127.0.0.1:0",
               "-o", "nfsvers=3", "-o", "noresvport", "-o", "nolocks",
               "--no-modtime", "--noappledouble", "--noapplexattr", "--umask", "077",
               "--file-perms", "0600", "--dir-perms", "0700",
               "--filter", "+ /._.", "--filter", "+ /._.VolumeIcon.icns",
               "--filter", "- .DS_Store", "--filter", "- ._*",
               "--filter", "- .Spotlight-V100/**", "--filter", "- .Trashes/**",
               "--vfs-cache-mode", "full", "--cache-dir", str(paths.cache / identity),
               "--vfs-cache-max-size", "2Gi", "--vfs-cache-min-free-space", "20Gi",
               "--vfs-cache-max-age", "24h", "--vfs-write-back", "5s",
               "--dir-cache-time", "30m", "--poll-interval", "0", "--buffer-size", "4Mi",
               "--vfs-read-chunk-size", "8Mi", "--vfs-read-chunk-size-limit", "128Mi",
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


class Supervisor:
    def __init__(self, paths):
        self.paths, self.store = paths, Store(paths)
        self.children, self.ejections, self.runtime = {}, {}, {}
        self.retry, self.failures, self.blocked = {}, {}, {}
        self.revisions = {}
        self.stop_requested = False

    def record(self, connection, state, message="", pid=None):
        self.runtime[connection["id"]] = {"state": state, "message": message, "pid": pid,
                                          "updatedAt": time.time()}

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
            if not current.get("desiredConnected") or current.get("revision") != connection.get("revision"):
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
                    connection["revision"] = connection.get("revision", 0) + 1
        mounts = mount_table()
        now = time.time()
        for connection in state["connections"]:
            identity = connection["id"]
            desired = connection.get("desiredConnected", False)
            if self.revisions.get(identity) != connection.get("revision", 0):
                self.retry[identity] = 0
                self.revisions[identity] = connection.get("revision", 0)
            path = str(self.paths.mounts / connection["name"])
            mounted = path in mounts
            child = self.children.get(identity)
            ejecting = self.ejections.get(identity)
            if child and mounted:
                child["seenMounted"] = True
            # A disappearing live mount, or its clean server exit, is Finder eject.
            # Preserve that user choice; only failures should automatically reconnect.
            if (desired and child and child.get("seenMounted") and not ejecting
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
                        child["process"].terminate()
                        self.record(connection, "disconnecting", "Finishing disconnection…", child["process"].pid)
                else:
                    self.record(connection, "disconnected")
                continue
            if mounted and child:
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
                    child["process"].terminate()  # No mount exists; failed startup only.
                    self.record(connection, "error", "Connection took too long; retrying shortly.", child["process"].pid)
                continue
            if now >= self.retry.get(identity, 0):
                try:
                    self.start_mount(connection)
                except Exception as error:
                    self.record(connection, "error", str(error))
                    self.retry[identity] = now + 30
        self.publish()
        return not (state.get("shutdown") and not self.children and not self.ejections
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
            while self.tick():
                time.sleep(1)


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
        live = runtime.get(connection["id"], {})
        item.update(desiredConnected=connection.get("desiredConnected", False),
                    state=live.get("state", "disconnected") if running else "disconnected",
                    message=live.get("message", "") if running else "", mountPath=str(paths.mounts / connection["name"]),
                    updatedAt=live.get("updatedAt", connection.get("updatedAt", 0)))
        item["mounted"] = item["mountPath"] in mounted
        if item["mountPath"] in mounted and item["state"] == "disconnected":
            item.update(state="connected", message="Drive remains attached; connect to resume supervision.")
        connections.append(item)
    return {"ok": True, "serviceRunning": running, "launchAtLogin": saved.get("launchAtLogin", False),
            "dependencies": dependencies(), "profiles": profiles(paths), "connections": connections}


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
    for command in ("remove", "connect", "disconnect", "login"):
        commands.add_parser(command).add_argument("id")
    commands.add_parser("autostart").add_argument("setting", choices=("on", "off"))
    commands.add_parser("serve").add_argument("--at-login", action="store_true")
    commands.add_parser("shutdown")
    return result


def action(args, paths):
    store = Store(paths)
    if args.command == "status":
        return status(paths)
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
            else:
                connection = {"id": identity}
                state["connections"].append(connection)
            connection.update(name=name, bucket=args.bucket, profile=args.profile, region=args.region,
                              readOnly=args.read_only, autoConnect=args.auto_connect, desiredConnected=False,
                              updatedAt=time.time(), revision=connection.get("revision", 0) + 1)
        else:
            connection = find_connection(state, args.id)
            identity = args.id
            if args.command == "remove":
                assert_disconnected(connection, store.runtime(), mounted, paths)
                state["connections"].remove(connection)
            elif args.command in ("connect", "disconnect"):
                connection["desiredConnected"] = args.command == "connect"
                connection["revision"] = connection.get("revision", 0) + 1
                state["shutdown"] = False
    if args.command == "connect" or (args.command == "disconnect" and str(paths.mounts / connection["name"]) in mounted):
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
