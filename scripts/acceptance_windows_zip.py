"""Exercise exact portable ZIP bytes from a relocated path without development PATH entries."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import zipfile
import psutil
from package_inventory import package_files
from testkit import Fault, MIB, digest, hidden_console, need, request, stop_process
from process_cleanup import wait_owned

DEFAULT_PORTS = (3181, 3182, 3183)
PROTOCOL = '2025-06-18'
INITIALIZE = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': PROTOCOL, 'capabilities': {},
    'clientInfo': {'name': 'portable-acceptance', 'version': '1'},
}}


def cli(executable, cwd, env, *args, reply=b''):
    return subprocess.run([executable, *args], input=reply, capture_output=True, cwd=cwd,
                          env=env, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)


def generated_token(result):
    raw = result.stdout.strip()
    need(result.returncode == 0 and re.fullmatch(rb'[a-f0-9]{64}\r?\n', result.stdout) and raw not in result.stderr,
         'Portable generator did not produce exactly one private token')
    return raw.decode('ascii')


def system_tool(name):
    return Path(os.environ['SystemRoot']) / 'System32' / name


def check_private_registry(path, env):
    script = r'''
$ErrorActionPreference = 'Stop'
$acl = [IO.File]::GetAccessControl($env:QCWS_ACCEPTANCE_PRIVATE_FILE)
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18', 'S-1-5-32-544')
$owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
$grants = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
    Where-Object AccessControlType -eq ([Security.AccessControl.AccessControlType]::Allow))
if (!$acl.AreAccessRulesProtected -or $owner -notin $allowed -or !$grants.Count -or
    @($grants | Where-Object { $_.IdentityReference.Value -notin $allowed }).Count) { exit 2 }
'''
    result = subprocess.run([system_tool('WindowsPowerShell/v1.0/powershell.exe'), '-NoProfile', '-NonInteractive',
                             '-Command', script], env=env | {'QCWS_ACCEPTANCE_PRIVATE_FILE': str(path)},
                            capture_output=True, timeout=8, **hidden_console())
    need(result.returncode in (0, 2), 'Could not inspect the portable client registry permissions')
    need(result.returncode == 0, 'Portable client registry permissions were not private')


def fixture_openssl():
    openssl = shutil.which('openssl')
    if openssl:
        return Path(openssl).resolve(strict=True)
    git = shutil.which('git')
    need(git, 'Generating temporary origin certificates requires OpenSSL or Git for Windows')
    return (Path(git).resolve().parents[1] / 'usr' / 'bin' / 'openssl.exe').resolve(strict=True)


def check_read_only(executable, cwd, env, data):
    whoami = subprocess.check_output([system_tool('whoami.exe'), '/user', '/fo', 'csv', '/nh'], env=env, timeout=8)
    sid = re.search(rb'"(S-1-[0-9-]+)"', whoami)
    need(sid is not None, 'Could not identify the portable fixture account')
    principal = '*' + sid[1].decode('ascii')
    data.mkdir()
    acl = [system_tool('icacls.exe'), str(data)]
    subprocess.run(acl + ['/deny', principal + ':(WD,AD)'], env=env, capture_output=True, check=True, timeout=8)
    try:
        probe = data / 'write probe'
        try:
            probe.touch(exist_ok=False)
        except PermissionError:
            pass
        else:
            probe.unlink()
            need(False, 'The portable data fixture is still writable')
        result = cli(executable, cwd, env)
        need(result.returncode != 0 and b'invalid_configuration:' in result.stderr and not any(data.iterdir()),
             'Read-only portable startup did not fail without creating files')
    finally:
        subprocess.run(acl + ['/remove:d', principal], env=env, capture_output=True, check=True, timeout=8)
        data.rmdir()


@contextmanager
def occupy_default_ports():
    with ExitStack() as sockets:
        for port in DEFAULT_PORTS:
            listener = sockets.enter_context(socket.socket())
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(('127.0.0.1', port))
                listener.listen()
            except OSError as exc:
                # Existing listeners or OS port reservations also block fixed startup.
                if exc.winerror not in (10013, 10048):
                    raise
        yield


@contextmanager
def running_portable(executable, cwd, env, runtime, racers=1):
    processes = []
    output_files = []
    gate = threading.Barrier(racers, timeout=4)

    def launch():
        gate.wait()
        # Inherited handles share a file position. Reading through the parent's
        # writer handle can rewind a child that is still printing its summary.
        with tempfile.NamedTemporaryFile(delete=False) as output:
            log = Path(output.name)
            output_files.append(log)
            proc = subprocess.Popen([executable], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=output, **hidden_console())
        processes.append(proc)
        proc.startup_output = log

    try:
        with ThreadPoolExecutor(max_workers=racers) as pool:
            list(pool.map(lambda _: launch(), range(racers)))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            running = [proc for proc in processes if proc.poll() is None]
            need(running, 'Every portable process exited before readiness')
            if len(running) == 1 and runtime.is_file():
                need(all(proc.returncode != 0 for proc in processes if proc is not running[0]),
                     'A second portable process exited as if it had served')
                try:
                    config = json.loads(runtime.read_bytes())
                    token = (runtime.parent / 'client.token').read_text('ascii').strip()
                    if request(config['control']['port'], token, timeout=0.5).get('live') is True:
                        summary = running[0].startup_output.read_text(encoding='utf-8')
                        if 'Press Ctrl+C to stop.' in summary:
                            missing = {role: config[role]['port'] for role in ('business', 'control', 'admin')
                                       if f'127.0.0.1:{config[role]["port"]}' not in summary}
                            observed = sorted(set(re.findall(r'127\.0\.0\.1:(\d+)', summary)))
                            need(not missing, 'Startup summary did not show every actual listener '
                                 f'(missing={missing}, observed_loopback_ports={observed})')
                            need(str(runtime) in summary and '--SettingsUrl' in summary and '/mcp' in summary,
                                 'Startup summary omitted configuration or login guidance')
                            admin_token = (runtime.parent / 'admin.token').read_text('ascii').strip()
                            need(token not in summary and admin_token not in summary,
                                 'Startup summary disclosed a credential')
                            break
                except (Fault, OSError, ValueError):
                    pass
            time.sleep(0.05)
        else:
            need(False, 'Portable startup did not yield exactly one ready process')
        yield config, token
        need(running[0].poll() is None, 'The portable server exited before shutdown')
    finally:
        stopped = [stop_process(proc) for proc in processes if proc.poll() is None]
        for output in output_files:
            output.unlink()
        need(all(report['passed'] for report in stopped), 'Portable server cleanup failed')


def mcp_request(port, token='', payload=None, session=''):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    headers = {'Accept': 'application/json, text/event-stream'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if session:
        headers.update({'Mcp-Session-Id': session, 'MCP-Protocol-Version': PROTOCOL})
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    try:
        connection.request('POST' if payload is not None else 'DELETE', '/mcp',
                           json.dumps(payload).encode() if payload is not None else None, headers)
        response = connection.getresponse()
        raw = response.read(MIB + 1)
        need(len(raw) <= MIB, 'Portable MCP response exceeded the fixture limit')
        return response.status, response.getheader('Mcp-Session-Id'), json.loads(raw) if response.status == 200 else {}
    finally:
        connection.close()


def verify_mcp(port, token):
    status, session, value = mcp_request(port, token, INITIALIZE)
    need(status == 200 and session and value['result']['protocolVersion'] == PROTOCOL,
         'Portable MCP initialization failed')
    try:
        status, _, _ = mcp_request(port, token, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, session)
        need(status == 202, 'Portable MCP initialized notification failed')
        status, _, value = mcp_request(port, token, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, session)
        need(status == 200 and any(tool['name'] == 'web_search_status' for tool in value['result']['tools']),
             'Portable MCP tool discovery failed')
        status, _, value = mcp_request(port, token, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'web_search_status', 'arguments': {}}}, session)
        need(status == 200 and value['result']['structuredContent']['contract_version'] == 1,
             'Portable MCP tool call failed')
    finally:
        need(mcp_request(port, token, session=session)[0] == 204, 'Portable MCP session cleanup failed')


def snapshot(directory):
    return {path.relative_to(directory): digest(path) if path.is_file() else None for path in directory.rglob('*')}


def verify_portable(executable, folder, cwd, env):
    data = executable.parent / 'data'
    runtime = data / 'runtime.json'
    decoys = [cwd / 'runtime.json', cwd / 'data' / 'runtime.json']
    for decoy in decoys:
        decoy.parent.mkdir(exist_ok=True)
        decoy.write_text('unrelated working-directory config\n', encoding='utf-8')
    decoy_snapshot = snapshot(cwd)
    program_data = folder / 'decoy ProgramData'
    program_data.mkdir()
    env = {key: value for key, value in env.items() if key.lower() != 'programdata'}
    env['ProgramData'] = str(program_data)
    acl = [system_tool('icacls.exe'), str(runtime)]

    generated_token(cli(executable, cwd, env, '--GenerateToken'))
    need(cli(executable, cwd, env, '--SetToken').returncode != 0,
         'Portable token update accepted a missing explicit configuration')
    need(not data.exists(), 'Standalone generator initialized portable data')
    # Exercise the relocated archive before the portable HTTP server initializes its data.
    legacy = INITIALIZE | {'params': INITIALIZE['params'] | {'protocolVersion': '2024-11-05'}}
    frames = [legacy, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
              {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
    stdio_input = b''.join(json.dumps(frame).encode('utf-8') + b'\n' for frame in frames)
    bare = cli(executable, cwd, env, '--Stdio', reply=stdio_input)
    need(bare.returncode == 0 and not data.exists(), 'Bare stdio failed or initialized HTTP service data')
    replies = [json.loads(line) for line in bare.stdout.splitlines()]
    need(len(replies) == 2 and replies[0]['id'] == 1 and replies[0]['result']['protocolVersion'] == '2024-11-05'
         and replies[1]['id'] == 2 and {tool['name'] for tool in replies[1]['result']['tools']} ==
         {'web_search', 'web_open', 'web_find', 'web_search_status'}, 'Portable stdio negotiation or listing failed')
    data.mkdir()
    runtime.write_bytes(b'{')
    try:
        rejected = cli(executable, cwd, env, '--Stdio', reply=stdio_input)
        need(rejected.returncode != 0 and not rejected.stdout and b'invalid_configuration' in rejected.stderr
             and runtime.read_bytes() == b'{', 'Bare stdio ignored or rewrote a corrupt adjacent configuration')
    finally:
        runtime.unlink()
        data.rmdir()
    check_read_only(executable, cwd, env, data)

    with occupy_default_ports():
        blocked = cli(executable, cwd, env)
        need(blocked.returncode != 0 and b'Cannot bind' in blocked.stderr and b' ready.' not in blocked.stderr,
             'Occupied fixed ports did not fail without a readiness summary')
        config = json.loads(runtime.read_bytes())
        need(tuple(config[role]['port'] for role in ('business', 'control', 'admin')) == DEFAULT_PORTS,
             'First portable startup silently selected alternative ports')

    with running_portable(executable, cwd, env, runtime, racers=2) as (config, local):
        need(tuple(config[role]['port'] for role in ('business', 'control', 'admin')) == DEFAULT_PORTS,
             'Portable startup changed the fixed default ports')
        need(all(config[role]['host'] == '127.0.0.1' for role in ('business', 'control')),
             'Portable service is not restricted to loopback')
        need(config['business']['allowed_hosts'] == ['127.0.0.1:3181', 'web-search.vastarmorai.com']
             and config['business']['public_url'] == 'https://web-search.vastarmorai.com/mcp'
             and 'origins' not in config['business'], 'Portable business defaults are incorrect')
        need((data / 'state').is_dir() and re.fullmatch(r'[a-f0-9]{64}', local),
             'Portable startup did not initialize executable-relative data')
        token_hashes = {data / name: digest(data / name) for name in ('admin.token', 'client.token')}
        before = digest(runtime)
        need(cli(executable, cwd, env, '--CheckConfig').returncode == 0,
             'Portable CheckConfig did not discover executable-relative data')
        settings = cli(executable, cwd, env, '--SettingsUrl')
        link = urlsplit(settings.stdout.decode('ascii').strip())
        need(settings.returncode == 0 and link.scheme == 'http' and link.hostname == '127.0.0.1'
             and link.port == config['admin']['port'] and link.path == '/settings/' and not link.query
             and re.fullmatch(r'login=[a-f0-9]{64}', link.fragment), 'Default portable settings link was invalid')
        explicit = folder / 'explicit config' / '运行.json'
        need(cli(executable, cwd, env, '--ConfigPath', str(explicit), '--InitializeConfig', '--SelectAvailablePorts').returncode == 0,
             'Explicit portable configuration initialization failed')
        need(explicit.is_file() and cli(executable, cwd, env, '--ConfigPath', str(explicit), '--CheckConfig').returncode == 0,
             'Explicit portable configuration did not take precedence')
        need(digest(runtime) == before and all(digest(path) == value for path, value in token_hashes.items()),
             'Portable discovery or explicit commands changed existing credentials')
        verify_mcp(config['business']['port'], local)
        port = config['business']['port']
        need(mcp_request(port, payload=INITIALIZE)[0] == 401, 'Unauthenticated portable MCP was accepted')
        original_acl = subprocess.check_output(acl, env=env, timeout=8)
        generated = generated_token(cli(executable, cwd, env, '--ConfigPath', str(runtime), '--GenerateToken',
                                        '--ClientId', 'acceptance', reply=b'yes\r\n'))
        need(mcp_request(port, generated, INITIALIZE)[0] == 401, 'Generated token became active without restart')

    with running_portable(executable, cwd, env, runtime) as (config, _):
        need(config['business']['port'] == port, 'Portable restart changed the persisted listener')
        verify_mcp(port, generated)
        rotated = secrets.token_hex(32)
        update = cli(executable, cwd, env, '--ConfigPath', str(runtime), '--SetToken', '--ClientId', 'acceptance',
                     reply=(rotated + '\r\nyes\r\n').encode('ascii'))
        need(update.returncode == 0 and not update.stdout and rotated.encode('ascii') not in update.stderr,
             'Portable token update rejected or disclosed the submitted credential')
        need(mcp_request(port, rotated, INITIALIZE)[0] == 401, 'Rotated token became active without restart')
        verify_mcp(port, generated)

    with running_portable(executable, cwd, env, runtime) as (config, _):
        registry = data / 'state' / 'client-registry.json'
        entry = next(client for client in json.loads(registry.read_bytes())['clients'] if client['id'] == 'acceptance')
        need(entry['token_sha256'] == hashlib.sha256(rotated.encode('ascii')).hexdigest()
             and entry.get('token_value') == rotated and generated.encode('ascii') not in registry.read_bytes(),
             'Portable token update did not retain only the current matching credential and digest')
        check_private_registry(registry, env)
        need(mcp_request(port, generated, INITIALIZE)[0] == 401, 'Old portable token survived restart')
        verify_mcp(port, rotated)

    need(subprocess.check_output(acl, env=env, timeout=8) == original_acl,
         'Portable token update changed effective runtime ACLs')
    need(digest(runtime) == before, 'Portable token update changed the deployment configuration')
    need(all(digest(path) == value for path, value in token_hashes.items()), 'Portable restart replaced credential files')
    need(not any(program_data.iterdir()) and snapshot(cwd) == decoy_snapshot,
         'Portable startup wrote to ProgramData or used the working directory')
    return {'bare_stdio': True, 'invalid_adjacent_stdio_rejected': True,
            'concurrent_startup': True, 'fixed_default_ports': True, 'occupied_port_failure': True,
            'startup_summary': True, 'read_only_failure': True, 'explicit_config': True,
            'standalone_generator': True, 'bearer_rotation_restart': True, 'runtime_acl_preserved': True}


def run_sdk(command, env, directory, timeout=180):
    """Retain process identities so timeout or early runner exit cannot orphan tests."""
    child = subprocess.Popen(command, env=env, cwd=directory,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    parent = psutil.Process(child.pid)
    owned = {(parent.pid, parent.create_time()): parent}
    deadline, code, forced = time.monotonic() + timeout, 124, 0
    try:
        while True:
            try:
                for proc in parent.children(recursive=True):
                    owned[(proc.pid, proc.create_time())] = proc
            except psutil.NoSuchProcess:
                pass
            try:
                code = child.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    break
    finally:
        remaining = wait_owned(owned.values(), timeout=2 if code != 124 else 0)
        for proc in reversed(remaining):
            try:
                if proc.is_running():
                    proc.kill()
                    forced += 1
            except psutil.NoSuchProcess:
                pass
        alive = wait_owned(owned.values(), timeout=5)
        need(not alive, 'Portable test process cleanup failed')
        child.wait(timeout=5)
    return code, forced


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--Package', type=Path, required=True)
    parser.add_argument('--PackageSHA256', required=True)
    parser.add_argument('--Output', type=Path, required=True)
    args = parser.parse_args()
    need(sys.platform == 'win32', 'Portable Windows acceptance requires Windows')
    package = args.Package.resolve(strict=True)
    output = args.Output.resolve()
    need(package != output and package.suffix.lower() == '.zip', 'Invalid ZIP or output')
    need(digest(package) == args.PackageSHA256.lower(), 'Candidate ZIP hash mismatch')
    inventory = package_files(package)
    need(inventory and all(item.get('hashes') for item in inventory), 'Windows ZIP must contain regular files')
    names = {item["name"].casefold() for item in inventory}
    for required in ("licenses/strata/license", "licenses/web/third_party_notices.txt", "config/settings.schema.json"):
        need(any(name.endswith("/" + required) for name in names), f"Missing portable asset: {required}")
    need(not any(name.endswith(("/libssl-3-x64.dll", "/libcrypto-3-x64.dll", "/qopensslbackend.dll")) for name in names),
         "Windows ZIP still contains OpenSSL binaries")
    report = {'passed': False}
    code = 1
    with tempfile.TemporaryDirectory(prefix='qcws portable acceptance ') as temporary:
        folder = Path(temporary).resolve(strict=True)
        original = folder / 'original extraction'
        # package_files has validated all member paths, duplicates and parent collisions.
        with zipfile.ZipFile(package) as archive:
            archive.extractall(original)
        moved = folder / 'relocated package 中文'
        need(original.resolve(strict=True).parent == moved.resolve().parent == folder, 'Unsafe ZIP relocation path')
        shutil.move(str(original), str(moved))
        found = list(moved.rglob('qcopilots-websearch.exe'))
        need(len(found) == 1 and ' ' in str(found[0]) and '中文' in str(found[0]), 'Ambiguous relocated executable')
        cwd = folder / 'unrelated working directory'
        cwd.mkdir()
        env = os.environ.copy()
        system = Path(os.environ['SystemRoot'])
        need(system.is_absolute() and (system / 'System32').is_dir(), 'Missing system runtime')
        for key in list(env):
            if key.lower() in {'path','qt_plugin_path','qml2_import_path','qml_import_path',
                               'qt_qpa_platform_plugin_path','ld_library_path','pythonpath'}:
                del env[key]
        env['PATH'] = os.pathsep.join(str(path) for path in (system / 'System32', system))
        portable = verify_portable(found[0], folder, cwd, env)
        tls_test = Path(__file__).resolve().parents[1] / 'tests' / 'python' / 'test_business_tls.py'
        tls_code, tls_forced = run_sdk([sys.executable, '-B', str(tls_test), '--Executable', str(found[0]),
                                       '--OpenSSL', str(fixture_openssl())], env, cwd, timeout=90)
        need(tls_code == 0 and not tls_forced, 'Portable origin TLS or its fixture cleanup failed')
        portable['origin_https'] = True
        exit_code, forced = run_sdk([sys.executable, '-B', str(Path(__file__).with_name('test_mcp_sdk.py')),
            '--Executable',str(found[0]),'--Output',str(folder / 'sdk.json')],
            env,cwd)
        if (folder / 'sdk.json').exists():
            report = json.loads((folder / 'sdk.json').read_text('utf-8'))
        if exit_code or forced:
            report.update(passed=False, error='sdk_runner_or_cleanup_failed')
        report['portable'] = {'package_sha256':digest(package), 'relocated_path_with_spaces':True,
                              'relocated_path_with_unicode':True, 'unrelated_working_directory':True,
                              'system_only_path':True, 'temporary_directory_removed':False,
                              'forced_process_count':forced, 'runner_exit_code':exit_code, **portable}
        need(report['portable']['package_sha256'] == args.PackageSHA256.lower(), 'ZIP changed during acceptance')
        code = 1 if forced else exit_code
    report['portable']['temporary_directory_removed'] = not Path(temporary).exists()
    need(report['portable']['temporary_directory_removed'], 'Portable test cleanup failed')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
