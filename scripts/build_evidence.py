"""Bind pipeline receipts to source bytes and the selected build profile."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def source_snapshot(root):
    names = subprocess.check_output(['git','ls-files','-z','--cached','--others',
                                     '--exclude-standard'], cwd=root).split(b'\0')
    entries = []
    for name in sorted(set(n for n in names if n)):
        relative = name.decode('utf-8')
        path = root / relative
        if not path.exists():
            entries.append([relative, 'deleted'])
        elif path.is_symlink():
            entries.append([relative, 'link:' + path.readlink().as_posix()])
        else:
            with path.open('rb') as stream:
                entries.append([relative, hashlib.file_digest(stream, 'sha256').hexdigest()])
    return hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()


def identity(root, profile):
    return {'source_sha256': source_snapshot(root),
            'profile_sha256': hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()}


def require_receipt(path, current):
    if not Path(path).is_file():
        raise RuntimeError('Required pipeline stage has not completed: ' + Path(path).name)
    receipt = json.loads(Path(path).read_text('utf-8'))
    if receipt.get('identity') != current:
        raise RuntimeError('Source or build profile changed. Repeat configure, build, test and install.')
    return receipt


def write_receipt(path, current, **evidence):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name + '.', delete=False) as target:
        target.write(json.dumps({'identity': current, **evidence}, indent=2) + '\n')
        pending = Path(target.name)
    try:
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def build_artifacts(build, qt_prefix, windows):
    paths = {}
    for name in ('qcopilots-websearch','qcws_tests','qcws_http_tests','qcws_process_tests','qcws_windows_security_tests'):
        path = build / (name + ('.exe' if windows else ''))
        if path.is_file():
            paths['build/' + path.name] = path
    if 'build/qcopilots-websearch' + ('.exe' if windows else '') not in paths:
        raise RuntimeError('Build executable is missing')
    for folder, label in ((build,'build'),(qt_prefix/'bin','qt/bin'),
                          (qt_prefix/'lib','qt/lib'),(qt_prefix/'plugins','qt/plugins')):
        if folder.is_dir():
            candidates = folder.rglob('*') if label == 'qt/plugins' else folder.iterdir()
            for path in candidates:
                if path.is_file() and (path.suffix.lower() == '.dll' or '.so' in path.name):
                    paths[label + '/' + path.relative_to(folder).as_posix()] = path
    cache = (build / 'CMakeCache.txt').read_text('utf-8')
    for line in cache.splitlines():
        if line.startswith(('OPENSSL_SSL_LIBRARY:FILEPATH=', 'OPENSSL_CRYPTO_LIBRARY:FILEPATH=')):
            key, value = line.split('=',1)
            path = Path(value)
            if path.is_file():
                paths[key.split(':')[0]] = path
    entries = {}
    for name, path in sorted(paths.items()):
        with path.open('rb') as stream:
            entries[name] = hashlib.file_digest(stream,'sha256').hexdigest()
    return entries
