"""Bounded, private snapshots of complete directory listings; never file contents."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

MAX_BYTES = 8 * 1024 * 1024
TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 512
MAX_AGE = 7 * 86400
FRESH_SECONDS = 30
FIELDS = ('id', 'backend', 'host', 'port', 'user', 'remotePath', 'authMode',
          'keyFile', 'knownHostsFile', 'bucket', 'profile', 'region')


def location(paths, connection, directory):
    scope = [{key: connection.get(key) for key in FIELDS}, directory]
    key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    return paths.cache / 'directory-listings' / (key + '.json')


def valid_entries(entries):
    return isinstance(entries, list) and all(
        isinstance(e, dict) and isinstance(e.get('name'), str) and e['name'] not in ('', '.', '..')
        and not any(c in e['name'] for c in ('/', '\0')) and type(e.get('directory')) is bool
        and type(e.get('size')) is int and type(e.get('modified')) in (int, float)
        and math.isfinite(e['modified']) for e in entries)


def load(paths, connection, directory):
    path = location(paths, connection, directory)
    try:
        if path.parent.is_symlink() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
            return None
        record = json.loads(path.read_text())
        age = time.time() - record['savedAt']
        if record.get('version') == 1 and 0 <= age <= MAX_AGE and valid_entries(record.get('entries')):
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save(paths, connection, directory, entries):
    if not valid_entries(entries):
        return
    data = json.dumps(dict(version=1, savedAt=time.time(), entries=entries)).encode()
    if len(data) > MAX_BYTES:
        return
    path = location(paths, connection, directory)
    temporary = None
    try:
        if path.parent.is_symlink():
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
        # Concurrent readers see either complete snapshot; eviction affects only
        # these disposable listings, never the drive's cached file data.
        files = sorted((p for p in path.parent.glob('*.json') if not p.is_symlink()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for index, item in enumerate(files):
            info = item.stat(); total += info.st_size
            if index >= MAX_FILES or total > TOTAL_BYTES or time.time() - info.st_mtime > MAX_AGE:
                item.unlink(missing_ok=True)
    except OSError:
        pass  # A full/unwritable cache must not prevent remote browsing.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
