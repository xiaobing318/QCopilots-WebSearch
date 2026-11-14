"""Validate retained Linux analysis evidence before promoting candidate bytes."""
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
import re
import xml.etree.ElementTree as ET


def verify_quality(path, source, info, commit, run_id, repository, run_attempt):
    def need(condition, message):
        if not condition:
            raise RuntimeError(message)

    def sha(file):
        with file.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def read(file):
        return json.loads(file.read_text(encoding='utf-8'))

    def recorded_path(value):
        # These are paths from the Linux producer, not the verifier's filesystem.
        # resolve() here would follow local aliases or probe a different machine.
        need(isinstance(value, str) and '\x00' not in value, 'Invalid recorded quality path')
        result = PurePosixPath(posixpath.normpath(value))
        need(result.is_absolute(), 'Recorded quality path must be absolute on Linux')
        return result

    folder, evidence = path.parent, read(path)
    need(evidence.get('schema_version') == 1 and evidence.get('passed') is True, 'Quality checks did not complete')
    need(evidence['source_commit'] == commit == evidence['github_sha'] and evidence['repository'] == repository
         and int(evidence['run_id']) == run_id and int(evidence['run_attempt']) == run_attempt, 'Quality source/run mismatch')
    need(evidence['clean_before'] is True and evidence['clean_after'] is True
         and evidence['source_after'] == evidence['build_identity']['source_sha256'] == info['build_identity']['source_sha256'],
         'Quality source snapshot differs from candidate')
    profile = evidence['profile']
    need(evidence['build_identity']['profile_sha256'] == hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
         'Quality profile identity mismatch')
    required_args = {'-DCMAKE_C_COMPILER=/usr/bin/clang-21', '-DCMAKE_CXX_COMPILER=/usr/bin/clang++-21',
                     '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', '-DQCWS_SANITIZERS=ON', '-DQCWS_FUZZING=ON'}
    need(profile['platform'] == 'ubuntu-x64' and recorded_path(profile['buildDir']).name == 'quality'
         and required_args <= set(profile['cmakeArgs']), 'Quality instrumentation settings missing')
    need(evidence['dependency_lock_sha256'] == sha(source / 'config/dependencies.lock.json')
         and evidence['clang_tidy_config_sha256'] == sha(source / '.clang-tidy'), 'Quality checked different source settings')
    flags = '-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer'
    expected_env = dict(CC='/usr/bin/clang-21', CXX='/usr/bin/clang++-21', CFLAGS=flags, CXXFLAGS=flags,
        LDFLAGS='-fsanitize=address,undefined', ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',
        UBSAN_OPTIONS='print_stacktrace=1:halt_on_error=1', ASAN_SYMBOLIZER_PATH='/usr/bin/llvm-symbolizer-21')
    need(evidence['environment'] == expected_env, 'Quality sanitizer environment differs')
    files = evidence['files']
    required = {'quality-config.json', 'discovered-tests.json', 'test-results.xml', 'compile_commands.json',
                'CMakeCache.txt', 'zlib-CMakeCache.txt'}
    need(required <= set(files), 'Missing bound quality artifacts')
    for name, digest in files.items():
        need(isinstance(name, str) and name not in ('', '.', '..')
             and not any(c in name for c in '/\\:\r\n\x00'), 'Unsafe quality artifact name')
        artifact = folder / name
        need(artifact.is_file() and not artifact.is_symlink() and sha(artifact) == digest, 'Quality evidence changed: ' + name)
    commands = evidence['commands']
    labels = {command['label']: command for command in commands}
    need(len(labels) == len(commands) and all(type(c['exit_code']) is int and c['exit_code'] == 0
         and c['timed_out'] is False and c['duration_seconds'] >= 0
         and files.get(c['log']) == c['log_sha256'] for c in commands), 'Failed or unbound quality command')
    for stage in ('doctor', 'configure', 'build', 'test'):
        command = labels['pipeline-' + stage]['argv']
        need(command[-2:] == ['--Stages', stage] and '--ConfigPath' in command and '--Profile' in command,
             'Missing actual quality pipeline stage')
    for key in ('packages', 'clang', 'clang-tidy', 'symbolizer'):
        need(evidence['tool_versions'][key] == (folder / labels['version-' + key]['log']).read_text('utf-8', errors='replace'),
             'Tool version evidence differs')
    for name in ('CMakeCache.txt', 'zlib-CMakeCache.txt'):
        cache = dict(line.split('=', 1) for line in (folder / name).read_text().splitlines() if '=' in line and not line.startswith('//'))
        need(cache.get('CMAKE_C_COMPILER:FILEPATH', cache.get('CMAKE_C_COMPILER:STRING')) == '/usr/bin/clang-21'
             and '-fsanitize=address,undefined' in cache.get('CMAKE_C_FLAGS:STRING', ''), 'Dependency sanitizer compiler flags missing')
        if name == 'CMakeCache.txt':
            need(cache.get('QCWS_SANITIZERS:BOOL') == 'ON' and cache.get('QCWS_FUZZING:BOOL') == 'ON', 'Quality CMake options disabled')
    need(files['discovered-tests.json'] == labels['ctest-discovery']['log_sha256'], 'CTest discovery copy differs')
    names = [test['name'] for test in read(folder / 'discovered-tests.json')['tests']]
    required_tests = {'core', 'http-limits', 'tls', 'stdio-process', 'release-assets', 'build-pipeline',
                      'fuzz-html-seeds', 'fuzz-url-seeds', 'fuzz-jsonrpc-seeds'}
    cases = ET.parse(folder / 'test-results.xml').getroot().findall('.//testcase')
    need(len(names) == len(set(names)) and required_tests <= set(names)
         and sorted(names) == sorted(case.get('name') for case in cases) == evidence['test_names']
         and not any(case.find(k) is not None for case in cases for k in ('failure', 'error', 'skipped')),
         'Incomplete or failed quality CTest results')
    original_source, units = recorded_path(evidence['source_root']), set()
    database = read(folder / 'compile_commands.json')
    lexbor = []
    for item in database:
        file = PurePosixPath(item['file'])
        file = recorded_path(str(file if file.is_absolute() else recorded_path(item['directory']) / file))
        if file.is_relative_to(original_source):
            relative = file.relative_to(original_source)
            if relative.parts[0] in ('src', 'app', 'tests') and relative.suffix == '.cpp':
                units.add(relative.as_posix())
        if 'lexbor_static.dir' in item.get('command', ''):
            lexbor.append(item['command'])
    need(lexbor and all('-fsanitize=address,undefined' in cmd and '-fsanitize=fuzzer-no-link' in cmd for cmd in lexbor),
         'Lexbor fuzz/sanitizer instrumentation missing')
    tidy = [c for c in commands if c['label'].startswith('tidy-')]
    actual = [recorded_path(c['argv'][3]).relative_to(original_source).as_posix() for c in tidy]
    need(units and sorted(units) == evidence['expected_tidy_files'] == evidence['tidy_files'] == sorted(actual)
         and len(actual) == len(units) and all(c['argv'][0] == 'clang-tidy-21' and '--warnings-as-errors=*' in c['argv']
         and '--config-file=' + str(original_source / '.clang-tidy') in c['argv'] for c in tidy),
         'Not all translation units passed clang-tidy')
    need(evidence['seconds'] >= 300 and set(evidence['fuzz']) == {'html', 'url', 'jsonrpc'}, 'Full fuzz campaigns required')
    for name, max_len in {'html': 1048578, 'url': 16384, 'jsonrpc': 1048576}.items():
        result, command = evidence['fuzz'][name], labels['fuzz-' + name]
        args = command['argv']
        need(command.get('duration_seconds', 0) >= evidence['seconds'] - 1
             and not any(arg.startswith('-runs=') for arg in args[1:]),
             'Fuzz campaign ended before its required duration or used an iteration shortcut')
        need(recorded_path(args[0]).name == f'qcws_{name}_fuzz' and {f'-max_total_time={evidence["seconds"]}',
             f'-max_len={max_len}', '-timeout=5', '-rss_limit_mb=1024', '-print_final_stats=1'} <= set(args), 'Wrong fuzz settings')
        text = (folder / command['log']).read_text('utf-8', errors='replace')
        counts = re.findall(r'stat::number_of_executed_units:\s*(\d+)', text)
        coverage = re.findall(r'(?m)^#\d+\s+DONE\s+cov:\s*(\d+)\b', text)
        need(counts and coverage and int(counts[-1]) >= 100 and int(coverage[-1]) > 0
             and result['executed_units'] == int(counts[-1]) and result['done_cov'] == int(coverage[-1])
             and result['log'] == command['log'] and result['max_len'] == max_len
             and re.fullmatch('[0-9a-f]{64}', result['executable_sha256']), 'Fuzz log/result mismatch')
        seed_dir = source / 'tests/fuzz/corpus' / name
        seed_hashes = {p.relative_to(seed_dir).as_posix(): sha(p) for p in sorted(seed_dir.rglob('*')) if p.is_file()}
        need(seed_hashes and seed_hashes == result['seeds'], 'Fuzz seed corpus differs from release source')
    return [path, *[folder / name for name in sorted(files)]]
