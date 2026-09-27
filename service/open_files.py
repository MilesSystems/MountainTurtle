"""Bounded, current-user open-handle inspection without traversing mounted paths."""
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 200


def scan(seconds=5):
    command = ['/usr/sbin/lsof', '-nP', '-b', '+c', '0', '-u', str(os.getuid()), '-F0pcuftan']
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    output, errors = bytearray(), bytearray()
    partial = False
    deadline = time.monotonic() + seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'out')
    selector.register(process.stderr, selectors.EVENT_READ, 'err')
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                partial = True
                break
            for key, _ in selector.select(min(remaining, .25)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == 'out':
                    room = MAX_BYTES - len(output)
                    output.extend(chunk[:room])
                    if len(chunk) > room:
                        partial = True
                        break
                elif len(errors) < 8192:
                    errors.extend(chunk[:8192 - len(errors)])
            if partial:
                break
        if process.poll() is None:
            if partial:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
                partial = True
        partial = partial or bool(errors) or process.returncode != 0
        return bytes(output), partial
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        process.stdout.close()
        process.stderr.close()


def clean(value):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', value)[:1024]


def parse(data, mount, service_lock, mount_pid=None):
    root = str(mount).rstrip('/')
    records, service_pids = [], set()
    process, file = {}, {}

    def flush():
        name = file.get('n', '')
        pid = process.get('pid')
        if not pid or not name:
            return
        if name == str(service_lock):
            service_pids.add(pid)
        if name != root and not name.startswith(root + '/'):
            return
        if file.get('t') not in ('REG', 'DIR', 'LINK'):
            return
        records.append(dict(pid=pid, command=process.get('command', 'Unknown process'),
                            path=clean(name), relativePath=clean(name[len(root):].lstrip('/') or '/'),
                            kind='Folder' if file.get('t') == 'DIR' else 'File',
                            access={'r': 'Read only', 'w': 'Write only', 'u': 'Read & write'}.get(file.get('a'), 'Not reported'),
                            descriptor=clean(file.get('f', ''))))

    # -F0 preserves spaces and newlines inside names; records are delimited by
    # field NULs, with an extra newline between process/file field sets.
    for raw in data.split(b'\0')[:-1]:
        raw = raw.lstrip(b'\n')
        if not raw:
            continue
        field, value = chr(raw[0]), raw[1:].decode(errors='replace')
        if field == 'p':
            flush(); file = {}
            process = {'pid': int(value)} if value.isdigit() and 0 < int(value) <= 2147483647 else {}
        elif field == 'c':
            process['command'] = clean(value)
        elif field == 'f':
            flush(); file = {'f': value}
        elif field in ('a', 't', 'n'):
            file[field] = value
    flush()
    grouped = {}
    for record in records:
        pid = record['pid']
        record['owner'] = ('Mountain Turtle background service' if pid in service_pids else
                           'Mountain Turtle drive process' if pid == mount_pid else record['command'])
        key = (pid, record['path'], record['access'], record['kind'])
        if key in grouped:
            grouped[key]['handleCount'] += 1
        else:
            record.update(id=f'{pid}:{len(grouped)}', handleCount=1)
            grouped[key] = record
    values = sorted(grouped.values(), key=lambda r: (r['owner'].casefold(), r['path']))
    return values[:MAX_ROWS], len(values) > MAX_ROWS


def snapshot(paths, connection, live=None, scanner=scan):
    try:
        data, partial = scanner()
        entries, truncated = parse(data, paths.mounts / connection['name'], paths.base / 'service.lock',
                                   (live or {}).get('pid'))
        partial = partial or truncated
        message = ('Snapshot of handles visible for your macOS user. '
                   'Folders and working directories can keep a drive busy too. Refresh to see changes.')
        if partial:
            message += ' Some handles may be missing because macOS limited inspection or the scan reached its time/size limit.'
        return dict(ok=True, entries=entries, partial=partial, checkedAt=time.time(), message=message)
    except (OSError, subprocess.SubprocessError, ValueError):
        return dict(ok=True, entries=[], partial=True, checkedAt=time.time(),
                    message='Open-file inspection is unavailable. No apps or drives were closed. Try refreshing.')
