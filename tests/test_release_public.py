"""Current-run public-query validation without network requests or services."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import live_acceptance
from public_evidence import load_queries, navigation_summary, verify_public_queries


def query_rows(path):
    return [{'id': query['id'], 'category': query['category'],
             'expected_domains': query.get('expected_domains', []),
             'request': {**{key: query[key] for key in ('query', 'language', 'domains', 'time_range')},
                         'limit': 5},
             'status': 'error', 'error': 'provider_unavailable', 'urls': []}
            for query in load_queries(path)]


class LivePublicGateTests(unittest.TestCase):
    def execute(self, mutate=None, interrupted=False):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            executable, output = folder / 'service', folder / 'result.json'
            executable.write_bytes(b'unexecuted test fixture')
            queries = folder / 'queries.json'
            queries.write_bytes((ROOT / 'tests/data/public_queries.json').read_bytes())

            def run(args, report, _token):
                report['queries'] = query_rows(args.Queries)
                report['closure']['completed'] = True
                report['cleanup'] = {'passed': True}
                if mutate:
                    mutate(report)
                if interrupted:
                    report['queries'] = report['queries'][:1]
                    raise live_acceptance.Problem('local_transport')

            argv = ['live_acceptance.py', '--Executable', str(executable), '--Output', str(output),
                    '--ProviderUrl', 'http://127.0.0.1:8989/search', '--Queries', str(queries)]
            with patch.object(sys, 'argv', argv), patch.object(live_acceptance, 'run', run), \
                    patch.object(live_acceptance, 'fingerprint', return_value={'sha256': 'fixed'}), \
                    patch.object(live_acceptance.signal, 'signal'):
                code = live_acceptance.main()
            return code, json.loads(output.read_text(encoding='utf-8'))

    def test_complete_queries_with_closure_have_no_ranking_threshold(self):
        code, report = self.execute()
        self.assertEqual(code, 0)
        self.assertTrue(report['minimum_live_evidence_passed'])
        self.assertEqual(report['navigation']['top5'], 0)
        self.assertFalse(report['navigation']['quality_threshold_applied'])

    def test_request_target_hash_and_ids_are_checked_before_success(self):
        mutations = {
            'hash': lambda report: report.update(queries_sha256='0' * 64),
            'id': lambda report: report['queries'][0].update(id='unplanned-query'),
            'request': lambda report: report['queries'][0]['request'].update(query='changed query'),
            'target': lambda report: report['queries'][0].update(expected_domains=['unplanned.example']),
            'missing': lambda report: report['queries'].pop(),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                code, report = self.execute(mutate)
                self.assertEqual(code, 1)
                self.assertFalse(report['minimum_live_evidence_passed'])
                self.assertEqual(report['outcome'], 'local_failure')

    def test_inconclusive_closure_and_cleanup_failure_remain_failures(self):
        code, report = self.execute(lambda value: value['closure'].update(completed=False))
        self.assertEqual(code, 2)
        self.assertEqual(report['outcome'], 'inconclusive')
        code, report = self.execute(lambda value: value['cleanup'].update(passed=False))
        self.assertEqual(code, 1)
        self.assertTrue(report['cleanup_failed'])
        self.assertFalse(report['minimum_live_evidence_passed'])

    def test_invalid_navigation_and_interrupted_run_still_write_failure_output(self):
        def mutate(report):
            row = next(row for row in report['queries'] if row['category'] == 'navigation')
            row.update(status='ok', urls=['file:///private'])
        code, report = self.execute(mutate)
        self.assertEqual(code, 1)
        self.assertEqual(report['error'], 'local_public_queries')
        code, report = self.execute(interrupted=True)
        self.assertEqual(code, 1)
        self.assertEqual(report['error'], 'local_transport')

    def test_inconsistent_rank_summary_is_rejected(self):
        path = ROOT / 'tests/data/public_queries.json'
        report = {'queries_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'queries': query_rows(path)}
        report['navigation'] = navigation_summary(report['queries'])
        verify_public_queries(report, path)
        changed = copy.deepcopy(report)
        changed['navigation']['top5'] = 1
        with self.assertRaisesRegex(RuntimeError, 'Navigation ranking'):
            verify_public_queries(changed, path)


if __name__ == '__main__':
    unittest.main()
