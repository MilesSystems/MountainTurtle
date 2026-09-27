#!/usr/bin/env python3
"""Build a pinned rclone with small, reviewed paging/concurrency patches."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = {'rclone': 'github.com/rclone/rclone@v1.75.1',
           'sftp': 'github.com/pkg/sftp@v1.13.11', 'nfs': 'github.com/willscott/go-nfs@v0.0.4'}


def run(args, **kwargs):
    return subprocess.run(list(map(str, args)), cwd=ROOT / 'mountengine', check=True, **kwargs)


def prepare(go):
    stage = ROOT / 'build/mount-engine'
    stage.mkdir(parents=True, exist_ok=True)
    patch = ROOT / 'mountengine/paging.patch'
    fingerprint = hashlib.sha256(patch.read_bytes() + (ROOT / 'mountengine/go.sum').read_bytes()).hexdigest()
    marker = stage / 'fingerprint'
    dependencies = stage / 'deps'
    if not marker.exists() or marker.read_text() != fingerprint:
        if dependencies.exists():
            shutil.rmtree(dependencies)
        dependencies.mkdir()
        for name, module in MODULES.items():
            record = json.loads(run([go, 'mod', 'download', '-json', module], capture_output=True, text=True).stdout)
            destination = dependencies / name
            shutil.copytree(record['Dir'], destination)
            for directory, _, files in os.walk(destination):
                Path(directory).chmod(0o755)
                for filename in files:
                    item = Path(directory) / filename
                    if not item.is_symlink():
                        item.chmod(0o644)
        subprocess.run(['/usr/bin/patch', '--batch', '-p1', '-i', str(patch)], cwd=dependencies, check=True, stdout=subprocess.DEVNULL)
        marker.write_text(fingerprint)
    modfile = stage / 'engine.mod'
    content = (ROOT / 'mountengine/go.mod').read_text()
    for name, module in MODULES.items():
        content += f'\nreplace {module.split("@")[0]} => {json.dumps(str(dependencies / name))}\n'
    modfile.write_text(content)
    shutil.copyfile(ROOT / 'mountengine/go.sum', modfile.with_suffix('.sum'))
    return modfile


def notices(go, modfile, output):
    raw = run([go, 'list', '-modfile', modfile, '-deps', '-json', '.'], capture_output=True, text=True).stdout
    decoder = json.JSONDecoder()
    modules = {}
    while raw.strip():
        record, end = decoder.raw_decode(raw.lstrip())
        raw = raw.lstrip()[end:]
        module = record.get('Module', {})
        if module.get('Path') and not module.get('Main'):
            modules[module['Path']] = module.get('Replace', module).get('Dir')
    with Path(output).open('w') as handle:
        handle.write('Mountain Turtle mount engine — dependency licenses\n\n')
        for name, directory in sorted(modules.items()):
            handle.write('\n' + '=' * 72 + '\n' + name + '\n')
            found = False
            for entry in sorted(Path(directory).iterdir()):
                if entry.is_file() and entry.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'COPYRIGHT')):
                    handle.write('\n' + entry.name + '\n' + entry.read_text(errors='replace') + '\n')
                    found = True
            if not found:
                raise RuntimeError(f'No license file found for bundled dependency {name}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output')
    parser.add_argument('--arch', choices=['arm64', 'x86_64'], default='arm64')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--notices')
    args = parser.parse_args()
    go = subprocess.check_output([ROOT / 'scripts/fetch-go.sh'], text=True).strip()
    modfile = prepare(go)
    if args.test:
        run([go, 'test', '-race', '-modfile', modfile, 'github.com/rclone/rclone/vfs',
             'github.com/rclone/rclone/cmd/serve/nfs', 'github.com/rclone/rclone/backend/union',
             'github.com/willscott/go-nfs', 'github.com/pkg/sftp'])
    if args.output:
        env = dict(os.environ, GOOS='darwin', GOARCH='amd64' if args.arch == 'x86_64' else 'arm64', CGO_ENABLED='0')
        run([go, 'build', '-trimpath', '-modfile', modfile, '-ldflags=-s -w', '-o', args.output, '.'], env=env)
    if args.notices:
        notices(go, modfile, args.notices)


if __name__ == '__main__':
    main()
