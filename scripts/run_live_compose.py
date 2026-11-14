#!/usr/bin/env python3
"""Run the pinned SearXNG/Valkey reference services in an owned Compose project."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--Executable', type=Path, required=True)
    parser.add_argument('--OutputDir', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.OutputDir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if sys.platform != 'linux':
        raise RuntimeError('Reference Compose acceptance requires Linux')
    report, code = {'owned_project':'qcws-live-' + secrets.token_hex(8), 'cleanup_passed':False,
                    'temporary_directory_removed':False}, 1
    with tempfile.TemporaryDirectory(prefix='qcws-live-compose-') as temporary:
        directory = Path(temporary) / 'team'
        shutil.copytree(root / 'deploy/team', directory)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0))
            port = reservation.getsockname()[1]
        env = dict(os.environ, QCWS_HOST='acceptance.invalid', QCWS_ACME_EMAIL='acceptance@example.invalid',
                   SEARXNG_SECRET=secrets.token_hex(32), QCWS_SEARXNG_PORT=str(port))
        # The image otherwise chowns the bind mount to UID 977, preventing the
        # unprivileged runner from deleting its own temporary settings directory.
        override = directory / 'acceptance-override.json'
        override.write_text(json.dumps({'services': {'searxng': {
            'environment': {'FORCE_OWNERSHIP': 'false'}}}}), encoding='utf-8')
        command = ['docker','compose','--project-name',report['owned_project'],
                   '-f',str(directory/'compose.yml'),'-f',str(override)]
        def run(arguments, **kwargs):
            return subprocess.run(command + arguments, env=env, cwd=directory,
                                  capture_output=True, text=True, **kwargs)
        try:
            run(['config','--quiet'], check=True, timeout=30)
            run(['up','-d','--wait','--wait-timeout','180','valkey','searxng'], check=True, timeout=300)
            result = run(['ps','--format','json'], check=True, timeout=30)
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            report['containers'] = [{key:row.get(key) for key in ('ID','Service','Image','State','Health')} for row in rows]
            report['images'] = []
            for row in rows:
                image = subprocess.run(['docker','inspect','--format','{{.Image}}',row['ID']],
                                       capture_output=True,text=True,check=True,timeout=20).stdout.strip()
                report['images'].append({'service':row['Service'],'image_id':image})
            code = subprocess.call([sys.executable,str(root/'scripts/live_acceptance.py'),
                '--Executable',str(args.Executable.resolve(strict=True)),
                '--ProviderUrl',f'http://127.0.0.1:{port}/search',
                '--Output',str(output/'public-search.json')],env=env)
        except (OSError, ValueError, subprocess.SubprocessError):
            report['error'] = 'compose_acceptance_failed'
        finally:
            stopped = run(['down','--volumes','--remove-orphans','--timeout','10'], timeout=60)
            remaining = subprocess.run(['docker','ps','-aq','--filter',
                'label=com.docker.compose.project=' + report['owned_project']],
                capture_output=True,text=True,timeout=20)
            report['cleanup_passed'] = stopped.returncode == remaining.returncode == 0 and not remaining.stdout.strip()
            if not report['cleanup_passed']:
                code = 1
            (output/'public-compose.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    report['temporary_directory_removed'] = not Path(temporary).exists()
    (output/'public-compose.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
