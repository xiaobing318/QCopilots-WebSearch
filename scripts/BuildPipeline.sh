#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
original=("$@")
die() { printf '%s\n' "$*" >&2; exit 2; }
python_ready() {
    command -v -- "$1" >/dev/null 2>&1 &&
        "$1" -I -S -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1
}
for arg in "$@"; do
    if [[ "$arg" == --help || "$arg" == -h ]]; then
        printf '%s\n' 'BuildPipeline.sh --Profile NAME [--ConfigPath FILE] [--Stages STAGES] [--CheckOnly] [--DryRun]' \
            'Stages: doctor,bootstrap,configure,build,test,install,publish' \
            'Ubuntu bootstrap installs Python >= 3.12 when unavailable.'
        exit 0
    fi
done
if [[ -n "${PYTHON:-}" ]]; then candidates=("$PYTHON"); else candidates=(python3 /usr/bin/python3); fi
for candidate in "${candidates[@]}"; do
    if python_ready "$candidate"; then
        exec "$candidate" "$script_dir/pipeline.py" "${original[@]}"
    fi
done
[[ -z "${PYTHON:-}" ]] || die 'PYTHON selects an unavailable or old interpreter. Unset it to bootstrap system Python.'
[[ -x /usr/bin/perl ]] || die 'Ubuntu Essential package perl-base is missing.'
temporary="$(mktemp -d "${TMPDIR:-/tmp}/qcws-python.XXXXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
if ! /usr/bin/env -u PERL5OPT -u PERL5LIB -u PERLLIB -u PERL_UNICODE -u PERL_JSON_PP_USE_B \
    PERL_USE_UNSAFE_INC=0 /usr/bin/perl -I "$script_dir/vendor/perl" "$script_dir/bootstrap_config.pl" \
    "$script_dir/../config/BuildPipeline.json" "${original[@]}" > "$temporary/state"; then
    die 'Python bootstrap configuration validation failed.'
fi
mapfile -d '' -t state < "$temporary/state"
[[ ${#state[@]} == 5 ]] || die 'Invalid bootstrap reader output.'
[[ "${state[2]}" == 0 ]] || die 'CheckOnly: Python >= 3.12 is unavailable. No changes were made.'
[[ "${state[0]}" == 1 ]] || die 'Python >= 3.12 is required. Include bootstrap in --Stages to install it.'
[[ "${state[1]}" == 0 ]] || die 'Offline bootstrap requires preinstalled Python >= 3.12. No network access was attempted.'
[[ -r /etc/os-release ]] || die 'Cannot identify this operating system.'
os_id="$(. /etc/os-release; printf '%s:%s' "${ID:-}" "${VERSION_ID:-}")"
[[ "$os_id" == ubuntu:26.04 ]] || die 'Automatic Python bootstrap supports Ubuntu 26.04 only.'
[[ "$(/usr/bin/dpkg --print-architecture)" == amd64 ]] || die 'Automatic Python bootstrap supports amd64 only.'
proxy="${state[4]:-DIRECT}"
if [[ "${state[3]}" == 1 ]]; then
    printf 'DryRun: apt update and install python3 python3-venv python3-pip, proxy=%q\n' "$proxy"
    printf 'DryRun: verify Python >= 3.12, then resume: '
    printf '%q ' /usr/bin/python3 "$script_dir/pipeline.py" "${original[@]}"
    printf '\n'
    exit 0
fi
prefix=()
if (( EUID != 0 )); then
    command -v sudo >/dev/null 2>&1 || die 'sudo is required for bootstrap.'
    if [[ -t 0 && -t 2 ]]; then sudo -v; fi
    prefix=(sudo -n --)
fi
apt_command=("${prefix[@]}" /usr/bin/env -u APT_CONFIG -u http_proxy -u https_proxy -u HTTP_PROXY \
    -u HTTPS_PROXY -u ALL_PROXY -u all_proxy -u no_proxy -u NO_PROXY DEBIAN_FRONTEND=noninteractive \
    /usr/bin/apt-get -c "$script_dir/apt-proxy-reset.conf" \
    -o "Acquire::http::Proxy=$proxy" -o "Acquire::https::Proxy=$proxy")
"${apt_command[@]}" update --error-on=any
"${apt_command[@]}" install -y --no-install-recommends python3 python3-venv python3-pip
python_ready /usr/bin/python3 || die 'APT completed, but Python still does not meet the minimum version.'
rm -rf -- "$temporary"
trap - EXIT
exec /usr/bin/python3 "$script_dir/pipeline.py" "${original[@]}"
