"""Promote exact CI bytes only after platform and external Windows 11 acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import xml.etree.ElementTree as ET
from package_inventory import package_files, require_windows_payload_match

ROOT = Path(__file__).resolve().parents[1]


def need(ok,message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def leaf(name):
    need(isinstance(name,str) and name not in ('','.','..')
         and not any(c in name for c in '/\\:\r\n\x00'),'Unsafe asset name')
    return name


def unique(root,name):
    found = list(root.rglob(name))
    need(len(found) == 1 and found[0].is_file() and not found[0].is_symlink(),'Expected one '+name)
    return found[0]


def sums(folder):
    result = {}
    for line in (folder/'SHA256SUMS').read_text('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)',line)
        need(match is not None,'Malformed checksum line')
        name = leaf(match[2])
        path = folder/name
        need(name not in result and path.is_file() and not path.is_symlink(),'Missing or duplicate checksum member')
        need(sha(path) == match[1],'Asset hash mismatch: '+name)
        result[name] = match[1]
    need(all(path.is_file() and not path.is_symlink() for path in folder.iterdir()),'Unexpected platform directory or link')
    need(set(result) == {path.name for path in folder.iterdir() if path.name != 'SHA256SUMS'},'Unlisted platform asset')
    return result


def checked_runtime(runtime):
    entries = runtime['files']
    need(entries and len({entry['path'] for entry in entries}) == len(entries),'Invalid runtime inventory')
    encoded = json.dumps(entries,sort_keys=True,separators=(',',':')).encode()
    need(hashlib.sha256(encoded).hexdigest() == runtime['sha256'],'Runtime fingerprint mismatch')
    return runtime


def require_windows11(report):
    host = report.get('host', {})
    windows = host.get('windows', {})
    need(host.get('system') == 'Windows' and windows.get('product_type') == 1
         and windows.get('build', 0) >= 22000, 'This report must run on Windows 11')


def verify(root,commit,tag,run_id,evidence_path,evidence_sha,repository,run_attempt):
    need(re.fullmatch(r'[0-9a-f]{40}',commit) is not None,'Invalid commit')
    need(re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',tag) is not None,'Invalid tag')
    need(sha(evidence_path) == evidence_sha,'External evidence SHA256 mismatch')
    lock, collected, infos = read(ROOT/'config/dependencies.lock.json'), {}, {}

    def collect(path,name=None):
        name,digest = leaf(name or path.name),sha(path)
        if name in collected:
            need(collected[name][1] == digest,'Conflicting same-name assets: '+name)
        else:
            collected[name] = path,digest

    for target,formats in [('windows-x64',{'.zip','.msi'}),('ubuntu-x64',{'.deb'})]:
        folder = root/('packages-'+target)
        info_path = folder/('build-info-'+target+'.json')
        info,checks = read(info_path),sums(folder)
        infos[target] = info
        need(info['source_commit'] == commit and info['working_tree_dirty'] is False,'Wrong or dirty source commit')
        need(info['target'] == target and info['project_version'] == tag[1:],'Wrong target or version')
        for name in (info['project_source'],info['qt_source']):
            leaf(name)
        packages = info['package_sha256']
        need(len(packages) == len(formats) and {Path(name).suffix for name in packages} == formats,'Incomplete package formats')
        need(set(info['sboms']) == set(packages),'Missing per-package SBOM')
        runtime = checked_runtime(info['installed_runtime'])
        receipts = {stage:read(folder/(target+'-'+stage+'-receipt.json')) for stage in ('build','test','install')}
        need(all(receipt['identity'] == info['build_identity'] for receipt in receipts.values()),'Stale source/profile receipt')
        need(receipts['build']['artifacts'] and receipts['build']['artifacts'] == receipts['test']['artifacts']
             == receipts['install']['tested_artifacts'],'Build/test/install artifact chain differs')
        need(receipts['install']['runtime'] == runtime,'Install receipt differs from packaged runtime')
        junit = folder/(target+'-test-results.xml')
        need(sha(junit) == info['junit_sha256'] == receipts['test']['junit_sha256'],'JUnit receipt mismatch')
        cases = ET.parse(junit).getroot().findall('.//testcase')
        expected_tests = {'core','http-limits','tls','stdio-process','release-assets'}
        if target == 'windows-x64':
            expected_tests.add('windows-security')
        need({case.get('name') for case in cases} == expected_tests and len(cases) == len(expected_tests)
             and not any(case.find(key) is not None for case in cases for key in ('failure','error','skipped')),
             'Incomplete or failed CTest report')
        inventories = {}
        required = set(packages)|set(info['sboms'].values())|set(info['metadata_files'])|set(info['extra_sources'])
        required.update({info_path.name,info['project_source'],info['qt_source'],junit.name})
        required.update(target+'-'+stage+'-receipt.json' for stage in receipts)
        for name,digest in packages.items():
            need(checks.get(leaf(name)) == digest,'Package receipt hash mismatch')
            bom = read(folder/leaf(info['sboms'][name]))
            need(bom.get('bomFormat') == 'CycloneDX' and bom.get('specVersion') == '1.6'
                 and bom['metadata']['component']['version'] == tag[1:],'Invalid SBOM')
            properties = {item['name']:item['value'] for item in bom['metadata']['properties']}
            need(properties.get('qcws:inventory-package') == name
                 and properties.get('qcws:inventory-sha256') == digest,'SBOM package mismatch')
            entries = [item for item in bom['components'] if item['type'] == 'file']
            need(len(entries) == info['inventory_files'][name]
                 and len({item['bom-ref'] for item in bom['components']}) == len(bom['components']),'SBOM inventory identity mismatch')
            if Path(name).suffix != '.msi':
                need(entries == package_files(folder/name),'SBOM differs from actual archive bytes')
            executable = 'bin/qcopilots-websearch'+('.exe' if target == 'windows-x64' else '')
            matches = [item for item in entries if item['name'] == executable or item['name'].endswith('/'+executable)]
            need(len(matches) == 1 and matches[0]['hashes'][0]['content'] == info['executable_sha256'],
                 'Packaged executable receipt mismatch')
            inventories[Path(name).suffix] = entries
            sidecar = name+'.sha256'
            if sidecar in checks:
                need(digest in (folder/sidecar).read_text('utf-8'),'CPack checksum differs')
                required.add(sidecar)
        need(checks[info['qt_source']] == info['qt_source_sha256'] == lock['qt_source']['sha256'],'Qt source mismatch')
        for name,digest in info['extra_sources'].items():
            need(checks[leaf(name)] == digest,'Extra source mismatch')
        with tarfile.open(folder/info['project_source'],'r:gz') as archive:
            need(archive.pax_headers.get('comment') == commit,'Source archive commit mismatch')
        if target == 'windows-x64':
            require_windows_payload_match(inventories['.zip'],inventories['.msi'])
            openssl = lock['openssl_windows']
            need(info['extra_sources'].get(openssl['url'].rsplit('/',1)[1]) == openssl['sha256'],'Missing OpenSSL source')
        diagnostics = root/('diagnostics-'+target)
        ci = read(unique(diagnostics,'ci-run.json'))
        need(int(ci['GITHUB_RUN_ID']) == run_id and ci['GITHUB_SHA'] == commit and ci['profile'] == target,'Diagnostic run mismatch')
        need(ci['GITHUB_REPOSITORY'] == repository and int(ci['GITHUB_RUN_ATTEMPT']) == run_attempt,
             'Candidate requires complete rerun from the same repository and attempt')
        sdk_path,soak_path = unique(diagnostics,'mcp-sdk.json'),unique(diagnostics,'soak.json')
        sdk,soak = read(sdk_path),read(soak_path)
        need(sdk.get('passed') is True and sdk['runtime'] == runtime
             and sdk['executable_sha256'] == info['executable_sha256'],'SDK runtime/result mismatch')
        need(soak.get('checks_passed') is True and soak['runtime'] == runtime
             and soak['executable_sha256'] == info['executable_sha256'],'Stability runtime/result mismatch')
        if target == 'ubuntu-x64':
            need(soak.get('full_acceptance') is True and soak['seconds_per_phase'] >= 1800,'Full Linux stability required')
            accepted_path = unique(diagnostics,'deb-acceptance.json')
            accepted = read(accepted_path)
            deb = next(name for name in packages if name.endswith('.deb'))
            need(accepted.get('status') == 'passed' and accepted['candidateSha256'].lower() == packages[deb],
                 'DEB acceptance used different bytes')
            caddy_path,public_path = unique(diagnostics,'caddy-contract.json'),unique(diagnostics,'public-search.json')
            caddy,public = read(caddy_path),read(public_path)
            need(caddy.get('passed') is True and caddy['runtime'] == runtime and caddy.get('compose_cleanup') is True,
                 'Caddy acceptance incomplete')
            need(public.get('minimum_live_evidence_passed') is True and public['runtime'] == runtime
                 and public['closure']['completed'] is True and len(public['queries']) == 60,'Live public evidence incomplete')
            compose_path = unique(diagnostics,'public-compose.json')
            cleanup = read(compose_path)
            need(cleanup.get('cleanup_passed') is True and cleanup.get('temporary_directory_removed') is True,
                 'Public deployment cleanup incomplete')
            for path in (accepted_path,caddy_path,public_path,compose_path):
                collect(path)
        need(required == set(checks),'Unexpected or missing platform asset')
        for name in sorted(required):
            collect(folder/leaf(name))
        collect(sdk_path,'mcp-sdk-'+target+'.json')
        collect(soak_path,'soak-'+target+'.json')
        collect(unique(diagnostics,'ci-run.json'),'ci-run-'+target+'.json')
    evidence,windows = read(evidence_path),infos['windows-x64']
    need(evidence.get('schema_version') == 1 and evidence['source_commit'] == commit
         and int(evidence['candidate_run_id']) == run_id,'External evidence source/run mismatch')
    need(evidence['package_sha256'] == windows['package_sha256'],'External evidence used different ZIP/MSI bytes')
    msi,portable,soak = evidence['msi_report'],evidence['portable_report'],evidence['soak_report']
    require_windows11(portable)
    require_windows11(soak)
    need(msi.get('Passed') is True and not msi.get('CleanupErrors') and int(msi['WindowsProductType']) == 1
         and int(msi['WindowsBuild']) >= 22000,'Windows 11 MSI acceptance required')
    msi_hash = next(value for name,value in windows['package_sha256'].items() if name.endswith('.msi'))
    zip_hash = next(value for name,value in windows['package_sha256'].items() if name.endswith('.zip'))
    need(msi['Candidate']['Sha256'].lower() == msi_hash,'MSI report hash mismatch')
    need(portable.get('passed') is True and portable['runtime'] == windows['installed_runtime']
         and portable['executable_sha256'] == windows['executable_sha256']
         and portable.get('portable', {}).get('package_sha256') == zip_hash
         and portable['portable'].get('relocated_path_with_spaces') is True
         and portable['portable'].get('system_only_path') is True
         and portable['portable'].get('temporary_directory_removed') is True,'Portable ZIP acceptance required')
    need(soak.get('full_acceptance') is True and soak['seconds_per_phase'] >= 1800
         and soak['runtime'] == windows['installed_runtime'],'Windows 11 stability required')
    gates = evidence['delivery_gates']
    need(gates.get('council_status') == 'approved' and gates.get('unresolved_p0_p1') == 0
         and gates.get('qgis_development_passed') is True and gates.get('qgis_release_passed') is True
         and gates.get('temporary_state_restored') is True,'Delivery signoff is incomplete')
    need(re.fullmatch(r'[0-9a-f]{40}',gates.get('qgis_commit','')) is not None,'Missing QGIS snapshot')
    collect(evidence_path,'windows11-evidence.json')
    return collected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ArtifactsRoot','WindowsEvidence','Output'):
        parser.add_argument('--'+name,type=Path,required=True)
    for name in ('Commit','Tag','WindowsEvidenceSHA256','Repository'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--CandidateRunId',type=int,required=True)
    parser.add_argument('--CandidateRunAttempt',type=int,required=True)
    args = parser.parse_args()
    files = verify(args.ArtifactsRoot,args.Commit,args.Tag,args.CandidateRunId,args.WindowsEvidence,
                   args.WindowsEvidenceSHA256,args.Repository,args.CandidateRunAttempt)
    need(not args.Output.exists() or not any(args.Output.iterdir()),'Output must be empty')
    args.Output.mkdir(parents=True,exist_ok=True)
    for name,(source,digest) in files.items():
        shutil.copyfile(source,args.Output/name)
        need(sha(args.Output/name) == digest,'Asset changed during copy')
    manifest = {'schema_version':1,'source_commit':args.Commit,'tag':args.Tag,'candidate_run_id':args.CandidateRunId,
        'windows_evidence_sha256':args.WindowsEvidenceSHA256,'signed':False,
        'assets':{name:digest for name,(_,digest) in sorted(files.items())}}
    (args.Output/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    (args.Output/'SHA256SUMS').write_text(''.join(sha(path)+'  '+path.name+'\n'
        for path in sorted(args.Output.iterdir()) if path.is_file() and path.name != 'SHA256SUMS'),encoding='utf-8')


if __name__ == '__main__':
    main()
