#!/usr/bin/env bash
set -euo pipefail
mode="${1:?Expected reject or online}"
[[ "$mode" == reject || "$mode" == online ]]
fail() { printf '%s\n' "$*" >&2; exit 1; }
no_python() {
    ! command -v python3 >/dev/null 2>&1 || fail 'Image unexpectedly contains python3'
    [[ ! -e /usr/bin/python3 ]] || fail 'Image unexpectedly contains /usr/bin/python3'
}
no_python
[[ -x /usr/bin/perl && "$(dpkg --print-architecture)" == amd64 ]]
. /etc/os-release
[[ "$ID:$VERSION_ID" == ubuntu:26.04 ]]
case_dir="$(mktemp -d)"
fixture="$case_dir/repository"
mkdir "$fixture"
cp -a /src/scripts "$fixture/scripts"
cp -a /src/config "$fixture/config"
entry="$fixture/scripts/BuildPipeline.sh"
profile='团队引导测试'
config="$case_dir/参数 ' \" 配置.json"
offline_config="$case_dir/offline.json"
bad_config="$case_dir/bad.json"
marker="$case_dir/apt-called"
report="$case_dir/handoff.json"
expected="$case_dir/expected.argv"
write_config() {
    local destination="$1" offline="$2" bom="$3"
    [[ "$offline" == true || "$offline" == false ]]
    {
        if [[ "$bom" == yes ]]; then printf '\357\273\277'; fi
        cat <<JSON
{"version":1,"profiles":{"团队引导测试":{"platform":"ubuntu-x64","qtRoot":"qt",
"buildDir":"build/ubuntu-x64","installDir":"install/ubuntu-x64","cacheDir":".cache",
"buildType":"RelWithDebInfo","proxy":"","jobs":1,"cmakeArgs":[],"packageGenerators":["DEB"],
"offline":$offline,"tools":{}}}}
JSON
    } > "$destination"
}
write_config "$config" false yes
write_config "$offline_config" true no
printf '{"version":' > "$bad_config"
# The sole replacement proves interpreter/argv handoff without downloading Qt.
# The main CI pipeline separately executes the complete original pipeline.py.
cat > "$fixture/scripts/pipeline.py" <<'PY'
import json, os, sys
from pathlib import Path
assert sys.version_info >= (3, 12)
expected = Path(os.environ['QCWS_EXPECTED']).read_bytes().split(b'\0')
assert expected[-1] == b''
assert sys.argv[1:] == [item.decode('utf-8') for item in expected[:-1]]
raw = Path(os.environ['QCWS_CONFIG']).read_bytes()
assert raw.startswith(b'\xef\xbb\xbf')
assert json.loads(raw.decode('utf-8-sig'))['profiles']['团队引导测试']['offline'] is False
assert os.path.samefile(sys.executable, '/usr/bin/python3')
record = {'status':'passed','python':sys.version,'argv':sys.argv[1:],'bom':True}
Path(os.environ['QCWS_REPORT']).write_text(json.dumps(record), encoding='utf-8')
print(json.dumps(record))
PY
while IFS= read -r -d '' source; do
    relative="${source#/src/scripts/}"
    [[ "$relative" == pipeline.py ]] && continue
    cmp -- "$source" "$fixture/scripts/$relative"
done < <(find /src/scripts -type f -print0)
export QCWS_EXPECTED="$expected" QCWS_CONFIG="$config" QCWS_REPORT="$report"
if [[ "$mode" == reject ]]; then
    printf 'APT::Update::Pre-Invoke { "/usr/bin/touch %s"; };\n' "$marker" > /etc/apt/apt.conf.d/99qcws-test-sentinel
    printf 'DPkg::Pre-Invoke { "/usr/bin/touch %s"; };\n' "$marker" >> /etc/apt/apt.conf.d/99qcws-test-sentinel
    before="$(sha256sum /var/lib/dpkg/status)"
    run_case() {
        local name="$1" expected_status="$2"
        shift 2
        set +e
        bash "$entry" "$@" > "$case_dir/$name.log" 2>&1
        local status=$?
        set -e
        if [[ "$status" != "$expected_status" ]]; then
            cat -- "$case_dir/$name.log"
            fail "$name: unexpected exit status $status"
        fi
        no_python
        [[ ! -e "$marker" && ! -e "$report" ]] || fail "$name called APT or Python"
        [[ "$(sha256sum /var/lib/dpkg/status)" == "$before" ]] || fail "$name changed packages"
        printf 'PASS %s\n' "$name"
    }
    run_case check-only 2 --ConfigPath "$config" --Profile "$profile" --Stages bootstrap --CheckOnly
    run_case dry-run-chinese-bom 0 "--ConfigPath=$config" "--Profile=$profile" --Stages bootstrap --DryRun
    run_case offline 2 --ConfigPath "$offline_config" --Profile "$profile" --Stages bootstrap
    run_case bad-json 2 --ConfigPath "$bad_config" --Profile "$profile" --Stages bootstrap
    run_case missing-bootstrap 2 --ConfigPath "$config" --Profile "$profile" --Stages doctor
    mkdir "$case_dir/empty-sources"
    apt-get -o Dir::Etc::sourcelist=/dev/null -o "Dir::Etc::sourceparts=$case_dir/empty-sources" update >/dev/null
    [[ -f "$marker" ]] || fail 'APT sentinel did not execute'
    no_python
    printf '{"case":"reject","status":"passed","python_present":false}\n'
else
    # Empty profile.proxy must defeat inherited/global/per-host proxy settings.
    printf 'Acquire::http::Proxy "http://127.0.0.1:9";\nAcquire::http::Proxy::archive.ubuntu.com "http://127.0.0.1:9";\n' > /etc/apt/apt.conf.d/99qcws-proxy-test
    export http_proxy=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 no_proxy='' NO_PROXY=''
    args=(--ConfigPath "$config" --Profile "$profile" --Stages bootstrap,doctor)
    printf '%s\0' "${args[@]}" > "$expected"
    no_python
    bash "$entry" "${args[@]}"
    [[ -f "$report" ]] || fail 'Python handoff report missing'
    for package in python3 python3-venv python3-pip; do
        [[ "$(dpkg-query -W '-f=${Status}' "$package")" == 'install ok installed' ]] || fail "$package missing"
    done
    /usr/bin/python3 -I -S -c 'import sys; assert sys.version_info >= (3,12)'
    printf '{"case":"online","status":"passed","real_apt":true}\n'
fi
