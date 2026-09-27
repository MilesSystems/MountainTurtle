"""Bounded local failure history; exported reports contain only allowlisted fields."""
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import stat
import time
from pathlib import Path

LIMIT = 200
RETENTION = 7 * 86400
READ_LIMIT = 512 * 1024
LINE = re.compile(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+(ERROR|CRITICAL)\s+:\s+(.*)$')
REASONS = {
    'permission': 'Permission denied. Check access on the server and the drive access setting.',
    'readOnly': 'The filesystem reported read-only access.',
    'notEmpty': 'The folder was not empty when removal was attempted.',
    'notFound': 'The requested item could not be found.',
    'exists': 'An item already exists at the destination.',
    'space': 'The filesystem reported insufficient space or a quota limit.',
    'timeout': 'The operation timed out while waiting for the server.',
    'connection': 'The connection was interrupted or unavailable.',
    'authentication': 'The server rejected authentication or server identity verification.',
    'busy': 'The item or drive was busy.',
    'other': 'An operation reported an error without a recognized reason. Raw logs are excluded from shared reports.',
}
RULES = [
    ('permission', r'permission denied|access denied|accessdenied|operation not permitted|\bEACCES\b|\bEPERM\b'),
    ('readOnly', r'read.only file|\bEROFS\b'),
    ('notEmpty', r'not empty|\bENOTEMPTY\b'),
    ('notFound', r'not found|no such file|\bENOENT\b'),
    ('exists', r'already exists|file exists|\bEEXIST\b'),
    ('space', r'no space|disk full|quota exceeded|\bENOSPC\b|\bEDQUOT\b'),
    ('timeout', r'timed? out|timeout|deadline exceeded'),
    ('authentication', r'authenticat|host key|knownhosts|expiredtoken|sso.*expired'),
    ('connection', r'connection (?:reset|refused|closed|lost)|broken pipe|network is unreachable|unexpected EOF'),
    ('busy', r'resource busy|device busy|\bEBUSY\b'),
]
OPERATIONS = ('delete', 'move', 'upload', 'download', 'list', 'drive', 'preview', 'read', 'metadata', 'prefetch')
COVERAGE = ('Captures errors reported in the local drive log. Finder may reject an operation before it reaches '
            'the drive. A recorded error is an observed attempt, not proof that a later retry failed. '
            'Keeps up to 200 recent error records for seven days; old or rotated log data may be unavailable.')


def reason_for(message):
    return next((code for code, pattern in RULES if re.search(pattern, message, re.I)), 'other')


def parse(line, now):
    match = LINE.match(line.strip())
    if not match:
        return None
    body = match[3]
    # Separate item names only at a known operation marker. Do not copy arbitrary
    # backend messages (which can contain tokens, URLs or credential output).
    item = re.match(r'^(.*?): (?=(?:vfs cache:|Dir\.|File\.|Failed to |failed to |Remove\b|Rename\b|ReadDir\b))', body)
    path = re.sub(r'[\x00-\x1f\x7f]', ' ', item[1])[:512] if item else ''
    detail = body[len(item[0]):] if item else body
    operation = 'drive'
    for kind, pattern in [('delete', r'remove|delete|rmdir|unlink'), ('move', r'rename|move'),
                          ('upload', r'upload|write|flush'), ('download', r'download|vfs reader'),
                          ('list', r'readdir|list(?:ing)?|read directory')]:
        if re.search(pattern, detail, re.I):
            operation = kind
            break
    reason = reason_for(detail)
    try:
        stamp = time.mktime(time.strptime(match[1], '%Y/%m/%d %H:%M:%S'))
    except (ValueError, OverflowError):
        stamp = now
    return dict(operation=operation, reason=reason, path=path, timestamp=min(stamp, now), count=1)


def valid_events(events, now):
    result = []
    for e in events if isinstance(events, list) else []:
        if (not isinstance(e, dict) or e.get('operation') not in OPERATIONS or e.get('reason') not in REASONS
                or not isinstance(e.get('id'), str) or not isinstance(e.get('connectionID'), str)
                or not isinstance(e.get('path'), str) or type(e.get('timestamp')) not in (int, float)
                or not math.isfinite(e['timestamp']) or not now - RETENTION <= e['timestamp'] <= now
                or type(e.get('count')) is not int or e['count'] < 1):
            continue
        item = {k: e[k] for k in ('id', 'connectionID', 'operation', 'reason', 'path', 'timestamp', 'count')}
        duration = e.get('durationSeconds')
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            item['durationSeconds'] = duration
        result.append(item)
    return sorted(result, key=lambda e: e['timestamp'], reverse=True)[:LIMIT]


def collect(paths, connections, now=None):
    """Only reads local logs. Persistent offsets survive app/service restarts."""
    import turtle_service as turtle
    now = time.time() if now is None else now
    paths.prepare()
    history = paths.base / 'failure-history.json'
    with (paths.base / 'failure-history.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            old = turtle.read_json(history, {}) if history.stat().st_size <= 2 * 1024 * 1024 else {}
        except (OSError, ValueError, TypeError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        events = valid_events(old.get('events'), now)
        cursors = old.get('cursors', {})
        if not isinstance(cursors, dict):
            cursors = {}
        cursors = dict(list(cursors.items())[-1000:])
        for connection in connections:
            identity = connection['id']
            log = paths.logs / (identity + '.log')
            try:
                fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    inode = [info.st_dev, info.st_ino]
                    cursor = cursors.get(identity, {})
                    if not isinstance(cursor, dict):
                        cursor = {}
                    offset = cursor.get('offset', 0)
                    if type(offset) is not int or offset < 0 or offset > info.st_size or cursor.get('inode') != inode:
                        offset = 0
                    start = max(offset, info.st_size - READ_LIMIT)
                    handle.seek(start)
                    data = handle.read(READ_LIMIT)
                    # Leave incomplete trailing lines for the next poll.
                    end = data.rfind(b'\n') + 1
                    complete = data[:end]
                    if start > offset:
                        first = complete.find(b'\n') + 1
                        start += first
                        complete = complete[first:]
                    position = start
                    for raw in complete.splitlines(keepends=True):
                        event = parse(raw.decode(errors='replace'), now)
                        if event and event['timestamp'] >= now - RETENTION:
                            event.update(connectionID=identity,
                                id=hashlib.sha256(f'{identity}:{inode}:{position}:'.encode() + raw).hexdigest()[:24])
                            if not any(e['id'] == event['id'] for e in events):
                                events.append(event)
                        position += len(raw)
                    cursors[identity] = {'inode': inode, 'offset': position}
            except OSError:
                continue
        import operation_activity
        for connection in connections:
            for operation in operation_activity.rows(paths, connection['id'], now):
                if operation['state'] == 'failed' and operation['kind'] in OPERATIONS:
                    event = dict(id=operation['id'], connectionID=connection['id'], operation=operation['kind'],
                                 reason=operation.get('reason') if operation.get('reason') in REASONS else 'other', path=operation['path'], timestamp=operation['updatedAt'], count=1,
                                 durationSeconds=operation.get('durationSeconds'))
                    if not any(e['id'] == event['id'] for e in events):
                        events.append(event)
        state = dict(version=1, events=valid_events(events, now), cursors=cursors)
        if state != old:
            turtle.write_json(history, state)
        return state['events']


def snapshot(paths, connection, runtime=None, now=None):
    now = time.time() if now is None else now
    events = [e for e in collect(paths, [connection], now) if e['connectionID'] == connection['id']]
    runtime = runtime or {}
    state = runtime.get('state')
    if state not in ('connected', 'connecting', 'disconnected', 'disconnecting', 'error', 'needsLogin'):
        state = 'unknown'
    # This is an allowlist, never a redacted copy of raw settings/logs. IDs,
    # paths, hosts, usernames, credentials and arbitrary error text stay local.
    labels = {}
    lines = ['Mountain Turtle diagnostic report', f'Generated: {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}',
             f'Platform: macOS {platform.mac_ver()[0]}',
             f'Backend: {"SFTP" if connection.get("backend") == "sftp" else "S3"}',
             f'Access: {"read only" if connection.get("readOnly", True) else "read and write"}',
             f'Drive state: {state}', f'Recent captured errors: {len(events)}', '',
             'Most recent errors (up to 20; item labels are anonymous within this report):']
    for event in events[:20]:
        label = 'not reported'
        if event['path']:
            label = labels.setdefault(event['path'], f'item-{len(labels) + 1}')
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(event['timestamp']))
        lines.append(f"- {stamp} | {event['operation']} | {event['reason']} | {label}"
                     + (f" | duration {event['durationSeconds']:.3f}s" if 'durationSeconds' in event else ''))
    if not events:
        lines.append('- No backend failures captured. Add the Finder message and steps below.')
    import operation_activity
    recent = operation_activity.rows(paths, connection['id'], now)[:10]
    if recent:
        lines += ['', 'Recent measured / observed requests (up to 10):']
        for operation in recent:
            label = labels.setdefault(operation['path'], f'item-{len(labels) + 1}') if operation['path'] else 'root'
            timing = ('observed' if operation.get('observed') else 'measured')
            duration = operation.get('durationSeconds')
            if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
                timing += f' {duration:.3f}s'
            elif operation['state'] == 'running':
                timing += f" elapsed {operation['elapsedSeconds']:.1f}s"
            lines.append(f"- {operation['kind']} | {operation['state']} | {label} | {timing}")
    lines += ['', COVERAGE, '', 'Excluded: filenames, paths, connection names, server addresses, usernames, credentials and raw logs.']
    return dict(ok=True, events=[dict(e, explanation=REASONS[e['reason']]) for e in events],
                coverage=COVERAGE, report='\n'.join(lines))
