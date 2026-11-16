#!/usr/bin/env python3
"""Observe 60 real public searches without substituting synthetic results."""
from __future__ import annotations
import argparse
import collections
import copy
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from soak import digest, environment, hidden_console, stop_process
from runtime_evidence import fingerprint
from public_evidence import load_queries, navigation_summary, verify_public_queries

ROOT = Path(__file__).resolve().parents[1]


class Problem(Exception):
    def __init__(self, code, external=False, status=None):
        super().__init__(code)
        self.code, self.external, self.status = code, external, status


def check(condition, code):
    if not condition:
        raise Problem(code)


def utc():
    return datetime.now(timezone.utc).isoformat()


def http_json(url, token=None, payload=None, timeout=35):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    headers = {'Accept':'application/json','Connection':'close'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    try:
        connection.request('GET' if body is None else 'POST', parsed.path or '/', body, headers)
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        check(len(raw) <= 2 * 1024 * 1024, 'local_response_size')
        check(response.getheader('Content-Type','').split(';')[0].strip() == 'application/json', 'local_content_type')
        try:
            return response.status, json.loads(raw)
        except (ValueError, UnicodeError):
            raise Problem('local_invalid_json') from None
    except (OSError, http.client.HTTPException):
        raise Problem('local_transport') from None
    finally:
        connection.close()


def api(base, token, operation, payload=None):
    path = '/health/live' if operation == 'health' else '/v1/' + operation
    status, value = http_json(base + path, token, payload, 1 if operation == 'health' else 35)
    if status != 200:
        error = value.get('error', {}) if isinstance(value, dict) else {}
        code = error.get('code') if isinstance(error, dict) else None
        if not isinstance(code, str) or not code.replace('_','').isalnum():
            code = 'local_error_envelope'
        external = {
            'search': {('provider_unavailable',502), ('timeout',504), ('response_too_large',502), ('rate_limited',429)},
            'open': {('provider_unavailable',502), ('timeout',504), ('response_too_large',502),
                     ('upstream_http_error',502), ('destination_blocked',403),
                     ('unsupported_content',415), ('rate_limited',429)},
        }
        # busy/429 and every cache/internal/protocol/find error remain local failures.
        raise Problem(code, (code,status) in external.get(operation,set()), status)
    check(isinstance(value,dict) and type(value.get('contract_version')) is int
          and value['contract_version'] == 1 and 'error' not in value, 'contract_version')
    return value


def diagnostics(provider):
    parsed = urlsplit(provider)
    url = urlunsplit((parsed.scheme,parsed.netloc,parsed.path.rsplit('/',1)[0] + '/stats/errors','',''))
    try:
        status, value = http_json(url, timeout=3)
        check(status == 200 and isinstance(value,dict), 'diagnostics_unavailable')
        result = []
        for engine, entries in list(value.items())[:100]:
            if isinstance(entries,list):
                for item in entries[:20]:
                    if isinstance(item,dict):
                        result.append({'engine':str(engine)[:100],
                            'exception_classname':str(item.get('exception_classname') or 'logged_error')[:150],
                            'percentage':item.get('percentage') if type(item.get('percentage')) is int else None,
                            'secondary':item.get('secondary') is True})
        return {'available':True,'errors':result}
    except Problem as exc:
        return {'available':False,'error':exc.code}


def public_url(value):
    parsed = urlsplit(value)
    check(parsed.scheme in ('http','https') and parsed.hostname
          and parsed.username is None and parsed.password is None, 'contract_result_url')
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))


def spawn(args, folder, token):
    config = copy.deepcopy(json.loads((ROOT / 'config/runtime.json').read_text('utf-8')))
    reservations = []
    try:
        for _ in range(2):
            sock = socket.socket()
            reservations.append(sock)
            if os.name == 'nt':
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            sock.bind(('127.0.0.1',0))
        ports = [sock.getsockname()[1] for sock in reservations]
        for name, port in zip(('business','control'),ports):
            config[name] = {'host':'127.0.0.1','port':port,'origins':[],
                            'allowed_hosts':[f'127.0.0.1:{port}']}
        config['clients'] = [{'id':'live-acceptance','token_env':'QCWS_LIVE_TOKEN'}]
        config['provider'] = {'url':args.ProviderUrl,'ca_file':'','engines':['google','bing','duckduckgo']}
        config['cache'].update(directory=str(folder/'cache'),search_ttl_seconds=300,page_ttl_seconds=1800)
        config['limits'] = {'max_concurrent':1,'search_timeout_seconds':15,'open_timeout_seconds':20}
        path = folder / 'runtime.json'
        path.write_text(json.dumps(config),encoding='utf-8')
    finally:
        for sock in reservations:
            sock.close()
    env = environment([])
    env['QCWS_LIVE_TOKEN'] = token
    process = subprocess.Popen([str(args.Executable),'--ConfigPath',str(path)],env=env,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**hidden_console())
    return process,[f'http://127.0.0.1:{port}' for port in ports]


def validate_search(value, request):
    check(value.get('status') in ('ok','partial') and isinstance(value.get('results'),list)
          and len(value['results']) <= 5, 'contract_search')
    check(isinstance(value.get('cache'),dict) and value['cache'].get('hit') is False, 'contract_cold_cache')
    check(isinstance(value.get('failures'),list) and isinstance(value.get('warnings'),list)
          and isinstance(value.get('fetched_at'),str), 'contract_provenance')
    hosts, urls = [], []
    for item in value['results']:
        check(isinstance(item,dict) and all(isinstance(item.get(key),str)
              for key in ('url','title','snippet')), 'contract_result')
        urls.append(public_url(item['url']))
        host = urlsplit(item['url']).hostname.rstrip('.').encode('idna').decode().lower()
        check(not request['domains'] or any(host == domain or host.endswith('.'+domain)
              for domain in request['domains']), 'contract_domain_filter')
        check(isinstance(item.get('sources'),list), 'contract_sources')
        hosts.append(host)
    check(len({item['url'] for item in value['results']}) == len(value['results']), 'contract_duplicates')
    failures = []
    for item in value['failures']:
        check(isinstance(item,dict) and isinstance(item.get('engine'),str)
              and isinstance(item.get('reason'),str), 'contract_failures')
        if item['engine'] == 'provider':
            check(item['reason'] in {'provider_unavailable','timeout','response_too_large','rate_limited'},
                  'local_partial_provider_failure')
        else:
            check(item['reason'] == 'unresponsive','contract_engine_failure')
        failures.append({'engine':item['engine'][:100],'reason':item['reason'][:100]})
    return {'status':value['status'],'result_count':len(urls),'urls':urls,'hosts':hosts,
            'failures':failures,'warning_count':len(value['warnings']),
            'fetched_at':value['fetched_at'],'cache_hit':False}


def run(args, report, token):
    rows = load_queries(args.Queries)
    groups = [[row for row in rows if row['language'] == lang] for lang in ('zh-CN','en')]
    rows = [row for pair in zip(*groups) for row in pair]
    next_start, tried = 0.0,set()

    def throttle():
        nonlocal next_start
        time.sleep(max(0,next_start-time.monotonic()))
        next_start = time.monotonic() + args.IntervalSeconds

    report['searxng_before'] = diagnostics(args.ProviderUrl)
    with tempfile.TemporaryDirectory(prefix='qcws-live-') as temporary:
        process,(base,control) = spawn(args,Path(temporary).resolve(),token)
        try:
            deadline = time.monotonic()+20
            while True:
                check(process.poll() is None and time.monotonic() < deadline,'local_startup')
                try:
                    if api(control,token,'health').get('live') is True:
                        break
                except Problem:
                    pass
                time.sleep(0.1)
            report['status_before'] = api(base,token,'status')
            for query in rows:
                payload = {key:query[key] for key in ('query','language','domains','time_range')}
                payload['limit'] = 5
                row = {'id':query['id'],'request':payload,'category':query['category'],
                       'expected_domains':query.get('expected_domains',[])}
                report['queries'].append(row)
                throttle()
                started = time.monotonic()
                try:
                    result = api(base,token,'search',payload)
                    row.update(validate_search(result,payload))
                except Problem as exc:
                    row.update(status='error',error=exc.code,http_status=exc.status,
                               failure_kind='public_upstream' if exc.external else 'local_failure')
                    if not exc.external:
                        raise
                    continue
                finally:
                    row['ms'] = round((time.monotonic()-started)*1000,3)
                print(f"{query['id']}: {row['status']}",flush=True)
                if report['closure']['completed'] or len(report['closure']['attempts']) >= 8:
                    continue
                candidate = next((item['url'] for item in result['results'] if item['url'] not in tried
                    and not urlsplit(item['url']).path.lower().endswith('.pdf')),None)
                if not candidate:
                    continue
                tried.add(candidate)
                attempt = {'query_id':query['id'],'url':public_url(candidate),'completed':False}
                report['closure']['attempts'].append(attempt)
                try:
                    throttle()
                    page = api(base,token,'open',{'url':candidate,'max_lines':200})
                    check(isinstance(page.get('page_id'),str) and isinstance(page.get('lines'),list),'contract_open')
                    check(0 < len(page['page_id']) <= 64,'contract_page_id')
                    check(isinstance(page.get('cache'),dict) and page['cache'].get('hit') is False,'contract_open_cache')
                    for line in page['lines']:
                        check(isinstance(line,dict) and type(line.get('line')) is int
                            and 1 <= line['line'] <= 10000 and isinstance(line.get('text'),str),'contract_lines')
                    selected = next((line for line in page['lines'] if len(line['text'].strip()) >= 12),None)
                    if selected is None:
                        raise Problem('empty_public_page',external=True)
                    literal = selected['text'].strip()[:64]
                    found = api(base,token,'find',{'page_id':page['page_id'],'pattern':literal,
                        'case_sensitive':True,'start_line':selected['line'],'max_matches':5})
                    check(found.get('page_id') == page['page_id'] and isinstance(found.get('matches'),list),'contract_find')
                    check(any(isinstance(hit,dict) and hit.get('line') == selected['line']
                        and isinstance(hit.get('text'),str) and literal in hit['text']
                        for hit in found['matches']),'contract_literal_find')
                    attempt.update(completed=True,matched_line=selected['line'],
                        literal_sha256=hashlib.sha256(literal.encode('utf-8')).hexdigest())
                    report['closure']['completed'] = True
                except Problem as exc:
                    attempt.update(error=exc.code,http_status=exc.status,
                        failure_kind='public_upstream' if exc.external else 'local_failure')
                    if exc.code == 'unsupported_content' and exc.status == 415:
                        attempt.update(outcome='skipped',failure_kind='unsupported_content')
                    if not exc.external:
                        raise
            report['status_after'] = api(base,token,'status')
        finally:
            report['cleanup'] = stop_process(process)
            report['searxng_after'] = diagnostics(args.ProviderUrl)


def interrupted(signum,frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--Executable',type=Path,required=True)
    parser.add_argument('--Output',type=Path,required=True)
    parser.add_argument('--ProviderUrl',required=True)
    parser.add_argument('--Queries',type=Path,default=ROOT/'tests/data/public_queries.json')
    parser.add_argument('--IntervalSeconds',type=float,default=6)
    args = parser.parse_args()
    args.Executable,args.Queries = args.Executable.resolve(strict=True),args.Queries.resolve(strict=True)
    output,parsed = args.Output.resolve(),urlsplit(args.ProviderUrl)
    if not 6 <= args.IntervalSeconds <= 60:
        parser.error('IntervalSeconds must be between 6 and 60')
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost','::1')
        or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
        or not parsed.path.endswith('/search')):
        parser.error('ProviderUrl must be an operator-owned loopback HTTP SearXNG /search endpoint')
    if output in (args.Executable,args.Queries,Path(__file__).resolve()):
        parser.error('Output must not overwrite input')
    signal.signal(signal.SIGTERM,interrupted)
    token,code = secrets.token_hex(32),1
    report = {'report_version':1,'evidence_kind':'live_public_web','started_at':utc(),
        'provider_url':args.ProviderUrl,'interval_seconds':args.IntervalSeconds,
        'interval_scope':'native_search_and_open_start_times','max_provider_pages_per_search':3,
        'executable_sha256':digest(args.Executable),'runtime':fingerprint(args.Executable),
        'queries_sha256':digest(args.Queries),'script_sha256':digest(Path(__file__)),
        'compose_sha256':digest(ROOT/'deploy/team/compose.yml'),'queries':[],
        'closure':{'completed':False,'attempts':[]},'outcome':'local_failure','minimum_live_evidence_passed':False}
    try:
        run(args,report,token)
        check(report.get('cleanup',{}).get('passed') is True,'local_cleanup')
        check(fingerprint(args.Executable) == report['runtime'],'local_runtime_changed')
        check(digest(args.Queries) == report['queries_sha256'],'local_queries_changed')
        try:
            report['navigation'] = navigation_summary(report['queries'])
            verify_public_queries(report,args.Queries)
        except (KeyError, TypeError, ValueError, RuntimeError):
            raise Problem('local_public_queries') from None
        passed = len(report['queries']) == 60 and report['closure']['completed']
        report.update(minimum_live_evidence_passed=passed,outcome='completed' if passed else 'inconclusive')
        code = 0 if passed else 2
    except Problem as exc:
        report.update(error=exc.code,http_status=exc.status,outcome='local_failure')
    except KeyboardInterrupt:
        report.update(error='interrupted',outcome='interrupted')
        code = 130
    except Exception:
        report.update(error='unexpected_local_error',outcome='local_failure')
    finally:
        if 'cleanup' in report and not report['cleanup'].get('passed'):
            report.update(outcome='local_failure',cleanup_failed=True,minimum_live_evidence_passed=False)
            code = 1
        rows = report['queries']
        report['finished_at'] = utc()
        def summary(subset):
            latency = sorted(row['ms'] for row in subset if 'ms' in row)
            return {'attempted':len(subset),'nonempty':sum(row.get('result_count',0)>0 for row in subset),
                'statuses':dict(collections.Counter(row.get('status','incomplete') for row in subset)),
                'errors':dict(collections.Counter(row['error'] for row in subset if 'error' in row)),
                'p50_ms':latency[math.ceil(len(latency)*.50)-1] if latency else None,
                'p95_ms':latency[math.ceil(len(latency)*.95)-1] if latency else None}
        report['summary'] = summary(rows)
        report['summary'].update(quality_threshold_applied=False,
            languages={lang:summary([row for row in rows if row['request']['language']==lang]) for lang in ('zh-CN','en')})
        if 'navigation' not in report:
            try:
                report['navigation'] = navigation_summary(rows)
            except (KeyError, TypeError, ValueError, RuntimeError):
                report.update(error='local_public_queries',outcome='local_failure',minimum_live_evidence_passed=False)
                code = 1
        output.parent.mkdir(parents=True,exist_ok=True)
        text = json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False).replace(token,'[REDACTED]')
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=output.parent,
                prefix=output.name+'.',delete=False) as target:
            target.write(text+'\n')
            pending = Path(target.name)
        try:
            os.replace(pending,output)
        finally:
            pending.unlink(missing_ok=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
