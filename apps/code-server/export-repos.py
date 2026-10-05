#!/usr/bin/env python3
"""Expose live devbox Git checkouts through a writable NFS directory."""

import os
from pathlib import Path
import subprocess

HOME = Path('/home/andrei')
EXPORT = Path('/srv/code-repos')
SKIP = {
    'node_modules', 'venv', 'env', '__pycache__', 'build', 'dist', 'target',
    'android-sdk', 'Android', 'yolov9-export', 'lib', 'site-packages',
}


def repositories():
    for directory, dirs, files in os.walk(HOME):
        source = Path(directory)
        relative = source.relative_to(HOME)
        if '.git' in dirs or '.git' in files:
            yield source, relative
            dirs[:] = []
            continue
        dirs[:] = [
            name for name in dirs
            if name not in SKIP
            and (not name.startswith('.') or name in {'.codex-work', '.worktrees'})
            and not (source / name).is_symlink()
        ]
        if len(relative.parts) >= 8:
            dirs[:] = []


def main():
    EXPORT.mkdir(parents=True, exist_ok=True)
    count = 0
    for source, relative in repositories():
        destination = EXPORT / relative
        destination.mkdir(parents=True, exist_ok=True)
        mounted = subprocess.run(
            ['findmnt', '--mountpoint', str(destination)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not mounted:
            subprocess.run(['mount', '--bind', str(source), str(destination)], check=True)
            subprocess.run(['mount', '--make-private', str(destination)], check=True)
            count += 1
        subprocess.run(['mount', '-o', 'remount,bind,rw', str(destination)], check=True)
    if count:
        subprocess.run(['exportfs', '-ra'], check=True)
    print(f'Added {count} live repository mounts.', flush=True)


if __name__ == '__main__':
    main()
