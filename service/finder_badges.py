"""On-demand Finder badges from local rclone cache metadata only.

This bridge never opens the mounted path, enumerates a directory, or contacts S3.
An rclone cache is evictable: a cached badge is not an offline pin or a promise
that the object has not changed remotely. Persisted ranges do not prove that a
transfer is active, so partial files are deliberately not labelled "syncing".
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import os
import secrets
import stat
import threading


MAX_BODY = 64 * 1024
MAX_PATHS = 128
MAX_METADATA = 1024 * 1024
ROOT_FIELDS = ("id", "name", "mountPath", "state", "mounted")


def _parts(path):
    """Keep validation lexical: resolve()/stat() here could block on NFS."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError("Expected an absolute file path")
    parts = path.split("/")[1:]
    if parts and parts[-1] == "":
        parts.pop()
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Invalid file path")
    return parts


def _component(value):
    if not isinstance(value, str) or value in ("", ".", "..") or "/" in value or "\0" in value:
        raise ValueError("Invalid local cache component")
    return value


def _local_file(root, components, metadata=False):
    """Open only beneath a trusted cache directory, never following symlinks.

    Parent traversal uses directory descriptors, so a rename/symlink swap cannot
    redirect the subsequent open. Nonblocking mode also prevents a special file
    in place of metadata from blocking Finder's status service.
    """
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        directory = os.open(str(root), flags | os.O_DIRECTORY)
        descriptors.append(directory)
        for component in components[:-1]:
            directory = os.open(_component(component), flags | os.O_DIRECTORY, dir_fd=directory)
            descriptors.append(directory)
        file_fd = os.open(_component(components[-1]), flags, dir_fd=directory)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            return "unknown", None
        if not metadata:
            return "present", before.st_size
        if before.st_size > MAX_METADATA:
            return "unknown", None
        contents = bytearray()
        while len(contents) <= MAX_METADATA:
            chunk = os.read(file_fd, min(65536, MAX_METADATA + 1 - len(contents)))
            if not chunk:
                break
            contents.extend(chunk)
        after = os.fstat(file_fd)
        if (len(contents) > MAX_METADATA or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            return "unknown", None
        return "present", json.loads(contents)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, UnicodeError):
        return "unknown", None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _coverage(info):
    """Return (valid, full, any_bytes); reject suspicious schema conservatively."""
    size, ranges = info.get("Size"), info.get("Rs")
    if type(size) is not int or size < 0 or size > (2 ** 63 - 1):
        return False, False, False
    if ranges is None:
        ranges = []
    if not isinstance(ranges, list):
        return False, False, False
    intervals = []
    for item in ranges:
        if not isinstance(item, dict):
            return False, False, False
        start, length = item.get("Pos"), item.get("Size")
        if (type(start) is not int or type(length) is not int
                or start < 0 or length < 0 or start + length > size):
            return False, False, False
        if length:
            intervals.append((start, start + length))
    intervals.sort()
    end = 0
    for start, stop in intervals:
        if start > end:
            return True, False, bool(intervals)
        end = max(end, stop)
    return True, end == size, bool(intervals)


def badge_for_path(paths, connections, requested_path):
    requested = _parts(requested_path)
    matches = []
    for connection in connections:
        try:
            root = _parts(connection["mountPath"])
            if requested[:len(root)] == root:
                matches.append((len(root), root, connection))
        except (KeyError, ValueError, TypeError):
            continue
    if not matches:
        raise ValueError("Path is outside Mountain Turtle drives")
    _, root, connection = max(matches, key=lambda item: item[0])
    relative = requested[len(root):]
    # Mount roots and directories cannot claim that their whole subtree is cached.
    if (not relative or connection.get("mounted") is not True
            or any(part in (".VolumeIcon.icns", ".DS_Store") or part.startswith("._")
                   for part in relative)):
        return "unknown"
    try:
        identity = _component(connection["id"])
        bucket = _component(connection["bucket"])
        overlay = paths.resources / "icon-overlay"
        uses_overlay = all((overlay / name).is_file() for name in
                           (".VolumeIcon.icns", "._.", "._.VolumeIcon.icns"))
        namespace = ["volume"] if uses_overlay else ["s3", bucket]
        meta_kind, info = _local_file(paths.cache, [identity, "vfsMeta", *namespace, *relative], True)
        data_kind, data_size = _local_file(paths.cache, [identity, "vfs", *namespace, *relative])
        if meta_kind == "missing" and data_kind == "missing":
            return "online"
        if meta_kind != "present" or not isinstance(info, dict) or data_kind != "present":
            return "unknown"
        if type(info.get("Dirty")) is not bool:
            return "unknown"
        if info["Dirty"]:
            return "pending"
        valid, complete, any_bytes = _coverage(info)
        if not valid or data_size != info["Size"]:
            return "unknown"
        if complete:
            return "cached"
        return "partial" if any_bytes else "online"
    except (KeyError, TypeError, ValueError, OSError):
        return "unknown"


class BadgeBridge:
    """One bounded worker serves authenticated read-only local badge requests."""

    def __init__(self, paths, state_reader):
        self.paths, self.state_reader = paths, state_reader
        self.directory = paths.base / "Finder"
        self.config = self.directory / "bridge.json"
        self.token = secrets.token_urlsafe(32)
        self.server = self.thread = None
        self.url = None

    def connections(self):
        state = self.state_reader()
        if isinstance(state, dict):
            state = state.get("connections", [])
        if not isinstance(state, list):
            raise ValueError("Invalid drive state")
        return state

    def start(self):
        if self.server is not None:
            return self
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Finder bridge directory must not be a symbolic link")
        self.directory.chmod(0o700)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def log_message(self, *_args):
                pass  # Paths, tokens and headers must never reach service logs.

            def reply(self, status, payload):
                data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass

            def authorized(self):
                host = "127.0.0.1:" + str(bridge.server.server_address[1])
                if (self.client_address[0] != "127.0.0.1" or self.headers.get_all("Host") != [host]
                        or self.headers.get_all("Origin") is not None):
                    self.reply(403, {"error": "Request rejected"})
                    return False
                authorization = self.headers.get_all("Authorization", [])
                expected = "Bearer " + bridge.token
                if (len(authorization) != 1
                        or not hmac.compare_digest(authorization[0].encode(), expected.encode())):
                    self.reply(401, {"error": "Authorization required"})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path != "/v1/roots":
                    self.reply(404, {"error": "Not found"})
                    return
                try:
                    roots = [{key: item.get(key) for key in ROOT_FIELDS}
                             for item in bridge.connections()]
                    self.reply(200, {"version": 1, "roots": roots})
                except Exception:
                    self.reply(503, {"error": "Drive state unavailable"})

            def do_POST(self):
                if not self.authorized():
                    return
                if self.path != "/v1/badges":
                    self.reply(404, {"error": "Not found"})
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
                    self.reply(400, {"error": "Invalid request body"})
                    return
                try:
                    length = int(lengths[0])
                except ValueError:
                    length = -1
                if not 0 < length <= MAX_BODY:
                    self.reply(413, {"error": "Request body too large or missing"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self.reply(415, {"error": "JSON required"})
                    return
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete request")
                    body = json.loads(raw)
                    requested = body.get("paths") if isinstance(body, dict) else None
                    if (not isinstance(requested, list) or len(requested) > MAX_PATHS
                            or any(not isinstance(path, str) for path in requested)):
                        raise ValueError("Invalid requested paths")
                    connections = bridge.connections()
                    badges = [{"path": path, "state": badge_for_path(bridge.paths, connections, path)}
                              for path in requested]
                    self.reply(200, {"badges": badges})
                except (ValueError, UnicodeError, TypeError):
                    self.reply(400, {"error": "Invalid requested paths"})
                except (OSError, TimeoutError):
                    self.reply(408, {"error": "Request did not complete"})
                except Exception:
                    self.reply(503, {"error": "Drive state unavailable"})

        class Server(HTTPServer):
            def handle_error(self, _request, _client_address):
                pass  # A malformed client must not put request details in logs.

        # A single worker plus a small accept queue bounds concurrent work and
        # memory. Each connection has a two-second timeout and closes after use.
        self.server = Server(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.request_queue_size = 8
        temporary = self.directory / (".bridge-" + secrets.token_hex(8) + ".json")
        try:
            self.server.server_bind()
            self.server.server_activate()
            self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                json.dump({"version": 1, "url": self.url, "token": self.token}, handle)
                handle.write("\n")
            temporary.replace(self.config)
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           kwargs={"poll_interval": 0.2}, name="Finder badges", daemon=True)
            self.thread.start()
        except Exception:
            temporary.unlink(missing_ok=True)
            self.server.server_close()
            self.server = None
            raise
        return self

    def stop(self):
        if self.server is None:
            return
        if self.thread is not None and self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(timeout=3)
        self.server = self.thread = None
        # Never remove a successor service's configuration.
        try:
            kind, saved = _local_file(self.directory, ["bridge.json"], True)
            if kind == "present" and saved.get("token") == self.token:
                self.config.unlink()
        except (OSError, AttributeError):
            pass
