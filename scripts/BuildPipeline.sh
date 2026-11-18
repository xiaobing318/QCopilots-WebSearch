#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for argument in "$@"; do
    case "$argument" in
        --help|-h)
            printf '%s\n' 'BuildPipeline.sh --Profile NAME [--ConfigPath FILE] [--Stages STAGES] [--CheckOnly] [--DryRun]' \
                'Stages: doctor,bootstrap,configure,build,test,install,publish' \
                'Install Python 3.12 or newer before running the pipeline.'
            exit 0
            ;;
    esac
done

if [[ -n "${PYTHON:-}" ]]; then
    candidates=("$PYTHON")
else
    candidates=(python3 /usr/bin/python3)
fi

for python in "${candidates[@]}"; do
    if command -v -- "$python" >/dev/null 2>&1 &&
        "$python" -I -S -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
        exec "$python" "$script_dir/pipeline.py" "$@"
    fi
done

printf '%s\n' 'Python 3.12 or newer is required. Install python3, python3-venv and python3-pip, or set PYTHON.' >&2
exit 2
