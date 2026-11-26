"""Check the annotated project tag and require its commit to have passed main CI."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def verify_tag(tag, commit, root=Path('.')):
    project = (root / 'CMakeLists.txt').read_text(encoding='utf-8')
    version = re.search(r'^set\(QCWS_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"', project, re.MULTILINE)
    if version is None or tag != f'v{version[1]}':
        raise RuntimeError('The release tag must match QCWS_VERSION in CMakeLists.txt')

    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, encoding='utf-8').strip()

    ref = f'refs/tags/{tag}'
    if git('cat-file', '-t', ref) != 'tag':
        raise RuntimeError('The release tag must be annotated')
    if git('rev-parse', f'{ref}^{{commit}}') != commit:
        raise RuntimeError('The release tag must point to the triggering commit')
    if not git('for-each-ref', '--format=%(contents:subject)%0a%(contents:body)', ref):
        raise RuntimeError('The release tag annotation must not be empty')
    return git('rev-parse', ref)


def verify(repository, commit, get_json):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise RuntimeError('Invalid GitHub repository')
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise RuntimeError('Invalid release commit SHA')
    root = f'/repos/{repository}'
    main = get_json(root + '/branches/main')['commit']['sha']
    comparison = get_json(root + f'/compare/{commit}...{main}')
    if comparison.get('merge_base_commit', {}).get('sha') != commit:
        raise RuntimeError('The release commit must already belong to main')
    query = urlencode({'branch': 'main', 'event': 'push', 'head_sha': commit, 'per_page': 100})
    result = get_json(root + '/actions/workflows/ci.yml/runs?' + query)
    runs = [run for run in result.get('workflow_runs', [])
            if run.get('head_sha') == commit and run.get('head_branch') == 'main'
            and run.get('event') == 'push' and run.get('path') == '.github/workflows/ci.yml']
    if not runs:
        raise RuntimeError('No main push CI run exists for the release commit')
    latest = max(runs, key=lambda run: run['id'])
    if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
        raise RuntimeError('The latest main push CI run for the release commit must succeed first')
    return latest['id']


def main():
    token = os.environ['GH_TOKEN']

    def get_json(path):
        request = Request('https://api.github.com' + path, headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    commit = os.environ['GITHUB_SHA']
    tag_object = verify_tag(os.environ['GITHUB_REF_NAME'], commit)
    run = verify(os.environ['GITHUB_REPOSITORY'], commit, get_json)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        print(f'tag_object={tag_object}', file=output)
    print(f'Release tag points to {commit} on main, which passed CI run {run}.')


if __name__ == '__main__':
    main()
