"""Publish the current tag's exact CI packages after automatic checks succeed."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from package_inventory import canonical_member
import release_transport as transport
from verify_release import assemble, identity, need, sha

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_NAMES = ('packages-windows-x64', 'packages-ubuntu-x64')


def extract(archive, directory):
    need(not directory.exists(), 'Artifact destination already exists')
    directory.mkdir(parents=True)
    seen, total = set(), 0
    with zipfile.ZipFile(archive) as source:
        need(len(source.infolist()) <= 10000, 'Artifact entry limit')
        for entry in source.infolist():
            name = canonical_member(entry.orig_filename.rstrip('/'), windows=True)
            need(name.casefold() not in seen, 'Duplicate artifact entry')
            seen.add(name.casefold())
            target = directory / name
            need(target.resolve().is_relative_to(directory.resolve()), 'Artifact path escapes destination')
            mode = stat.S_IFMT(entry.external_attr >> 16)
            need(mode in (0, stat.S_IFREG, stat.S_IFDIR), 'Unsupported artifact member')
            if entry.is_dir():
                need(entry.file_size == 0, 'Artifact directory has content')
                target.mkdir(parents=True, exist_ok=True)
                continue
            need(mode != stat.S_IFDIR, 'Artifact file has directory mode')
            total += entry.file_size
            need(total <= 2 * 1024**3, 'Artifact byte limit')
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output)
            need(target.stat().st_size == entry.file_size, 'Artifact size mismatch')


def run_identity(github, commit, tag, run_id, attempt):
    need(type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0, 'Invalid workflow run identity')
    repository = github.api('')
    run = github.api('/actions/runs/' + str(run_id))
    need(run['id'] == run_id and run['head_sha'] == commit and run['event'] == 'push',
         'Workflow source or event mismatch')
    need(run['repository']['id'] == repository['id'] == run['head_repository']['id'],
         'Workflow repository mismatch')
    need(run['run_attempt'] == attempt, 'Workflow attempt changed')
    workflow = github.api('/actions/workflows/' + str(run['workflow_id']))
    need(workflow['path'] == '.github/workflows/ci.yml', 'Unexpected release workflow')
    need(github.tag_commit(tag) == commit, 'Tag changed')
    return repository


def download_artifacts(root, github, repository, commit, run_id):
    """Pin both immutable artifact identities before downloading either."""
    need(not root.exists(), 'Artifact root must be new')
    artifacts = []
    for page in range(1, 101):
        batch = github.api(f'/actions/runs/{run_id}/artifacts?per_page=100&page={page}')
        artifacts.extend(batch['artifacts'])
        if len(artifacts) == batch['total_count']:
            break
        need(batch['artifacts'] and len(artifacts) < batch['total_count'], 'Truncated artifact listing')
    else:
        raise RuntimeError('Artifact pagination limit')
    selected = []
    for name in ARTIFACT_NAMES:
        found = [item for item in artifacts if item['name'] == name]
        need(len(found) == 1, 'Missing or ambiguous artifact: ' + name)
        item = found[0]
        origin = item['workflow_run']
        need(type(item['id']) is int and item['id'] > 0 and item['expired'] is False
             and re.fullmatch(r'sha256:[0-9a-f]{64}', item.get('digest', '')), 'Expired or unhashed artifact')
        need(origin['id'] == run_id and origin['head_sha'] == commit
             and origin['repository_id'] == repository['id'] == origin['head_repository_id'],
             'Artifact source mismatch')
        selected.append((item['id'], name, item['digest'].split(':', 1)[1]))
    need(len({item[0] for item in selected}) == len(selected), 'Duplicate artifact identity')
    root.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix='qcws-artifacts-') as temporary:
        for artifact_id, name, digest in selected:
            archive = Path(temporary) / (name + '.zip')
            with archive.open('xb') as stream:
                github.command(['api', '--allow-escape-sequences',
                                github.base + f'/actions/artifacts/{artifact_id}/zip'], stdout=stream)
            need(sha(archive) == digest, 'Downloaded artifact digest mismatch')
            extract(archive, root / name)


def attestation_match(verified, package_digest, repo_name, repository, commit, tag, run_id, attempt):
    need(type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0, 'Invalid workflow run identity')
    need(isinstance(verified, list) and verified, 'No verified package attestation')
    invocation = re.compile(re.escape(f'https://github.com/{repo_name}/actions/runs/{run_id}/attempts/') + r'([1-9][0-9]*)')
    matching = []
    for index, record in enumerate(verified):
        result = record.get('verificationResult', {})
        certificate = result.get('signature', {}).get('certificate', {})
        match = invocation.fullmatch(certificate.get('runInvocationURI', ''))
        if (match and int(match[1]) <= attempt
            and str(certificate.get('sourceRepositoryIdentifier')) == str(repository['id'])
            and str(certificate.get('sourceRepositoryOwnerIdentifier')) == str(repository['owner']['id'])
            and certificate.get('sourceRepositoryDigest') == commit
            and certificate.get('sourceRepositoryRef') == 'refs/tags/' + tag
            and result.get('verifiedTimestamps')
            and any(isinstance(subject, dict) and subject.get('digest', {}).get('sha256') == package_digest
                    for subject in result.get('statement', {}).get('subject', []))):
            matching.append(index)
    need(matching, 'Attestation does not belong to this workflow run and package')
    return matching


def verify_attestations(output, github, repository, commit, tag, run_id, attempt):
    packages = sorted(path for path in output.iterdir() if path.suffix in ('.zip', '.msi', '.deb'))
    need(len(packages) == 3 and {path.suffix for path in packages} == {'.zip', '.msi', '.deb'},
         'Attestation requires all three distribution packages')
    for package in packages:
        command = ['attestation', 'verify', str(package), '--hostname', 'github.com', '--repo', github.repository,
                   '--source-ref', 'refs/tags/' + tag, '--source-digest', commit,
                   '--cert-identity', f'https://github.com/{github.repository}/.github/workflows/ci.yml@refs/tags/{tag}',
                   '--signer-digest', commit, '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com',
                   '--deny-self-hosted-runners', '--predicate-type', 'https://slsa.dev/provenance/v1',
                   '--digest-alg', 'sha256', '--format', 'json']
        verified = json.loads(github.command(command, capture_output=True).stdout)
        attestation_match(verified, sha(package), github.repository, repository, commit, tag, run_id, attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ArtifactsRoot', 'Output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('Tag', 'Commit'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--CheckOnly', action='store_true')
    args = parser.parse_args()
    identity(args.Commit, args.Tag)
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    need(os.environ['GITHUB_SHA'] == args.Commit and os.environ['GITHUB_REF'] == 'refs/tags/' + args.Tag,
         'Publication differs from workflow source')
    need(os.environ['GITHUB_EVENT_NAME'] == 'push', 'Only a pushed version tag can publish')
    need(subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip() == args.Commit,
         'Checkout differs from tagged source')
    need(not subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain']),
         'Publication checkout is dirty')
    root, output = args.ArtifactsRoot.resolve(), args.Output.resolve()
    need(not root.exists() and not output.exists(), 'Use new artifact and output directories')
    need(not root.is_relative_to(ROOT) and not output.is_relative_to(ROOT)
         and not root.is_relative_to(output) and not output.is_relative_to(root),
         'Artifact and output directories must be separate and outside source')
    github = transport.GitHub(os.environ['GITHUB_REPOSITORY'])
    repository = run_identity(github, args.Commit, args.Tag, run_id, attempt)
    download_artifacts(root, github, repository, args.Commit, run_id)
    assemble(root, args.Commit, args.Tag, output)
    verify_attestations(output, github, repository, args.Commit, args.Tag, run_id, attempt)
    with tempfile.TemporaryDirectory(prefix='qcws-release-notes-') as temporary:
        notes = Path(temporary) / 'notes.md'
        notes.write_text(f'Source commit: {args.Commit}\n\n'
                         f'Built and checked by https://github.com/{github.repository}/actions/runs/{run_id}.\n\n'
                         'Windows ZIP/MSI and Ubuntu DEB packages are unsigned. '
                         'Public search is best effort. SHA256SUMS covers all attached distributions, '
                         'SBOMs and corresponding source archives.\n', encoding='utf-8')
        result = transport.publish(output, github, args.Tag, args.Commit, notes, check_only=args.CheckOnly)
    print(('Verified without remote changes: ' if args.CheckOnly else 'Published ') + result['tag'])


if __name__ == '__main__':
    main()
