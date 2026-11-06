#!/usr/bin/env python3
"""Read-only, cancellable directory streaming using the mount's authentication."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import directory_listings as listings

import turtle_service as turtle


def relative_directory(value):
    if not isinstance(value, str) or value.startswith('/') or '\0' in value:
        raise ValueError('Choose a folder inside this drive')
    if value and any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('Choose a folder inside this drive')
    return value


def listing_command(paths, connection, directory, config_path):
    directory = relative_directory(directory)
    helper = paths.resources.parent / 'Helpers/turtle-rclone'
    if not helper.is_file():
        raise ValueError('The file browser helper is missing. Reinstall Mountain Turtle.')
    # A private per-request config avoids changing a running mount's configuration.
    config, remote = turtle.connection_config(connection, paths)
    Path(config_path).write_text(config)
    Path(config_path).chmod(0o600)
    target = remote.rstrip('/') + ('/' if not remote.endswith(':') else '') + directory
    return [str(helper), 'tree-list', target, '--config', str(config_path),
            '--use-server-modtime', '--contimeout', '10s', '--timeout', '30s',
            '--retries', '1', '--low-level-retries', '1', '--stats', '0', '--log-level', 'ERROR',
            '--filter', '- .DS_Store', '--filter', '- ._*', '--filter', '- .VolumeIcon.icns']


def run(paths, connection_id, directory, refresh=False):
    connection = turtle.find_connection(turtle.Store(paths).read(), connection_id)
    directory = relative_directory(directory)
    cached = listings.load(paths, connection, directory)
    def emit(page):
        print(json.dumps(page), flush=True)
    if cached is not None:
        entries = cached['entries']
        for offset in range(0, len(entries), 256):
            emit({'entries': entries[offset:offset + 256], 'cached': True})
        emit({'cached': True})
        if not refresh and time.time() - cached['savedAt'] < listings.FRESH_SECONDS:
            emit({'complete': True, 'cached': True})
            return 0
    emit({'refreshing': True})
    rclone = turtle.executable('rclone')
    if not rclone:
        raise ValueError('Install rclone to browse this drive')
    environment = turtle.mount_environment(connection, paths, rclone)
    process = None
    def stop(signum, frame):
        if process and process.poll() is None:
            process.terminate()
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with tempfile.TemporaryDirectory(prefix='mountainturtle-browser-') as temporary:
        try:
            process = subprocess.Popen(listing_command(paths, connection, directory, Path(temporary) / 'remote.conf'),
                                       env=environment, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE)
            entries, size, complete, failed = [], 0, False, False
            while True:
                line = process.stdout.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 4 * 1024 * 1024:
                    raise ValueError('The folder response was too large. Retry to continue.')
                page = json.loads(line)
                if not isinstance(page, dict) or not listings.valid_entries(page.get('entries', [])):
                    raise ValueError('The folder response was invalid. Retry to continue.')
                complete = complete or page.get('complete') is True
                failed = failed or bool(page.get('error'))
                size += len(line)
                if entries is not None:
                    if size <= listings.MAX_BYTES:
                        entries.extend(page.get('entries', []))
                    else:
                        entries = None
                if page.get('entries') or page.get('error'):
                    emit({k: v for k, v in page.items() if k in ('entries', 'error')})
            code = process.wait()
            if code == 0 and complete and not failed:
                if entries is not None:
                    listings.save(paths, connection, directory, entries)
                emit({'complete': True})
            elif not failed:
                emit({'error': 'The folder refresh could not finish. Cached entries are still available.'
                      if cached is not None else 'The folder listing could not finish. Retry to continue.'})
            return code if code else (0 if complete and not failed else 1)
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resource-dir', required=True)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('connection')
    parser.add_argument('directory', nargs='?', default='')
    args = parser.parse_args()
    try:
        return run(turtle.Paths(resources=args.resource_dir), args.connection, args.directory, args.refresh)
    except (ValueError, OSError) as error:
        print(json.dumps({'error': str(error), 'complete': False}), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
