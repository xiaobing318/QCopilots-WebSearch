"""Quality checks must reject incomplete or ineffective execution immediately."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import quality


class QualityExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / 'CMakeCache.txt'
        self.zlib_cache = self.root / 'dependencies/zlib-build/CMakeCache.txt'
        self.zlib_cache.parent.mkdir(parents=True)
        c_settings = 'CMAKE_C_COMPILER:FILEPATH=/usr/bin/clang-21\nCMAKE_C_FLAGS:STRING=' + quality.FLAGS + '\n'
        self.cache.write_text(c_settings + 'CMAKE_CXX_COMPILER:STRING=/usr/bin/clang++-21\n'
            'CMAKE_CXX_FLAGS:STRING=' + quality.FLAGS + '\nQCWS_SANITIZERS:BOOL=ON\nQCWS_FUZZING:BOOL=ON\n')
        self.zlib_cache.write_text(c_settings)
        self.database = [
            {'file': str(self.root / 'src/server.cpp'), 'directory': str(self.root),
             'command': '/usr/bin/clang++-21 ' + quality.FLAGS + ' -c src/server.cpp'},
            {'file': 'app/main.cpp', 'directory': str(self.root),
             'command': '/usr/bin/clang++-21 ' + quality.FLAGS + ' -c app/main.cpp'},
            {'file': str(self.root / 'external/lexbor/parser.c'), 'directory': str(self.root),
             'command': '/usr/bin/clang-21 -fsanitize=address,undefined -fsanitize=fuzzer-no-link '
                        '-o CMakeFiles/lexbor_static.dir/parser.c.o -c parser.c'}]

    def test_generated_settings_and_all_translation_units_pass(self):
        quality.check_instrumentation(self.root, self.database)
        quality.check_tidy(self.database, self.root, ['src/server.cpp', 'app/main.cpp'])
        arguments = copy.deepcopy(self.database)
        arguments[-1]['arguments'] = arguments[-1].pop('command').split()
        quality.check_instrumentation(self.root, arguments)

    def test_wrong_compilers_disabled_options_or_missing_flags_fail(self):
        cases = [
            (self.cache, '/usr/bin/clang-21', '/usr/bin/gcc'),
            (self.cache, '/usr/bin/clang++-21', '/usr/bin/g++'),
            (self.cache, 'QCWS_SANITIZERS:BOOL=ON', 'QCWS_SANITIZERS:BOOL=OFF'),
            (self.cache, 'QCWS_FUZZING:BOOL=ON', 'QCWS_FUZZING:BOOL=OFF'),
            (self.cache, 'CMAKE_CXX_FLAGS:STRING=' + quality.FLAGS, 'CMAKE_CXX_FLAGS:STRING='),
            (self.zlib_cache, '/usr/bin/clang-21', '/usr/bin/gcc'),
            (self.zlib_cache, '-fsanitize=address,undefined', ''),
        ]
        for path, before, after in cases:
            with self.subTest(path=path.name, value=before):
                original = path.read_text()
                try:
                    path.write_text(original.replace(before, after))
                    with self.assertRaises(RuntimeError):
                        quality.check_instrumentation(self.root, self.database)
                finally:
                    path.write_text(original)

    def test_lexbor_must_be_present_and_instrumented_in_every_unit(self):
        variants = [self.database[:-1]]
        for flag in ('-fsanitize=address,undefined', '-fsanitize=fuzzer-no-link'):
            changed = copy.deepcopy(self.database)
            uninstrumented = copy.deepcopy(changed[-1])
            uninstrumented['command'] = uninstrumented['command'].replace(flag, '')
            changed.append(uninstrumented)
            variants.append(changed)
        for database in variants:
            with self.subTest(database=database), self.assertRaisesRegex(RuntimeError, 'Lexbor'):
                quality.check_instrumentation(self.root, database)

    def test_tidy_cannot_omit_or_duplicate_a_translation_unit(self):
        for completed in ([], ['app/main.cpp'], ['app/main.cpp', 'src/server.cpp', 'src/server.cpp']):
            with self.subTest(completed=completed), self.assertRaisesRegex(RuntimeError, 'translation units'):
                quality.check_tidy(self.database, self.root, completed)

    def write_junit(self, names, failure=None):
        root = ET.Element('testsuite')
        for index, name in enumerate(names):
            case = ET.SubElement(root, 'testcase', name=name)
            if index == 0 and failure:
                ET.SubElement(case, failure)
        path = self.root / 'test-results.xml'
        ET.ElementTree(root).write(path)
        return path

    def test_discovery_and_junit_must_include_every_successful_test(self):
        names = sorted(quality.REQUIRED)
        quality.check_tests(names, self.write_junit(names))
        for discovered, executed, failure in [
            (names[1:], names[1:], None),
            (names + names[:1], names + names[:1], None),
            (names, names[:-1], None),
            (names, names + ['unlisted-test'], None),
            *[(names, names, status) for status in ('failure', 'error', 'skipped')],
        ]:
            with self.subTest(discovered=discovered, executed=executed, failure=failure):
                with self.assertRaises(RuntimeError):
                    quality.check_tests(discovered, self.write_junit(executed, failure))

    def test_fuzz_requires_elapsed_time_execution_and_coverage(self):
        log = '#100 DONE cov: 10 ft: 20\nstat::number_of_executed_units: 100\n'
        self.assertEqual(quality.fuzz_result(log, 300, 300), {'executed_units': 100, 'done_cov': 10})
        self.assertEqual(quality.fuzz_result(log, 60, 60), {'executed_units': 100, 'done_cov': 10})
        for output, duration in [(log, 2), (log, float('nan')), ('', 300),
                                 (log.replace('cov: 10', 'cov: 0'), 300),
                                 (log.replace('units: 100', 'units: 99'), 300),
                                 (log.replace('DONE', 'INITED'), 300)]:
            with self.subTest(output=output, duration=duration), self.assertRaises(RuntimeError):
                quality.fuzz_result(output, duration, 300)


if __name__ == '__main__':
    unittest.main()
