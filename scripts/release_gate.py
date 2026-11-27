"""Prepare an annotated release tag and verify its main CI and workflow identity."""
import argparse
from functools import partial
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TAG_PATTERN = r'v[0-9]+\.[0-9]+\.[0-9]+'
SHA_PATTERN = r'[a-f0-9]{40}'
UNCERTAIN_RESPONSE = (URLError, ConnectionError, TimeoutError, HTTPException, json.JSONDecodeError, UnicodeError)


def repository_path(repository):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise RuntimeError('Invalid GitHub repository')
    return f'/repos/{repository}'


def verify_project_version(tag, root):
    project = (root / 'CMakeLists.txt').read_text(encoding='utf-8')
    version = re.search(r'^set\(QCWS_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"', project, re.MULTILINE)
    if version is None or tag != f'v{version[1]}':
        raise RuntimeError('The release tag must match QCWS_VERSION in CMakeLists.txt')


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, encoding='utf-8').strip()


def verify_tag(tag, commit, root=Path('.')):
    verify_project_version(tag, root)
    ref = f'refs/tags/{tag}'
    if git(root, 'cat-file', '-t', ref) != 'tag':
        raise RuntimeError('The release tag must be annotated')
    if git(root, 'rev-parse', f'{ref}^{{commit}}') != commit:
        raise RuntimeError('The release tag must point to the triggering commit')
    if not git(root, 'for-each-ref', '--format=%(contents:subject)%0a%(contents:body)', ref):
        raise RuntimeError('The release tag annotation must not be empty')
    return git(root, 'rev-parse', ref)


def verify(repository, commit, get_json, *, require_tip=False):
    root = repository_path(repository)
    if not re.fullmatch(SHA_PATTERN, commit):
        raise RuntimeError('Invalid release commit SHA')
    main = get_json(root + '/branches/main')['commit']['sha']
    if require_tip and main != commit:
        raise RuntimeError('main advanced. Start a new run from the current main commit.')
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


def github_json(token, path, body=None):
    request = Request('https://api.github.com' + path,
                      data=None if body is None else json.dumps(body).encode('utf-8'),
                      headers={
                          'Authorization': 'Bearer ' + token,
                          'Accept': 'application/vnd.github+json',
                          'Content-Type': 'application/json',
                          'X-GitHub-Api-Version': '2022-11-28',
                      })
    with urlopen(request, timeout=30) as response:
        return None if response.status == 204 else json.load(response)


def optional_get(request, path):
    try:
        return request(path)
    except HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def git_ref(request, api_root, ref):
    result = optional_get(request, api_root + '/git/ref/' + ref.removeprefix('refs/'))
    if result is not None and result.get('ref') != ref:
        raise RuntimeError(f'GitHub returned a different reference for {ref}')
    return result


def require_release_absent(request, api_root, tag):
    page = 1
    while True:
        releases = request(api_root + f'/releases?per_page=100&page={page}')
        for release in releases:
            if release.get('tag_name') == tag:
                state = 'draft release' if release['draft'] else 'published release'
                raise RuntimeError(f'A {state} already exists for {tag}: {release["html_url"]}')
        if len(releases) < 100:
            return
        page += 1


def compatible_tag_object(request, api_root, ref, tag, commit):
    target = ref['object']
    if target['type'] != 'tag' or not re.fullmatch(SHA_PATTERN, target['sha']):
        raise RuntimeError(f'{tag} already exists and is not an annotated tag')
    annotation = request(api_root + '/git/tags/' + target['sha'])
    pointed = annotation['object']
    if (annotation['tag'] != tag or not annotation['message'].strip()
            or pointed['type'] != 'commit' or pointed['sha'] != commit):
        raise RuntimeError(f'{tag} already exists with a different commit or incompatible annotation')
    return target['sha']


def create_release_ref(request, api_root, ref, tag_object):
    try:
        observed = request(api_root + '/git/refs', {'ref': ref, 'sha': tag_object})
    except HTTPError as error:
        if error.code not in (409, 422) and not 500 <= error.code < 600:
            raise
        error.close()
        observed = git_ref(request, api_root, ref)
    except UNCERTAIN_RESPONSE:
        observed = git_ref(request, api_root, ref)
    if (observed is None or observed.get('ref') != ref
            or observed['object']['type'] != 'tag' or observed['object']['sha'] != tag_object):
        raise RuntimeError(f'Could not confirm creation of {ref} with tag object {tag_object}')


def active_release_run(request, api_root, tag, commit):
    query = urlencode({'branch': tag, 'head_sha': commit, 'per_page': 100})
    runs = request(api_root + '/actions/workflows/release.yml/runs?' + query)['workflow_runs']
    return max((run['id'] for run in runs
                if run['head_branch'] == tag and run['head_sha'] == commit
                and run['path'] == '.github/workflows/release.yml'
                and run['event'] in ('push', 'workflow_dispatch')
                and run['status'] in ('requested', 'queued', 'pending', 'waiting', 'in_progress')),
               default=None)


def prepare_release(repository, commit, tag, request, report, root=Path('.')):
    api_root = repository_path(repository)
    if not re.fullmatch(TAG_PATTERN, tag):
        raise RuntimeError('Enter a release tag in vX.Y.Z format')
    verify_project_version(tag, root)
    if git(root, 'rev-parse', 'HEAD') != commit:
        raise RuntimeError('The checkout does not match the selected main commit')
    verify(repository, commit, request, require_tip=True)
    if git_ref(request, api_root, f'refs/heads/{tag}') is not None:
        raise RuntimeError(f'A branch already uses the name {tag}')
    require_release_absent(request, api_root, tag)

    ref = f'refs/tags/{tag}'
    existing = git_ref(request, api_root, ref)
    if existing is None:
        annotation = request(api_root + '/git/tags', {
            'tag': tag, 'message': tag, 'object': commit, 'type': 'commit',
        })
        tag_object = annotation['sha']
        if not re.fullmatch(SHA_PATTERN, tag_object):
            raise RuntimeError('GitHub returned an invalid annotated tag object SHA')
    else:
        tag_object = compatible_tag_object(request, api_root, existing, tag, commit)

    actions_url = f'https://github.com/{repository}/actions'
    report(f'Release target {ref}, commit {commit}, annotated object {tag_object}.')
    report(f'Check [Release runs]({actions_url}/workflows/release.yml) for this tag and commit before retrying.')
    if existing is None:
        if request(api_root + '/branches/main')['commit']['sha'] != commit:
            raise RuntimeError('main advanced before the tag reference could be created')
        create_release_ref(request, api_root, ref, tag_object)
    else:
        pending = active_release_run(request, api_root, tag, commit)
        if pending is not None:
            report(f'Release is already queued or running: [Release run]({actions_url}/runs/{pending}).')
            return pending
    retry_message = 'Could not confirm Release dispatch. Check the tag and commit above before retrying.'
    try:
        dispatched = request(api_root + '/actions/workflows/release.yml/dispatches', {
            'ref': tag,
            'inputs': {'tag': tag, 'expected_tag_object': tag_object},
            'return_run_details': True,
        })
    except UNCERTAIN_RESPONSE as error:
        raise RuntimeError(retry_message) from error
    run_id = (dispatched or {}).get('workflow_run_id')
    if type(run_id) is not int or run_id <= 0:
        raise RuntimeError(retry_message)
    report(f'Started [Release run]({actions_url}/runs/{run_id}).')
    return run_id


def verify_dispatch_tag(ref, tag, expected_tag_object):
    if not re.fullmatch(TAG_PATTERN, tag) or ref != f'refs/tags/{tag}':
        raise RuntimeError('Use the main branch to prepare a release or dispatch its matching version tag')
    if not re.fullmatch(SHA_PATTERN, expected_tag_object):
        raise RuntimeError('A tagged manual release requires the full expected annotated tag object SHA')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--check-release', action='store_true')
    args = parser.parse_args()

    repository = os.environ['GITHUB_REPOSITORY']
    ref = os.environ['GITHUB_REF']
    request = partial(github_json, os.environ['GH_TOKEN'])

    if args.prepare:
        if (os.environ['GITHUB_EVENT_NAME'] != 'workflow_dispatch' or ref != 'refs/heads/main'
                or os.environ.get('EXPECTED_TAG_OBJECT')):
            raise RuntimeError('Prepare from main and leave expected_tag_object empty')

        def report(message):
            print(message, flush=True)
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
                print(message, file=summary)

        prepare_release(repository, os.environ['GITHUB_SHA'], os.environ['REQUESTED_TAG'], request, report)
        return

    tag = os.environ['GITHUB_REF_NAME']
    if ref != f'refs/tags/{tag}' or not re.fullmatch(TAG_PATTERN, tag):
        raise RuntimeError('A release must run on a version tag. Select main to prepare one.')
    api_root = repository_path(repository)
    if args.check_release:
        require_release_absent(request, api_root, tag)
        return

    expected_tag_object = os.environ.get('EXPECTED_TAG_OBJECT', '')
    if os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch':
        verify_dispatch_tag(ref, os.environ.get('REQUESTED_TAG', ''), expected_tag_object)
    commit = os.environ['GITHUB_SHA']
    tag_object = verify_tag(tag, commit)
    if expected_tag_object and tag_object != expected_tag_object:
        raise RuntimeError('The annotated release tag changed before the tagged run began')
    published = optional_get(request, api_root + f'/releases/tags/{tag}')
    if published is not None:
        raise RuntimeError(f'A published release already exists for {tag}: {published["html_url"]}')
    run = verify(repository, commit, request)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        print(f'tag_object={tag_object}', file=output)
    print(f'Release tag points to {commit} on main, which passed CI run {run}.')


if __name__ == '__main__':
    main()
