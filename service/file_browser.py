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


def run(paths, connection_id, directory):
    connection = turtle.find_connection(turtle.Store(paths).read(), connection_id)
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
                                       env=environment, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return process.wait()
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
    parser.add_argument('connection')
    parser.add_argument('directory', nargs='?', default='')
    args = parser.parse_args()
    try:
        return run(turtle.Paths(resources=args.resource_dir), args.connection, args.directory)
    except (ValueError, OSError) as error:
        print(json.dumps({'error': str(error), 'complete': False}), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
