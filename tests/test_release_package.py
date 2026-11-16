"""Packaging retains local validation without exporting acceptance records."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_assets


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PackageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qcws-package-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.build, self.output = self.root / 'build', self.root / 'packages'
        self.build.mkdir()
        self.output.mkdir()
        self.current = {'source_sha256': '1' * 64, 'profile_sha256': '2' * 64}
        self.files = {
            'bin/qcopilots-websearch.exe': b'controlled executable',
            'bin/libssl-3-x64.dll': b'controlled ssl',
            'bin/libcrypto-3-x64.dll': b'controlled crypto',
            'share/qcopilots-websearch/licenses/openssl/license.txt': b'controlled license',
        }
        runtime = {'files': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest()}
                             for name, data in self.files.items() if name.startswith('bin/')]}
        self.names = ['core', 'http-limits', 'tls', 'stdio-process', 'release-assets',
                      'build-pipeline', 'windows-security']
        self.junit(self.names)
        artifacts = {'build/qcopilots-websearch.exe': runtime['files'][0]['sha256']}
        self.receipts = {
            'build': {'identity': self.current, 'artifacts': artifacts},
            'test': {'identity': self.current, 'artifacts': artifacts,
                     'junit_sha256': digest(self.build / 'test-results.xml')},
            'install': {'identity': self.current, 'tested_artifacts': artifacts, 'runtime': runtime},
        }
        self.write_receipts()
        self.pipeline = types.SimpleNamespace(build=self.build, profile={'platform': 'windows-x64'},
            install=self.root / 'install', env={}, tool_paths={}, digest=self.file_digest, run=self.command)
        self.build.joinpath('CMakeCache.txt').write_text(
            'QCWS_VERSION:STRING=0.1.4\nFETCHCONTENT_SOURCE_DIR_HTTPLIB:PATH=' + str(self.root) + '\n')
        self.pipeline.lock = {'sources': {key: {'url': 'https://example.invalid/' + key, 'sha256': '3' * 64}
                                         for key in release_assets.LIBRARIES}}
        self.downloads = {}
        for key, name in [('qt_source', 'qt-source.tar.xz'), ('openssl_windows', 'openssl-source.tar.gz')]:
            source = self.root / name
            source.write_bytes(('controlled ' + key).encode())
            spec = {'url': 'https://example.invalid/' + name, 'sha256': digest(source)}
            self.pipeline.lock[key] = spec
            self.downloads[spec['url']] = source
        self.pipeline.download = lambda spec: self.downloads[spec['url']]
        self.packages = [self.output / 'windows.zip', self.output / 'windows.msi']
        for package in self.packages:
            package.write_bytes(('controlled ' + package.suffix).encode())
        self.zip_items = self.inventory('QCopilots-WebSearch/')
        self.msi_items = self.inventory('[ProgramFiles64Folder]/QCopilots-WebSearch/')
        self.addCleanup(patch.stopall)
        patch.object(release_assets, 'os', types.SimpleNamespace(name='nt')).start()
        patch.object(release_assets, 'identity', return_value=self.current).start()
        patch.object(release_assets, 'package_files', return_value=self.zip_items).start()
        patch.object(release_assets, 'inventory_msi', return_value=(self.msi_items, {'private': 'tables'})).start()

    def file_digest(self, path):
        if Path(path).name == 'httplib.h':
            return '9234907a4431c8e62cb12f7573a03a1c095b028e246f578c9ede700395bd620c'
        return digest(path)

    def command(self, argv, capture=False):
        if argv[:3] == ['git', 'rev-parse', 'HEAD']:
            return 'a' * 40
        if argv[:2] == ['git', 'status']:
            return ''
        if argv[:2] == ['git', 'archive']:
            Path(argv[argv.index('-o') + 1]).write_bytes(b'controlled source archive')
            return ''
        if argv[0] == 'powershell.exe':
            return '14.0.0'
        self.fail('Unexpected external command: ' + repr(argv))

    def inventory(self, prefix):
        return [{'type': 'file', 'name': prefix + name, 'bom-ref': 'file:' + name,
                 'hashes': [{'alg': 'SHA-256', 'content': hashlib.sha256(data).hexdigest()}]}
                for name, data in self.files.items()]

    def junit(self, names, failure=None):
        suite = ET.Element('testsuite')
        for index, name in enumerate(names):
            case = ET.SubElement(suite, 'testcase', name=name)
            if failure and index == 0:
                ET.SubElement(case, failure)
        ET.ElementTree(suite).write(self.build / 'test-results.xml', encoding='utf-8')

    def write_receipts(self):
        for stage, receipt in self.receipts.items():
            (self.build / (stage + '-receipt.json')).write_text(json.dumps(receipt), encoding='utf-8')

    def test_outputs_keep_distribution_assets_without_acceptance_records(self):
        release_assets.generate(self.pipeline, self.output, self.packages)
        names = {path.name for path in self.output.iterdir()}
        self.assertEqual(names, {'windows.zip', 'windows.msi', 'windows.zip.cdx.json', 'windows.msi.cdx.json',
                                'qt-source.tar.xz', 'openssl-source.tar.gz', 'build-info-windows-x64.json',
                                'QCopilots-WebSearch-' + 'a' * 40 + '-source.tar.gz', 'SHA256SUMS'})
        info = json.loads((self.output / 'build-info-windows-x64.json').read_text())
        self.assertFalse({'installed_runtime', 'build_identity', 'junit_sha256', 'metadata_files'} & info.keys())
        self.assertEqual(info['package_sha256'], {package.name: digest(package) for package in self.packages})
        self.assertEqual(info['executable_sha256'], self.receipts['install']['runtime']['files'][0]['sha256'])
        actual_sums = dict(line.split('  ', 1)[::-1] for line in (self.output / 'SHA256SUMS').read_text().splitlines())
        self.assertEqual(actual_sums, {name: digest(self.output / name) for name in names - {'SHA256SUMS'}})
        self.assertTrue(all((self.build / (stage + '-receipt.json')).exists() for stage in self.receipts))

    def test_missing_stale_or_mismatched_build_stages_block_export(self):
        original = copy.deepcopy(self.receipts)
        for stage in original:
            with self.subTest(stage=stage, kind='missing'):
                (self.build / (stage + '-receipt.json')).unlink()
                with self.assertRaisesRegex(RuntimeError, 'Required pipeline stage'):
                    release_assets.validated_install(self.pipeline, self.root)
                self.write_receipts()
            with self.subTest(stage=stage, kind='stale'):
                self.receipts[stage]['identity'] = {'source_sha256': 'stale'}
                self.write_receipts()
                with self.assertRaisesRegex(RuntimeError, 'Source or build profile changed'):
                    release_assets.validated_install(self.pipeline, self.root)
                self.receipts = copy.deepcopy(original)
                self.write_receipts()
        for stage, key in [('build', 'artifacts'), ('test', 'artifacts'), ('install', 'tested_artifacts')]:
            with self.subTest(stage=stage, kind='changed-artifacts'):
                self.receipts[stage][key] = {'build/qcopilots-websearch.exe': 'changed'}
                self.write_receipts()
                with self.assertRaisesRegex(RuntimeError, 'artifacts differ'):
                    release_assets.validated_install(self.pipeline, self.root)
                self.receipts = copy.deepcopy(original)
                self.write_receipts()

    def test_missing_duplicate_failed_or_skipped_tests_block_export_even_with_matching_hash(self):
        cases = [(self.names[:-1], None), (self.names + ['extra'], None),
                 (self.names[:-1] + [self.names[0]], None)]
        cases += [(self.names, kind) for kind in ('failure', 'error', 'skipped')]
        for names, failure in cases:
            with self.subTest(names=names, failure=failure):
                self.junit(names, failure)
                self.receipts['test']['junit_sha256'] = digest(self.build / 'test-results.xml')
                self.write_receipts()
                with self.assertRaisesRegex(RuntimeError, 'Incomplete or failed CTest'):
                    release_assets.validated_install(self.pipeline, self.root)
        self.junit(self.names)
        with self.assertRaisesRegex(RuntimeError, 'CTest results changed'):
            release_assets.validated_install(self.pipeline, self.root)

    def test_packaged_runtime_must_still_match_validated_install(self):
        for items in (self.zip_items, self.msi_items):
            items[1]['hashes'][0]['content'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'Packaged runtime differs'):
            release_assets.generate(self.pipeline, self.output, self.packages)
        self.assertFalse((self.output / 'build-info-windows-x64.json').exists())

    def test_ubuntu_requires_its_complete_native_test_set(self):
        self.junit(self.names[:-1])
        self.receipts['test']['junit_sha256'] = digest(self.build / 'test-results.xml')
        self.write_receipts()
        with patch.object(release_assets, 'os', types.SimpleNamespace(name='posix')):
            self.assertEqual(release_assets.validated_install(self.pipeline, self.root), self.receipts['install'])
        with self.assertRaisesRegex(RuntimeError, 'Incomplete or failed CTest'):
            release_assets.validated_install(self.pipeline, self.root)


if __name__ == '__main__':
    unittest.main()
