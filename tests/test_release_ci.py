import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ci_cleanup


class RunnerTemporaryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = patch.dict(os.environ, RUNNER_TEMP=self.temporary.name,
                              GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', GITHUB_JOB='build')
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_owned_root_is_removed_and_repeated_cleanup_is_safe(self):
        root = ci_cleanup.create()
        (root / 'nested').mkdir()
        (root / 'nested' / 'result.json').write_text('{}')
        ci_cleanup.cleanup(root)
        self.assertFalse(root.exists())
        ci_cleanup.cleanup(root)

    def test_parent_and_foreign_directory_are_preserved(self):
        parent = Path(self.temporary.name)
        foreign = parent / 'other-job'
        foreign.mkdir()
        for path in (parent, foreign, parent / '..' / 'qcws-ci-outside'):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                ci_cleanup.cleanup(path)
        self.assertTrue(foreign.exists())

    def test_other_attempt_marker_is_preserved(self):
        root = ci_cleanup.create()
        with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='3'), self.assertRaises(RuntimeError):
            ci_cleanup.cleanup(root)
        self.assertTrue(root.exists())

    def test_alias_is_rejected_before_deletion(self):
        root = ci_cleanup.create()
        protected = Path(self.temporary.name) / 'protected'
        protected.write_text('keep')
        with patch.object(Path, 'is_symlink', lambda path: path.name == 'result.json'):
            (root / 'result.json').write_text('result')
            with self.assertRaises(RuntimeError):
                ci_cleanup.cleanup(root)
        self.assertEqual(protected.read_text(), 'keep')
        self.assertTrue(root.exists())


class DiagnosticSummaryTests(unittest.TestCase):
    def test_threshold_and_cleanup_failures_are_visible_without_raw_data(self):
        report = {'checks_passed': False, 'compose_cleanup': False,
                  'phases': [{'name': 'cold', 'passed': False, 'peak_rss_bytes': 999,
                              'requests': {'attempted': 10, 'successful': 9, 'p95_ms': 1001},
                              'samples': [{'token': 'private'}]}],
                  'settings': {'client_token': 'private'}}
        summary = ci_cleanup.diagnostic_summary(report)
        self.assertFalse(summary['checks_passed'])
        self.assertFalse(summary['compose_cleanup'])
        self.assertEqual(summary['phases'][0]['requests']['p95_ms'], 1001)
        self.assertNotIn('private', str(summary))


if __name__ == '__main__':
    unittest.main()
