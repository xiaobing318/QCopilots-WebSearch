"""Exercise exact portable ZIP bytes from a relocated path without development PATH entries."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import psutil
from package_inventory import package_files
from verify_release import need, sha
from process_cleanup import wait_owned


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
    need(sha(package) == args.PackageSHA256.lower(), 'Candidate ZIP hash mismatch')
    inventory = package_files(package)
    need(inventory and all(item.get('hashes') for item in inventory), 'Windows ZIP must contain regular files')
    report = {'passed': False}
    code = 1
    with tempfile.TemporaryDirectory(prefix='qcws portable acceptance ') as temporary:
        folder = Path(temporary)
        original = folder / 'original extraction'
        # package_files has validated all member paths, duplicates and parent collisions.
        with zipfile.ZipFile(package) as archive:
            archive.extractall(original)
        moved = folder / 'relocated package'
        shutil.move(str(original), str(moved))
        found = list(moved.rglob('qcopilots-websearch.exe'))
        need(len(found) == 1 and ' ' in str(found[0]), 'Ambiguous relocated executable')
        env = os.environ.copy()
        system = Path(os.environ['SystemRoot'])
        need(system.is_absolute() and (system / 'System32').is_dir(), 'Missing system runtime')
        for key in list(env):
            if key.lower() in {'path','qt_plugin_path','qml2_import_path','qml_import_path',
                               'qt_qpa_platform_plugin_path','ld_library_path','pythonpath'}:
                del env[key]
        env['PATH'] = os.pathsep.join(str(path) for path in (system / 'System32', system))
        exit_code, forced = run_sdk([sys.executable,str(Path(__file__).with_name('test_mcp_sdk.py')),
            '--Executable',str(found[0]),'--Output',str(folder / 'sdk.json')],
            env,folder)
        if (folder / 'sdk.json').exists():
            report = json.loads((folder / 'sdk.json').read_text('utf-8'))
        if exit_code or forced:
            report.update(passed=False, error='sdk_runner_or_cleanup_failed')
        report['portable'] = {'package_sha256':sha(package), 'relocated_path_with_spaces':True,
                              'system_only_path':True, 'temporary_directory_removed':False,
                              'forced_process_count':forced, 'runner_exit_code':exit_code}
        need(report['portable']['package_sha256'] == args.PackageSHA256.lower(), 'ZIP changed during acceptance')
        code = 1 if forced else exit_code
    report['portable']['temporary_directory_removed'] = not Path(temporary).exists()
    need(report['portable']['temporary_directory_removed'], 'Portable test cleanup failed')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
