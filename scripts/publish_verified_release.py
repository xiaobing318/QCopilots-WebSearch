"""Run an unchanged candidate's gates, then upload or resume its verified assets."""
import argparse
import base64
import os
from pathlib import Path
import re
import subprocess
import sys
import release_transport as transport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('Tag', 'EvidenceCommit', 'EvidencePath', 'EvidenceSHA256', 'ToolsCommit'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--CandidateRunId', required=True, type=int)
    parser.add_argument('--CandidateSource', required=True, type=Path)
    parser.add_argument('--Output', required=True, type=Path)
    parser.add_argument('--ResumeDraftId', type=int)
    parser.add_argument('--PreviousChecksumsAssetId', type=int)
    parser.add_argument('--PreviousChecksumsSHA256')
    parser.add_argument('--PreviousChecksumsPath')
    parser.add_argument('--CheckOnly', action='store_true')
    args = parser.parse_args()
    tools_root = Path(__file__).resolve().parents[1]
    identity = transport.tools_identity(tools_root, args.ToolsCommit)
    candidate, output = args.CandidateSource.resolve(), args.Output.resolve()
    transport.need(candidate != tools_root.resolve() and not output.exists(), 'Use separate candidate and new output directories')
    transport.need(not output.is_relative_to(candidate) and not output.is_relative_to(tools_root.resolve()),
                   'Output must be outside source checkouts')
    transport.need(not subprocess.check_output(['git', '-C', str(candidate), 'status', '--porcelain']),
                   'Candidate checkout is dirty')
    commit = subprocess.check_output(['git', '-C', str(candidate), 'rev-parse', 'HEAD'], text=True).strip()
    recovery_args = (args.ResumeDraftId, args.PreviousChecksumsAssetId, args.PreviousChecksumsSHA256, args.PreviousChecksumsPath)
    transport.need(all(x is None for x in recovery_args) or all(x is not None for x in recovery_args),
                   'All four recovery identity fields must be supplied together')
    output.mkdir(parents=True)
    original = output / 'verified-original'
    # Execute the candidate's own verifier and provenance checks unchanged.
    subprocess.run([sys.executable, str(candidate / 'scripts/promote_release.py'),
        '--Tag', args.Tag, '--CandidateRunId', str(args.CandidateRunId), '--EvidenceCommit', args.EvidenceCommit,
        '--EvidencePath', args.EvidencePath, '--EvidenceSHA256', args.EvidenceSHA256, '--Output', str(original)],
        cwd=candidate, check=True, timeout=1500)
    print('Original candidate verification and attestations completed', flush=True)
    github = transport.GitHub(os.environ['GITHUB_REPOSITORY'])
    recovery = None
    if args.ResumeDraftId is not None:
        transport.need(args.ResumeDraftId > 0 and args.PreviousChecksumsAssetId > 0
                       and re.fullmatch(r'[0-9a-f]{64}', args.PreviousChecksumsSHA256), 'Invalid recovery identity')
        transport.draft_state(github, args.ResumeDraftId, args.Tag, commit)
        path = args.PreviousChecksumsPath
        transport.need(re.fullmatch(r'release-evidence/[A-Za-z0-9_./-]+', path)
                       and not any(part in ('', '.', '..') for part in path.split('/')), 'Invalid previous checksum path')
        # Immutable original bytes allow retries even if a prior attempt already
        # replaced the remote checksums before failing later in the upload.
        previous = subprocess.check_output(['git', '-C', str(tools_root), 'cat-file', 'blob', args.ToolsCommit + ':' + path])
        import hashlib
        transport.need(hashlib.sha256(previous).hexdigest() == args.PreviousChecksumsSHA256, 'Previous checksum bytes differ')
        recovery = {'draft_id': args.ResumeDraftId, 'old_checksums_asset_id': args.PreviousChecksumsAssetId,
                    'old_checksums_sha256': args.PreviousChecksumsSHA256,
                    'old_checksums_base64': base64.b64encode(previous).decode('ascii')}
    identity['candidate_verifier_sha256'] = transport.sha(candidate / 'scripts/promote_release.py')
    physical = transport.prepare(original, output / 'publication', identity, recovery)
    notes = output / 'notes.md'
    notes.write_text(f'Source commit: {commit}\n\nUnsigned Windows and Ubuntu packages, verified on the exact '
        'CI bytes. Public search is best effort. See windows11-evidence.json and publication-proof.json. '
        'quality-evidence.zip preserves every original quality log, including empty successful-check logs.\n',
        encoding='utf-8', newline='\n')
    result = transport.publish(original, physical, identity, github, notes, recovery, check_only=args.CheckOnly)
    transport.write(output / 'publication-result.json', result)
    print(('Verified without remote mutation: ' if args.CheckOnly else 'Verified and published ') + result['tag'])


if __name__ == '__main__':
    main()
