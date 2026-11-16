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
import promote_release
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


class EvidenceDownloadTests(unittest.TestCase):
    def download(self, raw, metadata, digest=None):
        endpoint = 'repos/owner/project/contents/release-evidence/windows11.json?ref=' + 'a' * 40
        def command(argv, **kwargs):
            self.assertEqual(argv[:3], ['gh', 'api', endpoint])
            if '-H' not in argv or 'Accept: application/vnd.github.object+json' in argv:
                self.assertTrue(kwargs.get('text'))
                return types.SimpleNamespace(stdout=json.dumps(metadata))
            self.assertIn('Accept: application/vnd.github.raw+json', argv)
            self.assertFalse(kwargs.get('text'))
            return types.SimpleNamespace(stdout=raw)
        with patch.object(promote_release.subprocess, 'run', command):
            return promote_release.evidence_bytes('repos/owner/project', 'release-evidence/windows11.json',
                                                  'a' * 40, digest or hashlib.sha256(raw).hexdigest())

    def test_full_soak_evidence_above_contents_inline_limit(self):
        raw = ('{"samples":"' + '中文\\n' * 200000 + '"}\r\n').encode('utf-8')
        self.assertGreater(len(raw), 1024 * 1024)
        metadata = {'type': 'file', 'size': len(raw), 'encoding': 'none', 'content': ''}
        self.assertEqual(self.download(raw, metadata), raw)

    def test_evidence_length_digest_and_metadata_fail_closed(self):
        raw = b'{"passed":true}\r\n'
        metadata = {'type': 'file', 'size': len(raw), 'encoding': 'base64', 'content': ''}
        for changed in ({**metadata, 'size': len(raw) + 1}, {**metadata, 'type': 'dir'},
                        {**metadata, 'size': 101 * 1024**2}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                self.download(raw, changed)
        with self.assertRaisesRegex(RuntimeError, 'digest'):
            self.download(raw, metadata, '0' * 64)


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
                    promote_release.extract(archive,root/('output-'+str(number)))
            self.assertFalse((root/'escape').exists())

    def test_runtime_evidence_rejects_modified_library_hash(self):
        import json
        files = [{'path':'bin/library.dll','sha256':'a'*64}]
        runtime = {'files':files,'sha256':hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        self.assertEqual(verify_release.checked_runtime(runtime),runtime)
        files[0]['sha256'] = 'b'*64
        with self.assertRaisesRegex(RuntimeError,'fingerprint mismatch'):
            verify_release.checked_runtime(runtime)

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
            return promote_release.attestation_match(records, 'b' * 64, 'owner/project', repository,
                                                      'a' * 40, 'v0.1.0', 123, 2)
        self.assertEqual(validate([record]), [0])
        for key, value in (('runInvocationURI', 'https://github.com/owner/project/actions/runs/123/attempts/1'),
                           ('sourceRepositoryIdentifier', '303'), ('sourceRepositoryOwnerIdentifier', '303'),
                           ('sourceRepositoryDigest', 'c' * 40), ('sourceRepositoryRef', 'refs/heads/main')):
            altered = copy.deepcopy(record)
            altered['verificationResult']['signature']['certificate'][key] = value
            with self.assertRaises(RuntimeError):
                validate([altered])
        for altered in ({}, {'verificationResult': {'statement': record['verificationResult']['statement']}}):
            with self.assertRaises(RuntimeError):
                validate([altered])

    def test_windows11_gate_rejects_server_and_missing_host(self):
        for host in ({}, {'system':'Windows','windows':{'product_type':3,'build':26100}},
                     {'system':'Windows','windows':{'product_type':1,'build':19045}}):
            with self.assertRaisesRegex(RuntimeError,'Windows 11'):
                verify_release.require_windows11({'host':host})
        verify_release.require_windows11({'host':{'system':'Windows',
            'windows':{'product_type':1,'build':26100}}})


if __name__ == '__main__':
    unittest.main()
