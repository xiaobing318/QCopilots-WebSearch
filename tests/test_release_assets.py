import hashlib
import io
from pathlib import Path
import stat
import sys
import tarfile
import types
import unittest
from unittest.mock import patch
import warnings
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import package_inventory as assets
REAL_ZIP = zipfile.ZipFile
REG = stat.S_IFREG | 0o644
LINK = stat.S_IFLNK | 0o777


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


if __name__ == '__main__':
    unittest.main()
