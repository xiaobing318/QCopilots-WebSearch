"""Retrieve, verify and promote an existing CI candidate without rebuilding it."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import quote
import zipfile
from package_inventory import canonical_member
from verify_release import need, sha


def run(arguments, capture=False, **kwargs):
    return subprocess.run(arguments,check=True,text=True,capture_output=capture,**kwargs).stdout


def api(path):
    return json.loads(run(['gh','api',path],capture=True))


def attestation_match(verified, package_digest, repo_name, repository, commit, tag, run_id, attempt):
    need(isinstance(verified, list) and verified, 'No verified package attestation')
    invocation = f'https://github.com/{repo_name}/actions/runs/{run_id}/attempts/{attempt}'
    matching = []
    for index, record in enumerate(verified):
        result = record.get('verificationResult', {})
        certificate = result.get('signature', {}).get('certificate', {})
        if (certificate.get('runInvocationURI') == invocation
            and str(certificate.get('sourceRepositoryIdentifier')) == str(repository['id'])
            and str(certificate.get('sourceRepositoryOwnerIdentifier')) == str(repository['owner']['id'])
            and certificate.get('sourceRepositoryDigest') == commit
            and certificate.get('sourceRepositoryRef') == 'refs/tags/' + tag
            and result.get('verifiedTimestamps')
            and any(isinstance(subject, dict) and subject.get('digest', {}).get('sha256') == package_digest
                    for subject in result.get('statement', {}).get('subject', []))):
            matching.append(index)
    need(matching, 'Attestation does not belong to the exact candidate run/attempt')
    return matching


def verify_attestations(output, repo_name, repository, commit, tag, run_id, attempt):
    packages = sorted(path for path in output.iterdir() if path.suffix in ('.zip', '.msi', '.deb'))
    need(len(packages) == 3 and {path.suffix for path in packages} == {'.zip', '.msi', '.deb'},
         'Attestation requires all three actual distribution packages')
    report = {'schema_version': 1, 'source_commit': commit, 'run_id': run_id, 'run_attempt': attempt,
              'gh_version': run(['gh', '--version'], capture=True).strip(), 'packages': []}
    for package in packages:
        command = ['gh', 'attestation', 'verify', str(package), '--hostname', 'github.com', '--repo', repo_name,
                   '--source-ref', 'refs/tags/' + tag, '--source-digest', commit,
                   '--cert-identity', f'https://github.com/{repo_name}/.github/workflows/ci.yml@refs/tags/{tag}',
                   '--signer-digest', commit, '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com',
                   '--deny-self-hosted-runners', '--predicate-type', 'https://slsa.dev/provenance/v1',
                   '--digest-alg', 'sha256', '--format', 'json']
        verified = json.loads(run(command, capture=True))
        digest = sha(package)
        matching = attestation_match(verified, digest, repo_name, repository, commit, tag, run_id, attempt)
        report['packages'].append({'name': package.name, 'sha256': digest, 'verified': verified,
            'matching_records': matching, 'policy_arguments': command[4:],
            'api_url': f'https://api.github.com/repos/{repo_name}/attestations/sha256:{digest}?predicate_type=provenance'})
    path = output / 'verified-build-provenance.json'
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return path


def tag_commit(base,tag):
    obj = api(base+'/git/ref/tags/'+quote(tag,safe=''))['object']
    for _ in range(10):
        if obj['type'] == 'commit':
            return obj['sha']
        need(obj['type'] == 'tag','Tag does not resolve to a commit')
        obj = api(base+'/git/tags/'+obj['sha'])['object']
    raise RuntimeError('Tag nesting limit exceeded')


def extract(archive,directory):
    need(not directory.exists(),'Artifact destination already exists')
    directory.mkdir(parents=True)
    seen,total = set(),0
    with zipfile.ZipFile(archive) as source:
        need(len(source.infolist()) <= 10000,'Artifact entry limit')
        for entry in source.infolist():
            name = canonical_member(entry.orig_filename.rstrip('/'))
            need(name not in seen,'Duplicate artifact entry')
            seen.add(name)
            target = directory/name
            need(target.resolve().is_relative_to(directory.resolve()),'Artifact path escapes destination')
            mode = stat.S_IFMT(entry.external_attr >> 16)
            need(mode in (0,stat.S_IFREG,stat.S_IFDIR),'Unsupported artifact member')
            if entry.is_dir():
                need(entry.file_size == 0,'Artifact directory has content')
                target.mkdir(parents=True,exist_ok=True)
                continue
            total += entry.file_size
            need(total <= 2*1024**3,'Artifact byte limit')
            target.parent.mkdir(parents=True,exist_ok=True)
            with source.open(entry) as incoming,target.open('xb') as output:
                shutil.copyfileobj(incoming,output)
            need(target.stat().st_size == entry.file_size,'Artifact size mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('Tag','EvidenceCommit','EvidencePath','EvidenceSHA256'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--CandidateRunId',type=int,required=True)
    parser.add_argument('--Output',type=Path,required=True)
    parser.add_argument('--Publish',action='store_true')
    args = parser.parse_args()
    need(re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',args.Tag),'Invalid tag')
    need(args.CandidateRunId > 0 and re.fullmatch(r'[0-9a-f]{40}',args.EvidenceCommit)
         and re.fullmatch(r'[0-9a-f]{64}',args.EvidenceSHA256),'Invalid evidence identity')
    need(args.EvidencePath.startswith('release-evidence/')
         and re.fullmatch(r'[A-Za-z0-9_./-]+\.json',args.EvidencePath)
         and not any(part in ('','.','..') for part in args.EvidencePath.split('/')),'Invalid evidence path')
    repo_name = os.environ['GITHUB_REPOSITORY']
    need(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo_name),'Invalid repository')
    base = 'repos/'+repo_name
    repo,commit = api(base),tag_commit(base,args.Tag)
    need(run(['git','rev-parse','HEAD'],capture=True).strip() == commit,'Checkout differs from the tagged candidate')
    candidate = api(base+'/actions/runs/'+str(args.CandidateRunId))
    need(candidate['id'] == args.CandidateRunId and candidate['status'] == 'completed'
         and candidate['conclusion'] == 'success','Candidate CI has not succeeded')
    need(candidate['head_sha'] == commit and candidate['event'] in ('push','workflow_dispatch'),'Candidate source/event mismatch')
    need(candidate['repository']['id'] == repo['id'] == candidate['head_repository']['id'],'Candidate repository mismatch')
    workflow = api(base+'/actions/workflows/'+str(candidate['workflow_id']))
    need(workflow['path'] == '.github/workflows/ci.yml','Unexpected candidate workflow')
    artifacts,page = [],1
    while True:
        batch = api(base+f'/actions/runs/{args.CandidateRunId}/artifacts?per_page=100&page={page}')
        artifacts.extend(batch['artifacts'])
        if len(artifacts) >= batch['total_count']:
            break
        need(batch['artifacts'] and page < 100,'Truncated artifact listing')
        page += 1
    selected = []
    for name in ('packages-windows-x64','packages-ubuntu-x64','diagnostics-windows-x64','diagnostics-ubuntu-x64'):
        found = [item for item in artifacts if item['name'] == name]
        need(len(found) == 1,'Missing or ambiguous artifact: '+name)
        item,origin = found[0],found[0]['workflow_run']
        need(not item['expired'] and re.fullmatch(r'sha256:[0-9a-f]{64}',item.get('digest','')),'Expired or unhashed artifact')
        need(origin['id'] == args.CandidateRunId and origin['head_sha'] == commit
             and origin['repository_id'] == repo['id'] == origin['head_repository_id'],'Artifact source mismatch')
        selected.append(item)
    content = api(base+'/contents/'+quote(args.EvidencePath,safe='/')+'?ref='+args.EvidenceCommit)
    need(content['type'] == 'file' and content.get('encoding') == 'base64','Evidence is not a retrievable JSON file')
    raw = base64.b64decode(''.join(content['content'].split()),validate=True)
    need(len(raw) == content['size'] and hashlib.sha256(raw).hexdigest() == args.EvidenceSHA256,'Evidence digest mismatch')
    with tempfile.TemporaryDirectory(prefix='qcws-promote-') as temporary:
        directory = Path(temporary)
        evidence = directory/'windows11-evidence.json'
        evidence.write_bytes(raw)
        artifact_root = directory/'artifacts'
        artifact_root.mkdir()
        for item in selected:
            archive = directory/(item['name']+'.zip')
            with archive.open('xb') as output:
                subprocess.run(['gh','api','--allow-escape-sequences',base+f"/actions/artifacts/{item['id']}/zip"],
                               stdout=output,check=True)
            need(sha(archive) == item['digest'].split(':',1)[1],'Downloaded artifact digest mismatch')
            extract(archive,artifact_root/item['name'])
        import sys
        run([sys.executable,str(Path(__file__).with_name('verify_release.py')),
             '--ArtifactsRoot',str(artifact_root),'--Commit',commit,'--Tag',args.Tag,
             '--CandidateRunId',str(args.CandidateRunId),'--WindowsEvidence',str(evidence),
             '--Repository',repo_name,'--CandidateRunAttempt',str(candidate['run_attempt']),
             '--WindowsEvidenceSHA256',args.EvidenceSHA256,'--Output',str(args.Output)])
        metadata = {'source_commit':commit,'tag':args.Tag,'candidate_run_id':args.CandidateRunId,
            'run_attempt':candidate['run_attempt'],
            'artifacts':[{'id':item['id'],'name':item['name'],'digest':item['digest']} for item in selected],
            'evidence_commit':args.EvidenceCommit,'evidence_path':args.EvidencePath,'evidence_sha256':args.EvidenceSHA256}
        provenance = args.Output/'promotion-inputs.json'
        provenance.write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
        manifest_path = args.Output/'release-manifest.json'
        manifest = json.loads(manifest_path.read_text('utf-8'))
        attestation = verify_attestations(args.Output, repo_name, repo, commit, args.Tag,
                                          args.CandidateRunId, candidate['run_attempt'])
        manifest['assets'][attestation.name] = sha(attestation)
        manifest['assets'][provenance.name] = sha(provenance)
        manifest_path.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        (args.Output/'SHA256SUMS').write_text(''.join(sha(path)+'  '+path.name+'\n'
            for path in sorted(args.Output.iterdir()) if path.is_file() and path.name != 'SHA256SUMS'),encoding='utf-8')
        if not args.Publish:
            return
        need(tag_commit(base,args.Tag) == commit,'Tag changed during verification')
        files = sorted(args.Output.iterdir())
        notes = directory/'release-notes.txt'
        notes.write_text('Source commit: '+commit+'\n\nUnsigned Windows and Ubuntu packages. '
            'Windows 11 acceptance was performed on these exact MSI and ZIP bytes. '
            'Public search performance is best effort. See the included acceptance reports.\n',encoding='utf-8')
        run(['gh','release','create',args.Tag,'--repo',repo_name,'--draft','--verify-tag','--target',commit,
             '--title',args.Tag,'--notes-file',str(notes),*[str(path) for path in files]])
        state = json.loads(run(['gh','release','view',args.Tag,'--repo',repo_name,'--json','isDraft,assets'],capture=True))
        need(state['isDraft'] and {item['name'] for item in state['assets']} == {path.name for path in files},'Draft assets differ')
        downloaded = directory/'downloaded'
        run(['gh','release','download',args.Tag,'--repo',repo_name,'--dir',str(downloaded)])
        need({path.name for path in downloaded.iterdir()} == {path.name for path in files},'Downloaded assets differ')
        for source in files:
            need(sha(downloaded/source.name) == sha(source),'Uploaded bytes differ: '+source.name)
        need(tag_commit(base,args.Tag) == commit,'Tag changed before publication')
        run(['gh','release','edit',args.Tag,'--repo',repo_name,'--draft=false','--latest'])


if __name__ == '__main__':
    main()
