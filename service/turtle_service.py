#!/usr/bin/env python3
"""Mountain Turtle's local JSON CLI and native macOS NFS supervisor."""

import argparse
import contextlib
import fcntl
import ipaddress
import json
import logging
import logging.handlers
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import socket
import stat
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


def write_json(path, value, durable=False):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        if durable:
            handle.flush()
            os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    if durable:
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


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
    if profile:
        env.update(AWS_PROFILE=profile, AWS_SDK_LOAD_CONFIG="1", AWS_PAGER="",
                   AWS_EC2_METADATA_DISABLED="true", AWS_CLI_AUTO_PROMPT="off")
    return env


def connection_backend(connection):
    backend = connection.get("backend", "s3")
    if backend not in ("s3", "sftp"):
        raise ValueError("Choose Amazon S3 or SFTP for this drive")
    return backend


def credential(paths, operation, identity, password=None):
    """Secrets travel only through pipes to the native macOS Keychain helper."""
    helper = paths.resources.parent / "Helpers/Mountain Turtle Credentials"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise ValueError("Use the built Mountain Turtle app to save or read SFTP passwords in macOS Keychain")
    try:
        result = subprocess.run([str(helper), operation, identity], input=password,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("macOS Keychain is unavailable. Unlock your login keychain and try again.") from None
    if result.returncode:
        raise ValueError("Could not access this drive's SFTP password in macOS Keychain. Edit the connection to save it again.")
    return result.stdout if operation == "get" else ""


def forget_credential(paths, identity):
    try:
        credential(paths, "delete", identity)
    except ValueError:
        # A locked Keychain must not prevent removing a disconnected drive.
        logging.warning("Could not remove an unused SFTP password from macOS Keychain")


class PasswordChange:
    """Roll back Keychain changes if the corresponding settings cannot commit."""
    def __init__(self, paths):
        self.paths, self.previous = paths, []

    def set(self, identity, password, had_password):
        previous = credential(self.paths, "get", identity) if had_password else None
        # Record before the helper call: a timeout may have happened after the
        # Keychain accepted the new value but before reporting success.
        self.previous.append((identity, previous))
        credential(self.paths, "set", identity, password)

    def rollback(self):
        for identity, previous in reversed(self.previous):
            try:
                if previous is None:
                    credential(self.paths, "delete", identity)
                else:
                    credential(self.paths, "set", identity, previous)
            except ValueError:
                raise ValueError("Connection settings were not saved, and the previous Keychain password could not be restored. Edit this connection and save its correct password before connecting.") from None


def password_input():
    password = sys.stdin.read(16385)
    if len(password) > 16384 or any(c in password for c in ("\r", "\n", "\0")):
        raise ValueError("Use an SFTP password without line breaks, up to 16384 characters")
    return password


def mount_environment(connection, paths, rclone):
    env = environment(connection.get("profile") if connection_backend(connection) == "s3" else None, paths)
    if connection_backend(connection) == "sftp" and connection.get("authMode", "agent") == "password":
        password = credential(paths, "get", connection["id"])
        if not password or any(c in password for c in ("\r", "\n", "\0")):
            raise ValueError("Save this drive's SFTP password again before connecting")
        try:
            obscured = subprocess.run([rclone, "obscure", "-"], input=password + "\n", env=env,
                                      capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("Could not prepare SFTP authentication. Check rclone and try again.") from None
        token = obscured.stdout.strip()
        if obscured.returncode or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError("Could not prepare SFTP authentication. Check rclone and try again.")
        env["RCLONE_CONFIG_SFTP_PASS"] = token
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


def validate_name(name):
    name = name.strip()
    if (not name or name in (".", "..") or name.startswith(".") or len(name.encode()) > 180
            or any(ord(c) < 32 or c in "/:\\" for c in name)):
        raise ValueError("Choose a visible drive name without slashes, colons, or control characters")
    return name


def validate_fields(name, bucket, profile, region):
    name = validate_name(name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Enter a valid S3 bucket name, without s3:// or a folder path")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", profile):
        raise ValueError("Select a valid AWS profile")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", region):
        raise ValueError("Enter an AWS region such as us-east-1")
    return name


def local_ssh_file(value, paths, label):
    if not value or any(ord(c) < 32 or ord(c) == 127 or c == "$" for c in value):
        raise ValueError(f"Choose a local {label} file without control characters or environment variables")
    if value.startswith("~/"):
        value = str(paths.home / value[2:])
    path = Path(value)
    if not path.is_absolute() or value != value.strip():
        raise ValueError(f"Choose an absolute path or ~/ path for the {label} file")
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"The {label} file is missing or unreadable. Choose an existing file before connecting.")
    return str(path)


def validate_sftp(connection, paths):
    host = connection.get("host", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:%-]+", host):
        raise ValueError("Enter an SFTP hostname or IP address, without sftp:// or a port")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                      for label in host.removesuffix(".").split(".")):
            raise ValueError("Enter an SFTP hostname or IP address, without sftp:// or a port")
    user = connection.get("user", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@\\-]{0,127}", user):
        raise ValueError("Enter a valid SSH username without spaces or control characters")
    port = connection.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Enter an SFTP port between 1 and 65535")
    remote_path = connection.get("remotePath", "")
    if (len(remote_path.encode()) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in remote_path)
            or remote_path.startswith("~") or any(part == ".." for part in remote_path.split("/"))):
        raise ValueError("Use a remote folder path without ~, parent traversal, or control characters; leave it blank for your home folder")
    mode = connection.get("authMode", "agent")
    if mode not in ("agent", "keyFile", "password"):
        raise ValueError("Choose SSH agent, private key file, or password authentication")
    known_hosts = local_ssh_file(connection.get("knownHostsFile") or "~/.ssh/known_hosts", paths, "known hosts")
    key_file = local_ssh_file(connection.get("keyFile", ""), paths, "SSH private key") if mode == "keyFile" else ""
    return {"host": host, "user": user, "port": port, "remotePath": remote_path,
            "authMode": mode, "keyFile": key_file, "knownHostsFile": known_hosts}


def read_setup_source(path, maximum, label):
    """Read a chosen regular credential file without following its final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError(f"The {label} file could not be opened safely. Choose its original file.") from None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Choose a regular {label} file.")
        if info.st_size > maximum:
            raise ValueError(f"The {label} file is too large for a setup file.")
        data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError(f"The {label} file is too large for a setup file.")
        return data


@contextlib.contextmanager
def setup_credentials_directory(paths):
    """Anchor every managed directory to an open parent, rejecting symlinks."""
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors.append(os.open(paths.home, flags))
        for component in ("Library", "Application Support", "Mountain Turtle", "credentials"):
            parent = descriptors[-1]
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            descriptor = os.open(component, flags, dir_fd=parent)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid():
                raise ValueError("The local connection folder must belong to your macOS account.")
            if component == "credentials" and stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("The local key folder must be private to your macOS account.")
        # Store's lock and JSON must also remain ordinary local files.
        for name in ("state.lock", "connections.json", f"connections.json.{os.getpid()}.tmp"):
            try:
                info = os.stat(name, dir_fd=descriptors[-2], follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("The local connection store could not be opened safely.")
        yield descriptors[-1]
    except OSError:
        raise ValueError("The local key folder could not be opened safely. It cannot use symbolic links.") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def import_setup(data, name, paths):
    """Validate before installing credentials, then commit one disconnected drive."""
    import setup_bundle
    decoded = setup_bundle.decode(data)
    name = validate_name(name)
    portable = decoded["connection"]
    identity = str(uuid.uuid4())
    store = Store(paths)
    # Reject an existing name before creating any application folders. Recheck
    # under the store lock below to cover another simultaneous import.
    if any(c["name"].casefold() == name.casefold() for c in store.read()["connections"]):
        raise ValueError("Another saved drive already uses this name")
    with setup_credentials_directory(paths) as credentials:
        staging = ".import-" + secrets.token_hex(16)
        installed = False
        staged = False
        reserved = False

        def remove_directory(directory):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=credentials)
            try:
                for filename in ("identity", "known_hosts"):
                    try:
                        os.unlink(filename, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(descriptor)
            os.rmdir(directory, dir_fd=credentials)

        def rollback():
            if installed or reserved:
                remove_directory(identity)
            if staged:
                remove_directory(staging)

        try:
            with store.update(rollback=rollback) as state:
                assert_no_update(state)
                if any(c["name"].casefold() == name.casefold() for c in state["connections"]):
                    raise ValueError("Another saved drive already uses this name")
                # A UUID collision is an error, never permission to overwrite.
                if any(c["id"] == identity for c in state["connections"]):
                    raise ValueError("Could not assign a new connection identity. Try importing again.")
                os.mkdir(staging, mode=0o700, dir_fd=credentials)
                staged = True
                directory = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=credentials)
                try:
                    for filename, data in (("identity", decoded["privateKey"]),
                                           ("known_hosts", decoded["knownHosts"])):
                        descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                             0o600, dir_fd=directory)
                        with os.fdopen(descriptor, "wb") as output:
                            os.fchmod(output.fileno(), 0o600)
                            output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                finally:
                    os.close(directory)
                # Reserve the final name exclusively before the atomic rename;
                # only our own empty reservation can be replaced.
                os.mkdir(identity, mode=0o700, dir_fd=credentials)
                reserved = True
                os.rename(staging, identity, src_dir_fd=credentials, dst_dir_fd=credentials)
                staged = False
                installed = True
                key_directory = paths.base / "credentials" / identity
                fields = validate_sftp(dict(portable,
                    keyFile=str(key_directory / "identity"),
                    knownHostsFile=str(key_directory / "known_hosts")), paths)
                connection = dict(portable, **fields)
                connection.update(id=identity, name=name, backend="sftp", bucket="", profile="", region="",
                                  authMode="keyFile", passwordConfigured=False, autoConnect=False,
                                  desiredConnected=False, updatedAt=time.time(), revision=1)
                state["connections"].append(connection)
        except FileExistsError:
            raise ValueError("The imported key's destination already exists. Try importing again.") from None
    return {"ok": True, "id": identity}


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
    def update(self, rollback=None, timeout=None, durable=False):
        self.paths.prepare()
        with (self.paths.base / "state.lock").open("a+") as handle:
            if timeout is None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Connection settings are busy. Wait a moment and try the update again.") from None
                        time.sleep(0.05)
            value = self.read()
            try:
                yield value
                write_json(self.paths.base / "connections.json", value, durable=durable)
            except BaseException:
                if rollback:
                    rollback()
                raise

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


def materialized_icon_overlay(paths, backend="s3"):
    suffix = "-sftp" if backend == "sftp" else ""
    assets = paths.resources / ("icon-overlay-assets" + suffix)
    if not all((assets / name).is_file() for name in ICON_ASSETS):
        return None
    overlay = paths.base / ("icon-overlay" + suffix)
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
    if connection_backend(connection) == "sftp":
        fields = validate_sftp(connection, paths)
        config = ("[sftp]\ntype = sftp\n"
                  f'host = {fields["host"]}\nuser = {fields["user"]}\nport = {fields["port"]}\n'
                  f'known_hosts_file = {fields["knownHostsFile"]}\n'
                  "shell_type = none\ndisable_hashcheck = true\nuse_insecure_cipher = false\n"
                  f'key_use_agent = {str(fields["authMode"] == "agent").lower()}\n')
        if fields["authMode"] == "keyFile":
            config += f'key_file = {fields["keyFile"]}\n'
        remote = "sftp:" + fields["remotePath"]
    else:
        validate_fields(connection["name"], connection["bucket"], connection["profile"], connection["region"])
        config = ("[s3]\ntype = s3\nprovider = AWS\nenv_auth = true\n"
                  f'profile = {connection["profile"]}\nregion = {connection["region"]}\n'
                  "no_check_bucket = true\ndirectory_markers = false\n")
        remote = "s3:" + connection["bucket"]
    overlay = materialized_icon_overlay(paths, connection_backend(connection))
    if overlay:
        # Rclone's SpaceSepList uses CSV quoting, not shell/backslash escaping.
        quoted = str(overlay).replace('"', '""')
        # A trailing slash keeps a folder named e.g. "photos:ro" from being
        # interpreted as a union backend option instead of the remote folder.
        upstream = remote + "/" if connection_backend(connection) == "sftp" and connection.get("remotePath") and not remote.endswith("/") else remote
        upstream = upstream.replace('"', '""')
        config += (f'\n[volume]\ntype = union\nupstreams = "{quoted}:ro" "{upstream}"\n'
                   "action_policy = epall\ncreate_policy = ff\nsearch_policy = epall\n")
        remote = "volume:"
    return config, remote


def mount_command(connection, paths, rclone, remote, rc_port=None):
    identity = connection["id"]
    cache = cache_settings(connection)
    # Keep backend modification times: --no-modtime exposes rclone's fixed
    # 2000-01-01 fallback in Finder. S3 reads the saved mtime via object metadata.
    command = [rclone, "nfsmount", remote, str(paths.mounts / connection["name"]),
               "--config", str(paths.remotes / (identity + ".conf")), "--addr", "127.0.0.1:0",
               "-o", "nfsvers=3", "-o", "noresvport", "-o", "nolocks", "-o", "readahead=0",
               "--noappledouble", "--noapplexattr", "--umask", "077",
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
    if rc_port is not None:
        command += ["--rc", "--rc-addr", f"127.0.0.1:{rc_port}"]
    return command


def remote_control_settings():
    # If another process wins this short port reservation race, rclone fails
    # closed and the supervisor retries with a fresh authenticated endpoint.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    return {"rcPort": port, "rcUser": secrets.token_urlsafe(18),
            "rcPass": secrets.token_urlsafe(32), "sessionID": str(uuid.uuid4())}


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
            if connection_backend(connection) == "sftp":
                if re.search(r"knownhosts|host key|no authorities for hostname", line, re.I):
                    return "SFTP server identity could not be verified. Check its host key with the server administrator and update your known hosts file."
                if re.search(r"unable to authenticate|authentication failed|no supported methods|ssh agent|ssh-agent|private key", line, re.I):
                    return "SFTP authentication failed. Check your username and password, or unlock your SSH key in the SSH agent."
            elif AUTH_ERRORS.search(line):
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
        self.update_handoff_token = None
        self.badge_connections = []
        self.sidebar_processes, self.sidebar_results = {}, {}

    def record(self, connection, state, message="", pid=None):
        rc = self.children.get(connection["id"], {}).get("remoteControl", {})
        self.runtime[connection["id"]] = {"state": state, "message": message, "pid": pid,
                                          "updatedAt": time.time(), **rc, **self.sidebar_results.get(connection["id"], {})}

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
                   {"pid": os.getpid(), "updatedAt": time.time(), "connections": self.runtime,
                    "updateHandoffToken": self.update_handoff_token})

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
                                                    "seenMounted": str(self.paths.mounts / connection["name"]) in mount_table(),
                                                    "remoteControl": {key: previous[connection["id"]][key]
                                                        for key in ("rcPort", "rcUser", "rcPass", "sessionID")
                                                        if key in previous[connection["id"]]}}

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
            child_env = mount_environment(connection, self.paths, deps["rclone"])
            rc = remote_control_settings()
            child_env.update(RCLONE_RC_USER=rc["rcUser"], RCLONE_RC_PASS=rc["rcPass"])
            config_path = self.paths.remotes / (identity + ".conf")
            config_path.write_text(config)
            config_path.chmod(0o600)
            (self.paths.cache / identity).mkdir(parents=True, exist_ok=True, mode=0o700)
            started = time.time()
            with (self.paths.logs / (identity + ".log")).open("ab", buffering=0) as error_log:
                process = subprocess.Popen(mount_command(connection, self.paths, deps["rclone"], remote, rc["rcPort"]),
                                           env=child_env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_log,
                                           start_new_session=True)
            # Logs span reconnects; keep errors from an earlier process out of
            # the new connection's status, including after supervisor recovery.
            current["lastMountAt"] = connection["lastMountAt"] = started
            current.pop("refreshRequested", None)
            self.children[identity] = {"process": process, "started": started, "seenMounted": False, "remoteControl": rc}
            self.record(connection, "connecting", "Connecting to SFTP…" if connection_backend(connection) == "sftp" else "Connecting to S3…", process.pid)
            self.publish()

    def eject(self, connection):
        identity = connection["id"]
        if pending_writes(connection, self.paths):
            child = self.children.get(identity, {}).get("process")
            self.record(connection, "disconnecting", "Waiting for pending uploads; cached changes are preserved.",
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
        marker = state.get("updateHandoff") or {}
        self.update_handoff_token = (marker.get("token")
                                     if marker.get("phase") == "resuming" and not state.get("shutdown") else None)
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
                        self.record(connection, "disconnecting", "Waiting for pending uploads; cached changes are preserved.", child["process"].pid)
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

    def restore_login_intent(self):
        # launchd also reruns --at-login after a crash. An existing, verified
        # owned mount proves this is recovery within the current login, so keep
        # every user's current connection choice, including disconnected drives.
        if any(child.get("seenMounted") for child in self.children.values()):
            return
        with self.store.update() as state:
            # A launchd retry during installation must not reconnect drives or
            # replace the user's saved pre-update choices with login defaults.
            if state.get("updateHandoff"):
                return
            state["shutdown"] = False
            for connection in state["connections"]:
                connection["desiredConnected"] = connection.get("autoConnect", False)

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
            self.recover(self.store.read()["connections"])
            if at_login:
                self.restore_login_intent()
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


@contextlib.contextmanager
def update_handoff_lock(paths):
    """Serialize installers and recovery without an unbounded CLI wait."""
    paths.prepare()
    with (paths.base / "update.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another update operation is still running. Wait for it to finish.") from None
        yield


def update_marker(state):
    marker = state.get("updateHandoff")
    if marker is None:
        return None
    if (not isinstance(marker, dict) or marker.get("version") != 1
            or marker.get("phase") not in ("preparing", "prepared", "resuming")
            or not isinstance(marker.get("token"), str) or not marker["token"]
            or not isinstance(marker.get("intent"), dict)
            or any(not isinstance(key, str) or type(value) is not bool
                   for key, value in marker["intent"].items())):
        raise RuntimeError("The saved update recovery information is invalid. Connection settings have been preserved.")
    return marker


def assert_no_update(state):
    if state.get("updateHandoff") is not None:
        raise RuntimeError("An app update is in progress. Finish or cancel it before changing connections.")


def legacy_update_recovery_ready(paths, runtime):
    # The first updater-capable app can inherit an older supervisor. During a
    # busy-drive rollback it cannot acknowledge a token, but a still-mounted
    # owned child proves it has not finished shutdown. The restored state will
    # keep that supervisor alive on its next tick. Without an attached child,
    # wait for the old owner to exit and start the new service instead.
    if "updateHandoffToken" in runtime:
        return False
    mounts = mount_table()
    live = runtime.get("connections", {})
    return any(str(paths.mounts / connection["name"]) in mounts
               and process_alive(live.get(connection["id"], {}).get("pid"))
               for connection in Store(paths).read()["connections"])


def resume_update_locked(paths):
    store = Store(paths)
    with store.update(timeout=5, durable=True) as state:
        marker = update_marker(state)
        if marker is None:
            return {"ok": True, "resumed": False}
        marker["phase"] = "resuming"
        state["shutdown"] = False
        for connection in state["connections"]:
            if connection["id"] in marker["intent"]:
                connection["desiredConnected"] = marker["intent"][connection["id"]]
                connection["reconnectRequested"] = False
                connection["revision"] = connection.get("revision", 0) + 1
    # Keep recovery intent until a supervisor is actually running. The marker
    # survives a crash or launch failure and the next launch can retry safely.
    deadline = time.monotonic() + 8
    while True:
        ensure_service(paths)
        runtime = store.runtime()
        if (service_running(paths) and process_alive(runtime.get("pid"))
                and (runtime.get("updateHandoffToken") == marker["token"]
                     or legacy_update_recovery_ready(paths, runtime))):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Your connection choices are saved, but the service has not restarted. Reopen Mountain Turtle to retry.")
        time.sleep(0.1)
    with store.update(timeout=5, durable=True) as state:
        state.pop("updateHandoff", None)
    return {"ok": True, "resumed": True, "message": "Connection choices restored after the app update."}


def resume_update(paths):
    # Ordinary launches must not reset choices or start a previously stopped
    # service. Recovery is authorized only by an actual saved handoff marker.
    if update_marker(Store(paths).read()) is None:
        return {"ok": True, "resumed": False}
    with update_handoff_lock(paths):
        return resume_update_locked(paths)


def prepare_update(paths, timeout=90):
    """Wait for the supervisor's normal safe eject, preserving cache and intent."""
    if not 5 <= timeout <= 180:
        raise ValueError("Use an update preparation timeout between 5 and 180 seconds.")
    with update_handoff_lock(paths):
        store = Store(paths)
        created = False
        try:
            with store.update(timeout=5, durable=True) as state:
                if update_marker(state) is not None:
                    raise RuntimeError("A previous update still needs recovery. Reopen Mountain Turtle before trying again.")
                if any(c.get("reconnectRequested") for c in state["connections"]):
                    raise RuntimeError("A reconnect is already in progress. Let it finish before updating.")
                runtime = store.runtime()
                previous_pids = {item.get("pid") for item in runtime.get("connections", {}).values()
                                 if item.get("pid")}
                previous_pid = runtime.get("pid")
                created_at = time.time()
                state["updateHandoff"] = {"version": 1, "phase": "preparing", "token": uuid.uuid4().hex,
                                          "createdAt": created_at,
                                          "intent": {c["id"]: bool(c.get("desiredConnected", False))
                                                     for c in state["connections"]}}
                created = True
                state["shutdown"] = True
                for connection in state["connections"]:
                    connection["desiredConnected"] = False
                    connection["reconnectRequested"] = False
                    connection["revision"] = connection.get("revision", 0) + 1
            deadline = time.monotonic() + timeout
            mount_prefix = str(paths.mounts) + "/"
            while True:
                attached = {path for path in mount_table() if path.startswith(mount_prefix)}
                runtime = store.runtime()
                live = runtime.get("connections", {})
                previous_pids.update(item["pid"] for item in live.values() if item.get("pid"))
                active = any(process_alive(pid) for pid in previous_pids)
                running = service_running(paths)
                if any("busy" in item.get("message", "").lower() for item in live.values()
                       if item.get("updatedAt", 0) >= created_at):
                    raise RuntimeError("A drive is busy. Close its open files and try updating again. No drive was forcibly disconnected.")
                if not attached and not active and not running and not process_alive(previous_pid):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Safe disconnection timed out. Wait for uploads to finish and close open files before updating. Cached changes are preserved.")
                if not running and (attached or active):
                    # Recover a crashed owner solely to finish its normal safe
                    # ejection. With shutdown set, it cannot start new mounts.
                    ensure_service(paths)
                time.sleep(0.25)
            with store.update(timeout=5, durable=True) as state:
                update_marker(state)["phase"] = "prepared"
            return {"ok": True, "prepared": True, "message": "Drives safely disconnected; ready to install and relaunch."}
        except Exception as error:
            if created:
                try:
                    resume_update_locked(paths)
                except Exception:
                    raise RuntimeError("The update was stopped. Your saved connection choices and cache are preserved; reopen Mountain Turtle to retry service recovery.") from None
            raise


def status(paths):
    store = Store(paths)
    saved, runtime = store.read(), store.runtime().get("connections", {})
    running, mounted = service_running(paths), mount_table()
    connections = []
    for connection in saved["connections"]:
        item = {key: connection[key] for key in ("id", "name", "readOnly", "autoConnect")}
        item.update(backend=connection_backend(connection),
                    **{key: connection.get(key, "") for key in ("bucket", "profile", "region")})
        if item["backend"] == "sftp":
            item.update({key: connection.get(key, "") for key in
                         ("host", "user", "remotePath", "keyFile", "knownHostsFile")})
            item.update(port=connection.get("port", 22), authMode=connection.get("authMode", "agent"),
                        passwordConfigured=connection.get("passwordConfigured", False))
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
            "AssociatedBundleIdentifiers": ["io.mountainturtle.app"],
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
    commands.add_parser("export-connection").add_argument("id")
    commands.add_parser("inspect-connection")
    commands.add_parser("export-setup").add_argument("id")
    commands.add_parser("inspect-setup")
    commands.add_parser("import-setup").add_argument("--name", required=True)
    for command in ("add", "edit"):
        operation = commands.add_parser(command)
        if command == "edit":
            operation.add_argument("id")
        operation.add_argument("--name", required=True)
        operation.add_argument("--backend", choices=("s3", "sftp"), default="s3")
        for field in ("bucket", "profile", "region", "host", "user", "remote-path", "key-file"):
            operation.add_argument("--" + field, default="")
        operation.add_argument("--port", type=int, default=22)
        operation.add_argument("--auth-mode", choices=("agent", "keyFile", "password"), default="agent")
        operation.add_argument("--known-hosts-file", default="~/.ssh/known_hosts")
        operation.add_argument("--password-stdin", action="store_true")
        access = operation.add_mutually_exclusive_group()
        access.add_argument("--read-only", action="store_true", dest="read_only")
        access.add_argument("--read-write", action="store_false", dest="read_only")
        operation.set_defaults(read_only=True)
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
    commands.add_parser("prepare-update").add_argument("--timeout", type=int, default=90)
    commands.add_parser("resume-update")
    return result


def action(args, paths):
    if args.command in ("export-setup", "inspect-setup", "import-setup"):
        import setup_bundle
        if args.command in ("inspect-setup", "import-setup"):
            data = sys.stdin.buffer.read(setup_bundle.MAX_FILE_BYTES + 1)
            if args.command == "inspect-setup":
                # Only reviewed settings cross back into the UI; credentials
                # stay in its original in-memory file until the user imports.
                return {"ok": True, "connection": setup_bundle.decode(data)["connection"]}
            return import_setup(data, args.name, paths)
        if sys.stdout.isatty():
            raise ValueError("Save this setup file through Mountain Turtle so its private key is not displayed in a terminal.")
        connection = find_connection(Store(paths).read(), args.id)
        if connection_backend(connection) != "sftp" or connection.get("authMode") != "keyFile":
            raise ValueError("A setup file needs an SFTP connection with a private key file.")
        fields = validate_sftp(connection, paths)
        private_key = read_setup_source(fields["keyFile"], setup_bundle.MAX_PRIVATE_KEY_BYTES, "SSH private key")
        known_hosts = read_setup_source(fields["knownHostsFile"], setup_bundle.MAX_KNOWN_HOSTS_SOURCE_BYTES, "known hosts")
        return json.loads(setup_bundle.encode(connection, private_key, known_hosts))
    if args.command in ("export-connection", "inspect-connection"):
        import connection_transfer
        if args.command == "inspect-connection":
            # Inspection must not initialize a store, read credentials, or mount.
            data = sys.stdin.buffer.read(65537)
            return {"ok": True, "connection": connection_transfer.decode(data)}
        connection = find_connection(Store(paths).read(), args.id)
        # Export stdout is the portable document itself, without a CLI envelope.
        return json.loads(connection_transfer.encode(connection))
    store = Store(paths)
    if args.command == "status":
        return status(paths)
    if args.command == "cache-info":
        return cache_info(find_connection(store.read(), args.id), paths)
    paths.prepare()
    if args.command == "serve":
        Supervisor(paths).serve(args.at_login)
        return {"ok": True}
    if args.command == "prepare-update":
        return prepare_update(paths, args.timeout)
    if args.command == "resume-update":
        return resume_update(paths)
    assert_no_update(store.read())
    if args.command == "autostart":
        set_autostart(paths, args.setting == "on")
        return {"ok": True, "message": "Login startup updated; active drives stay connected."}
    if args.command == "login":
        connection = find_connection(store.read(), args.id)
        if connection_backend(connection) != "s3":
            raise ValueError("SFTP uses your saved SSH authentication. Edit this connection to update its credentials.")
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
            assert_no_update(state)
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
    credential_to_remove = None
    password_change = PasswordChange(paths)
    with store.update(rollback=password_change.rollback) as state:
        assert_no_update(state)
        if args.command in ("add", "edit"):
            name = validate_name(args.name)
            if args.backend == "sftp":
                fields = validate_sftp({"host": args.host, "user": args.user, "port": args.port,
                    "remotePath": args.remote_path, "authMode": args.auth_mode,
                    "keyFile": args.key_file, "knownHostsFile": args.known_hosts_file}, paths)
                fields.update(bucket="", profile="", region="")
            else:
                validate_fields(name, args.bucket, args.profile, args.region)
                fields = {"bucket": args.bucket, "profile": args.profile, "region": args.region}
            if args.password_stdin and (args.backend != "sftp" or args.auth_mode != "password"):
                raise ValueError("Password input is available only for an SFTP connection using password authentication")
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
            settings = cache_settings(connection, args.cache_max_size_mib, args.cache_max_age_hours)
            old_password = connection.get("passwordConfigured", False)
            password = ""
            if args.backend == "sftp" and args.auth_mode == "password":
                password = password_input() if args.password_stdin else ""
                same_endpoint = (connection_backend(connection) == "sftp"
                    and all(connection.get(key) == fields[key] for key in ("host", "user", "port")))
                if old_password and not password and not same_endpoint:
                    raise ValueError("Enter the SFTP password again when changing the server, port, or username")
                if not password and not old_password:
                    raise ValueError("Enter an SFTP password to save securely in macOS Keychain")
                fields["passwordConfigured"] = True
            elif old_password:
                credential_to_remove = identity
            if args.command == "edit":
                destination_fields = ("bucket", "profile", "region") if args.backend == "s3" else ("host", "user", "port", "remotePath")
                if (connection_backend(connection) != args.backend
                        or any(connection.get(key, "") != fields[key] for key in destination_fields)):
                    clear_cache(connection, paths)
            if password:
                password_change.set(identity, password, old_password)
            for key in ("host", "user", "port", "remotePath", "authMode", "keyFile", "knownHostsFile", "passwordConfigured"):
                connection.pop(key, None)
            connection.update(fields)
            connection.update(name=name, backend=args.backend,
                              readOnly=args.read_only, autoConnect=args.auto_connect, desiredConnected=False,
                              updatedAt=time.time(), revision=connection.get("revision", 0) + 1)
            connection.update(settings)
        else:
            connection = find_connection(state, args.id)
            identity = args.id
            if args.command == "remove":
                assert_disconnected(connection, store.runtime(), mounted, paths)
                state["connections"].remove(connection)
                if connection.get("passwordConfigured"):
                    credential_to_remove = identity
            elif args.command in ("rename", "settings", "clear-cache"):
                assert_disconnected(connection, store.runtime(), mounted, paths)
                if args.command == "rename":
                    name = validate_name(args.name)
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
    if credential_to_remove:
        forget_credential(paths, credential_to_remove)
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
