#!/usr/bin/env python3
"""Run source-bound Linux sanitizer, static analysis and bounded fuzz checks."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pipeline import apt_command

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {'core', 'http-limits', 'tls', 'stdio-process', 'release-quality', 'build-pipeline',
            'settings', 'admin-http', 'providers', 'fuzz-html-seeds', 'fuzz-url-seeds',
            'fuzz-jsonrpc-seeds', 'fuzz-serp-seeds'}
FUZZERS = {'html': 1048578, 'url': 16384, 'jsonrpc': 1048576, 'serp': 1048576}
FLAGS = '-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer'
QUALITY_ENV = dict(CC='/usr/bin/clang-21', CXX='/usr/bin/clang++-21', CFLAGS=FLAGS, CXXFLAGS=FLAGS,
    LDFLAGS='-fsanitize=address,undefined', ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',
    UBSAN_OPTIONS='print_stacktrace=1:halt_on_error=1', ASAN_SYMBOLIZER_PATH='/usr/bin/llvm-symbolizer-21')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def need(value, message):
    if not value:
        raise RuntimeError(message)


def seeds(folder):
    paths = sorted(p for p in folder.rglob('*') if p.is_file())
    need(paths and not any(p.is_symlink() for p in paths), 'Missing or linked seed corpus')
    return {p.relative_to(folder).as_posix(): sha(p) for p in paths}


def first_party(database, source):
    result = set()
    for item in database:
        path = Path(item['file'])
        path = (path if path.is_absolute() else Path(item['directory']) / path).resolve()
        if path.is_relative_to(source):
            relative = path.relative_to(source)
            if relative.parts[0] in ('src', 'app', 'tests') and relative.suffix == '.cpp':
                result.add(relative.as_posix())
    need(result, 'No first-party translation units')
    return sorted(result)


def check_instrumentation(build, database):
    """Check the generated build settings before trusting instrumented tests."""
    required_flags = set(FLAGS.split())
    for relative, languages in (('CMakeCache.txt', ('C', 'CXX')),
                                ('dependencies/zlib-build/CMakeCache.txt', ('C',))):
        cache = dict(line.split('=', 1) for line in (build / relative).read_text().splitlines()
                     if '=' in line and not line.startswith(('//', '#')))
        for language in languages:
            compiler = '/usr/bin/clang++-21' if language == 'CXX' else '/usr/bin/clang-21'
            key = 'CMAKE_' + language + '_COMPILER'
            need(cache.get(key + ':FILEPATH', cache.get(key + ':STRING')) == compiler,
                 f'Unexpected {language} compiler in {relative}')
            flags = shlex.split(cache.get('CMAKE_' + language + '_FLAGS:STRING', ''))
            need(required_flags <= set(flags), f'Sanitizer compiler flags missing in {relative}')
        if relative == 'CMakeCache.txt':
            need(cache.get('QCWS_SANITIZERS:BOOL') == 'ON' and cache.get('QCWS_FUZZING:BOOL') == 'ON',
                 'Quality CMake options disabled')
    lexbor = []
    for item in database:
        command = item.get('arguments') or shlex.split(item.get('command', ''))
        if any('lexbor_static.dir' in argument for argument in command):
            lexbor.append(command)
    need(lexbor and all({'-fsanitize=address,undefined', '-fsanitize=fuzzer-no-link'} <= set(command)
                        for command in lexbor), 'Lexbor fuzz/sanitizer instrumentation missing')


def check_tests(names, junit):
    need(len(names) == len(set(names)) and REQUIRED <= set(names), 'Missing or duplicate discovered tests')
    cases = ET.parse(junit).getroot().findall('.//testcase')
    need(sorted(case.get('name') for case in cases) == sorted(names)
         and not any(case.find(key) is not None for case in cases for key in ('failure', 'error', 'skipped')),
         'CTest discovery and successful JUnit cases differ')


def check_tidy(database, source, completed):
    need(first_party(database, source) == sorted(completed), 'Not all translation units passed clang-tidy')


def fuzz_result(text, duration, seconds):
    need(duration >= seconds - 1, 'Fuzz campaign ended before its required duration')
    counts = re.findall(r'stat::number_of_executed_units:\s*(\d+)', text)
    coverage = re.findall(r'(?m)^#\d+\s+DONE\s+cov:\s*(\d+)\b', text)
    need(counts and coverage and int(counts[-1]) >= 100 and int(coverage[-1]) > 0,
         'Insufficient fuzz execution/coverage')
    return {'executed_units': int(counts[-1]), 'done_cov': int(coverage[-1])}


def stop_group(pid):
    for sig, delay in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 0.05)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ConfigPath', type=Path, required=True)
    parser.add_argument('--Profile', required=True)
    parser.add_argument('--Seconds', type=int, choices=(60, 300), required=True)
    parser.add_argument('--OutputDir', type=Path, required=True)
    args = parser.parse_args()
    need(sys.platform == 'linux', 'Quality checks require Linux')
    out = args.OutputDir.resolve()
    need(not out.exists() or not any(out.iterdir()), 'Quality output must be empty')
    out.mkdir(parents=True, exist_ok=True)
    report = {'schema_version': 1, 'passed': False, 'source_root': str(ROOT), 'seconds': args.Seconds,
        'commands': [], 'fuzz': {}, 'tidy_files': [], 'environment': QUALITY_ENV,
        'coverage_exclusions': ['prebuilt Qt and ICU', 'system OpenSSL']}
    env = dict(os.environ, **QUALITY_ENV)
    build = ROOT / 'build/quality'

    def run(label, command, timeout=1800):
        command = list(map(str, command))
        log = out / f'{len(report["commands"]):03d}-{label}.log'
        row = {'label': label, 'argv': command, 'cwd': str(ROOT), 'log': log.name,
               'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'exit_code': None, 'timed_out': False}
        report['commands'].append(row)
        started, process = time.monotonic(), None
        print('Quality:', label, flush=True)
        try:
            with log.open('wb') as stream:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    row['exit_code'] = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    row['timed_out'] = True
                    stop_group(process.pid)
                    row['exit_code'] = process.wait(timeout=10)
        finally:
            if process is not None:
                stop_group(process.pid)
                if process.poll() is None:
                    process.wait(timeout=10)
            row['duration_seconds'] = round(time.monotonic() - started, 3)
            if log.is_file():
                row['log_sha256'] = sha(log)
        if (row['exit_code'] != 0 or row['timed_out']) and log.is_file():
            with log.open('rb') as stream:
                stream.seek(max(0, log.stat().st_size - 32768))
                print(stream.read().decode('utf-8', errors='replace'), file=sys.stderr, flush=True)
        need(row['exit_code'] == 0 and not row['timed_out'], f'Command failed: {label}')
        return log

    try:
        report['source_commit'] = run('git-head', ['git', 'rev-parse', 'HEAD']).read_text().strip()
        report['clean_before'] = not run('git-clean-before', ['git', 'status', '--porcelain=v1']).read_text().strip()
        need(report['clean_before'], 'Quality checks require a clean checkout')
        report.update(repository=env.get('GITHUB_REPOSITORY'), run_id=env.get('GITHUB_RUN_ID'),
                      run_attempt=env.get('GITHUB_RUN_ATTEMPT'), github_sha=env.get('GITHUB_SHA'))
        if report['github_sha']:
            need(report['github_sha'] == report['source_commit'], 'Checkout differs from GITHUB_SHA')
        config = json.loads(args.ConfigPath.read_text(encoding='utf-8-sig'))
        profile = config['profiles'][args.Profile]
        need(profile['platform'] == 'ubuntu-x64', 'Quality profile must target Ubuntu')
        profile.update(buildDir=str(build), installDir=str(ROOT / 'install/quality'), buildType='RelWithDebInfo', jobs=2)
        profile['cmakeArgs'] = profile.get('cmakeArgs', []) + ['-DCMAKE_C_COMPILER=/usr/bin/clang-21',
            '-DCMAKE_CXX_COMPILER=/usr/bin/clang++-21', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
            '-DQCWS_SANITIZERS=ON', '-DQCWS_FUZZING=ON']
        config['profiles'] = {"ubuntu-x64-quality": profile}
        cfg = out / 'quality-config.json'
        cfg.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        report['profile'] = profile
        report['dependency_lock_sha256'] = sha(ROOT / 'config/dependencies.lock.json')
        report['clang_tidy_config_sha256'] = sha(ROOT / '.clang-tidy')
        packages = ['clang-21', 'clang-tidy-21', 'llvm-21', 'libclang-rt-21-dev']
        apt = apt_command(profile['proxy'], os.geteuid() == 0)
        run('apt-update', [*apt, 'update', '--error-on=any'])
        run('apt-install', [*apt, 'install', '-y', '--no-install-recommends', *packages])
        report['tool_versions'] = {}
        for name, command in [('packages', ['dpkg-query', '-W', *packages]), ('clang', ['clang-21', '--version']),
                              ('clang-tidy', ['clang-tidy-21', '--version']), ('symbolizer', ['llvm-symbolizer-21', '--version'])]:
            report['tool_versions'][name] = run('version-' + name, command).read_text(encoding='utf-8', errors='replace')
        base = [sys.executable, ROOT / 'scripts/pipeline.py', '--ConfigPath', cfg,
                '--Profile', 'ubuntu-x64-quality', '--Stages']
        for stage in ('bootstrap', 'doctor', 'configure', 'build'):
            run('pipeline-' + stage, [*base, stage], timeout=5400)
        discovery = run('ctest-discovery', ['ctest', '--test-dir', build, '--show-only=json-v1'])
        shutil.copyfile(discovery, out / 'discovered-tests.json')
        names = [item['name'] for item in json.loads(discovery.read_text())['tests']]
        need(len(names) == len(set(names)) and REQUIRED <= set(names), 'Missing or duplicate discovered tests')
        database = json.loads((build / 'compile_commands.json').read_text())
        check_instrumentation(build, database)
        run('pipeline-test', [*base, 'test'])
        shutil.copyfile(build / 'test-results.xml', out / 'test-results.xml')
        check_tests(names, out / 'test-results.xml')
        report['test_names'] = sorted(names)
        for name in ('compile_commands.json', 'CMakeCache.txt'):
            shutil.copyfile(build / name, out / name)
        shutil.copyfile(build / 'dependencies/zlib-build/CMakeCache.txt', out / 'zlib-CMakeCache.txt')
        units = first_party(database, ROOT)
        report['expected_tidy_files'] = units
        for unit in units:
            run('tidy-' + str(len(report['tidy_files'])), ['clang-tidy-21', '-p', build, ROOT / unit,
                '--config-file=' + str(ROOT / '.clang-tidy'), '--warnings-as-errors=*',
                '--header-filter=^' + re.escape(str(ROOT)) + '/(include/qws|src|app|tests)/'], timeout=600)
            report['tidy_files'].append(unit)
        check_tidy(database, ROOT, report['tidy_files'])
        for name, max_len in FUZZERS.items():
            executable, seed_dir = build / f'qcws_{name}_fuzz', ROOT / 'tests/fuzz/corpus' / name
            corpus = out / 'corpus' / name
            corpus.mkdir(parents=True, exist_ok=True)
            artifacts = out / 'failures' / name
            artifacts.mkdir(parents=True, exist_ok=True)
            item = {'executable_sha256': sha(executable), 'seeds': seeds(seed_dir), 'max_len': max_len}
            report['fuzz'][name] = item
            log = run('fuzz-' + name, [executable, corpus, seed_dir, f'-max_total_time={args.Seconds}',
                f'-max_len={max_len}', '-timeout=5', '-rss_limit_mb=1024', '-print_final_stats=1',
                '-artifact_prefix=' + artifacts.as_posix() + '/'], timeout=args.Seconds + 60)
            text = log.read_text(encoding='utf-8', errors='replace')
            result = fuzz_result(text, report['commands'][-1]['duration_seconds'], args.Seconds)
            need(item['executable_sha256'] == sha(executable) and item['seeds'] == seeds(seed_dir), 'Fuzz inputs changed')
            item.update(log=log.name, **result)
        report['clean_after'] = not run('git-clean-after', ['git', 'status', '--porcelain=v1']).read_text().strip()
        need(report['clean_after'], 'Source changed during quality checks')
        report['passed'] = True
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        print(report['error'], file=sys.stderr)
    finally:
        # Keep diagnostics in the caller's output directory. CI uses a temporary
        # directory and exposes failures through its normal console log.
        for relative, name in (
            ('test-results.xml', 'test-results.xml'),
            ('compile_commands.json', 'compile_commands.json'),
            ('CMakeCache.txt', 'CMakeCache.txt'),
            ('dependencies/zlib-build/CMakeCache.txt', 'zlib-CMakeCache.txt'),
            ('CMakeFiles/CMakeConfigureLog.yaml', 'CMakeConfigureLog.yaml'),
            ('Testing/Temporary/LastTest.log', 'LastTest.log'),
            ('Testing/Temporary/LastTestsFailed.log', 'LastTestsFailed.log'),
        ):
            candidate = build / relative
            if candidate.is_file():
                shutil.copyfile(candidate, out / name)
        report['files'] = {p.name: sha(p) for p in sorted(out.iterdir()) if p.is_file() and p.name != 'quality.json'}
        (out / 'quality.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
