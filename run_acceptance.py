"""Supplemental verification of original v0.1.4 Linux runtime, without rebuilding."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'candidate-source'
OUT = ROOT / 'evidence'
OUT.mkdir()
COMMIT = '3e13f7bd930ffa495e14d50c3556028a21484204'
RUN = 43805306130
REPO = 'xiaobing318/QCopilots-WebSearch'
def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', path]))
def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
assert subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip() == COMMIT
run = api(f'repos/{REPO}/actions/runs/{RUN}')
assert run['head_sha'] == COMMIT and run['conclusion'] == 'success'
entry, = [e for e in api(f'repos/{REPO}/actions/runs/{RUN}/artifacts')['artifacts']
          if e['name'] == 'packages-ubuntu-x64' and not e['expired']]
archive = ROOT / 'packages.zip'
with archive.open('wb') as stream:
    subprocess.run(['gh', 'api', f'repos/{REPO}/actions/artifacts/{entry["id"]}/zip'], stdout=stream, check=True)
assert entry['workflow_run']['head_sha'] == COMMIT and entry['workflow_run']['id'] == RUN
assert entry['digest'] == 'sha256:' + sha(archive)
packages = ROOT / 'packages'
with zipfile.ZipFile(archive) as bundle:
    for item in bundle.infolist():
        name = PurePosixPath(item.filename)
        assert not name.is_absolute() and '..' not in name.parts and '\\' not in item.filename
    bundle.extractall(packages)
info = json.loads((packages / 'build-info-ubuntu-x64.json').read_text())
assert info['source_commit'] == COMMIT and not info['working_tree_dirty'] and info['project_version'] == '0.1.4'
deb, = [packages / name for name in info['package_sha256'] if name.endswith('.deb')]
assert sha(deb) == info['package_sha256'][deb.name]
runtime = ROOT / 'runtime'
subprocess.run(['dpkg-deb', '--extract', str(deb), str(runtime)], check=True)
exe = runtime / 'opt/qcopilots-websearch/bin/qcopilots-websearch'
sys.path.insert(0, str(SOURCE / 'scripts'))
from runtime_evidence import fingerprint
assert sha(exe) == info['executable_sha256'] and fingerprint(exe) == info['installed_runtime']
for label, binary in [('executable', exe), ('sqlite', next(runtime.rglob('libqsqlite.so')))]:
    checked = subprocess.run(['ldd', str(binary)], text=True, capture_output=True, check=True)
    (OUT / ('ldd-' + label + '.txt')).write_text(checked.stdout + checked.stderr)
    assert 'not found' not in checked.stdout + checked.stderr
script = ROOT / 'accept_stdio_backpressure.py'
subprocess.run([sys.executable, str(script), '--Executable', str(exe), '--Template', str(SOURCE / 'config/runtime.json'),
    '--EvidenceLibrary', str(SOURCE / 'scripts'), '--Output', str(OUT / 'backpressure.json')], check=True)
report = json.loads((OUT / 'backpressure.json').read_text())
assert report['passed'] and report['runtime'] == info['installed_runtime']
assert report['host']['system'] == 'Linux' and report['host']['architecture'] == 'x86_64'
assert report['host']['linux'] == {'ID': 'ubuntu', 'VERSION_ID': '26.04'}
record = {'passed': True, 'candidate_source_commit': COMMIT, 'candidate_run_id': RUN,
          'candidate_run_attempt': run['run_attempt'],
          'artifact': entry, 'archive_sha256': sha(archive), 'deb_sha256': sha(deb),
          'build_info_sha256': sha(packages / 'build-info-ubuntu-x64.json'),
          'helper_sha256': sha(script), 'report_sha256': sha(OUT / 'backpressure.json'),
          'supplement_run_id': os.environ['GITHUB_RUN_ID'], 'supplement_commit': os.environ['GITHUB_SHA'],
          'supplement_run_attempt': os.environ['GITHUB_RUN_ATTEMPT']}
(OUT / 'binding.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record, indent=2))
