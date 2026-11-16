"""Synthetic release contracts. ZIP inventory is real, DEB reader is controlled.
These fixtures do not claim installation, performance or analysis acceptance.
"""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import package_inventory
import verify_release
from release_quality_fixture import make_quality
from quality_evidence import verify_quality
from public_evidence import load_queries, navigation_summary
COMMIT, TAG, RUN, ATTEMPT, REPOSITORY = '1' * 40, 'v0.1.0', 123, 2, 'example/QCopilots-WebSearch'


def build_fixture(FIXTURE):
    CONTROL = FIXTURE / 'source'
    (CONTROL / 'config').mkdir(parents=True)
    INPUTS = FIXTURE / 'artifacts'
    INPUTS.mkdir()
    (CONTROL / 'tests/data').mkdir(parents=True)
    (CONTROL / 'scripts').mkdir()
    shutil.copyfile(REPO / 'scripts/AcceptWindowsMsi.ps1', CONTROL / 'scripts/AcceptWindowsMsi.ps1')
    query_path = CONTROL / 'tests/data/public_queries.json'
    shutil.copyfile(REPO / 'tests/data/public_queries.json', query_path)
    COMMIT, TAG, RUN, ATTEMPT, REPOSITORY = '1' * 40, 'v0.1.0', 123, 2, 'example/QCopilots-WebSearch'

    def sha_bytes(data):
        return hashlib.sha256(data).hexdigest()

    def put_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

    def component(name, data):
        return {'type': 'file', 'name': name, 'bom-ref': 'file:' + sha_bytes(name.encode()),
                'hashes': [{'alg': 'SHA-256', 'content': sha_bytes(data)}]}

    def runtime(files):
        entries = [{'path': name, 'sha256': sha_bytes(data)} for name, data in sorted(files.items())]
        return {'files': entries, 'sha256': sha_bytes(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode())}

    QT_NAME, QT_BYTES = 'qtbase-everywhere-src-6.12.0.tar.xz', b'controlled Qt source fixture'
    SSL_NAME, SSL_BYTES = 'openssl-3.5.9.tar.gz', b'controlled OpenSSL source fixture'
    put_json(CONTROL / 'config/dependencies.lock.json', {
        'qt_source': {'url': 'https://example.invalid/' + QT_NAME, 'sha256': sha_bytes(QT_BYTES)},
        'openssl_windows': {'url': 'https://example.invalid/' + SSL_NAME, 'sha256': sha_bytes(SSL_BYTES)},
    })
    source_name = f'QCopilots-WebSearch-{COMMIT}-source.tar.gz'
    source_path = FIXTURE / source_name
    with tarfile.open(source_path, 'w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': COMMIT}) as archive:
        entry = tarfile.TarInfo('QCopilots-WebSearch/README.md')
        content = b'controlled project source fixture'
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))

    WIN_HOST = {'system': 'Windows', 'architecture': 'AMD64',
                'windows': {'product_type': 1, 'build': 26100, 'major': 10, 'minor': 0}}
    SERVER_HOST = {'system': 'Windows', 'architecture': 'AMD64',
                   'windows': {'product_type': 3, 'build': 20348, 'major': 10, 'minor': 0}}
    LINUX_HOST = {'system': 'Linux', 'architecture': 'x86_64'}
    debian_entries = None
    infos, sdk_reports, soak_reports = {}, {}, {}

    def refresh_sums(folder):
        (folder / 'SHA256SUMS').write_text(''.join(
            verify_release.sha(p) + '  ' + p.name + '\n'
            for p in sorted(folder.iterdir()) if p.is_file() and p.name != 'SHA256SUMS'), encoding='utf-8')

    for target in ('windows-x64', 'ubuntu-x64'):
        windows = target == 'windows-x64'
        folder = INPUTS / ('packages-' + target)
        folder.mkdir()
        diagnostic = INPUTS / ('diagnostics-' + target) / 'work' / 'ci-artifacts'
        diagnostic.mkdir(parents=True)
        executable = 'bin/qcopilots-websearch' + ('.exe' if windows else '')
        runtime_files = {executable: b'controlled application ' + target.encode()}
        if windows:
            runtime_files.update({'bin/libssl-3-x64.dll': b'ssl', 'bin/libcrypto-3-x64.dll': b'crypto'})
            payload = dict(runtime_files)
            payload['share/qcopilots-websearch/licenses/openssl/LICENSE.txt'] = b'Apache-2.0'
            zipped = folder / 'candidate-windows.zip'
            with zipfile.ZipFile(zipped, 'w') as archive:
                for name, data in payload.items():
                    archive.writestr('QCopilots-WebSearch-0.1.0-win64/' + name, data)
            msi = folder / 'candidate-windows.msi'
            msi.write_bytes(b'opaque MSI fixture. Inventory is supplied by controlled Windows producer.')
            inventories = {zipped.name: package_inventory.package_files(zipped), msi.name: [
                component('[ProgramFiles64Folder]/QCopilots-WebSearch/' + name, data)
                for name, data in sorted(payload.items())]}
        else:
            runtime_files['lib/libQt6Core.so.6'] = b'qt-core-linux'
            deb = folder / 'candidate-ubuntu.deb'
            deb.write_bytes(b'controlled DEB fixture. dpkg reader is substituted on Windows.')
            debian_entries = [component('opt/qcopilots-websearch/' + name, data)
                              for name, data in sorted(runtime_files.items())]
            inventories = {deb.name: debian_entries}
        installed = runtime(runtime_files)
        identity = {'source_sha256': 'a' * 64, 'profile_sha256': sha_bytes(target.encode())}
        artifacts = {'build/' + Path(executable).name: 'b' * 64, 'build/qcws_tests': 'c' * 64}
        names = ['core', 'http-limits', 'tls', 'stdio-process', 'release-assets', 'build-pipeline']
        if windows:
            names.append('windows-security')
        junit = folder / (target + '-test-results.xml')
        junit.write_text('<testsuite tests="' + str(len(names)) + '" failures="0" skipped="0">' +
                         ''.join('<testcase name="' + name + '" status="run"/>' for name in names) + '</testsuite>', encoding='utf-8')
        put_json(folder / (target + '-build-receipt.json'), {'identity': identity, 'artifacts': artifacts})
        put_json(folder / (target + '-test-receipt.json'), {'identity': identity, 'artifacts': artifacts,
                                                         'junit_sha256': verify_release.sha(junit)})
        put_json(folder / (target + '-install-receipt.json'), {'identity': identity, 'tested_artifacts': artifacts,
                                                            'runtime': installed})
        package_hashes = {name: verify_release.sha(folder / name) for name in inventories}
        sboms = {}
        for name, entries in inventories.items():
            sboms[name] = name + '.cdx.json'
            put_json(folder / sboms[name], {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
                'metadata': {'component': {'version': '0.1.0'}, 'properties': [
                    {'name': 'qcws:inventory-package', 'value': name},
                    {'name': 'qcws:inventory-sha256', 'value': package_hashes[name]}]}, 'components': entries})
        (folder / QT_NAME).write_bytes(QT_BYTES)
        extra = {}
        if windows:
            (folder / SSL_NAME).write_bytes(SSL_BYTES)
            extra[SSL_NAME] = sha_bytes(SSL_BYTES)
        shutil.copyfile(source_path, folder / source_name)
        info = {'schema_version': 1, 'source_commit': COMMIT, 'working_tree_dirty': False, 'target': target,
            'project_version': '0.1.0', 'project_source': source_name, 'qt_source': QT_NAME,
            'qt_source_sha256': sha_bytes(QT_BYTES), 'extra_sources': extra, 'package_sha256': package_hashes,
            'sboms': sboms, 'inventory_files': {name: len(entries) for name, entries in inventories.items()},
            'installed_runtime': installed, 'build_identity': identity, 'junit_sha256': verify_release.sha(junit),
            'metadata_files': [], 'executable_sha256': sha_bytes(runtime_files[executable])}
        infos[target] = info
        put_json(folder / ('build-info-' + target + '.json'), info)
        refresh_sums(folder)
        sdk = {'passed': True, 'runtime': installed, 'executable_sha256': info['executable_sha256'],
               'host': SERVER_HOST if windows else LINUX_HOST}
        soak = {'checks_passed': True, 'full_acceptance': True, 'seconds_per_phase': 300, 'runtime': installed,
                'executable_sha256': info['executable_sha256'], 'host': SERVER_HOST if windows else LINUX_HOST}
        sdk_reports[target], soak_reports[target] = sdk, soak
        put_json(diagnostic / 'mcp-sdk.json', sdk)
        put_json(diagnostic / 'soak.json', soak)
        put_json(diagnostic / 'ci-run.json', {'GITHUB_RUN_ID': str(RUN), 'GITHUB_SHA': COMMIT, 'profile': target,
            'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_RUN_ATTEMPT': str(ATTEMPT)})
        if not windows:
            put_json(diagnostic / 'deb-acceptance.json', {'status': 'passed', 'candidateSha256': package_hashes[deb.name]})
            put_json(diagnostic / 'caddy-contract.json', {'passed': True, 'runtime': installed, 'compose_cleanup': True})
            queries = [{'id': q['id'], 'category': q['category'],
                        'expected_domains': q.get('expected_domains', []),
                        'request': {**{key: q[key] for key in ('query', 'language', 'domains', 'time_range')}, 'limit': 5},
                        'urls': [], 'status': 'ok'} for q in load_queries(query_path)]
            put_json(diagnostic / 'public-search.json', {'minimum_live_evidence_passed': True, 'runtime': installed,
                'queries_sha256': verify_release.sha(query_path), 'navigation': navigation_summary(queries),
                'closure': {'completed': True}, 'queries': queries})
            put_json(diagnostic / 'public-compose.json', {'cleanup_passed': True, 'temporary_directory_removed': True})
            make_quality(CONTROL, diagnostic / 'quality', info)

    evidence_path = FIXTURE / 'windows11-evidence.json'
    portable = copy.deepcopy(sdk_reports['windows-x64'])
    portable['host'] = WIN_HOST
    portable['portable'] = {'package_sha256': infos['windows-x64']['package_sha256']['candidate-windows.zip'],
                            'relocated_path_with_spaces': True, 'system_only_path': True, 'temporary_directory_removed': True}
    soak = copy.deepcopy(soak_reports['windows-x64'])
    soak['host'] = WIN_HOST
    evidence = {'schema_version': 1, 'source_commit': COMMIT, 'candidate_run_id': RUN,
        'package_sha256': infos['windows-x64']['package_sha256'],
        'msi_report': {'Passed': True, 'CleanupErrors': [], 'WindowsProductType': 1, 'WindowsBuild': 26100,
                       'ScriptSha256': verify_release.sha(CONTROL / 'scripts/AcceptWindowsMsi.ps1'),
                       'PowerShellVersion': '5.1.26100.1', 'PowerShellEdition': 'Desktop',
                       'BaselineSource': {'SourceCommit': '3' * 40, 'WorkingTreeDirty': True,
                                          'BuildInfoSha256': '4' * 64, 'PackageSha256': '5' * 64},
                       'Baseline': {'Sha256': '5' * 64, 'ProductVersion': '0.0.1', 'UpgradeCode': 'fixture'},
                       'Candidate': {'Sha256': infos['windows-x64']['package_sha256']['candidate-windows.msi'],
                                     'ProductVersion': '0.1.0', 'UpgradeCode': 'fixture'}},
        'portable_report': portable, 'soak_report': soak,
        'delivery_gates': {'council_status': 'approved', 'unresolved_p0_p1': 0, 'qgis_development_passed': False,
                          'qgis_launch_stage': 'launch4Release', 'qgis_release_passed': True,
                          'temporary_state_restored': True, 'qgis_commit': '2' * 40}}
    put_json(evidence_path, evidence)
    return types.SimpleNamespace(root=FIXTURE,control=CONTROL,inputs=INPUTS,evidence=evidence_path,
                                 infos=infos,debian_entries=debian_entries,refresh_sums=refresh_sums)


class ReleaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qcws release acceptance 中文 ')
        self.addCleanup(temporary.cleanup)
        self.f = build_fixture(Path(temporary.name))
        original = verify_release.package_files
        def inventory(path):
            if path == self.f.inputs / 'packages-ubuntu-x64/candidate-ubuntu.deb':
                return copy.deepcopy(self.f.debian_entries)
            return original(path)
        self.enterContext(patch.object(verify_release, 'ROOT', self.f.control))
        self.enterContext(patch.object(verify_release, 'package_files', inventory))

    def verify(self, **overrides):
        values = dict(root=self.f.inputs, commit=COMMIT, tag=TAG, run_id=RUN, evidence_path=self.f.evidence,
            evidence_sha=verify_release.sha(self.f.evidence), repository=REPOSITORY, run_attempt=ATTEMPT)
        values.update(overrides)
        return verify_release.verify(**values)

    def edit(self, path, change):
        value = verify_release.read(path)
        change(value)
        path.write_text(json.dumps(value), encoding='utf-8')

    def diagnostic(self, name):
        return self.f.inputs / 'diagnostics-ubuntu-x64/work/ci-artifacts' / name

    def test_complete_original_bytes_and_cli_manifest(self):
        expected = self.verify()
        self.assertTrue({'candidate-windows.zip', 'candidate-windows.msi', 'candidate-ubuntu.deb',
                         'windows11-evidence.json', 'quality-quality.json'} <= set(expected))
        output = self.f.root / 'release output 中文'
        argv = ['verify_release.py', '--ArtifactsRoot', str(self.f.inputs), '--Commit', COMMIT, '--Tag', TAG,
                '--CandidateRunId', str(RUN), '--Repository', REPOSITORY, '--CandidateRunAttempt', str(ATTEMPT),
                '--WindowsEvidence', str(self.f.evidence), '--WindowsEvidenceSHA256', verify_release.sha(self.f.evidence),
                '--Output', str(output)]
        with patch.object(sys, 'argv', argv):
            verify_release.main()
        manifest = verify_release.read(output / 'release-manifest.json')
        self.assertEqual(manifest['assets'], {name: digest for name, (_, digest) in expected.items()})
        for name, (source, digest) in expected.items():
            self.assertEqual((output / name).read_bytes(), source.read_bytes())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        self.assertEqual(len(verify_release.sums(output)), len(expected) + 1)

    def test_evidence_hash_and_previous_attempt(self):
        with self.assertRaisesRegex(RuntimeError, 'External evidence SHA256'):
            self.verify(evidence_sha='0' * 64)
        self.edit(self.diagnostic('ci-run.json'), lambda v: v.update(GITHUB_RUN_ATTEMPT='1'))
        with self.assertRaisesRegex(RuntimeError, 'same repository and attempt'):
            self.verify()

    def test_windows_evidence_failures(self):
        original = self.f.evidence.read_bytes()
        cases = [('Windows 11', lambda v: v['portable_report']['host']['windows'].update(product_type=3)),
                 ('MSI acceptance', lambda v: v['msi_report'].update(Passed=False)),
                 ('MSI acceptance', lambda v: v['msi_report'].update(CleanupErrors=['fixture'])),
                 ('MSI script', lambda v: v['msi_report'].update(ScriptSha256='0' * 64)),
                 ('MSI script', lambda v: v['msi_report'].pop('ScriptSha256')),
                 ('PowerShell', lambda v: v['msi_report'].pop('PowerShellVersion')),
                 ('baseline', lambda v: v['msi_report']['Baseline'].update(ProductVersion='0.2.0')),
                 ('baseline', lambda v: v['msi_report']['Baseline'].update(UpgradeCode='other')),
                 ('baseline', lambda v: v['msi_report']['BaselineSource'].update(PackageSha256='0' * 64)),
                 ('baseline', lambda v: v['msi_report']['BaselineSource'].pop('SourceCommit')),
                 ('Windows 11 stability', lambda v: v['soak_report'].update(checks_passed=False)),
                 ('Windows 11 stability', lambda v: v['soak_report'].update(seconds_per_phase=299)),
                 ('Delivery signoff', lambda v: v['delivery_gates'].update(qgis_release_passed=False)),
                 ('Delivery signoff', lambda v: v['delivery_gates'].pop('qgis_release_passed')),
                 ('Delivery signoff', lambda v: v['delivery_gates'].update(qgis_launch_stage='launch4Development')),
                 ('Delivery signoff', lambda v: v['delivery_gates'].pop('qgis_launch_stage')),
                 ('Delivery signoff', lambda v: v['delivery_gates'].update(temporary_state_restored=False))]
        for message, change in cases:
            with self.subTest(message=message):
                self.f.evidence.write_bytes(original)
                self.edit(self.f.evidence, change)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.verify()

    def test_junit_completeness_after_rehashing(self):
        folder = self.f.inputs / 'packages-windows-x64'
        junit = folder / 'windows-x64-test-results.xml'
        junit.write_text('<testsuite><testcase name="core"/></testsuite>')
        for name in ('windows-x64-test-receipt.json', 'build-info-windows-x64.json'):
            self.edit(folder / name, lambda v: v.update(junit_sha256=verify_release.sha(junit)))
        self.f.refresh_sums(folder)
        with self.assertRaisesRegex(RuntimeError, 'Incomplete or failed CTest'):
            self.verify()

    def test_runtime_chain_and_msi_payload(self):
        folder = self.f.inputs / 'packages-windows-x64'
        receipt = folder / 'windows-x64-install-receipt.json'
        original = receipt.read_bytes()
        self.edit(receipt, lambda v: v.update(tested_artifacts={'build/app': 'f' * 64}))
        self.f.refresh_sums(folder)
        with self.assertRaisesRegex(RuntimeError, 'artifact chain'):
            self.verify()
        receipt.write_bytes(original)
        bom = folder / 'candidate-windows.msi.cdx.json'
        self.edit(bom, lambda v: v['components'][0]['hashes'][0].update(content='f' * 64))
        self.f.refresh_sums(folder)
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_quality_cannot_be_replaced_by_passed_boolean(self):
        path = self.diagnostic('quality/quality.json')
        original = path.read_bytes()
        for change in (lambda v: v.update(passed=False), lambda v: v.update(source_after='0' * 64),
                       lambda v: v.update(tidy_files=[]), lambda v: v.update(seconds=60),
                       lambda v: next(c for c in v['commands'] if c['label'] == 'fuzz-html').update(duration_seconds=1),
                       lambda v: next(c for c in v['commands'] if c['label'] == 'fuzz-html')['argv'].append('-runs=100'),
                       lambda v: v['fuzz']['html'].update(executed_units=1)):
            path.write_bytes(original)
            self.edit(path, change)
            with self.assertRaises(RuntimeError):
                self.verify()
        path.write_bytes(original)
        (path.parent / 'fuzz-html.log').write_text('tampered evidence')
        with self.assertRaisesRegex(RuntimeError, 'Quality evidence changed'):
            self.verify()

    def test_foreign_provenance_does_not_resolve_on_verifier_filesystem(self):
        with patch.object(Path, 'resolve', side_effect=AssertionError('Foreign paths are not local paths')):
            files = verify_quality(self.diagnostic('quality/quality.json'), self.f.control,
                self.f.infos['ubuntu-x64'], COMMIT, RUN, REPOSITORY, ATTEMPT)
        self.assertTrue(files)

    def test_live_and_stability_gates(self):
        path = self.diagnostic('soak.json')
        original = path.read_bytes()
        self.edit(path, lambda v: v.update(seconds_per_phase=299))
        with self.assertRaisesRegex(RuntimeError, 'Full Linux stability'):
            self.verify()
        path.write_bytes(original)
        self.edit(self.diagnostic('public-search.json'), lambda v: v['closure'].update(completed=False))
        with self.assertRaisesRegex(RuntimeError, 'Live public evidence'):
            self.verify()

    def test_navigation_evidence_cannot_omit_or_change_targets(self):
        path = self.diagnostic('public-search.json')
        original = path.read_bytes()
        self.edit(path, lambda value: value.pop('navigation'))
        with self.assertRaisesRegex(RuntimeError, 'Navigation ranking evidence'):
            self.verify()
        path.write_bytes(original)
        def change_target(value):
            next(row for row in value['queries'] if row['category'] == 'navigation')['expected_domains'] = ['wrong.example']
        self.edit(path, change_target)
        with self.assertRaisesRegex(RuntimeError, 'predeclared request or target'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
