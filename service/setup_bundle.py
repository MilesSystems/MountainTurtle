"""A bounded, self-contained SFTP setup document, with no extraction or network IO.

The application's existing encrypted envelope can protect these JSON bytes.
Only one connection, one private key and that endpoint's pinned host keys are
portable. Local paths, SSH configuration and unrelated trust entries are not.
"""

import base64
import binascii
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import connection_transfer


FORMAT = "io.mountainturtle.setup"
VERSION = 1
MAX_FILE_BYTES = 64 * 1024
MAX_PRIVATE_KEY_BYTES = 32 * 1024
MAX_KNOWN_HOSTS_BYTES = 16 * 1024
MAX_KNOWN_HOSTS_SOURCE_BYTES = 4 * 1024 * 1024
SSH_KEYGEN = "/usr/bin/ssh-keygen"
KEY_TYPES = frozenset(("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                       "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"))
PRIVATE_KEY_ERROR = "The private key must be a valid, unencrypted SSH key. Choose an unencrypted key to create this setup file."
HOST_KEY_ERROR = "The setup file must contain valid pinned host keys for this connection only."


class SetupBundleError(connection_transfer.ConnectionTransferError):
    """A setup file cannot be exported or installed safely."""


def _run_keygen(arguments, error):
    try:
        return subprocess.run([SSH_KEYGEN, *arguments], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10, check=False,
                              env=dict(os.environ, SSH_ASKPASS_REQUIRE="never"))
    except (OSError, subprocess.SubprocessError):
        raise SetupBundleError(error) from None


def _private_file(directory, name, contents):
    path = Path(directory) / name
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(contents)
    return str(path)


def _validate_private_key(data):
    if type(data) is not bytes or not data or len(data) > MAX_PRIVATE_KEY_BYTES:
        raise SetupBundleError(PRIVATE_KEY_ERROR)
    # One complete PEM/OpenSSH block prevents trailing configuration or extra
    # credentials from being smuggled into a file ssh-keygen otherwise accepts.
    pattern = (rb"\A-----BEGIN (OPENSSH PRIVATE KEY|RSA PRIVATE KEY|EC PRIVATE KEY|PRIVATE KEY)-----\r?\n"
               rb"(?:[A-Za-z0-9+/=]+\r?\n)+-----END \1-----\r?\n?\Z")
    if not re.fullmatch(pattern, data):
        raise SetupBundleError(PRIVATE_KEY_ERROR)
    with tempfile.TemporaryDirectory(prefix="mountainturtle-key-check-") as directory:
        path = _private_file(directory, "identity", data)
        result = _run_keygen(["-y", "-P", "", "-f", path], PRIVATE_KEY_ERROR)
    if result.returncode != 0:
        raise SetupBundleError(PRIVATE_KEY_ERROR)
    try:
        public_type = result.stdout.decode("ascii").split()[0]
    except (UnicodeError, IndexError):
        raise SetupBundleError(PRIVATE_KEY_ERROR) from None
    if public_type not in KEY_TYPES:
        raise SetupBundleError(PRIVATE_KEY_ERROR)
    return data


def _endpoint(connection):
    return (connection["host"] if connection["port"] == 22
            else f'[{connection["host"]}]:{connection["port"]}')


def _decode_base64(value, limit, error):
    if type(value) is not str or len(value) > ((limit + 2) // 3) * 4:
        raise SetupBundleError(error)
    try:
        data = base64.b64decode(value, validate=True)
    except (UnicodeError, ValueError, binascii.Error):
        raise SetupBundleError(error) from None
    if not data or len(data) > limit or base64.b64encode(data).decode("ascii") != value:
        raise SetupBundleError(error)
    return data


def _host_field_is_exact(field, endpoint):
    if field.startswith("|1|"):
        parts = field.split("|")
        if len(parts) != 4:
            return False
        try:
            salt = _decode_base64(parts[2], 20, HOST_KEY_ERROR)
            digest = _decode_base64(parts[3], 20, HOST_KEY_ERROR)
        except SetupBundleError:
            return False
        return len(salt) == len(digest) == 20
    return field.casefold() == endpoint.casefold()


def _validate_public_blob(algorithm, encoded, directory):
    if algorithm not in KEY_TYPES:
        raise SetupBundleError(HOST_KEY_ERROR)
    blob = _decode_base64(encoded, MAX_KNOWN_HOSTS_BYTES, HOST_KEY_ERROR)
    # Verify that the visible algorithm agrees with the SSH wire-format blob.
    length = int.from_bytes(blob[:4], "big") if len(blob) >= 4 else 0
    if not length or blob[4:4 + length] != algorithm.encode("ascii"):
        raise SetupBundleError(HOST_KEY_ERROR)
    path = Path(directory) / "host-key.pub"
    if path.exists():
        path.unlink()
    _private_file(directory, path.name, f"{algorithm} {encoded}\n".encode("ascii"))
    result = _run_keygen(["-l", "-E", "sha256", "-f", str(path)], HOST_KEY_ERROR)
    if result.returncode != 0:
        raise SetupBundleError(HOST_KEY_ERROR)


def _trusted_hosts(data, connection, *, exporting):
    limit = MAX_KNOWN_HOSTS_SOURCE_BYTES if exporting else MAX_KNOWN_HOSTS_BYTES
    if type(data) is not bytes or not data or len(data) > limit:
        raise SetupBundleError(HOST_KEY_ERROR)
    try:
        source = data.decode("utf-8")
    except UnicodeError:
        raise SetupBundleError(HOST_KEY_ERROR) from None
    if any(ord(character) < 32 and character not in "\r\n\t" or ord(character) == 127
           for character in source):
        raise SetupBundleError(HOST_KEY_ERROR)

    endpoint = _endpoint(connection)
    source_lines = [line.strip() for line in source.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    with tempfile.TemporaryDirectory(prefix="mountainturtle-host-check-") as directory:
        path = _private_file(directory, "known_hosts", data)
        result = _run_keygen(["-F", endpoint, "-f", path], HOST_KEY_ERROR)
        if result.returncode != 0:
            raise SetupBundleError("No trusted host key was found for this connection. Connect and verify the server before exporting its setup file.")
        try:
            matching = [line.strip() for line in result.stdout.decode("utf-8").splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
        except UnicodeError:
            raise SetupBundleError(HOST_KEY_ERROR) from None
        if not matching or (not exporting and source_lines != matching):
            raise SetupBundleError(HOST_KEY_ERROR)

        pins = []
        seen = set()
        for line in matching:
            fields = line.split()
            if len(fields) < 3:
                raise SetupBundleError(HOST_KEY_ERROR)
            host, algorithm, encoded = fields[:3]
            # Markers, wildcard patterns, negation and comma-separated aliases
            # are not a single pinned endpoint, even when ssh-keygen matches.
            if any(character in host for character in "*,?!@") or not _host_field_is_exact(host, endpoint):
                raise SetupBundleError(HOST_KEY_ERROR)
            if (algorithm, encoded) in seen:
                continue
            _validate_public_blob(algorithm, encoded, directory)
            seen.add((algorithm, encoded))
            # The endpoint is already in the connection document. Normalizing
            # hashed source entries makes the installed trust file explicit.
            pins.append(f"{endpoint} {algorithm} {encoded}\n")
        trusted = "".join(pins).encode("utf-8")
    if not trusted or len(trusted) > MAX_KNOWN_HOSTS_BYTES:
        raise SetupBundleError(HOST_KEY_ERROR)
    return trusted


def _portable_connection(connection, *, exporting):
    try:
        data = (connection_transfer.encode(connection) if exporting else
                json.dumps({"format": connection_transfer.FORMAT,
                            "version": connection_transfer.VERSION,
                            "connection": connection}).encode("utf-8"))
        portable = connection_transfer.decode(data)
    except (connection_transfer.ConnectionTransferError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, connection_transfer.ConnectionTransferError):
            raise SetupBundleError(str(error)) from None
        raise SetupBundleError("The setup file contains invalid connection settings.") from None
    if portable["backend"] != "sftp" or portable["authMode"] != "keyFile":
        raise SetupBundleError("Setup files require an SFTP connection using a private key.")
    return portable


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupBundleError("The setup file contains duplicate settings.")
        result[key] = value
    return result


def _constant(value):
    raise SetupBundleError("The setup file contains an invalid number.")


def _document(data):
    if type(data) is not bytes:
        raise SetupBundleError("Read the setup file as bytes.")
    if len(data) > MAX_FILE_BYTES:
        raise SetupBundleError("The setup file is too large. The maximum size is 64 KiB.")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except SetupBundleError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise SetupBundleError("This is not a valid Mountain Turtle setup file.") from None


def is_setup(data):
    """Identify the format only; callers must still validate it with decode."""
    try:
        document = _document(data)
    except SetupBundleError:
        return False
    return type(document) is dict and document.get("format") == FORMAT


def encode(connection, private_key, known_hosts):
    """Export a connection and its key, filtering trust to its exact endpoint."""
    portable = _portable_connection(connection, exporting=True)
    key = _validate_private_key(private_key)
    trusted = _trusted_hosts(known_hosts, portable, exporting=True)
    document = {"format": FORMAT, "version": VERSION, "connection": portable,
                "privateKey": base64.b64encode(key).decode("ascii"),
                "knownHosts": base64.b64encode(trusted).decode("ascii")}
    data = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise SetupBundleError("The setup file is too large. The maximum size is 64 KiB.")
    return data


def decode(data):
    """Validate an untrusted setup document without installing or connecting."""
    document = _document(data)
    if type(document) is not dict or set(document) != {"format", "version", "connection", "privateKey", "knownHosts"}:
        raise SetupBundleError("This is not a valid Mountain Turtle setup file.")
    if document["format"] != FORMAT:
        raise SetupBundleError("This is not a Mountain Turtle setup file.")
    if type(document["version"]) is not int or document["version"] != VERSION:
        raise SetupBundleError("This setup file version is unsupported. Update Mountain Turtle or export it again.")
    connection = _portable_connection(document["connection"], exporting=False)
    key = _decode_base64(document["privateKey"], MAX_PRIVATE_KEY_BYTES, PRIVATE_KEY_ERROR)
    trusted = _decode_base64(document["knownHosts"], MAX_KNOWN_HOSTS_BYTES, HOST_KEY_ERROR)
    _validate_private_key(key)
    trusted = _trusted_hosts(trusted, connection, exporting=False)
    return {"connection": connection, "privateKey": key, "knownHosts": trusted}
