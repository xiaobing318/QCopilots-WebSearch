"""Synthetic, hash-consistent evidence for release-validator contract tests only."""
import hashlib
import json


def make_quality(source, folder, info):
    folder.mkdir(parents=True)
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    def write(name, value):
        path = folder / name
        path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
        return path
    (source / '.clang-tidy').write_text('Checks: clang-analyzer-core.*\n')
    flags = '-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer'
    profile = {'platform': 'ubuntu-x64', 'buildDir': str(source / 'build/quality'), 'cmakeArgs': [
        '-DCMAKE_C_COMPILER=/usr/bin/clang-21', '-DCMAKE_CXX_COMPILER=/usr/bin/clang++-21',
        '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', '-DQCWS_SANITIZERS=ON', '-DQCWS_FUZZING=ON']}
    commands = []
    def command(label, argv, text='controlled successful command\n'):
        path = folder / (label + '.log')
        path.write_text(text, encoding='utf-8')
        commands.append({'label': label, 'argv': argv, 'log': path.name, 'log_sha256': sha(path),
                         'exit_code': 0, 'timed_out': False, 'duration_seconds': 1})
        return text
    for stage in ('doctor', 'configure', 'build', 'test'):
        command('pipeline-' + stage, ['python', 'pipeline.py', '--ConfigPath', 'config', '--Profile', 'ubuntu-x64', '--Stages', stage])
    versions = {name: command('version-' + name, [name, '--version'])
                for name in ('packages', 'clang', 'clang-tidy', 'symbolizer')}
    names = sorted(['core', 'http-limits', 'tls', 'stdio-process', 'release-assets', 'build-pipeline',
                    'fuzz-html-seeds', 'fuzz-url-seeds', 'fuzz-jsonrpc-seeds'])
    discovered = write('discovered-tests.json', {'tests': [{'name': name} for name in names]})
    command('ctest-discovery', ['ctest', '--show-only=json-v1'], discovered.read_text())
    (folder / 'test-results.xml').write_text('<testsuite>' + ''.join('<testcase name="' + name + '"/>' for name in names) + '</testsuite>')
    cache = 'CMAKE_C_COMPILER:FILEPATH=/usr/bin/clang-21\nCMAKE_C_FLAGS:STRING=' + flags + '\n'
    (folder / 'CMakeCache.txt').write_text(cache + 'QCWS_SANITIZERS:BOOL=ON\nQCWS_FUZZING:BOOL=ON\n')
    (folder / 'zlib-CMakeCache.txt').write_text(cache)
    unit = 'src/example.cpp'
    write('compile_commands.json', [{'file': str(source / unit), 'directory': str(source), 'command': 'clang++ ' + flags},
        {'file': str(source / 'external/parser.c'), 'directory': str(source),
         'command': 'clang -fsanitize=address,undefined -fsanitize=fuzzer-no-link -o lexbor_static.dir/parser.o'}])
    command('tidy-0', ['clang-tidy-21', '-p', profile['buildDir'], str(source / unit),
            '--config-file=' + str(source / '.clang-tidy'), '--warnings-as-errors=*'])
    fuzz = {}
    for name, maximum in {'html': 1048578, 'url': 16384, 'jsonrpc': 1048576}.items():
        seed = source / 'tests/fuzz/corpus' / name / 'fixture'
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b'controlled seed')
        command('fuzz-' + name, [str(source / ('build/quality/qcws_' + name + '_fuzz')),
            '-max_total_time=300', f'-max_len={maximum}', '-timeout=5', '-rss_limit_mb=1024', '-print_final_stats=1'],
            '#100 DONE cov: 10 ft: 20\nstat::number_of_executed_units: 100\n')
        fuzz[name] = {'log': 'fuzz-' + name + '.log', 'executed_units': 100, 'done_cov': 10,
                      'max_len': maximum, 'executable_sha256': 'd' * 64, 'seeds': {'fixture': sha(seed)}}
    write('quality-config.json', {'version': 1, 'profiles': {'ubuntu-x64': profile}})
    evidence = {'schema_version': 1, 'passed': True, 'source_commit': '1' * 40, 'github_sha': '1' * 40,
        'repository': 'example/QCopilots-WebSearch', 'run_id': '123', 'run_attempt': '2',
        'clean_before': True, 'clean_after': True, 'source_root': str(source),
        'source_after': info['build_identity']['source_sha256'], 'profile': profile,
        'build_identity': {'source_sha256': info['build_identity']['source_sha256'],
            'profile_sha256': hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()},
        'dependency_lock_sha256': sha(source / 'config/dependencies.lock.json'),
        'clang_tidy_config_sha256': sha(source / '.clang-tidy'),
        'environment': dict(CC='/usr/bin/clang-21', CXX='/usr/bin/clang++-21', CFLAGS=flags, CXXFLAGS=flags,
            LDFLAGS='-fsanitize=address,undefined', ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',
            UBSAN_OPTIONS='print_stacktrace=1:halt_on_error=1', ASAN_SYMBOLIZER_PATH='/usr/bin/llvm-symbolizer-21'),
        'commands': commands, 'tool_versions': versions, 'test_names': names,
        'expected_tidy_files': [unit], 'tidy_files': [unit], 'seconds': 300, 'fuzz': fuzz,
        'files': {path.name: sha(path) for path in folder.iterdir() if path.is_file()}}
    write('quality.json', evidence)
