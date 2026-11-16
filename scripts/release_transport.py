"""Publish verified distribution files without overwriting existing assets."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote

SUMS = 'SHA256SUMS'


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def leaf(name):
    need(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name), 'Unsafe asset name')
    return name


def inventory(folder):
    folder = Path(folder)
    need(folder.is_dir() and not folder.is_symlink(), 'Expected a distribution directory')
    paths = list(folder.iterdir())
    need(paths and all(p.is_file() and not p.is_symlink() and p.stat().st_size > 0 for p in paths),
         'Distribution assets must be regular, nonempty files')
    return {leaf(p.name): sha(p) for p in sorted(paths)}


def checksums(folder):
    folder = Path(folder)
    actual = inventory(folder)
    need(SUMS in actual, 'Missing SHA256SUMS')
    result = {}
    for line in (folder / SUMS).read_text('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        need(match is not None, 'Malformed checksum')
        name = leaf(match[2])
        need(name not in result and name != SUMS, 'Duplicate or self-referencing checksum')
        result[name] = match[1]
    need(result and result == {k: v for k, v in actual.items() if k != SUMS}, 'Checksum inventory mismatch')
    return actual


class GitHub:
    def __init__(self, repository):
        need(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'Invalid repository')
        self.repository = repository
        self.base = 'repos/' + repository

    def command(self, arguments, **kwargs):
        return subprocess.run(['gh', *arguments], check=True, timeout=300, **kwargs)

    def api(self, path, *arguments):
        return json.loads(self.command(['api', self.base + path, *arguments], capture_output=True).stdout)

    def tag_commit(self, tag):
        obj = self.api('/git/ref/tags/' + quote(tag, safe=''))['object']
        for _ in range(10):
            if obj['type'] == 'commit':
                return obj['sha']
            need(obj['type'] == 'tag', 'Unexpected tag object')
            obj = self.api('/git/tags/' + obj['sha'])['object']
        raise RuntimeError('Tag nesting limit')

    def find_release(self, tag):
        found = []
        for page in range(1, 101):
            batch = self.api(f'/releases?per_page=100&page={page}')
            found.extend(item for item in batch if item['tag_name'] == tag)
            need(len(found) <= 1, 'Multiple releases use the requested tag')
            if len(batch) < 100:
                return found[0] if found else None
        raise RuntimeError('Release pagination limit')

    def release(self, release_id):
        return self.api('/releases/' + str(release_id))

    def assets(self, release_id):
        result = []
        for page in range(1, 101):
            batch = self.api(f'/releases/{release_id}/assets?per_page=100&page={page}')
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise RuntimeError('Asset pagination limit')

    def download(self, asset, target):
        with Path(target).open('xb') as stream:
            self.command(['api', '--allow-escape-sequences', self.base + '/releases/assets/' + str(asset['id']),
                          '-H', 'Accept: application/octet-stream'], stdout=stream)

    def create(self, tag, commit, notes):
        need(self.find_release(tag) is None, 'Release appeared before draft creation, retry without overwriting')
        return self.api('/releases', '--method', 'POST', '-f', 'tag_name=' + tag,
                        '-f', 'target_commitish=' + commit, '-F', 'draft=true', '-f', 'name=' + tag,
                        '-f', 'body=' + Path(notes).read_text('utf-8'))['id']

    def upload(self, release_id, path):
        path = Path(path)
        url = (f'https://uploads.github.com/repos/{self.repository}/releases/{release_id}/assets?name='
               + quote(path.name, safe=''))
        uploaded = json.loads(self.command(['api', url, '--method', 'POST', '-H', 'Content-Type: application/octet-stream',
                                           '--input', str(path)], capture_output=True).stdout)
        need(uploaded['name'] == path.name and uploaded['size'] == path.stat().st_size, 'Upload acknowledgement differs')

    def publish(self, release_id):
        self.api('/releases/' + str(release_id), '--method', 'PATCH', '-F', 'draft=false', '-f', 'make_latest=true')


def release_state(github, release_id, tag, commit, draft=None):
    state = github.release(release_id)
    need(type(release_id) is int and release_id > 0 and state['id'] == release_id
         and type(state['draft']) is bool and state['tag_name'] == tag
         and state['target_commitish'] == commit, 'Release identity changed')
    need((state['draft'] and state.get('published_at') is None)
         or (not state['draft'] and bool(state.get('published_at'))), 'Inconsistent publication state')
    need(draft is None or state['draft'] is draft, 'Release publication state changed')
    need(github.tag_commit(tag) == commit, 'Tag changed')
    return state


def asset_map(github, release_id):
    result, ids = {}, set()
    for asset in github.assets(release_id):
        name = leaf(asset['name'])
        need(type(asset['id']) is int and asset['id'] > 0 and type(asset['size']) is int and asset['size'] >= 0
             and name not in result and asset['id'] not in ids, f'Ambiguous asset in release {release_id}: {name}')
        need(asset['state'] == 'uploaded',
             f'Unfinished asset in release {release_id}: {asset["id"]} ({name}), state={asset["state"]}')
        need(asset['size'] > 0, f'Empty asset in release {release_id}: {asset["id"]} ({name})')
        # Download counters change while verifying. Compare content identity only.
        result[name] = {key: asset.get(key) for key in ('id', 'name', 'size', 'state', 'digest')}
        ids.add(asset['id'])
    return result


def verify_remote(github, assets, expected, folder):
    folder = Path(folder)
    folder.mkdir()
    for name, asset in assets.items():
        path = folder / name
        github.download(asset, path)
        need(path.stat().st_size == asset['size'] and sha(path) == expected[name],
             'Remote asset bytes differ: ' + name)


def publish(output, github, tag, commit, notes, check_only=False):
    """Reuse matching assets, verify all bytes, and publish only a complete release."""
    need(re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag), 'Invalid release tag')
    need(re.fullmatch(r'[0-9a-f]{40}', commit), 'Invalid source commit')
    output = Path(output)
    expected = checksums(output)
    need(github.tag_commit(tag) == commit, 'Tag changed before publication')
    state = github.find_release(tag)
    result = {'tag': tag, 'source_commit': commit, 'assets': expected, 'check_only': check_only, 'published': False}
    if state is None:
        if check_only:
            return result
        release_id = github.create(tag, commit, notes)
        state = release_state(github, release_id, tag, commit, draft=True)
    else:
        release_id = state['id']
        state = release_state(github, release_id, tag, commit)
    result['release_id'] = release_id
    initial = asset_map(github, release_id)
    need(set(initial) <= set(expected), 'Unexpected existing release assets')
    if not state['draft']:
        need(set(initial) == set(expected), 'Published release is incomplete')
    with tempfile.TemporaryDirectory(prefix='qcws-release-verification-') as temporary:
        directory = Path(temporary)
        verify_remote(github, initial, expected, directory / 'existing')
        release_state(github, release_id, tag, commit, draft=state['draft'])
        need(asset_map(github, release_id) == initial, 'Release assets changed during validation')
        result['reused_assets'] = sorted(initial)
        if check_only or not state['draft']:
            result['published'] = not state['draft']
            return result
        # Nothing may be overwritten. A mismatched or unfinished upload requires
        # explicit operator repair, and cannot authorize deleting unrelated files.
        for name in sorted(set(expected) - set(initial)):
            need((output / name).is_file() and not (output / name).is_symlink()
                 and sha(output / name) == expected[name], 'Local distribution changed: ' + name)
            release_state(github, release_id, tag, commit, draft=True)
            github.upload(release_id, output / name)
        release_state(github, release_id, tag, commit, draft=True)
        uploaded = asset_map(github, release_id)
        need(set(uploaded) == set(expected), 'Uploaded asset set differs')
        verify_remote(github, uploaded, expected, directory / 'uploaded')
        release_state(github, release_id, tag, commit, draft=True)
        need(asset_map(github, release_id) == uploaded, 'Release assets changed before publication')
        try:
            github.publish(release_id)
        except Exception:
            # A timeout can follow a successful PATCH. Inspect remote state and
            # bytes before deciding whether the operation actually failed.
            final = release_state(github, release_id, tag, commit)
            if final['draft']:
                raise
            current = asset_map(github, release_id)
            need(current == uploaded, 'Release assets changed after publication')
            verify_remote(github, current, expected, directory / 'recovered')
            release_state(github, release_id, tag, commit, draft=False)
            need(asset_map(github, release_id) == current, 'Release assets changed during recovery')
        else:
            release_state(github, release_id, tag, commit, draft=False)
            need(asset_map(github, release_id) == uploaded, 'Release assets changed after publication')
    result['published'] = True
    return result
