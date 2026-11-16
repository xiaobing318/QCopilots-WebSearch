"""Validate and assemble distribution files produced by the current CI run."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
from package_inventory import package_files, require_windows_payload_match

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'windows-x64': {'.zip', '.msi'}, 'ubuntu-x64': {'.deb'}}


def need(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def leaf(name):
    need(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name), 'Unsafe asset name')
    return name


def identity(commit, tag):
    need(re.fullmatch(r'[0-9a-f]{40}', commit) is not None, 'Invalid commit')
    need(re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag) is not None, 'Invalid tag')


def sums(folder):
    need(folder.is_dir() and not folder.is_symlink(), 'Missing platform directory')
    checksum = folder / 'SHA256SUMS'
    need(checksum.is_file() and not checksum.is_symlink(), 'Missing platform checksums')
    result = {}
    for line in checksum.read_text('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        need(match is not None, 'Malformed checksum line')
        name = leaf(match[2])
        path = folder / name
        need(name != 'SHA256SUMS' and name not in result and path.is_file() and not path.is_symlink(),
             'Missing or duplicate checksum member')
        need(sha(path) == match[1], 'Asset hash mismatch: ' + name)
        result[name] = match[1]
    need(result and all(path.is_file() and not path.is_symlink() for path in folder.iterdir()),
         'Unexpected platform directory or link')
    need(set(result) == {path.name for path in folder.iterdir() if path.name != 'SHA256SUMS'},
         'Unlisted platform asset')
    return result


def verify(root, commit, tag):
    identity(commit, tag)
    root = Path(root)
    need(root.is_dir() and not root.is_symlink(), 'Missing artifact root')
    need({path.name for path in root.iterdir()} == {'packages-' + target for target in TARGETS},
         'Unexpected or missing platform artifact')
    lock, collected = read(ROOT / 'config/dependencies.lock.json'), {}

    def collect(path):
        name, digest = leaf(path.name), sha(path)
        if name in collected:
            need(collected[name][1] == digest, 'Conflicting same-name assets: ' + name)
        else:
            collected[name] = path, digest

    for target, formats in TARGETS.items():
        folder = root / ('packages-' + target)
        checks = sums(folder)
        info_path = folder / ('build-info-' + target + '.json')
        need(info_path.name in checks, 'Missing platform build metadata')
        info = read(info_path)
        need(info.get('schema_version') == 1, 'Unsupported build metadata')
        need(info['source_commit'] == commit and info['working_tree_dirty'] is False, 'Wrong or dirty source commit')
        need(info['target'] == target and info['project_version'] == tag[1:], 'Wrong target or version')
        packages, sboms, extras = info['package_sha256'], info['sboms'], info['extra_sources']
        need(len(packages) == len(formats) and {Path(leaf(name)).suffix for name in packages} == formats,
             'Incomplete package formats')
        need(set(sboms) == set(packages) and len(set(sboms.values())) == len(packages), 'Missing per-package SBOM')
        need(set(info['inventory_files']) == set(packages), 'Incomplete package inventories')
        qt_name, source_name = leaf(info['qt_source']), leaf(info['project_source'])
        need(qt_name == lock['qt_source']['url'].rsplit('/', 1)[1], 'Wrong Qt source filename')
        need(source_name == f'QCopilots-WebSearch-{commit}-source.tar.gz', 'Wrong project source filename')
        public = set(packages) | {leaf(name) for name in sboms.values()} | {leaf(name) for name in extras}
        public |= {source_name, qt_name}
        need(len(public) == len(packages) * 2 + len(extras) + 2, 'Overlapping asset roles')
        required = public | {info_path.name}
        inventories = {}
        for name, digest in packages.items():
            need(re.fullmatch(r'[0-9a-f]{64}', digest) and checks.get(name) == digest, 'Package hash mismatch')
            need(sboms[name] == name + '.cdx.json', 'Wrong SBOM filename')
            bom = read(folder / leaf(sboms[name]))
            need(bom.get('bomFormat') == 'CycloneDX' and bom.get('specVersion') == '1.6'
                 and bom['metadata']['component']['version'] == tag[1:], 'Invalid SBOM')
            properties = {item['name']: item['value'] for item in bom['metadata']['properties']}
            need(properties.get('qcws:inventory-package') == name
                 and properties.get('qcws:inventory-sha256') == digest, 'SBOM package mismatch')
            components = bom['components']
            entries = [item for item in components if item['type'] == 'file']
            need(entries and len(entries) == info['inventory_files'][name]
                 and len({item['bom-ref'] for item in components}) == len(components), 'SBOM inventory identity mismatch')
            if Path(name).suffix != '.msi':
                need(entries == package_files(folder / name), 'SBOM differs from actual archive bytes')
            executable = 'bin/qcopilots-websearch' + ('.exe' if target == 'windows-x64' else '')
            matches = [item for item in entries if item['name'] == executable or item['name'].endswith('/' + executable)]
            need(len(matches) == 1 and matches[0]['hashes'] == [{'alg': 'SHA-256', 'content': info['executable_sha256']}],
                 'Packaged executable hash mismatch')
            inventories[Path(name).suffix] = entries
            sidecar = name + '.sha256'
            if sidecar in checks:
                need((folder / sidecar).read_text('utf-8').strip() == digest + '  ' + name, 'CPack checksum differs')
                required.add(sidecar)
        need(checks.get(qt_name) == info['qt_source_sha256'] == lock['qt_source']['sha256'], 'Qt source mismatch')
        expected_extras = {}
        if target == 'windows-x64':
            openssl = lock['openssl_windows']
            expected_extras[openssl['url'].rsplit('/', 1)[1]] = openssl['sha256']
            require_windows_payload_match(inventories['.zip'], inventories['.msi'])
        need(extras == expected_extras, 'Unexpected or missing dependency source')
        for name, digest in extras.items():
            need(checks.get(name) == digest, 'Extra source mismatch')
        with tarfile.open(folder / source_name, 'r:gz') as archive:
            need(archive.pax_headers.get('comment') == commit, 'Source archive commit mismatch')
        need(required == set(checks), 'Unexpected or missing platform asset')
        for name in sorted(public):
            collect(folder / name)
    need(len(collected) == 9, 'Unexpected public release layout')
    return collected


def assemble(root, commit, tag, output):
    files = verify(root, commit, tag)
    output = Path(output)
    need(not output.exists(), 'Output must be new')
    need(not output.resolve().is_relative_to(Path(root).resolve()), 'Output must be outside artifacts')
    output.mkdir(parents=True)
    for name, (source, digest) in files.items():
        shutil.copyfile(source, output / name)
        need(sha(output / name) == digest, 'Asset changed during copy')
    (output / 'SHA256SUMS').write_text(
        ''.join(digest + '  ' + name + '\n' for name, (_, digest) in sorted(files.items())),
        encoding='utf-8', newline='\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ArtifactsRoot', 'Output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('Commit', 'Tag'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    assemble(args.ArtifactsRoot, args.Commit, args.Tag, args.Output)


if __name__ == '__main__':
    main()
