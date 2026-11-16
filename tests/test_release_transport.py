"""Transport-only fixtures, not claims of product installation or acceptance."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import types
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_transport as t
import publish_verified_release as runner

COMMIT, TAG = '1' * 40, 'v0.1.4'
IDENTITY = {'commit': '2' * 40, 'files': {'transport.py': '3' * 64}}


def original(folder):
    folder.mkdir()
    content = {'package.zip': b'original zip bytes', 'package.msi': b'original msi bytes',
               'package.deb': b'original deb bytes', 'source.tar.gz': b'original source bytes',
               'quality-001-clean.log': b'', 'quality-032-clean.log': b'', 'quality-002-build.log': b'build OK\r\n'}
    for name, raw in content.items():
        (folder / name).write_bytes(raw)
    t.write(folder / 'quality-quality.json', {'files': {n.removeprefix('quality-'): t.sha(folder / n)
                                                    for n in content if n.startswith('quality-')}})
    t.write(folder / t.MANIFEST, {'schema_version': 1, 'source_commit': COMMIT, 'tag': TAG,
                                'candidate_run_id': 12, 'signed': False, 'assets': t.inventory(folder)})
    (folder / t.SUMS).write_text(''.join(f'{v}  {k}\n' for k, v in t.inventory(folder).items()), encoding='ascii')
    return folder


def recovery():
    previous = b'original draft checksum inventory\n'
    return {'draft_id': 77, 'old_checksums_asset_id': 91,
            'old_checksums_sha256': hashlib.sha256(previous).hexdigest(),
            'old_checksums_base64': base64.b64encode(previous).decode()}


class FakeGitHub:
    def __init__(self):
        self.state = {'id': 77, 'draft': True, 'published_at': None, 'tag_name': TAG, 'target_commitish': COMMIT}
        self.tag = COMMIT
        self.stored = {}
        self.calls = []
        self.next_id = 100
        self.bad_download = None
        self.fail_upload = None
        self.after_upload = False
        self.change_tag_after_upload = False
        self.omit_uploaded = False
        self.download_counts = {}

    def add(self, name, raw, asset_id=None):
        self.next_id += 1
        self.stored[name] = {'id': asset_id or self.next_id, 'name': name, 'size': len(raw), 'state': 'uploaded', 'raw': raw}

    def tag_commit(self, tag):
        return 'f' * 40 if self.change_tag_after_upload and self.after_upload else self.tag

    def release(self, release_id):
        return copy.deepcopy(self.state)

    def assets(self, release_id):
        return [{**{k: v for k, v in a.items() if k != 'raw'}, 'download_count': self.download_counts.get(a['name'], 0)}
                for a in self.stored.values() if not (self.omit_uploaded and self.after_upload and a['name'] == 'package.zip')]

    def download(self, asset, target):
        self.calls.append(('download', asset['name']))
        self.download_counts[asset['name']] = self.download_counts.get(asset['name'], 0) + 1
        raw = self.stored[asset['name']]['raw']
        if self.bad_download == asset['name'] and self.after_upload:
            raw += b'corrupt'
        Path(target).write_bytes(raw)

    def create(self, tag, commit, notes):
        self.calls.append(('create', tag))
        return self.state['id']

    def delete(self, asset_id):
        self.calls.append(('delete', asset_id))
        name, = [n for n, a in self.stored.items() if a['id'] == asset_id]
        del self.stored[name]

    def upload(self, tag, path):
        self.calls.append(('upload', path.name))
        if self.fail_upload == path.name:
            raise RuntimeError('injected upload failure')
        if path.name in self.stored:
            raise RuntimeError('no clobber permitted')
        self.add(path.name, path.read_bytes())
        self.after_upload = True

    def publish(self, release_id):
        self.calls.append(('publish', release_id))
        self.state['draft'] = False
        self.state['published_at'] = 'published'


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = original(self.root / 'original')
        self.notes = self.root / 'notes.md'
        self.notes.write_text('reviewed notes')

    def prepare(self, resume=None, name='physical'):
        return t.prepare(self.original, self.root / name, IDENTITY, resume)

    def refresh_sums(self, folder):
        (folder / t.SUMS).write_text(''.join(f'{v}  {k}\n' for k, v in t.inventory(folder).items() if k != t.SUMS),
                                   encoding='ascii')

    def test_empty_logs_are_real_empty_members_with_original_bytes_and_deterministic_archive(self):
        before = t.inventory(self.original)
        output = self.prepare()
        second = self.prepare(name='second')
        self.assertEqual(t.inventory(self.original), before)
        self.assertEqual(t.inventory(output), t.inventory(second))
        self.assertTrue(all(p.stat().st_size > 0 for p in output.iterdir()))
        self.assertFalse((output / 'quality-001-clean.log').exists())
        self.assertEqual((output / 'quality-002-build.log').read_bytes(), b'build OK\r\n')
        with zipfile.ZipFile(output / t.ARCHIVE) as archive:
            self.assertEqual(archive.read('quality-001-clean.log'), b'')
            self.assertEqual(archive.read('quality-032-clean.log'), b'')
            self.assertEqual(archive.read(t.MANIFEST), (self.original / t.MANIFEST).read_bytes())
            self.assertEqual(archive.read(t.SUMS), (self.original / t.SUMS).read_bytes())
        self.assertEqual(t.verify(self.original, output, IDENTITY), t.inventory(output))

    def test_original_mismatch_extra_or_unexpected_empty_are_rejected(self):
        for mutation in ('tamper', 'extra', 'empty-package'):
            with self.subTest(mutation=mutation):
                folder = original(self.root / mutation)
                if mutation == 'extra':
                    (folder / 'extra.txt').write_text('unverified')
                else:
                    (folder / 'package.zip').write_bytes(b'changed' if mutation == 'tamper' else b'')
                with self.assertRaises(RuntimeError):
                    t.prepare(folder, self.root / (mutation + '-out'), IDENTITY)
                self.assertFalse((self.root / (mutation + '-out')).exists())

    def test_coherently_rehashed_archive_tamper_and_duplicate_are_rejected(self):
        for mode in ('changed', 'duplicate', 'extra', 'link'):
            with self.subTest(mode=mode):
                output = self.prepare(name=mode)
                with zipfile.ZipFile(output / t.ARCHIVE) as archive:
                    entries = [(x, archive.read(x)) for x in archive.infolist()]
                with zipfile.ZipFile(output / t.ARCHIVE, 'w') as archive:
                    for item, raw in entries:
                        if item.filename == 'quality-001-clean.log':
                            if mode == 'changed': raw = b'not empty'
                            if mode == 'link': item.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(item, raw)
                    if mode == 'extra': archive.writestr('../escape', b'bad')
                    if mode == 'duplicate': archive.writestr('quality-001-clean.log', b'')
                proof = t.read(output / t.PROOF)
                proof['archive_sha256'] = t.sha(output / t.ARCHIVE)
                t.write(output / t.PROOF, proof)
                manifest = t.read(output / t.MANIFEST)
                for name in (t.ARCHIVE, t.PROOF): manifest['assets'][name] = t.sha(output / name)
                t.write(output / t.MANIFEST, manifest)
                self.refresh_sums(output)
                with self.assertRaises(RuntimeError): t.verify(self.original, output, IDENTITY)

    def test_new_release_uploads_only_nonempty_and_verifies_before_publish(self):
        output = self.prepare()
        github = FakeGitHub()
        result = t.publish(self.original, output, IDENTITY, github, self.notes)
        self.assertTrue(result['published'])
        self.assertEqual(github.calls[-1], ('publish', 77))
        self.assertEqual(set(github.stored), set(t.inventory(output)))
        self.assertTrue(all(a['size'] > 0 for a in github.stored.values()))

    def test_check_only_validates_existing_bytes_without_any_remote_mutation(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                resume = recovery() if existing else None
                output = self.prepare(resume, 'check-' + str(existing))
                github = FakeGitHub()
                if existing:
                    github.add('package.zip', (output / 'package.zip').read_bytes(), 90)
                    github.add(t.SUMS, base64.b64decode(resume['old_checksums_base64']), 91)
                result = t.publish(self.original, output, IDENTITY, github, self.notes, resume, check_only=True)
                self.assertFalse(result['published'])
                self.assertTrue(result['check_only'])
                self.assertFalse(any(c[0] in ('create', 'upload', 'delete', 'publish') for c in github.calls))
                if existing: self.assertIn(('download', 'package.zip'), github.calls)

    def test_resume_reuses_package_and_only_replaces_authorized_checksum_id(self):
        resume = recovery()
        output = self.prepare(resume)
        github = FakeGitHub()
        github.add('package.zip', (output / 'package.zip').read_bytes(), 90)
        github.add(t.SUMS, base64.b64decode(resume['old_checksums_base64']), 91)
        result = t.publish(self.original, output, IDENTITY, github, self.notes, resume)
        self.assertTrue(result['published'])
        self.assertNotIn(('upload', 'package.zip'), github.calls)
        self.assertEqual([c for c in github.calls if c[0] == 'delete'], [('delete', 91)])
        self.assertEqual(github.stored['package.zip']['id'], 90)

    def test_unknown_or_mismatched_existing_assets_never_mutate(self):
        for mode in ('extra', 'package', 'wrong-checksum-id', 'wrong-checksum-hash', 'published', 'wrong-tag', 'wrong-target'):
            with self.subTest(mode=mode):
                resume = recovery()
                output = self.prepare(resume, mode)
                github = FakeGitHub()
                if mode == 'extra': github.add('unrelated.bin', b'other')
                if mode == 'package': github.add('package.zip', b'wrong')
                if mode.startswith('wrong-checksum'):
                    github.add(t.SUMS, b'wrong' if mode.endswith('hash') else base64.b64decode(resume['old_checksums_base64']),
                               92 if mode.endswith('id') else 91)
                if mode == 'published': github.state.update(draft=False, published_at='already')
                if mode == 'wrong-tag': github.tag = 'f' * 40
                if mode == 'wrong-target': github.state['target_commitish'] = 'main'
                with self.assertRaises(RuntimeError): t.publish(self.original, output, IDENTITY, github, self.notes, resume)
                self.assertFalse(any(c[0] in ('upload', 'delete', 'publish') for c in github.calls))

    def test_failures_after_partial_upload_remain_draft(self):
        for mode in ('upload', 'download', 'tag-change', 'missing'):
            with self.subTest(mode=mode):
                output = self.prepare(name=mode)
                github = FakeGitHub()
                if mode == 'upload': github.fail_upload = t.PROOF
                if mode == 'download': github.bad_download = 'package.zip'
                if mode == 'tag-change': github.change_tag_after_upload = True
                if mode == 'missing': github.omit_uploaded = True
                with self.assertRaises(RuntimeError): t.publish(self.original, output, IDENTITY, github, self.notes)
                self.assertTrue(github.state['draft'])
                self.assertFalse(any(c[0] == 'publish' for c in github.calls))

    def test_same_recovery_inputs_can_resume_after_partial_upload_replaced_checksums(self):
        resume = recovery()
        output = self.prepare(resume)
        github = FakeGitHub()
        github.add(t.SUMS, base64.b64decode(resume['old_checksums_base64']), 91)
        github.fail_upload = t.PROOF
        with self.assertRaises(RuntimeError): t.publish(self.original, output, IDENTITY, github, self.notes, resume)
        self.assertNotEqual(github.stored[t.SUMS]['id'], 91)
        prior_uploads = [c for c in github.calls if c[0] == 'upload' and c[1] == 'package.zip']
        github.fail_upload = None
        result = t.publish(self.original, output, IDENTITY, github, self.notes, resume)
        self.assertTrue(result['published'])
        self.assertEqual(prior_uploads, [c for c in github.calls if c[0] == 'upload' and c[1] == 'package.zip'])

    def test_create_refuses_an_existing_draft_without_mutating(self):
        github = t.GitHub('owner/project')
        with patch.object(github, 'api', return_value=[{'tag_name': TAG, 'draft': True}]) as request:
            with self.assertRaisesRegex(RuntimeError, 'already exists'): github.create(TAG, COMMIT, self.notes)
            self.assertEqual(request.call_count, 1)

    def test_exact_incomplete_source_is_recorded_replaced_and_retry_reuses_bytes(self):
        resume = recovery()
        starter = {'id': 95, 'name': 'source.tar.gz', 'size': (self.original / 'source.tar.gz').stat().st_size,
                   'state': 'starter', 'digest': None}
        resume['incomplete_assets'] = [starter]
        output = self.prepare(resume)
        github = FakeGitHub()
        github.stored[starter['name']] = {**starter, 'raw': b'not retrievable'}
        result = t.publish(self.original, output, IDENTITY, github, self.notes, resume, check_only=True)
        self.assertEqual(result['incomplete_assets_identified'], {'source.tar.gz': 95})
        self.assertNotIn('source.tar.gz', result['existing_assets_verified'])
        self.assertEqual(github.calls, [])
        github.fail_upload = 'source.tar.gz'
        with self.assertRaises(RuntimeError): t.publish(self.original, output, IDENTITY, github, self.notes, resume)
        self.assertEqual(github.calls.count(('delete', 95)), 1)
        self.assertNotIn('source.tar.gz', github.stored)
        github.fail_upload = None
        result = t.publish(self.original, output, IDENTITY, github, self.notes, resume)
        self.assertTrue(result['published'])
        self.assertEqual(github.calls.count(('delete', 95)), 1)
        self.assertEqual(github.stored['source.tar.gz']['raw'], (self.original / 'source.tar.gz').read_bytes())
        github.state.update(draft=True, published_at=None)
        github.calls.clear()
        t.publish(self.original, output, IDENTITY, github, self.notes, resume)
        self.assertFalse(any(c[0] in ('delete', 'upload') for c in github.calls))

    def test_incomplete_source_identity_or_verified_size_mismatch_never_mutates(self):
        starter = {'id': 95, 'name': 'source.tar.gz', 'size': (self.original / 'source.tar.gz').stat().st_size,
                   'state': 'starter', 'digest': None}
        for field, value in [('id', 96), ('name', 'package.zip'), ('size', 1), ('digest', 'sha256:' + 'a' * 64),
                             ('state', 'uploaded'), ('allowed_size', 1)]:
            with self.subTest(field=field):
                resume = recovery()
                resume['incomplete_assets'] = [copy.deepcopy(starter)]
                actual = copy.deepcopy(starter)
                if field == 'allowed_size':
                    actual['size'] = resume['incomplete_assets'][0]['size'] = value
                else:
                    actual[field] = value
                output = self.prepare(resume, name='mismatch-' + field)
                github = FakeGitHub()
                github.stored[actual['name']] = {**actual, 'raw': b'invalid uploaded bytes'}
                with self.assertRaises(RuntimeError): t.publish(self.original, output, IDENTITY, github, self.notes, resume)
                self.assertFalse(any(c[0] in ('delete', 'upload', 'publish') for c in github.calls))

    def test_incomplete_asset_changes_during_validation_prevent_deletion(self):
        resume = recovery()
        starter = {'id': 95, 'name': 'source.tar.gz', 'size': (self.original / 'source.tar.gz').stat().st_size,
                   'state': 'starter', 'digest': None}
        resume['incomplete_assets'] = [starter]
        output = self.prepare(resume)
        github = FakeGitHub()
        github.stored[starter['name']] = {**starter, 'raw': b'incomplete'}
        original_assets = github.assets
        calls = 0
        def changed(release_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                github.stored['source.tar.gz']['state'] = 'uploaded'
            return original_assets(release_id)
        with patch.object(github, 'assets', side_effect=changed), self.assertRaises(RuntimeError):
            t.publish(self.original, output, IDENTITY, github, self.notes, resume)
        self.assertEqual(github.calls, [])

    def test_upload_targets_exact_release_id_with_raw_file(self):
        github = t.GitHub('owner/project')
        package = self.original / 'package.zip'
        with patch.object(github, 'command', return_value=types.SimpleNamespace(stdout=json.dumps(
                {'name': package.name, 'size': package.stat().st_size}).encode())) as command:
            github.upload(77, package)
            argv = command.call_args.args[0]
            self.assertIn('/releases/77/assets?name=package.zip', argv[1])
            self.assertEqual(argv[argv.index('--input') + 1], str(package))

    def test_runner_uses_original_verifier_without_publish_and_stops_on_failure(self):
        candidate = self.root / 'candidate'
        candidate.mkdir()
        calls = []
        def git(argv, **kwargs):
            return COMMIT + '\n' if argv[-2:] == ['rev-parse', 'HEAD'] else b''
        def execute(argv, **kwargs):
            calls.append((argv, kwargs))
            raise RuntimeError('injected original gate failure')
        argv = ['publish_verified_release.py', '--Tag', TAG, '--CandidateRunId', '12', '--EvidenceCommit', '4' * 40,
                '--EvidencePath', 'release-evidence/windows.json', '--EvidenceSHA256', '5' * 64,
                '--ToolsCommit', '2' * 40, '--CandidateSource', str(candidate), '--Output', str(self.root / 'runner-out')]
        with patch.object(sys, 'argv', argv), patch.object(t, 'tools_identity', return_value=IDENTITY), \
                patch.object(runner.subprocess, 'check_output', side_effect=git), \
                patch.object(runner.subprocess, 'run', side_effect=execute), patch.object(t, 'GitHub') as github:
            with self.assertRaisesRegex(RuntimeError, 'original gate failure'): runner.main()
            github.assert_not_called()
        command, options = calls[0]
        self.assertEqual(command[1], str(candidate / 'scripts/promote_release.py'))
        self.assertNotIn('--Publish', command)
        self.assertEqual(options['cwd'], candidate)

    def test_pagination_collects_more_than_one_hundred_assets(self):
        github = t.GitHub('owner/project')
        with patch.object(github, 'api', side_effect=[list(range(100)), list(range(100, 125))]) as request:
            self.assertEqual(github.assets(77), list(range(125)))
            self.assertIn('page=2', request.call_args.args[0])

    def test_ambiguous_or_incomplete_remote_assets_rejected(self):
        github = FakeGitHub()
        github.add('a.log', b'a')
        item = github.assets(77)[0]
        for entries in ([item, item], [{**item, 'state': 'starter'}], [{**item, 'name': '../escape'}]):
            with patch.object(github, 'assets', return_value=entries), self.assertRaises(RuntimeError):
                t.asset_map(github, 77)


if __name__ == '__main__':
    unittest.main()
