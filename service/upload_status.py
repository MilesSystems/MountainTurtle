"""Local-only upload confidence and bounded retry; never enumerate remote files."""
import json
import os
import time
from pathlib import Path


def dirty_cache(connection, paths, limit=10000, seconds=0.2):
    root = paths.cache / connection['id'] / 'vfsMeta'
    count, visited, complete = 0, 0, True
    deadline = time.monotonic() + seconds
    if any(p.is_symlink() for p in (paths.cache, root.parent, root)):
        return 0, False
    def failed(_):
        nonlocal complete
        complete = False
    for directory, directories, files in os.walk(root, followlinks=False, onerror=failed):
        visited += 1
        if visited > limit or time.monotonic() > deadline:
            return count, False
        for name in list(directories):
            if (Path(directory) / name).is_symlink():
                directories.remove(name)
                complete = False
        for name in files:
            visited += 1
            if visited > limit or time.monotonic() > deadline:
                return count, False
            path = Path(directory) / name
            try:
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    complete = False
                    continue
                item = json.loads(path.read_text())
                if not isinstance(item, dict) or type(item.get('Dirty')) is not bool:
                    complete = False
                elif item['Dirty']:
                    count += 1
            except (OSError, ValueError):
                complete = False
    # A missing cache is normal; an inaccessible cache is not proof of completion.
    if not root.exists() and root.parent.exists():
        try:
            list(root.parent.iterdir())
        except OSError:
            complete = False
    return count, complete


def queue_counts(response):
    """Queue inspection does not acquire rclone's directory-tree locks."""
    queue = response.get('queue')
    if not isinstance(queue, list):
        raise ValueError('Upload queue unavailable')
    seen, queued, active, retrying = set(), 0, 0, 0
    for item in queue:
        if (not isinstance(item, dict) or type(item.get('id')) is not int
                or item['id'] < 0 or item['id'] in seen
                or type(item.get('uploading')) is not bool
                or type(item.get('tries')) is not int or item['tries'] < 0):
            raise ValueError('Invalid upload queue')
        seen.add(item['id'])
        active += int(item['uploading'])
        queued += int(not item['uploading'])
        retrying += int(not item['uploading'] and item['tries'] > 0)
    return queued, active, retrying


def snapshot(connection, paths, live, mounted, post):
    dirty, complete = dirty_cache(connection, paths)
    result = dict(ok=True, connectionID=connection['id'], checkedAt=time.time(),
                  state='unknown', title='Upload status unavailable',
                  message='Connect this drive to check uploads. Cached changes are preserved.',
                  canRetry=False)
    if dirty:
        result.update(state='pending', title='Changes waiting to upload',
                      message=f'{dirty} cached file(s) contain changes. Keep this drive connected until uploads finish.')
    if not mounted:
        return result
    result['message'] = 'Could not read the live upload queue. Cached changes are preserved; check drive insights or try again.'
    try:
        queue = post(live, 'vfs/queue', {}, timeout=1, max_response_bytes=1024 * 1024)
        queued, active, retrying = queue_counts(queue)
        result.update(queued=queued, active=active, canRetry=queued > 0 and not connection['readOnly'])
        if retrying:
            result.update(state='attention', title='Uploads waiting to retry',
                          message=f'{retrying} queued upload(s) have already been attempted. Keep the drive connected and retry when the server is available.')
        elif queued or active or dirty:
            result.update(state='pending', title='Changes waiting to upload',
                          message=f'{queued} queued · {active} uploading · {dirty} cached file(s) with changes. Keep the drive connected.')
        elif complete:
            result.update(state='clear', title='No queued uploads',
                          message='No upload jobs or dirty cached files were found. Open apps may still have unsaved changes; this is not a backup verification.')
        else:
            result.update(message='The upload queue is empty, but the local cache check is incomplete. Upload completion cannot be confirmed yet.')
    except (OSError, ValueError, TypeError):
        pass
    return result


def retry(connection, live, mounted, post):
    if connection['readOnly'] or not mounted:
        raise ValueError('Connect this drive with write access before retrying uploads.')
    try:
        queue = post(live, 'vfs/queue', {}, timeout=2, max_response_bytes=1024 * 1024).get('queue')
        if not isinstance(queue, list):
            raise ValueError()
        eligible = [item for item in queue if isinstance(item, dict)
                    and type(item.get('id')) is int and item['id'] >= 0
                    and item.get('uploading') is False]
        # Bound the operation; further batches can be requested without remounting.
        count = 0
        deadline = time.monotonic() + 5
        for item in eligible[:20]:
            if time.monotonic() >= deadline:
                break
            post(live, 'vfs/queue-set-expiry', {'id': item['id'], 'expiry': -1}, timeout=1)
            count += 1
        return dict(ok=True, message=f'Retry requested for {count} queued upload(s). Open files upload after they close.')
    except (OSError, ValueError, TypeError):
        raise ValueError('Could not retry the upload queue. Uploads retain their automatic retry schedule; check your connection and rclone version.') from None
