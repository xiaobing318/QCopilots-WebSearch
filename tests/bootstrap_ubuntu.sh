#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
image='docker.io/library/ubuntu@sha256:0d5255be560e2d2ddcfbfda88549e9e815ef29e7cd7fc62183d5709e6e2963c6'
scratch="$(mktemp -d)"
cleanup() {
    for cid_file in "$scratch"/*.cid; do
        [[ -f "$cid_file" ]] || continue
        docker rm -f "$(cat -- "$cid_file")" >/dev/null 2>&1 || true
    done
    rm -rf -- "$scratch"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker pull --platform linux/amd64 "$image"
for mode in reject online; do
    network=none
    [[ "$mode" == online ]] && network=bridge
    timeout --signal=TERM 900 docker run --rm --pull=never --platform linux/amd64 \
        --cidfile "$scratch/$mode.cid" --network "$network" --env LANG=C.UTF-8 --env LC_ALL=C.UTF-8 \
        --mount "type=bind,src=$repo,dst=/src,readonly" \
        "$image" /bin/bash /src/tests/bootstrap_ubuntu_container.sh "$mode"
done
