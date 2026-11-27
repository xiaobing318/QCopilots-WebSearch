"""Quality checks must reject incomplete or ineffective execution immediately."""
import copy
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import quality
import release_gate


@unittest.skipUnless(sys.platform.startswith("linux"), "Requires Linux flock")
class DebAcceptanceLockTests(unittest.TestCase):
    def test_concurrent_probe_fails_then_reacquires_the_same_inode_after_release(self):
        from acceptance_deb import fixture_lock

        script = (
            "from pathlib import Path\n"
            "from acceptance_deb import fixture_lock\n"
            "import sys\n"
            "try:\n"
            "    with fixture_lock(Path(sys.argv[1])): pass\n"
            "except BlockingIOError:\n"
            "    sys.exit(75)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.lock"

            def probe():
                return subprocess.run([sys.executable, "-c", script, str(path)],
                                      cwd=Path(__file__).resolve().parents[2] / "scripts", timeout=10).returncode

            with fixture_lock(path):
                inode = path.stat().st_ino
                self.assertEqual(probe(), 75)
            self.assertEqual(probe(), 0)
            self.assertEqual(path.stat().st_ino, inode)


class ParallelQualityTests(unittest.TestCase):
    def test_parallel_checks_complete_all_items_and_join_workers(self):
        pair = threading.Barrier(2, timeout=5)
        completed = set()
        lock = threading.Lock()

        def check(item):
            pair.wait()
            with lock:
                completed.add(item)

        stopping = threading.Event()
        quality.parallel_checks(range(4), check, 2, stopping)
        self.assertEqual(completed, set(range(4)))
        self.assertFalse(stopping.is_set())

    def test_failure_signals_the_active_peer_and_preserves_the_original_error(self):
        stopping = threading.Event()
        pair = threading.Barrier(2, timeout=5)
        exited = threading.Event()

        def check(item):
            pair.wait()
            if item == 'broken':
                raise RuntimeError('analysis failed')
            try:
                if not stopping.wait(5):
                    raise AssertionError('peer was not cancelled')
            finally:
                exited.set()

        with self.assertRaisesRegex(RuntimeError, 'analysis failed'):
            quality.parallel_checks(['broken', 'running'], check, 2, stopping)
        self.assertTrue(exited.is_set())


class ReleaseGateTests(unittest.TestCase):
    commit = 'a' * 40

    def test_project_tag_requires_a_nonempty_annotation_for_the_triggering_commit(self):
        project = (Path(__file__).resolve().parents[2] / 'CMakeLists.txt').read_text(encoding='utf-8')
        version = re.search(r'^set\(QCWS_VERSION "([^"]+)"', project, re.MULTILINE)[1]
        tag = f'v{version}'
        # Git subprocesses, including those inside the gate, must only see the fixture's configuration.
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment, clear=True):
            root = Path(directory)
            (root / 'CMakeLists.txt').write_text(project, encoding='utf-8')

            def git(*args):
                return subprocess.check_output(
                    ['git', '-c', 'user.name=Release Test', '-c', 'user.email=test@example.invalid', *args],
                    cwd=root, encoding='utf-8',
                ).strip()

            git('init', '--quiet', '--template=', '--initial-branch=main')
            git('add', 'CMakeLists.txt')
            git('commit', '--quiet', '-m', 'Release fixture')
            commit = git('rev-parse', 'HEAD')
            git('tag', tag)
            with self.assertRaisesRegex(RuntimeError, 'must be annotated'):
                release_gate.verify_tag(tag, commit, root)
            git('tag', '-d', tag)
            git('tag', '-a', '--cleanup=verbatim', '-m', '   ', tag)
            with self.assertRaisesRegex(RuntimeError, 'annotation must not be empty'):
                release_gate.verify_tag(tag, commit, root)
            git('tag', '-d', tag)
            git('tag', '-a', '--cleanup=verbatim', '-m', tag, tag)
            self.assertEqual(release_gate.verify_tag(tag, commit, root), git('rev-parse', f'refs/tags/{tag}'))
            with self.assertRaisesRegex(RuntimeError, 'triggering commit'):
                release_gate.verify_tag(tag, '0' * 40, root)
            with self.assertRaisesRegex(RuntimeError, 'must match QCWS_VERSION'):
                release_gate.verify_tag('not-the-project-version', commit, root)

    def verify(self, runs, *, ancestor=True):
        responses = iter([
            {'commit': {'sha': 'b' * 40}},
            {'merge_base_commit': {'sha': self.commit if ancestor else 'c' * 40}},
            {'workflow_runs': runs},
        ])
        paths = []

        def get_json(path):
            paths.append(path)
            return next(responses)

        result = release_gate.verify('example/project', self.commit, get_json)
        self.assertIn('head_sha=' + self.commit, paths[-1])
        return result

    def run_record(self, **changes):
        return {'id': 10, 'head_sha': self.commit, 'head_branch': 'main', 'event': 'push',
                'path': '.github/workflows/ci.yml', 'status': 'completed', 'conclusion': 'success',
                **changes}

    def test_success_requires_main_ancestry_and_matching_ci(self):
        self.assertEqual(self.verify([self.run_record()]), 10)
        with self.assertRaisesRegex(RuntimeError, 'belong to main'):
            self.verify([self.run_record()], ancestor=False)

    def test_no_run_or_another_context_cannot_authorize_release(self):
        for changes in ({'head_sha': 'd' * 40}, {'head_branch': 'feature/example'},
                        {'event': 'pull_request'}, {'event': 'workflow_dispatch'},
                        {'path': '.github/workflows/release.yml'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'No main push CI'):
                self.verify([self.run_record(**changes)])
        with self.assertRaisesRegex(RuntimeError, 'No main push CI'):
            self.verify([])

    def test_incomplete_or_failed_latest_run_blocks_old_success(self):
        for changes in ({'status': 'in_progress', 'conclusion': None},
                        {'conclusion': 'failure'}, {'conclusion': 'cancelled'},
                        {'conclusion': 'skipped'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'must succeed first'):
                self.verify([self.run_record(), self.run_record(id=11, **changes)])

    def test_new_success_can_replace_failed_attempt(self):
        self.assertEqual(self.verify([self.run_record(conclusion='failure'), self.run_record(id=11)]), 11)

    def test_invalid_context_is_rejected_without_request(self):
        for repository, commit in [('bad/repo/path', self.commit), ('example/project', '../main')]:
            with self.subTest(repository=repository, commit=commit), self.assertRaises(RuntimeError):
                release_gate.verify(repository, commit, lambda _: self.fail('Unexpected request'))


class ManualReleaseTests(unittest.TestCase):
    repo = 'example/project'
    api_root = '/repos/' + repo
    commit = 'a' * 40
    object_sha = 'b' * 40

    def setUp(self):
        self.project = Path(__file__).resolve().parents[2]
        cmake = (self.project / 'CMakeLists.txt').read_text(encoding='utf-8')
        version = re.search(r'^set\(QCWS_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"', cmake, re.MULTILINE)[1]
        self.tag = f'v{version}'
        self.ref = f'refs/tags/{self.tag}'
        self.head = self.commit
        self.ci_conclusion = 'success'
        self.branch_collision = False
        self.releases = []
        self.existing_ref = None
        self.annotation_commit = self.commit
        self.ref_result = self.tag_ref(self.object_sha)
        self.ref_error = None
        self.dispatch_error = None
        self.writes = []
        self.messages = []

    def tag_ref(self, object_sha):
        return {'ref': self.ref, 'object': {'type': 'tag', 'sha': object_sha}}

    def request(self, path, body=None):
        self.assertTrue(path.startswith(self.api_root + '/'))
        path = path.removeprefix(self.api_root)
        if body is not None:
            self.writes.append((path, body))
        if path == '/branches/main':
            return {'commit': {'sha': self.head}}
        if path.startswith('/compare/'):
            return {'merge_base_commit': {'sha': self.commit}}
        if path.startswith('/actions/workflows/ci.yml/runs?'):
            return {'workflow_runs': [{
                'id': 10, 'head_sha': self.commit, 'head_branch': 'main', 'event': 'push',
                'path': '.github/workflows/ci.yml', 'status': 'completed',
                'conclusion': self.ci_conclusion,
            }]}
        if path == '/releases?per_page=100&page=1':
            return self.releases
        if path == f'/git/ref/heads/{self.tag}' and self.branch_collision:
            return {'ref': f'refs/heads/{self.tag}'}
        if path == f'/git/ref/tags/{self.tag}' and self.existing_ref is not None:
            return self.existing_ref
        if path.startswith('/git/ref/'):
            raise HTTPError(path, 404, 'Not found', None, None)
        if path == '/git/tags':
            return {'sha': self.object_sha}
        if path == '/git/tags/' + self.object_sha:
            return {'tag': self.tag, 'message': self.tag,
                    'object': {'sha': self.annotation_commit, 'type': 'commit'}}
        if path == '/git/refs':
            if self.ref_error is not None:
                self.existing_ref = self.ref_result
                raise self.ref_error
            return self.ref_result
        if path == '/actions/workflows/release.yml/dispatches':
            if self.dispatch_error is not None:
                raise self.dispatch_error
            return {'workflow_run_id': 27}
        self.fail(f'Unexpected GitHub request: {path}')

    def launch(self, request=None, tag=None):
        with patch.object(release_gate, 'git', return_value=self.commit):
            return release_gate.prepare_release(
                self.repo, self.commit, self.tag if tag is None else tag,
                request or self.request, self.messages.append, self.project,
            )

    def test_preparation_creates_and_dispatches_an_exact_annotated_tag(self):
        self.assertEqual(self.launch(), 27)
        self.assertEqual(self.writes, [
            ('/git/tags', {'tag': self.tag, 'message': self.tag, 'object': self.commit, 'type': 'commit'}),
            ('/git/refs', {'ref': self.ref, 'sha': self.object_sha}),
            ('/actions/workflows/release.yml/dispatches', {
                'ref': self.tag, 'inputs': {'tag': self.tag, 'expected_tag_object': self.object_sha},
                'return_run_details': True,
            }),
        ])
        self.assertIn('https://github.com/example/project/actions/runs/27', self.messages[-1])

    def test_invalid_input_ci_failure_and_branch_collision_never_write(self):
        with self.assertRaisesRegex(RuntimeError, 'vX.Y.Z'):
            self.launch(tag='unusable version')
        self.ci_conclusion = 'failure'
        with self.assertRaisesRegex(RuntimeError, 'latest main push CI'):
            self.launch()
        self.ci_conclusion = 'success'
        self.branch_collision = True
        with self.assertRaisesRegex(RuntimeError, 'A branch'):
            self.launch()
        self.assertEqual(self.writes, [])

    def test_existing_published_release_or_draft_never_writes(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                self.releases = [{'tag_name': self.tag, 'draft': draft, 'html_url': 'https://example.test/release'}]
                state = 'draft' if draft else 'published'
                with self.assertRaisesRegex(RuntimeError, f'{state} release already exists'):
                    self.launch()
        self.assertEqual(self.writes, [])

    def test_main_advancing_after_preflight_prevents_ref_creation(self):
        def request(path, body=None):
            result = self.request(path, body)
            if path == self.api_root + '/git/tags':
                self.head = 'd' * 40
            return result

        with self.assertRaisesRegex(RuntimeError, 'main advanced before the tag reference'):
            self.launch(request)
        self.assertEqual([path for path, _ in self.writes], ['/git/tags'])

    def test_retry_reuses_same_commit_and_rejects_other_commit(self):
        self.existing_ref = self.tag_ref(self.object_sha)
        self.assertEqual(self.launch(), 27)
        self.assertEqual(len(self.writes), 1)
        self.writes.clear()
        self.annotation_commit = 'e' * 40
        with self.assertRaisesRegex(RuntimeError, 'different commit'):
            self.launch()
        self.assertEqual(self.writes, [])

    def test_ref_conflict_reconciles_only_the_same_raw_object(self):
        self.ref_error = HTTPError('/git/refs', 422, 'Reference exists', None, None)
        self.ref_result = self.tag_ref('e' * 40)
        with self.assertRaisesRegex(RuntimeError, 'Could not confirm creation'):
            self.launch()
        self.assertEqual(len(self.writes), 2)
        self.existing_ref = None
        self.ref_result = self.tag_ref(self.object_sha)
        self.assertEqual(self.launch(), 27)

    def test_dispatch_with_unknown_outcome_reports_recovery_and_does_not_retry(self):
        self.dispatch_error = URLError('connection lost')
        with self.assertRaisesRegex(RuntimeError, 'Could not confirm Release dispatch'):
            self.launch()
        self.assertEqual(len(self.writes), 3)
        self.assertIn(self.object_sha, self.messages[0])
        self.assertIn('/actions/workflows/release.yml', self.messages[1])

    def test_worker_rejects_branch_mismatched_tag_invalid_sha_or_moved_object(self):
        release_gate.verify_dispatch_tag(self.ref, self.tag, self.object_sha)
        release_gate.verify_dispatch_tag(self.ref, self.tag, '')
        for ref, tag, sha in [('refs/heads/topic', self.tag, ''),
                              (self.ref, 'v1.2.3' if self.tag != 'v1.2.3' else 'v1.2.4', ''),
                              (self.ref, self.tag, 'x' * 40)]:
            with self.subTest(ref=ref, tag=tag, sha=sha), self.assertRaises(RuntimeError):
                release_gate.verify_dispatch_tag(ref, tag, sha)
        environment = {
            'GITHUB_REPOSITORY': self.repo, 'GITHUB_REF': self.ref, 'GITHUB_REF_NAME': self.tag,
            'GITHUB_SHA': self.commit, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GH_TOKEN': 'test-token', 'REQUESTED_TAG': self.tag, 'EXPECTED_TAG_OBJECT': self.object_sha,
        }
        with (patch.dict(os.environ, environment, clear=True), patch.object(sys, 'argv', ['release_gate.py']),
              patch.object(release_gate, 'verify_tag', return_value='e' * 40),
              self.assertRaisesRegex(RuntimeError, 'changed before the tagged run began')):
            release_gate.main()


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
