"""Predeclared, unfiltered navigation targets and reproducible rank observations."""
import collections
import hashlib
import json
import re
from urllib.parse import urlsplit


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load_queries(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    rows = data.get('queries', [])
    require(data.get('schema_version') == 1 and len(rows) == 60, 'Public query count')
    require(collections.Counter(row['language'] for row in rows) == {'en': 30, 'zh-CN': 30},
            'Public query languages')
    require(len({row['id'] for row in rows}) == len({row['query'] for row in rows}) == 60,
            'Public query uniqueness')
    navigation = [row for row in rows if row.get('category') == 'navigation']
    require(collections.Counter(row['language'] for row in navigation) == {'en': 4, 'zh-CN': 4},
            'Eight predeclared navigation targets required')
    for row in rows:
        targets = row.get('expected_domains', [])
        if row.get('category') == 'navigation':
            require(row['domains'] == [] and row['time_range'] == 'any',
                    'Navigation queries must not filter or hint domains/dates')
            require(re.search(r'\bsite\s*:', row['query'], re.IGNORECASE) is None,
                    'Navigation queries must not contain a site operator')
            require(isinstance(targets, list) and targets and all(isinstance(host, str)
                    and '.' in host and host == host.lower() and host.isascii()
                    and urlsplit('https://' + host).hostname == host
                    and not any(c in host for c in '/:@?#') for host in targets),
                    'Invalid predeclared navigation target')
        else:
            require(not targets, 'Only navigation queries declare expected domains')
    return rows


def navigation_summary(rows):
    cases = []
    for row in rows:
        if row.get('category') != 'navigation':
            continue
        require(row['request']['domains'] == [], 'Filtered navigation evidence is invalid')
        targets = row['expected_domains']
        urls = row.get('urls', [])
        require(isinstance(urls, list) and len(urls) <= 5, 'Invalid navigation result list')
        status = row.get('status', 'incomplete')
        require(status in ('ok', 'partial', 'error', 'incomplete'), 'Invalid navigation result status')
        require(status in ('ok', 'partial') or not urls, 'Failed navigation result contains URLs')
        rank = None
        for index, url in enumerate(urls, 1):
            try:
                require(isinstance(url, str) and not any(c.isspace() or ord(c) < 32 for c in url),
                        'Invalid navigation result URL')
                parsed = urlsplit(url)
                require(parsed.scheme in ('http', 'https') and parsed.hostname
                        and parsed.username is None and parsed.password is None
                        and (parsed.port is None or 1 <= parsed.port <= 65535), 'Invalid navigation result URL')
                host = parsed.hostname.rstrip('.').encode('idna').decode().lower()
            except (ValueError, UnicodeError) as error:
                raise RuntimeError('Invalid navigation result URL') from error
            if rank is None and any(host == target or host.endswith('.' + target) for target in targets):
                rank = index
        cases.append({'id': row['id'], 'language': row['request']['language'],
                      'expected_domains': targets, 'status': status,
                      'top_url': urls[0] if urls else None, 'first_expected_domain_rank': rank})

    def score(subset):
        ranks = [case['first_expected_domain_rank'] for case in subset]
        return {'attempted': len(subset), 'top1': sum(rank == 1 for rank in ranks),
                'top5': sum(rank is not None for rank in ranks),
                'mrr_at5': round(sum(1 / rank if rank else 0 for rank in ranks) / len(ranks), 6)
                if ranks else None}
    return {'metric': 'predeclared_official_domain_rank_at5', 'domain_filter_applied': False,
            'quality_threshold_applied': False, **score(cases),
            'languages': {lang: score([case for case in cases if case['language'] == lang])
                          for lang in ('zh-CN', 'en')}, 'cases': cases}


def verify_public_queries(report, path):
    expected = {row['id']: row for row in load_queries(path)}
    rows = report['queries']
    require(report.get('queries_sha256') == hashlib.sha256(path.read_bytes()).hexdigest(),
            'Public query source hash mismatch')
    require(len(rows) == len(expected) and {row['id'] for row in rows} == set(expected),
            'Public query evidence IDs differ')
    for row in rows:
        query = expected[row['id']]
        payload = {key: query[key] for key in ('query', 'language', 'domains', 'time_range')}
        require(row['request'] == {**payload, 'limit': 5}
                and row.get('category') == query['category']
                and row.get('expected_domains', []) == query.get('expected_domains', []),
                'Public query evidence changed its predeclared request or target')
    require(report.get('navigation') == navigation_summary(rows),
            'Navigation ranking evidence is missing or inconsistent')
