"""Preserve logical evidence while transporting nonempty GitHub Release assets."""
from __future__ import annotations
import hashlib
import base64
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import quote
import zipfile

ARCHIVE = 'quality-evidence.zip'
PROOF = 'publication-proof.json'
MANIFEST = 'release-manifest.json'
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


def read(path):
    return json.loads(Path(path).read_text('utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')


def inventory(folder):
    paths = list(Path(folder).iterdir())
    need(all(p.is_file() and not p.is_symlink() for p in paths), 'Unexpected asset directory or link')
    return {leaf(p.name): sha(p) for p in sorted(paths)}


def checksums(folder):
    result = {}
    for line in (folder / SUMS).read_text('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        need(match is not None, 'Malformed checksum')
        name = leaf(match[2])
        need(name not in result and name != SUMS, 'Duplicate or self-referencing checksum')
        result[name] = match[1]
    actual = inventory(folder)
    need(result == {k: v for k, v in actual.items() if k != SUMS}, 'Checksum inventory mismatch')
    return actual


def logical_inventory(original):
    original = Path(original)
    files = checksums(original)
    manifest = read(original / MANIFEST)
    need(manifest['assets'] == {k: v for k, v in files.items() if k not in (MANIFEST, SUMS)},
         'Original manifest differs from verified files')
    quality = read(original / 'quality-quality.json')
    names = {'quality-quality.json'}
    for member, digest in quality['files'].items():
        name = 'quality-' + leaf(member)
        need(name not in names and files.get(name) == digest, 'Quality member binding mismatch')
        names.add(name)
    need(names == {n for n in files if n.startswith('quality-')}, 'Unlisted quality asset')
    empty = {name for name in files if (original / name).stat().st_size == 0}
    need(empty <= names and all(name.endswith('.log') for name in empty), 'Unexpected empty non-log asset')
    need(not ({ARCHIVE, PROOF} & set(files)), 'Reserved publication names already present')
    return files, manifest, names, empty


def tools_identity(root, expected=None):
    root = Path(root)
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    need(re.fullmatch(r'[0-9a-f]{40}', commit) and (expected is None or expected == commit), 'Tool commit differs')
    need(not subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain']), 'Tool checkout is dirty')
    names = ('scripts/release_transport.py', 'scripts/publish_verified_release.py', 'scripts/promote_release.py',
             '.github/workflows/release.yml')
    return {'commit': commit, 'files': {name: sha(root / name) for name in names}}


def check_recovery(recovery):
    if recovery is None:
        return
    need(type(recovery.get('draft_id')) is int and recovery['draft_id'] > 0
         and type(recovery.get('old_checksums_asset_id')) is int and recovery['old_checksums_asset_id'] > 0
         and re.fullmatch(r'[0-9a-f]{64}', recovery.get('old_checksums_sha256', '')), 'Invalid recovery identity')
    previous = base64.b64decode(recovery['old_checksums_base64'], validate=True)
    need(hashlib.sha256(previous).hexdigest() == recovery['old_checksums_sha256'], 'Previous checksum evidence differs')
    ids, names = set(), set()
    for asset in recovery.get('incomplete_assets', []):
        need(set(asset) == {'id', 'name', 'size', 'state', 'digest'}
             and type(asset['id']) is int and asset['id'] > 0
             and asset['id'] != recovery['old_checksums_asset_id'] and asset['id'] not in ids
             and type(asset['size']) is int and asset['size'] > 0
             and asset['state'] == 'starter' and asset['digest'] is None,
             'Invalid incomplete asset identity')
        need(leaf(asset['name']) not in names and asset['name'] not in (SUMS, MANIFEST, PROOF, ARCHIVE),
             'Invalid incomplete asset name')
        ids.add(asset['id'])
        names.add(asset['name'])


def prepare(original, output, identity, recovery=None):
    """Keep the verifier output intact and create a separate physical layout."""
    original, output = Path(original), Path(output)
    check_recovery(recovery)
    files, manifest, quality, empty = logical_inventory(original)
    need(not output.exists(), 'Publication output must be new')
    need(output.resolve() != original.resolve() and not output.resolve().is_relative_to(original.resolve()),
         'Publication output must be outside the original directory')
    output.mkdir(parents=True)
    for name in files:
        if name not in empty | {MANIFEST, SUMS}:
            shutil.copyfile(original / name, output / name)
    members = {name: (original / name).read_bytes() for name in sorted(quality | {MANIFEST, SUMS})}
    with zipfile.ZipFile(output / ARCHIVE, 'x', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(members.items()):
            item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(item, raw)
    proof = {'schema_version': 1, 'source_commit': manifest['source_commit'], 'tag': manifest['tag'],
             'candidate_run_id': manifest['candidate_run_id'], 'tools': identity,
             'recovery': recovery, 'logical_files': files, 'archive': ARCHIVE,
             'archive_sha256': sha(output / ARCHIVE),
             'archive_members': {name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                                 for name, raw in sorted(members.items())},
             'archived_empty_logs': sorted(empty)}
    write(output / PROOF, proof)
    physical = {**manifest, 'schema_version': 2, 'logical_manifest': {'archive': ARCHIVE, 'member': MANIFEST,
                  'sha256': files[MANIFEST]}, 'archived_empty_logs': {n: ARCHIVE + '/' + n for n in sorted(empty)},
                'assets': inventory(output)}
    write(output / MANIFEST, physical)
    (output / SUMS).write_text(''.join(f'{digest}  {name}\n' for name, digest in inventory(output).items()),
                              encoding='utf-8', newline='\n')
    verify(original, output, identity, recovery)
    return output


def verify(original, output, identity, recovery=None):
    """Check every logical input byte, including empty archive members."""
    original, output = Path(original), Path(output)
    check_recovery(recovery)
    files, original_manifest, quality, empty = logical_inventory(original)
    physical = checksums(output)
    expected = (set(files) - empty) | {ARCHIVE, PROOF}
    need(set(physical) == expected and all((output / n).stat().st_size > 0 for n in physical),
         'Physical asset set must be complete and nonempty')
    for name in set(files) - empty - {MANIFEST, SUMS}:
        need(physical[name] == files[name], 'Original asset changed: ' + name)
    manifest, proof = read(output / MANIFEST), read(output / PROOF)
    need(manifest['assets'] == {k: v for k, v in physical.items() if k not in (MANIFEST, SUMS)},
         'Physical manifest mismatch')
    need(manifest['schema_version'] == 2 and all(manifest[k] == v for k, v in original_manifest.items()
         if k not in ('assets', 'schema_version')), 'Manifest identity changed')
    need(manifest['logical_manifest'] == {'archive': ARCHIVE, 'member': MANIFEST, 'sha256': files[MANIFEST]}
         and manifest['archived_empty_logs'] == {n: ARCHIVE + '/' + n for n in sorted(empty)}, 'Logical mapping mismatch')
    need(proof['logical_files'] == files and proof['tools'] == identity and proof['recovery'] == recovery
         and proof['archive'] == ARCHIVE and proof['archive_sha256'] == physical[ARCHIVE]
         and proof['archived_empty_logs'] == sorted(empty), 'Publication proof mismatch')
    for key in ('source_commit', 'tag', 'candidate_run_id'):
        need(proof[key] == original_manifest[key], 'Publication identity mismatch')
    expected_members = quality | {MANIFEST, SUMS}
    need(set(proof['archive_members']) == expected_members, 'Archive proof member set differs')
    with zipfile.ZipFile(output / ARCHIVE) as archive:
        entries = archive.infolist()
        need(len(entries) == len(expected_members) and {i.filename for i in entries} == expected_members,
             'Archive has missing, duplicate or unexpected members')
        for item in entries:
            raw = (original / leaf(item.filename)).read_bytes()
            need(stat.S_IFMT(item.external_attr >> 16) == stat.S_IFREG and item.file_size == len(raw)
                 and item.compress_type == zipfile.ZIP_STORED and archive.read(item) == raw,
                 'Archive member bytes or type changed')
            need(proof['archive_members'][item.filename] == {'bytes': len(raw), 'sha256': files[item.filename]},
                 'Archive member digest differs')
    return physical


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
        for page in range(1, 101):
            existing = self.api(f'/releases?per_page=100&page={page}')
            need(not any(r['tag_name'] == tag for r in existing), 'Release already exists, use explicit draft recovery')
            if len(existing) < 100:
                break
        else:
            raise RuntimeError('Release pagination limit')
        return self.api('/releases', '--method', 'POST', '-f', 'tag_name=' + tag,
                        '-f', 'target_commitish=' + commit, '-F', 'draft=true', '-f', 'name=' + tag,
                        '-f', 'body=' + Path(notes).read_text('utf-8'))['id']

    def delete(self, asset_id):
        self.command(['api', '--method', 'DELETE', self.base + '/releases/assets/' + str(asset_id)])

    def upload(self, release_id, path):
        url = (f'https://uploads.github.com/repos/{self.repository}/releases/{release_id}/assets?name='
               + quote(path.name, safe=''))
        uploaded = json.loads(self.command(['api', url, '--method', 'POST', '-H', 'Content-Type: application/octet-stream',
                                           '--input', str(path)], capture_output=True).stdout)
        need(uploaded['name'] == path.name and uploaded['size'] == path.stat().st_size, 'Upload acknowledgement differs')

    def publish(self, release_id):
        self.api('/releases/' + str(release_id), '--method', 'PATCH', '-F', 'draft=false', '-f', 'make_latest=true')


def draft_state(github, release_id, tag, commit):
    state = github.release(release_id)
    need(state['id'] == release_id and state['draft'] is True and state.get('published_at') is None
         and state['tag_name'] == tag and state['target_commitish'] == commit, 'Draft identity changed')
    need(github.tag_commit(tag) == commit, 'Tag changed')
    return state


def asset_map(github, release_id, incomplete=()):
    result, ids = {}, set()
    for asset in github.assets(release_id):
        name = leaf(asset['name'])
        entry = {key: asset.get(key) for key in ('id', 'name', 'size', 'state', 'digest')}
        need(type(asset['id']) is int and asset['id'] > 0 and type(asset['size']) is int and asset['size'] >= 0
             and name not in result and asset['id'] not in ids
             and (asset['state'] == 'uploaded' or entry in incomplete),
             'Ambiguous or unfinished draft asset')
        # Downloads change download_count. Compare content identity, not counters.
        result[name] = entry
        ids.add(asset['id'])
    return result


def publish(original, output, identity, github, notes, recovery=None, check_only=False):
    expected = verify(original, output, identity, recovery)
    manifest = read(Path(output) / MANIFEST)
    tag, commit = manifest['tag'], manifest['source_commit']
    need(github.tag_commit(tag) == commit, 'Tag changed before draft creation')
    if recovery:
        release_id = recovery['draft_id']
    else:
        if check_only:
            return {'tag': tag, 'source_commit': commit, 'assets': expected, 'check_only': True, 'published': False}
        release_id = github.create(tag, commit, notes)
    draft_state(github, release_id, tag, commit)
    incomplete = recovery.get('incomplete_assets', []) if recovery else []
    initial = asset_map(github, release_id, incomplete)
    need(set(initial) <= set(expected), 'Unexpected existing draft assets')
    print(f'Validating {len(initial)} existing assets for draft {release_id}', flush=True)
    replacement = None
    starters = {}
    with tempfile.TemporaryDirectory(prefix='qcws-draft-verification-') as temporary:
        directory = Path(temporary)
        before = directory / 'before'
        before.mkdir()
        for name, asset in initial.items():
            if asset['state'] == 'starter':
                need(asset in incomplete and (Path(output) / name).stat().st_size == asset['size'],
                     'Incomplete asset differs from verified source')
                starters[name] = asset['id']
                continue
            path = before / name
            github.download(asset, path)
            digest = sha(path)
            need(path.stat().st_size == asset['size'], 'Existing asset size mismatch')
            if digest != expected[name]:
                need(recovery and name == SUMS and asset['id'] == recovery['old_checksums_asset_id']
                     and digest == recovery['old_checksums_sha256'],
                     'Existing asset bytes differ: ' + name)
                replacement = asset['id']
        # Complete validation precedes any deletion or upload. No blanket clobber.
        draft_state(github, release_id, tag, commit)
        need(asset_map(github, release_id, incomplete) == initial, 'Draft assets changed during validation')
        if check_only:
            return {'release_id': release_id, 'tag': tag, 'source_commit': commit, 'assets': expected,
                    'existing_assets_verified': sorted(set(initial) - set(starters)),
                    'incomplete_assets_identified': starters, 'check_only': True, 'published': False}
        if replacement is not None:
            github.delete(replacement)
        for asset_id in starters.values():
            github.delete(asset_id)
        print(f'Uploading verified missing assets, total physical inventory {len(expected)}', flush=True)
        for name in sorted(expected):
            if name not in initial or name in starters or (name == SUMS and replacement is not None):
                github.upload(release_id, Path(output) / name)
        draft_state(github, release_id, tag, commit)
        uploaded = asset_map(github, release_id)
        need(set(uploaded) == set(expected), 'Uploaded asset set differs')
        downloaded = directory / 'downloaded'
        downloaded.mkdir()
        print(f'Downloading and verifying all {len(uploaded)} draft assets', flush=True)
        for name, asset in uploaded.items():
            github.download(asset, downloaded / name)
            need((downloaded / name).stat().st_size == asset['size'] and sha(downloaded / name) == expected[name],
                 'Uploaded asset bytes differ: ' + name)
        verify(original, downloaded, identity, recovery)
        draft_state(github, release_id, tag, commit)
        need(asset_map(github, release_id) == uploaded, 'Draft assets changed before publication')
        github.publish(release_id)
        final = github.release(release_id)
        need(final['draft'] is False and final['tag_name'] == tag and final['target_commitish'] == commit,
             'Publication did not complete')
    return {'release_id': release_id, 'tag': tag, 'source_commit': commit, 'assets': expected,
            'replaced_checksum_asset_id': replacement, 'replaced_incomplete_assets': starters, 'published': True}
