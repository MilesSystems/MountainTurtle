"""Measured app operations and bounded observations of rclone transfer counters."""
from contextlib import contextmanager
import fcntl
import math
import os
import time
import uuid

KINDS = {'list': 'Listing folder', 'preview': 'Retrieving preview', 'read': 'Retrieving file',
         'metadata': 'Reading photo metadata', 'prefetch': 'Reading folder files', 'transfer': 'File transfer'}
STATES = ('running', 'complete', 'failed', 'cancelled', 'paused', 'unavailable', 'unknown')


def valid(rows):
    result = []
    for r in rows if isinstance(rows, list) else []:
        if (not isinstance(r, dict) or r.get('kind') not in KINDS or r.get('state') not in STATES
                or not isinstance(r.get('id'), str) or not isinstance(r.get('connectionID'), str)
                or not isinstance(r.get('path'), str) or type(r.get('startedAt')) not in (int, float)
                or not math.isfinite(r['startedAt']) or type(r.get('updatedAt')) not in (int, float)
                or not math.isfinite(r['updatedAt'])):
            continue
        result.append(r)
    return result[-100:]


@contextmanager
def update(paths):
    import turtle_service as t
    paths.prepare()
    file = paths.base / 'operation-activity.json'
    with (paths.base / 'operation-activity.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = t.read_json(file, {}) if file.stat().st_size < 1024 * 1024 else {}
        except (OSError, ValueError, TypeError):
            state = {}
        rows = valid(state.get('events', [])) if isinstance(state, dict) else []
        yield rows
        t.write_json(file, {'events': valid(rows)})


def start(paths, connection_id, kind, path):
    if kind not in KINDS:
        raise ValueError('Invalid operation')
    identity = str(uuid.uuid4())
    now = time.time()
    try:
        with update(paths) as rows:
            rows.append(dict(id=identity, connectionID=connection_id, kind=kind, path=str(path)[:512],
                             state='running', startedAt=now, updatedAt=now, ownerPID=os.getpid()))
    except OSError:
        pass  # Telemetry must never fail the file operation.
    return identity


def finish(paths, identity, state, duration, reason=None):
    try:
        with update(paths) as rows:
            for row in rows:
                if row['id'] == identity:
                    row.update(state=state, updatedAt=time.time(), durationSeconds=max(0, duration))
                    if reason is not None:
                        row['reason'] = reason
    except OSError:
        pass


@contextmanager
def measured(paths, connection_id, kind, path, cancelled=()):
    identity = start(paths, connection_id, kind, path)
    began = time.monotonic()
    outcome = {'state': 'complete'}
    try:
        yield outcome
    except BaseException as error:
        from failure_reports import reason_for
        outcome['reason'] = reason_for(str(error))
        outcome['state'] = 'cancelled' if isinstance(error, cancelled) else 'failed'
        raise
    finally:
        finish(paths, identity, outcome['state'], time.monotonic() - began, outcome.get('reason'))


def rows(paths, connection_id, now=None):
    import turtle_service as t
    now = time.time() if now is None else now
    try:
        file = paths.base / 'operation-activity.json'
        state = t.read_json(file, {}) if file.stat().st_size < 1024 * 1024 else {}
    except (OSError, ValueError, TypeError):
        return []
    items = valid(state.get('events')) if isinstance(state, dict) else []
    result = []
    for row in items:
        if row['connectionID'] != connection_id or now - row['updatedAt'] > 7 * 86400:
            continue
        row = dict(row)
        if row['state'] == 'running':
            try:
                pid = row.get('ownerPID')
                if type(pid) is not int or pid <= 0:
                    raise ProcessLookupError()
                os.kill(pid, 0)
                if now - row['startedAt'] > 6 * 3600:
                    raise ProcessLookupError()
            except OSError:
                row['state'] = 'unknown'
        row['title'] = KINDS[row['kind']]
        row['elapsedSeconds'] = max(0, now - row['startedAt'])
        result.append(row)
    return sorted(result, key=lambda r: (r['state'] == 'running', r['updatedAt']), reverse=True)[:30]


def snapshot(paths, connection, live, post, now=None):
    now = time.time() if now is None else now
    result = dict(ok=True, events=rows(paths, connection['id'], now),
                  message='App requests show measured elapsed time or duration. Transfer timing starts when observed. Finder cache hits and Quick Look requests may not be individually identifiable.')
    if live.get('state') != 'connected':
        return result
    try:
        core = post(live, 'core/stats', {}, timeout=1, max_response_bytes=1024 * 1024)
        transfers = core.get('transferring', []) or []
        if not isinstance(transfers, list):
            raise ValueError('Invalid transfers')
        current = []
        for transfer in transfers[:30]:
            if not isinstance(transfer, dict) or not isinstance(transfer.get('name'), str):
                continue
            name = transfer['name'][:512]
            current.append(name)
            # Keep observations distinct from exact-duration app operations.
            with update(paths) as records:
                row = next((r for r in records if r['connectionID'] == connection['id'] and r['kind'] == 'transfer'
                            and r.get('sessionID') == live.get('sessionID') and r['path'] == name and r['state'] == 'running'), None)
                if row is None:
                    row = dict(id=str(uuid.uuid4()), connectionID=connection['id'], kind='transfer', path=name,
                               state='running', startedAt=now, updatedAt=now, observed=True,
                               sessionID=live.get('sessionID'), ownerPID=live.get('pid'))
                    records.append(row)
                row['updatedAt'] = now
                for source, target in [('bytes', 'bytes'), ('size', 'size'), ('speed', 'speed')]:
                    value = transfer.get(source)
                    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                        row[target] = value
        with update(paths) as records:
            for row in records:
                if (row['connectionID'] == connection['id'] and row['kind'] == 'transfer' and row['state'] == 'running'
                        and (row['path'] not in current or row.get('sessionID') != live.get('sessionID'))):
                    row.update(state='unknown', updatedAt=now)
        result['events'] = rows(paths, connection['id'], now)
    except (OSError, ValueError, TypeError):
        result['message'] = 'Live transfer counters are unavailable. App request history remains visible; missing observations are not proof of completion.'
        for row in result['events']:
            if row.get('observed') and row['state'] == 'running':
                row['state'] = 'unknown'
    return result
