"""Own and remove one runner-temporary directory used by this CI job."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile


def identity():
    return {key: os.environ[key] for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB')}


def create():
    root = Path(tempfile.mkdtemp(prefix='qcws-ci-', dir=Path(os.environ['RUNNER_TEMP']).resolve()))
    (root / '.qcws-owned.json').write_text(json.dumps(identity()), encoding='utf-8')
    return root


def diagnostic_summary(report):
    """Keep failure reasons and aggregate metrics without raw samples or settings."""
    if not isinstance(report, dict):
        return {}
    fields = ('name', 'error', 'Error', 'CleanupErrors', 'passed', 'Passed',
              'checks_passed', 'full_acceptance', 'workload_passed', 'outcome',
              'minimum_live_evidence_passed', 'cleanup_passed', 'compose_cleanup',
              'fixture_cleanup', 'temporary_directory_removed', 'peak_rss_bytes',
              'expected_requests', 'fixture_calls', 'measured_seconds', 'attempted',
              'successful', 'failed', 'p95_ms', 'seconds', 'forced', 'remaining',
              'available_cpus', 'memory_bytes', 'minimum_met')
    result = {key: report[key] for key in fields if key in report}
    for key in ('requests', 'health', 'stop', 'native_stop', 'resources', 'portable'):
        if isinstance(report.get(key), dict):
            result[key] = diagnostic_summary(report[key])
    if isinstance(report.get('phases'), list):
        result['phases'] = [diagnostic_summary(phase) for phase in report['phases']]
    return result


def report_failures(root):
    root = Path(root).resolve(strict=True)
    for path in sorted(root.rglob('*.json')):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        try:
            summary = diagnostic_summary(json.loads(path.read_text(encoding='utf-8-sig')))
        except (OSError, ValueError):
            continue
        if summary:
            print(str(path.relative_to(root)) + ': ' + json.dumps(summary, ensure_ascii=False))


def cleanup(root):
    """Reject aliases and foreign roots before removing only this job's files."""
    root = Path(root).absolute()
    parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    if (root.parent != parent or not root.name.startswith('qcws-ci-')
            or root.is_symlink() or root.resolve() != root):
        raise RuntimeError('Refusing a directory outside this job runner temporary root')
    if not root.exists():
        return
    marker = root / '.qcws-owned.json'
    if marker.is_symlink() or json.loads(marker.read_text(encoding='utf-8')) != identity():
        raise RuntimeError('Refusing a temporary directory owned by another job')
    # A directory junction is not a symbolic link on older Python versions.
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            item = Path(directory, name)
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise RuntimeError('Refusing an alias in the temporary directory')
    shutil.rmtree(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--Root', required=True, type=Path)
    args = parser.parse_args()
    cleanup(args.Root)


if __name__ == '__main__':
    main()
