"""Distribution verification uses a real ZIP and a controlled cross-platform DEB reader."""
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

COMMIT, TAG = '1' * 40, 'v0.1.0'


def build_fixture(root):
    control, inputs = root / 'source', root / 'artifacts'
    (control / 'config').mkdir(parents=True)
    inputs.mkdir()

    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def put_json(path, value):
        path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

    def component(name, data):
        return {'type': 'file', 'name': name, 'bom-ref': 'file:' + digest(name.encode()),
                'hashes': [{'alg': 'SHA-256', 'content': digest(data)}]}

    def refresh_sums(folder):
        (folder / 'SHA256SUMS').write_text(''.join(
            verify_release.sha(path) + '  ' + path.name + '\n'
            for path in sorted(folder.iterdir()) if path.name != 'SHA256SUMS'), encoding='utf-8')

    qt_name, qt_bytes = 'qtbase-everywhere-src-6.12.0.tar.xz', b'controlled Qt source'
    ssl_name, ssl_bytes = 'openssl-3.5.9.tar.gz', b'controlled OpenSSL source'
    put_json(control / 'config/dependencies.lock.json', {
        'qt_source': {'url': 'https://example.invalid/' + qt_name, 'sha256': digest(qt_bytes)},
        'openssl_windows': {'url': 'https://example.invalid/' + ssl_name, 'sha256': digest(ssl_bytes)},
    })
    source_name = f'QCopilots-WebSearch-{COMMIT}-source.tar.gz'
    source_path = root / source_name
    with tarfile.open(source_path, 'w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': COMMIT}) as archive:
        entry = tarfile.TarInfo('QCopilots-WebSearch/README.md')
        data = b'controlled project source'
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    infos, debian_entries = {}, None
    for target in ('windows-x64', 'ubuntu-x64'):
        windows = target == 'windows-x64'
        folder = inputs / ('packages-' + target)
        folder.mkdir()
        executable = 'bin/qcopilots-websearch' + ('.exe' if windows else '')
        payload = {executable: b'controlled application ' + target.encode()}
        if windows:
            payload.update({'bin/libssl-3-x64.dll': b'ssl', 'bin/libcrypto-3-x64.dll': b'crypto',
                            'share/qcopilots-websearch/licenses/openssl/LICENSE.txt': b'Apache-2.0'})
            zipped = folder / 'candidate-windows.zip'
            with zipfile.ZipFile(zipped, 'w') as archive:
                for name, data in payload.items():
                    archive.writestr('QCopilots-WebSearch-0.1.0-win64/' + name, data)
            msi = folder / 'candidate-windows.msi'
            msi.write_bytes(b'MSI fixture inventory supplied by the Windows producer.')
            inventories = {zipped.name: package_inventory.package_files(zipped), msi.name: [
                component('[ProgramFiles64Folder]/QCopilots-WebSearch/' + name, data)
                for name, data in sorted(payload.items())]}
        else:
            payload['lib/libQt6Core.so.6'] = b'qt-core-linux'
            deb = folder / 'candidate-ubuntu.deb'
            deb.write_bytes(b'DEB fixture with controlled dpkg reader.')
            debian_entries = [component('opt/qcopilots-websearch/' + name, data) for name, data in sorted(payload.items())]
            inventories = {deb.name: debian_entries}
        package_hashes = {name: verify_release.sha(folder / name) for name in inventories}
        sboms = {}
        for name, entries in inventories.items():
            sboms[name] = name + '.cdx.json'
            put_json(folder / sboms[name], {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
                'metadata': {'component': {'version': '0.1.0'}, 'properties': [
                    {'name': 'qcws:inventory-package', 'value': name},
                    {'name': 'qcws:inventory-sha256', 'value': package_hashes[name]}]}, 'components': entries})
            (folder / (name + '.sha256')).write_text(package_hashes[name] + '  ' + name + '\n')
        (folder / qt_name).write_bytes(qt_bytes)
        extra = {}
        if windows:
            (folder / ssl_name).write_bytes(ssl_bytes)
            extra[ssl_name] = digest(ssl_bytes)
        shutil.copyfile(source_path, folder / source_name)
        info = {'schema_version': 1, 'source_commit': COMMIT, 'working_tree_dirty': False, 'target': target,
                'project_version': '0.1.0', 'project_source': source_name, 'qt_source': qt_name,
                'qt_source_sha256': digest(qt_bytes), 'extra_sources': extra, 'package_sha256': package_hashes,
                'sboms': sboms, 'inventory_files': {name: len(entries) for name, entries in inventories.items()},
                'executable_sha256': digest(payload[executable])}
        infos[target] = info
        put_json(folder / ('build-info-' + target + '.json'), info)
        refresh_sums(folder)
    return types.SimpleNamespace(root=root, control=control, inputs=inputs, infos=infos,
                                 debian_entries=debian_entries, refresh_sums=refresh_sums)


class ReleaseAssetSetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qcws release 中文 ')
        self.addCleanup(temporary.cleanup)
        self.f = build_fixture(Path(temporary.name))
        original = verify_release.package_files
        self.inventory_patch = patch.object(verify_release, 'package_files',
            lambda path: self.f.debian_entries if path.suffix == '.deb' else original(path))
        self.inventory_patch.start()
        self.addCleanup(self.inventory_patch.stop)
        source_patch = patch.object(verify_release, 'ROOT', self.f.control)
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def verify(self):
        return verify_release.verify(self.f.inputs, COMMIT, TAG)

    def edit(self, path, change):
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))
        self.f.refresh_sums(path.parent)

    def test_complete_distribution_and_cli_checksums_exclude_internal_metadata(self):
        files = self.verify()
        self.assertEqual(len(files), 9)
        output = self.f.root / 'release'
        with patch.object(sys, 'argv', ['verify_release.py', '--ArtifactsRoot', str(self.f.inputs),
                                      '--Commit', COMMIT, '--Tag', TAG, '--Output', str(output)]):
            verify_release.main()
        self.assertEqual(set(verify_release.sums(output)), set(files))
        self.assertEqual(len(list(output.iterdir())), 10)
        self.assertFalse(any(name.endswith('.sha256') or name.startswith('build-info') for name in files))
        for name, (path, digest) in files.items():
            self.assertEqual((output / name).read_bytes(), path.read_bytes())
            self.assertEqual(verify_release.sha(output / name), digest)

    def test_identity_and_platform_requirements(self):
        for commit, tag in (('bad', TAG), (COMMIT, 'v01.1.0'), (COMMIT, 'v0.1.0/escape')):
            with self.subTest(commit=commit, tag=tag), self.assertRaises(RuntimeError):
                verify_release.verify(self.f.inputs, commit, tag)
        folder = self.f.inputs / 'packages-windows-x64'
        metadata = folder / 'build-info-windows-x64.json'
        original = metadata.read_bytes()
        for changes in ({'source_commit': '2' * 40}, {'working_tree_dirty': True}, {'project_version': '0.2.0'},
                        {'target': 'ubuntu-x64'}, {'schema_version': 99}):
            with self.subTest(changes=changes):
                metadata.write_bytes(original)
                self.edit(metadata, lambda info: info.update(changes))
                with self.assertRaises(RuntimeError):
                    self.verify()
        metadata.write_bytes(original)
        self.f.refresh_sums(folder)
        missing = self.f.inputs / 'packages-ubuntu-x64'
        missing.rename(self.f.inputs / 'unexpected-platform')
        with self.assertRaisesRegex(RuntimeError, 'platform artifact'):
            self.verify()

    def test_missing_or_extra_asset_is_rejected(self):
        folder = self.f.inputs / 'packages-windows-x64'
        package = folder / 'candidate-windows.msi'
        package.unlink()
        with self.assertRaises(RuntimeError):
            self.verify()
        self.f.refresh_sums(folder)
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_unexpected_tracked_report_cannot_enter_release(self):
        folder = self.f.inputs / 'packages-windows-x64'
        (folder / 'unneeded-report.json').write_text('{}')
        self.f.refresh_sums(folder)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected or missing platform asset'):
            self.verify()

    def test_rehashed_sbom_version_hash_inventory_and_payload_tampering(self):
        folder = self.f.inputs / 'packages-windows-x64'
        sbom = folder / 'candidate-windows.msi.cdx.json'
        original = sbom.read_bytes()
        for change in (lambda v: v['metadata']['component'].update(version='0.2.0'),
                       lambda v: v['metadata']['properties'][1].update(value='f' * 64),
                       lambda v: v['components'].pop(),
                       lambda v: v['components'][0]['hashes'][0].update(content='f' * 64),
                       lambda v: v['components'][0].update(**{'bom-ref': v['components'][1]['bom-ref']})):
            sbom.write_bytes(original)
            self.edit(sbom, change)
            with self.assertRaises(RuntimeError):
                self.verify()

    def test_zip_sbom_matches_actual_bytes(self):
        folder = self.f.inputs / 'packages-windows-x64'
        sbom = folder / 'candidate-windows.zip.cdx.json'
        self.edit(sbom, lambda v: v['components'][0]['hashes'][0].update(content='f' * 64))
        with self.assertRaisesRegex(RuntimeError, 'actual archive bytes'):
            self.verify()

    def test_pinned_dependency_sources_and_cpack_sidecar(self):
        folder = self.f.inputs / 'packages-windows-x64'
        qt = folder / self.f.infos['windows-x64']['qt_source']
        original = qt.read_bytes()
        qt.write_bytes(b'wrong source')
        self.f.refresh_sums(folder)
        with self.assertRaisesRegex(RuntimeError, 'Qt source mismatch'):
            self.verify()
        qt.write_bytes(original)
        sidecar = folder / 'candidate-windows.zip.sha256'
        sidecar.write_text('0' * 64 + '  candidate-windows.zip\n')
        self.f.refresh_sums(folder)
        with self.assertRaisesRegex(RuntimeError, 'CPack checksum'):
            self.verify()

    def test_source_archive_commit_and_common_asset_conflict(self):
        folder = self.f.inputs / 'packages-ubuntu-x64'
        source = folder / self.f.infos['ubuntu-x64']['project_source']
        for archive_commit, error in (('2' * 40, 'Source archive commit'), (COMMIT, 'Conflicting same-name')):
            with tarfile.open(source, 'w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': archive_commit}) as archive:
                item = tarfile.TarInfo('QCopilots-WebSearch/different.txt')
                item.size = 1
                archive.addfile(item, io.BytesIO(b'x'))
            self.f.refresh_sums(folder)
            with self.assertRaisesRegex(RuntimeError, error):
                self.verify()

    def test_existing_output_is_not_replaced(self):
        output = self.f.root / 'release'
        output.mkdir()
        protected = output / 'keep'
        protected.write_text('existing output')
        with self.assertRaisesRegex(RuntimeError, 'Output must be new'):
            verify_release.assemble(self.f.inputs, COMMIT, TAG, output)
        self.assertEqual(protected.read_text(), 'existing output')


if __name__ == '__main__':
    unittest.main()
