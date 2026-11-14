import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from public_evidence import load_queries, navigation_summary


class NavigationEvidenceTests(unittest.TestCase):
    def test_fixed_targets_have_no_search_filter(self):
        queries = load_queries(Path(__file__).resolve().parent / 'data/public_queries.json')
        self.assertEqual(sum(q['category'] == 'navigation' for q in queries), 8)

    def test_rank_boundary_subdomain_and_missing_result(self):
        base = {'category': 'navigation', 'expected_domains': ['official.example'],
                'request': {'domains': [], 'language': 'en'}, 'status': 'partial'}
        rows = [{**copy.deepcopy(base), 'id': 'first', 'urls': ['https://WWW.OFFICIAL.EXAMPLE/path']},
                {**copy.deepcopy(base), 'id': 'fifth', 'urls': ['https://official.example.attacker.test/'] * 4
                    + ['https://docs.official.example/page']},
                {**copy.deepcopy(base), 'id': 'missing', 'urls': ['https://other.example/'] * 5},
                {**copy.deepcopy(base), 'id': 'error', 'status': 'error'}]
        result = navigation_summary(rows)
        self.assertEqual((result['attempted'], result['top1'], result['top5'], result['mrr_at5']),
                         (4, 1, 2, 0.3))
        self.assertEqual([case['first_expected_domain_rank'] for case in result['cases']], [1, 5, None, None])
        self.assertFalse(result['quality_threshold_applied'])
        rows[0]['request']['domains'] = ['official.example']
        with self.assertRaisesRegex(RuntimeError, 'Filtered navigation'):
            navigation_summary(rows)

    def test_failed_or_invalid_results_cannot_receive_quality_credit(self):
        base = {'id': 'case', 'category': 'navigation', 'expected_domains': ['official.example'],
                'request': {'domains': [], 'language': 'en'}, 'status': 'ok'}
        for url in ('ftp://official.example/', '//official.example/',
                    'https://official.example:bad/', 'https://[', 'https://user@official.example/'):
            with self.subTest(url=url), self.assertRaisesRegex(RuntimeError, 'Invalid navigation result URL'):
                navigation_summary([{**base, 'urls': [url]}])
        with self.assertRaisesRegex(RuntimeError, 'Failed navigation result'):
            navigation_summary([{**base, 'status': 'error', 'urls': ['https://official.example/']}])
        with self.assertRaisesRegex(RuntimeError, 'Invalid navigation result list'):
            navigation_summary([{**base, 'urls': ['https://other.example/'] * 5 + ['https://official.example/']}])


if __name__ == '__main__':
    unittest.main()
