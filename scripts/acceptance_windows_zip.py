"""Exercise exact portable ZIP bytes from a relocated path without development PATH entries."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from package_inventory import package_files
from verify_release import need, sha


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
        result = subprocess.run([sys.executable,str(Path(__file__).with_name('test_mcp_sdk.py')),
            '--Executable',str(found[0]),'--Output',str(folder / 'sdk.json')],
            env=env,cwd=folder,check=False,timeout=180)
        report = json.loads((folder / 'sdk.json').read_text('utf-8'))
        report['portable'] = {'package_sha256':sha(package), 'relocated_path_with_spaces':True,
                              'system_only_path':True, 'temporary_directory_removed':False}
        need(report['portable']['package_sha256'] == args.PackageSHA256.lower(), 'ZIP changed during acceptance')
        code = result.returncode
    report['portable']['temporary_directory_removed'] = not Path(temporary).exists()
    need(report['portable']['temporary_directory_removed'], 'Portable test cleanup failed')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
