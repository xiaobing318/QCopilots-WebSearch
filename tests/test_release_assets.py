import hashlib
import json
import io
from pathlib import Path
import stat
import sys
import tarfile
import types
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import package_inventory as assets
import publish_verified_release as publication
import verify_release
from process_cleanup import wait_owned
import soak
REAL_ZIP = zipfile.ZipFile
REG = stat.S_IFREG | 0o644
LINK = stat.S_IFLNK | 0o777


class ProcessCleanupTests(unittest.TestCase):
    def test_reaped_root_pid_is_never_reopened(self):
        process = types.SimpleNamespace(pid=12345, poll=lambda: 1, returncode=1)
        with patch.object(soak.psutil, 'Process', side_effect=AssertionError('reused root PID')), \
                patch.object(soak, 'wait_owned', side_effect=AssertionError('unowned descendants')):
            report = soak.stop_process(process)
        self.assertEqual(report['error'], 'early_exit')
        self.assertFalse(report['passed'])
        self.assertFalse(report['forced'])

    def test_reused_pid_is_not_reopened_and_uncertain_identity_is_not_success(self):
        def forbidden_wait(*args, **kwargs):
            raise AssertionError('A reused numeric PID must never be reopened for wait')
        reused = types.SimpleNamespace(is_running=lambda: False, wait=forbidden_wait,
                                      status=forbidden_wait)
        alive = types.SimpleNamespace(is_running=lambda: True, status=lambda: 'running', wait=forbidden_wait)
        zombie = types.SimpleNamespace(is_running=lambda: True, status=lambda: 'zombie', wait=forbidden_wait)
        def denied():
            raise PermissionError('Unconfirmed original process')
        uncertain = types.SimpleNamespace(is_running=denied, wait=forbidden_wait)
        self.assertEqual(wait_owned([reused, alive, zombie, uncertain], 0), [alive, uncertain])

    def test_identity_exit_is_observed_without_numeric_pid_wait(self):
        states = iter([True, False])
        process = types.SimpleNamespace(is_running=lambda: next(states), status=lambda: 'running')
        with patch('process_cleanup.time.sleep'), patch('process_cleanup.time.monotonic', side_effect=[0, 0, 0.1]):
            self.assertEqual(wait_owned([process], 1), [])


class PackageInventoryTests(unittest.TestCase):
    def inventory(self, members):
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with REAL_ZIP(buffer, 'w') as archive:
                for name, content, mode in members:
                    entry = zipfile.ZipInfo(name)
                    entry.create_system = 3
                    entry.external_attr = mode << 16
                    archive.writestr(entry, content)
        with patch.object(assets.zipfile, 'ZipFile', lambda _: REAL_ZIP(io.BytesIO(buffer.getvalue()))):
            return assets.package_files(types.SimpleNamespace(suffix='.zip'))

    def deb_inventory(self, members, exit_code=0):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            for name, kind, payload in members:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                if kind == tarfile.REGTYPE:
                    entry.size = len(payload)
                    archive.addfile(entry, io.BytesIO(payload))
                else:
                    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                        entry.linkname = payload
                    archive.addfile(entry)
        process = types.SimpleNamespace(stdout=io.BytesIO(buffer.getvalue()),
            wait=lambda **kwargs: exit_code, poll=lambda: exit_code, kill=lambda: None)
        with patch.object(assets.subprocess, 'Popen', return_value=process):
            return assets.package_files(types.SimpleNamespace(suffix='.deb'))

    def test_hashes_payload_bytes(self):
        item = self.inventory([('bin/app', b'hello', REG)])[0]
        self.assertEqual(item['hashes'][0]['content'], hashlib.sha256(b'hello').hexdigest())

    def test_rejects_unsafe_paths(self):
        for name in ('/escape', 'C:/escape', '../escape', 'a/../b', 'a\\..\\b', 'a//b',
                     'CON.txt', 'a.', 'a ', 'a:stream', 'a?'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.inventory([(name, b'x', REG)])
        with self.assertRaises(RuntimeError):
            assets.canonical_member('a\x00suffix', windows=True)

    def test_checks_directory_entries(self):
        with self.assertRaises(RuntimeError):
            self.inventory([('../', b'', stat.S_IFDIR | 0o755), ('safe', b'x', REG)])
        with self.assertRaises(RuntimeError):
            self.inventory([('a/', b'hidden', stat.S_IFDIR | 0o755)])
        with self.assertRaises(RuntimeError):
            self.inventory([('a/', b'', stat.S_IFDIR | 0o755), ('a', b'x', REG)])

    def test_rejects_duplicate_normalized_names(self):
        for pair in (('a','a'),('./a','a'),('A','a')):
            with self.subTest(pair=pair), self.assertRaises(RuntimeError):
                self.inventory([(name, b'x', REG) for name in pair])

    def test_rejects_file_as_parent(self):
        with self.assertRaises(RuntimeError):
            self.inventory([('a', b'x', REG), ('a/b', b'y', REG)])

    def test_records_internal_symlink(self):
        result = self.inventory([('lib/real', b'x', REG), ('lib/link', b'real', LINK)])
        item = next(value for value in result if value['name'] == 'lib/link')
        self.assertNotIn('hashes', item)
        self.assertIn({'name':'qcws:link-target','value':'real'}, item['properties'])

    def test_rejects_invalid_links(self):
        for target in (b'missing', b'../../escape', b'/absolute', b'x' * 4097):
            with self.subTest(target=target[:20]), self.assertRaises(RuntimeError):
                self.inventory([('lib/link', target, LINK)])
        with self.assertRaises(RuntimeError):
            self.inventory([('a', b'b', LINK), ('b', b'a', LINK)])

    def test_rejects_device(self):
        with self.assertRaises(RuntimeError):
            self.inventory([('dev/x', b'', stat.S_IFCHR | 0o600)])

    def test_debian_root_and_links(self):
        items = self.deb_inventory([('./', tarfile.DIRTYPE, ''),
            ('./opt/app/lib/real', tarfile.REGTYPE, b'x'),
            ('./opt/app/lib/symbolic', tarfile.SYMTYPE, 'real'),
            ('./opt/app/lib/hard', tarfile.LNKTYPE, './opt/app/lib/real')])
        self.assertEqual(len(items), 3)
        hard = next(x for x in items if x['name'].endswith('/hard'))
        self.assertIn({'name':'qcws:link-kind','value':'hard'}, hard['properties'])

    def test_debian_keeps_case_distinct(self):
        self.assertEqual(len(self.deb_inventory([('A', tarfile.REGTYPE, b'x'),
                                               ('a', tarfile.REGTYPE, b'y')])), 2)

    def test_debian_failures_propagate(self):
        for members in ([('../bad', tarfile.REGTYPE, b'x')], [('dev', tarfile.CHRTYPE, '')]):
            with self.assertRaises(RuntimeError):
                self.deb_inventory(members)
        with self.assertRaises(RuntimeError):
            self.deb_inventory([('safe', tarfile.REGTYPE, b'x')], exit_code=2)

    def test_checksum_gate_detects_tampering_and_unlisted_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root/'candidate.zip'
            package.write_bytes(b'accepted bytes')
            (root/'SHA256SUMS').write_text(verify_release.sha(package)+'  candidate.zip\n',encoding='utf-8')
            self.assertEqual(set(verify_release.sums(root)),{'candidate.zip'})
            package.write_bytes(b'changed bytes')
            with self.assertRaisesRegex(RuntimeError,'hash mismatch'):
                verify_release.sums(root)
            package.write_bytes(b'accepted bytes')
            (root/'unexpected.msi').write_bytes(b'incomplete package set')
            with self.assertRaisesRegex(RuntimeError,'Unlisted'):
                verify_release.sums(root)

    def test_artifact_extraction_rejects_paths_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for number,(name,mode) in enumerate((('../escape',REG),('/absolute',REG),('link',LINK))):
                archive = root/(str(number)+'.zip')
                with REAL_ZIP(archive,'w') as output:
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = mode << 16
                    output.writestr(entry,b'content')
                with self.assertRaises(RuntimeError):
                    publication.extract(archive,root/('output-'+str(number)))
            self.assertFalse((root/'escape').exists())

    def test_attestation_requires_certificate_bound_candidate(self):
        import copy
        repository = {'id': 101, 'owner': {'id': 202}}
        certificate = {'runInvocationURI': 'https://github.com/owner/project/actions/runs/123/attempts/2',
            'sourceRepositoryIdentifier': '101', 'sourceRepositoryOwnerIdentifier': '202',
            'sourceRepositoryDigest': 'a' * 40, 'sourceRepositoryRef': 'refs/tags/v0.1.0'}
        record = {'verificationResult': {'signature': {'certificate': certificate},
            'verifiedTimestamps': [{'type': 'timestamp'}],
            'statement': {'subject': [{'digest': {'sha256': 'b' * 64}}]}}}
        def validate(records):
            return publication.attestation_match(records, 'b' * 64, 'owner/project', repository,
                                                      'a' * 40, 'v0.1.0', 123, 2)
        self.assertEqual(validate([record]), [0])
        previous = copy.deepcopy(record)
        previous['verificationResult']['signature']['certificate']['runInvocationURI'] = 'https://github.com/owner/project/actions/runs/123/attempts/1'
        self.assertEqual(validate([previous]), [0])
        for key, value in (('runInvocationURI', 'https://github.com/owner/project/actions/runs/123/attempts/3'),
                           ('runInvocationURI', 'https://github.com/owner/project/actions/runs/123/attempts/0'),
                           ('runInvocationURI', 'https://github.com/owner/project/actions/runs/999/attempts/1'),
                           ('sourceRepositoryIdentifier', '303'), ('sourceRepositoryOwnerIdentifier', '303'),
                           ('sourceRepositoryDigest', 'c' * 40), ('sourceRepositoryRef', 'refs/heads/main')):
            altered = copy.deepcopy(record)
            altered['verificationResult']['signature']['certificate'][key] = value
            with self.assertRaises(RuntimeError):
                validate([altered])
        for altered in ({}, {'verificationResult': {'statement': record['verificationResult']['statement']}}):
            with self.assertRaises(RuntimeError):
                validate([altered])

class PublicationInputTests(unittest.TestCase):
    commit, tag, run_id = 'a' * 40, 'v0.1.0', 123
    repository = {'id': 101, 'owner': {'id': 202}}

    def artifact_fixture(self):
        values, raw = [], {}
        for number, name in enumerate(publication.ARTIFACT_NAMES, 11):
            archive = io.BytesIO()
            with REAL_ZIP(archive, 'w') as target:
                target.writestr('payload', name)
            raw[number] = archive.getvalue()
            values.append({'id': number, 'name': name, 'expired': False,
                'digest': 'sha256:' + hashlib.sha256(raw[number]).hexdigest(),
                'workflow_run': {'id': self.run_id, 'head_sha': self.commit,
                                 'repository_id': 101, 'head_repository_id': 101}})
        calls = []
        def api(path):
            self.assertEqual(path, '/actions/runs/123/artifacts?per_page=100&page=1')
            return {'total_count': len(values), 'artifacts': values}
        def command(args, **kwargs):
            self.assertEqual(args[:2], ['api', '--allow-escape-sequences'])
            artifact_id = int(args[2].split('/')[-2])
            calls.append(artifact_id)
            kwargs['stdout'].write(raw[artifact_id])
        github = types.SimpleNamespace(base='repos/owner/project', api=api, command=command)
        return values, raw, calls, github

    def test_artifact_ids_and_digests_are_pinned_before_first_download(self):
        values, raw, calls, github = self.artifact_fixture()
        original = github.command
        def replace_listing(args, **kwargs):
            original(args, **kwargs)
            values[1]['id'] = 999
            values[1]['digest'] = 'sha256:' + '0' * 64
        github.command = replace_listing
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'artifacts'
            publication.download_artifacts(destination, github, self.repository, self.commit, self.run_id)
            self.assertEqual(calls, [11, 12])
            for name in publication.ARTIFACT_NAMES:
                self.assertEqual((destination / name / 'payload').read_text(), name)

    def test_invalid_artifact_origin_or_ambiguity_stops_before_download(self):
        for change in (lambda v: v[0].update(expired=True),
                       lambda v: v[0].update(digest='invalid'),
                       lambda v: v[0]['workflow_run'].update(id=999),
                       lambda v: v[0]['workflow_run'].update(head_sha='b' * 40),
                       lambda v: v[0]['workflow_run'].update(repository_id=999),
                       lambda v: v[0]['workflow_run'].update(head_repository_id=999),
                       lambda v: v.append(v[0]),
                       lambda v: v.pop()):
            values, raw, calls, github = self.artifact_fixture()
            change(values)
            with tempfile.TemporaryDirectory() as temporary, self.assertRaises(RuntimeError):
                publication.download_artifacts(Path(temporary) / 'artifacts', github, self.repository,
                                               self.commit, self.run_id)
            self.assertEqual(calls, [])

    def test_download_hash_is_checked_before_extraction(self):
        values, raw, calls, github = self.artifact_fixture()
        raw[11] += b'changed'
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'artifacts'
            with self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
                publication.download_artifacts(destination, github, self.repository, self.commit, self.run_id)
            self.assertFalse((destination / publication.ARTIFACT_NAMES[0]).exists())

    def test_same_run_in_progress_is_valid_but_foreign_sources_fail(self):
        run = {'id': self.run_id, 'head_sha': self.commit, 'event': 'push', 'run_attempt': 2,
               'status': 'in_progress', 'workflow_id': 77, 'repository': {'id': 101},
               'head_repository': {'id': 101}}
        data = {'': self.repository, '/actions/runs/123': run,
                '/actions/workflows/77': {'path': '.github/workflows/ci.yml'}}
        github = types.SimpleNamespace(api=lambda path: data[path], tag_commit=lambda tag: self.commit)
        self.assertEqual(publication.run_identity(github, self.commit, self.tag, self.run_id, 2), self.repository)
        for name, value in (('event', 'pull_request'), ('head_sha', 'b' * 40), ('run_attempt', 3)):
            original = run[name]
            run[name] = value
            with self.assertRaises(RuntimeError):
                publication.run_identity(github, self.commit, self.tag, self.run_id, 2)
            run[name] = original
        for attempt in (0, -1, True):
            with self.assertRaises(RuntimeError):
                publication.run_identity(github, self.commit, self.tag, self.run_id, attempt)

    def test_attestation_cli_pins_workflow_signer_and_exact_package_digest(self):
        import json
        commands = []
        def command(args, **kwargs):
            commands.append(args)
            path = Path(args[2])
            certificate = {'runInvocationURI': 'https://github.com/owner/project/actions/runs/123/attempts/1',
                'sourceRepositoryIdentifier': '101', 'sourceRepositoryOwnerIdentifier': '202',
                'sourceRepositoryDigest': self.commit, 'sourceRepositoryRef': 'refs/tags/' + self.tag}
            record = {'verificationResult': {'signature': {'certificate': certificate}, 'verifiedTimestamps': [1],
                'statement': {'subject': [{'digest': {'sha256': verify_release.sha(path)}}]}}}
            return types.SimpleNamespace(stdout=json.dumps([record]))
        github = types.SimpleNamespace(repository='owner/project', command=command)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for suffix in ('.zip', '.msi', '.deb'):
                (output / ('package' + suffix)).write_bytes(suffix.encode())
            publication.verify_attestations(output, github, self.repository, self.commit, self.tag, self.run_id, 2)
            self.assertEqual(len(list(output.iterdir())), 3)
        self.assertEqual(len(commands), 3)
        for args in commands:
            self.assertIn('--deny-self-hosted-runners', args)
            self.assertEqual(args[args.index('--cert-identity') + 1],
                             'https://github.com/owner/project/.github/workflows/ci.yml@refs/tags/v0.1.0')
            self.assertEqual(args[args.index('--signer-digest') + 1], self.commit)
            self.assertEqual(args[args.index('--source-digest') + 1], self.commit)

    def test_cli_verification_failure_never_calls_publication(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / 'checkout'
            checkout.mkdir()
            argv = ['publish_verified_release.py', '--ArtifactsRoot', str(root / 'artifacts'),
                    '--Tag', self.tag, '--Commit', self.commit, '--Output', str(root / 'release'), '--CheckOnly']
            env = {'GITHUB_RUN_ID': str(self.run_id), 'GITHUB_RUN_ATTEMPT': '2',
                   'GITHUB_SHA': self.commit, 'GITHUB_REF': 'refs/tags/' + self.tag,
                   'GITHUB_EVENT_NAME': 'push', 'GITHUB_REPOSITORY': 'owner/project'}
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, env), \
                    patch.object(publication, 'ROOT', checkout), \
                    patch.object(publication.subprocess, 'check_output', side_effect=[self.commit, b'']), \
                    patch.object(publication, 'run_identity', return_value=self.repository), \
                    patch.object(publication, 'download_artifacts'), \
                    patch.object(publication, 'assemble', side_effect=RuntimeError('bad package')), \
                    patch.object(publication, 'verify_attestations') as attestation, \
                    patch.object(publication.transport, 'publish') as publish:
                with self.assertRaisesRegex(RuntimeError, 'bad package'):
                    publication.main()
                attestation.assert_not_called()
                publish.assert_not_called()

    def test_cli_attestation_failure_stops_and_check_only_reaches_transport(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / 'checkout'
            checkout.mkdir()
            argv = ['publish_verified_release.py', '--ArtifactsRoot', str(root / 'artifacts'),
                    '--Tag', self.tag, '--Commit', self.commit, '--Output', str(root / 'release'), '--CheckOnly']
            env = {'GITHUB_RUN_ID': str(self.run_id), 'GITHUB_RUN_ATTEMPT': '2',
                   'GITHUB_SHA': self.commit, 'GITHUB_REF': 'refs/tags/' + self.tag,
                   'GITHUB_EVENT_NAME': 'push', 'GITHUB_REPOSITORY': 'owner/project'}
            for failure in (RuntimeError('unsigned package'), None):
                with patch.object(sys, 'argv', argv), patch.dict(os.environ, env), \
                        patch.object(publication, 'ROOT', checkout), \
                        patch.object(publication.subprocess, 'check_output', side_effect=[self.commit, b'']), \
                        patch.object(publication, 'run_identity', return_value=self.repository), \
                        patch.object(publication, 'download_artifacts'), \
                        patch.object(publication, 'assemble'), \
                        patch.object(publication, 'verify_attestations', side_effect=failure), \
                        patch.object(publication.transport, 'publish', return_value={'tag': self.tag}) as publish, \
                        patch('builtins.print'):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, 'unsigned package'):
                            publication.main()
                        publish.assert_not_called()
                    else:
                        publication.main()
                        self.assertTrue(publish.call_args.kwargs['check_only'])


if __name__ == '__main__':
    unittest.main()
