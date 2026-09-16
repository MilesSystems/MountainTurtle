"""Portable connection settings, deliberately independent of local credentials.

This is also the inner document for password-protected exports. Credential
handling and encryption belong to the application, outside this format.
"""

import ipaddress
import json
import re


FORMAT = "io.mountainturtle.connection"
VERSION = 1
MAX_FILE_BYTES = 64 * 1024

COMMON_FIELDS = frozenset(("name", "backend", "readOnly", "autoConnect",
                           "cacheMaxSizeMiB", "cacheMaxAgeHours"))
BACKEND_FIELDS = {
    "s3": frozenset(("bucket", "profile", "region")),
    "sftp": frozenset(("host", "user", "port", "remotePath", "authMode")),
}
DEFAULTS = {"readOnly": True, "autoConnect": False,
            "cacheMaxSizeMiB": 2048, "cacheMaxAgeHours": 24,
            "port": 22, "remotePath": "", "authMode": "agent"}


class ConnectionTransferError(ValueError):
    """A connection file cannot be exported or safely opened for review."""


def _string(connection, key, limit):
    value = connection[key]
    if type(value) is not str:
        raise ConnectionTransferError(f"The connection's {key} must be text.")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError:
        raise ConnectionTransferError(f"The connection's {key} contains invalid text.") from None
    if length > limit:
        raise ConnectionTransferError(f"The connection's {key} is too long.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConnectionTransferError(f"The connection's {key} contains control characters.")
    return value


def _integer(connection, key, minimum, maximum):
    value = connection[key]
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConnectionTransferError(f"The connection's {key} must be a whole number between {minimum} and {maximum}.")
    return value


def _validate(connection):
    if type(connection) is not dict:
        raise ConnectionTransferError("The connection file must contain one connection object.")
    backend = connection.get("backend")
    if type(backend) is not str or backend not in BACKEND_FIELDS:
        raise ConnectionTransferError("This connection file uses an unsupported connection type.")
    allowed = COMMON_FIELDS | BACKEND_FIELDS[backend]
    if connection.keys() - allowed:
        raise ConnectionTransferError("The connection file contains unsupported settings. Export it again from Mountain Turtle.")
    if allowed - connection.keys():
        raise ConnectionTransferError("The connection file is missing required settings. Export it again from Mountain Turtle.")

    name = _string(connection, "name", 180).strip()
    if not name or name.startswith(".") or any(c in "/:\\" for c in name):
        raise ConnectionTransferError("Choose a visible drive name without slashes, colons, or control characters.")
    for key in ("readOnly", "autoConnect"):
        if type(connection[key]) is not bool:
            raise ConnectionTransferError(f"The connection's {key} must be true or false.")
    _integer(connection, "cacheMaxSizeMiB", 64, 1048576)
    _integer(connection, "cacheMaxAgeHours", 1, 8760)

    result = dict(connection, name=name, autoConnect=False)
    if backend == "s3":
        bucket = _string(connection, "bucket", 63)
        profile = _string(connection, "profile", 128)
        region = _string(connection, "region", 128)
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ConnectionTransferError("Enter a valid S3 bucket name, without s3:// or a folder path.")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", profile):
            raise ConnectionTransferError("Select a valid AWS profile.")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", region):
            raise ConnectionTransferError("Enter an AWS region such as us-east-1.")
    else:
        host = _string(connection, "host", 253).strip()
        user = _string(connection, "user", 128).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:%-]+", host):
            raise ConnectionTransferError("Enter an SFTP hostname or IP address, without sftp:// or a port.")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in host.removesuffix(".").split(".")):
                raise ConnectionTransferError("Enter an SFTP hostname or IP address, without sftp:// or a port.") from None
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@\\-]{0,127}", user):
            raise ConnectionTransferError("Enter a valid SSH username without spaces or control characters.")
        _integer(connection, "port", 1, 65535)
        remote_path = _string(connection, "remotePath", 4096)
        if remote_path.startswith("~") or any(part == ".." for part in remote_path.split("/")):
            raise ConnectionTransferError("Use a remote folder path without ~ or parent traversal; leave it blank for your home folder.")
        mode = _string(connection, "authMode", 16)
        if mode not in ("agent", "keyFile", "password"):
            raise ConnectionTransferError("Choose SSH agent, private key file, or password authentication.")
        result.update(host=host, user=user)
    return result


def encode(connection):
    """Export an existing stored connection as UTF-8 settings-only JSON bytes.

    Only the explicit portable field list is ever serialized. Old S3 records
    may omit their backend and cache settings. Imported connections always
    start with automatic connection disabled.
    """
    if type(connection) is not dict:
        raise ConnectionTransferError("Choose a saved connection to export.")
    backend = connection.get("backend", "s3")
    if type(backend) is not str or backend not in BACKEND_FIELDS:
        raise ConnectionTransferError("This connection uses an unsupported connection type.")
    portable = {key: connection.get(key, DEFAULTS.get(key))
                for key in COMMON_FIELDS | BACKEND_FIELDS[backend]}
    portable.update(backend=backend, autoConnect=False)
    portable = _validate(portable)
    data = (json.dumps({"format": FORMAT, "version": VERSION, "connection": portable},
                       indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ConnectionTransferError("The connection file is too large. The maximum size is 64 KiB.")
    return data


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConnectionTransferError("The connection file contains duplicate settings.")
        result[key] = value
    return result


def _constant(value):
    raise ConnectionTransferError("The connection file contains an invalid number.")


def decode(data):
    """Read untrusted file bytes into portable settings for local review.

    This performs no writes and does not connect or retrieve credentials.
    Unknown fields, duplicate keys, invalid types and unsafe field values are
    rejected without including any document values in error messages.
    """
    if type(data) is not bytes:
        raise ConnectionTransferError("Read the connection file as bytes.")
    if len(data) > MAX_FILE_BYTES:
        raise ConnectionTransferError("The connection file is too large. The maximum size is 64 KiB.")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except ConnectionTransferError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise ConnectionTransferError("This is not a valid Mountain Turtle connection file.") from None
    if type(document) is not dict or set(document) != {"format", "version", "connection"}:
        raise ConnectionTransferError("This is not a valid Mountain Turtle connection file.")
    if document["format"] != FORMAT:
        raise ConnectionTransferError("This is not a Mountain Turtle connection file.")
    if type(document["version"]) is not int or document["version"] != VERSION:
        raise ConnectionTransferError("This connection file version is unsupported. Update Mountain Turtle or export it again.")
    return _validate(document["connection"])
