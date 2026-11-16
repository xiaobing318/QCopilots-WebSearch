"""Exercise publication with in-memory GitHub assets, never remote writes."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_transport as t

COMMIT, TAG = '1' * 40, 'v0.1.4'


def distribution(folder):
    folder.mkdir()
    files = {'package.zip': b'original zip bytes', 'package.msi': b'original msi bytes',
             'package.deb': b'original deb bytes', 'source.tar.gz': b'original source bytes'}
    for name, content in files.items():
        (folder / name).write_bytes(content)
    (folder / t.SUMS).write_text(''.join(f'{digest}  {name}\n' for name, digest in t.inventory(folder).items()),
                               encoding='ascii')
    return folder


class FakeGitHub:
    def __init__(self, exists=False):
        self.exists = exists
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
        self.publish_error = None
        self.download_counts = {}

    def add(self, name, raw):
        self.next_id += 1
        self.stored[name] = {'id': self.next_id, 'name': name, 'size': len(raw), 'state': 'uploaded', 'raw': raw}

    def tag_commit(self, tag):
        return 'f' * 40 if self.change_tag_after_upload and self.after_upload else self.tag

    def find_release(self, tag):
        return copy.deepcopy(self.state) if self.exists else None

    def release(self, release_id):
        assert release_id == self.state['id']
        return copy.deepcopy(self.state)

    def assets(self, release_id):
        assert release_id == self.state['id']
        return [{**{k: v for k, v in item.items() if k != 'raw'},
                 'download_count': self.download_counts.get(item['name'], 0)}
                for item in self.stored.values()
                if not (self.omit_uploaded and self.after_upload and item['name'] == 'package.zip')]

    def download(self, asset, target):
        self.calls.append(('download', asset['name']))
        self.download_counts[asset['name']] = self.download_counts.get(asset['name'], 0) + 1
        raw = self.stored[asset['name']]['raw']
        if self.bad_download == asset['name'] and self.after_upload:
            raw += b'corrupt'
        Path(target).write_bytes(raw)

    def create(self, tag, commit, notes):
        self.calls.append(('create', tag))
        assert not self.exists
        self.exists = True
        return self.state['id']

    def upload(self, release_id, path):
        assert release_id == self.state['id']
        self.calls.append(('upload', path.name))
        if self.fail_upload == path.name:
            raise RuntimeError('injected upload failure')
        if path.name in self.stored:
            raise RuntimeError('no clobber permitted')
        self.add(path.name, path.read_bytes())
        self.after_upload = True

    def publish(self, release_id):
        self.calls.append(('publish', release_id))
        if self.publish_error == 'before':
            raise RuntimeError('injected publication failure')
        self.state.update(draft=False, published_at='published')
        if self.publish_error == 'after':
            raise TimeoutError('publication response was lost')


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = distribution(self.root / 'distribution')
        self.notes = self.root / 'notes.md'
        self.notes.write_text('Source commit: ' + COMMIT)

    def publish(self, github, **options):
        return t.publish(self.output, github, TAG, COMMIT, self.notes, **options)

    def assert_read_only(self, github):
        self.assertFalse(any(call[0] in ('create', 'upload', 'delete', 'publish') for call in github.calls))

    def copy_all(self, github):
        for path in self.output.iterdir():
            github.add(path.name, path.read_bytes())

    def test_new_release_uploads_and_downloads_complete_set_before_publish(self):
        github = FakeGitHub()
        result = self.publish(github)
        self.assertTrue(result['published'])
        self.assertEqual(github.calls[0], ('create', TAG))
        self.assertEqual(github.calls[-1], ('publish', 77))
        self.assertEqual(set(github.stored), set(t.checksums(self.output)))
        self.assertEqual({call[1] for call in github.calls if call[0] == 'download'}, set(github.stored))

    def test_check_only_never_creates_uploads_or_publishes(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                github = FakeGitHub(existing)
                if existing:
                    github.add('package.zip', (self.output / 'package.zip').read_bytes())
                result = self.publish(github, check_only=True)
                self.assertTrue(result['check_only'])
                self.assertFalse(result['published'])
                self.assert_read_only(github)
                if existing:
                    self.assertIn(('download', 'package.zip'), github.calls)

    def test_existing_draft_reuses_matching_files_and_adds_only_missing(self):
        github = FakeGitHub(True)
        github.add('package.zip', (self.output / 'package.zip').read_bytes())
        existing_id = github.stored['package.zip']['id']
        result = self.publish(github)
        self.assertTrue(result['published'])
        self.assertEqual(result['reused_assets'], ['package.zip'])
        self.assertNotIn(('upload', 'package.zip'), github.calls)
        self.assertNotIn(('create', TAG), github.calls)
        self.assertEqual(github.stored['package.zip']['id'], existing_id)

    def test_existing_unknown_mismatched_or_unfinished_assets_never_mutate(self):
        for mode in ('extra', 'package', 'checksum', 'starter', 'tag', 'target'):
            with self.subTest(mode=mode):
                github = FakeGitHub(True)
                if mode == 'extra':
                    github.add('unrelated.bin', b'other')
                if mode == 'package':
                    github.add('package.zip', b'wrong')
                if mode == 'checksum':
                    github.add(t.SUMS, b'old checksum bytes')
                if mode == 'starter':
                    github.add('package.zip', b'incomplete')
                    github.stored['package.zip']['state'] = 'starter'
                if mode == 'tag':
                    github.tag = 'f' * 40
                if mode == 'target':
                    github.state['target_commitish'] = 'main'
                with self.assertRaises(RuntimeError):
                    self.publish(github)
                self.assert_read_only(github)

    def test_published_matching_release_is_idempotent_including_check_only(self):
        for check_only in (False, True):
            with self.subTest(check_only=check_only):
                github = FakeGitHub(True)
                github.state.update(draft=False, published_at='already')
                self.copy_all(github)
                result = self.publish(github, check_only=check_only)
                self.assertTrue(result['published'])
                self.assertEqual(result['check_only'], check_only)
                self.assert_read_only(github)
                self.assertEqual(len(github.calls), len(github.stored))

    def test_published_incomplete_or_changed_release_is_never_repaired(self):
        for mode in ('missing', 'changed', 'extra'):
            with self.subTest(mode=mode):
                github = FakeGitHub(True)
                github.state.update(draft=False, published_at='already')
                self.copy_all(github)
                if mode == 'missing':
                    del github.stored['package.zip']
                if mode == 'changed':
                    github.add('package.zip', b'different')
                if mode == 'extra':
                    github.add('unknown.bin', b'unknown')
                with self.assertRaises(RuntimeError):
                    self.publish(github)
                self.assert_read_only(github)

    def test_failures_after_upload_leave_draft(self):
        for mode in ('upload', 'download', 'tag-change', 'missing'):
            with self.subTest(mode=mode):
                github = FakeGitHub()
                if mode == 'upload':
                    github.fail_upload = 'package.zip'
                if mode == 'download':
                    github.bad_download = 'package.zip'
                if mode == 'tag-change':
                    github.change_tag_after_upload = True
                if mode == 'missing':
                    github.omit_uploaded = True
                with self.assertRaises(RuntimeError):
                    self.publish(github)
                self.assertTrue(github.state['draft'])
                self.assertFalse(any(call[0] == 'publish' for call in github.calls))

    def test_retry_after_partial_upload_reuses_exact_original_assets(self):
        github = FakeGitHub()
        github.fail_upload = 'package.zip'
        with self.assertRaisesRegex(RuntimeError, 'upload failure'):
            self.publish(github)
        original_ids = {name: item['id'] for name, item in github.stored.items()}
        self.assertTrue(original_ids)
        github.fail_upload = None
        result = self.publish(github)
        self.assertTrue(result['published'])
        self.assertTrue(all(github.stored[name]['id'] == asset_id for name, asset_id in original_ids.items()))
        self.assertEqual(github.calls.count(('create', TAG)), 1)

    def test_publication_response_loss_recovers_only_after_remote_confirmation(self):
        github = FakeGitHub()
        github.publish_error = 'after'
        result = self.publish(github)
        self.assertTrue(result['published'])
        self.assertTrue(all(count == 2 for count in github.download_counts.values()))
        github = FakeGitHub()
        github.publish_error = 'before'
        with self.assertRaisesRegex(RuntimeError, 'publication failure'):
            self.publish(github)
        self.assertTrue(github.state['draft'])

    def test_asset_changes_during_existing_validation_prevent_upload(self):
        github = FakeGitHub(True)
        github.add('package.zip', (self.output / 'package.zip').read_bytes())
        original_assets = github.assets
        calls = 0
        def changed(release_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                github.stored['package.zip']['id'] += 1
            return original_assets(release_id)
        with patch.object(github, 'assets', side_effect=changed), self.assertRaisesRegex(RuntimeError, 'changed'):
            self.publish(github)
        self.assert_read_only(github)

    def test_local_checksum_missing_extra_empty_and_tampered_assets_fail_before_github(self):
        for mode in ('missing', 'extra', 'empty', 'tampered'):
            with self.subTest(mode=mode):
                output = distribution(self.root / mode)
                if mode == 'missing':
                    (output / t.SUMS).unlink()
                elif mode == 'extra':
                    (output / 'extra.bin').write_bytes(b'extra')
                else:
                    (output / 'package.zip').write_bytes(b'' if mode == 'empty' else b'changed')
                with patch.object(t, 'GitHub') as github, self.assertRaises(RuntimeError):
                    t.publish(output, github, TAG, COMMIT, self.notes)
                self.assertEqual(github.mock_calls, [])

    def test_duplicate_or_self_referencing_checksum_rejected(self):
        for duplicate in ('package.zip', t.SUMS):
            with self.subTest(duplicate=duplicate):
                output = distribution(self.root / ('duplicate-' + duplicate))
                with (output / t.SUMS).open('a') as sums:
                    sums.write('a' * 64 + '  ' + duplicate + '\n')
                with self.assertRaisesRegex(RuntimeError, 'Duplicate or self-referencing'):
                    t.checksums(output)

    def test_upload_uses_exact_release_id_and_raw_file(self):
        github = t.GitHub('owner/project')
        package = self.output / 'package.zip'
        with patch.object(github, 'command', return_value=types.SimpleNamespace(stdout=json.dumps(
                {'name': package.name, 'size': package.stat().st_size}).encode())) as command:
            github.upload(77, package)
            argv = command.call_args.args[0]
            self.assertIn('/releases/77/assets?name=package.zip', argv[1])
            self.assertEqual(argv[argv.index('--input') + 1], str(package))

    def test_asset_and_release_pagination(self):
        github = t.GitHub('owner/project')
        with patch.object(github, 'api', side_effect=[list(range(100)), list(range(100, 125))]) as request:
            self.assertEqual(github.assets(77), list(range(125)))
            self.assertIn('page=2', request.call_args.args[0])
        wanted = {'id': 77, 'tag_name': TAG}
        with patch.object(github, 'api', side_effect=[[{'tag_name': 'old'}] * 100, [wanted]]) as request:
            self.assertEqual(github.find_release(TAG), wanted)
            self.assertIn('page=2', request.call_args.args[0])
        with patch.object(github, 'api', return_value=[wanted, wanted]), self.assertRaisesRegex(RuntimeError, 'Multiple'):
            github.find_release(TAG)

    def test_create_checks_for_racing_release_without_writing(self):
        github = t.GitHub('owner/project')
        with patch.object(github, 'api', return_value=[{'tag_name': TAG}]) as request:
            with self.assertRaisesRegex(RuntimeError, 'appeared'):
                github.create(TAG, COMMIT, self.notes)
            self.assertEqual(request.call_count, 1)

    def test_ambiguous_unsafe_and_unfinished_remote_assets_rejected(self):
        github = FakeGitHub(True)
        github.add('package.zip', b'content')
        item = github.assets(77)[0]
        for entries in ([item, item], [{**item, 'state': 'starter'}], [{**item, 'name': '../escape'}],
                        [{**item, 'size': 0}], [{**item, 'id': True}]):
            with patch.object(github, 'assets', return_value=entries), self.assertRaises(RuntimeError):
                t.asset_map(github, 77)
        starter = {**item, 'state': 'starter', 'size': 0}
        with patch.object(github, 'assets', return_value=[starter]), \
                self.assertRaisesRegex(RuntimeError, 'Unfinished asset in release 77: 101'):
            t.asset_map(github, 77)


if __name__ == '__main__':
    unittest.main()
