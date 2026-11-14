"""Generate source, provenance and actual package-file SBOMs without publishing."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import uuid
from package_inventory import package_files, require_windows_payload_match
from msi_inventory import inventory_msi
from build_evidence import identity, require_receipt

LIBRARIES = {
    "cares": ("c-ares", "1.34.9", "MIT"),
    "curl": ("libcurl", "8.23.0", "curl"),
    "httplib": ("cpp-httplib", "0.66.0", "MIT"),
    "lexbor": ("Lexbor", "3.0.0", "Apache-2.0"),
    "nlohmann_json": ("nlohmann/json", "3.12.0", "MIT"),
    "json_schema_validator": ("json-schema-validator", "2.4.1", "MIT"),
}


def stream_sha(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(block)
    return result.hexdigest()


def generate(pipeline, destination, packages):
    root = Path(__file__).resolve().parents[1]
    commit = pipeline.run(["git", "rev-parse", "HEAD"], True).strip()
    dirty = bool(pipeline.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], True).strip())
    cache = (pipeline.build / "CMakeCache.txt").read_text(encoding="utf-8")
    version = re.search(r"^QCWS_VERSION:STRING=(.+)$", cache, re.MULTILINE).group(1)
    target = pipeline.profile["platform"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    package_hashes = {p.name: pipeline.digest(p) for p in packages}
    primary = next(p for p in packages if p.suffix in (".zip", ".deb"))
    installed = require_receipt(pipeline.build / "install-receipt.json", identity(root, pipeline.profile))
    tested = require_receipt(pipeline.build / "test-receipt.json", installed['identity'])
    if installed['tested_artifacts'] != tested['artifacts']:
        raise RuntimeError('Installation does not refer to the completed test artifacts')
    if pipeline.digest(pipeline.build / "test-results.xml") != tested['junit_sha256']:
        raise RuntimeError("CTest evidence changed after the test stage")
    components = [{"type": "library", "bom-ref": "library:qt", "name": "Qt",
                   "version": "6.12.0", "licenses": [{"license": {"id": "LGPL-3.0-only"}}],
                   "properties": [{"name": "qcws:linkage", "value": "dynamic"},
                                  {"name": "qcws:vendor-sbom", "value": "See package share/qcopilots-websearch/sbom/qt"}]}]
    for key, (name, dep_version, license_id) in LIBRARIES.items():
        spec = pipeline.lock["sources"][key]
        components.append({"type": "library", "bom-ref": "library:" + key,
            "name": name, "version": dep_version, "licenses": [{"license": {"id": license_id}}],
            "externalReferences": [{"type": "distribution", "url": spec["url"],
                                    "hashes": [{"alg": "SHA-256", "content": spec["sha256"]}]}]})
        if key == "httplib":
            source_dir = re.search(r"^FETCHCONTENT_SOURCE_DIR_HTTPLIB:PATH=(.+)$", cache, re.MULTILINE).group(1)
            patched_hash = pipeline.digest(Path(source_dir) / "httplib.h")
            if patched_hash != "9234907a4431c8e62cb12f7573a03a1c095b028e246f578c9ede700395bd620c":
                raise RuntimeError("Unexpected compiled httplib header")
            components[-1]['properties'] = [
                {"name":"qcws:modified","value":"true"},
                {"name":"qcws:patch-script","value":"cmake/patches/httplib_input_lifetime.py"},
                {"name":"qcws:patch-sha256","value":pipeline.digest(root / "cmake/patches/httplib_input_lifetime.py")},
                {"name":"qcws:upstream-header-sha256","value":"51ddd1482c67acb31f3064e48329aade14acc7dd0fab31ee5397193697ea1c35"},
                {"name":"qcws:patched-header-sha256","value":patched_hash}]
    components.append({"type": "library", "bom-ref": "library:zlib", "name": "zlib", "version": "1.4",
                       "licenses": [{"license": {"id": "Zlib"}}]})
    if os.name != "nt":
        components.append({"type": "library", "bom-ref": "library:icu", "name": "ICU", "version": "73.2",
                           "licenses": [{"license": {"id": "Unicode-DFS-2016"}}]})
        packages_query = subprocess.run(["dpkg-query", "-W", "-f=${binary:Package}=${Version}\n", "libssl3t64"],
                                        capture_output=True, text=True, check=True).stdout.strip()
        components.append({"type": "library", "bom-ref": "library:system-openssl", "name": "Ubuntu OpenSSL",
                           "version": packages_query, "licenses": [{"license": {"id": "Apache-2.0"}}],
                           "properties": [{"name": "qcws:linkage", "value": "dynamic-system"}]})
    else:
        components.append({"type": "library", "bom-ref": "library:openssl", "name": "OpenSSL", "version": "3.5.9",
            "licenses": [{"license": {"id": "Apache-2.0"}}],
            "externalReferences": [{"type":"distribution","url":pipeline.lock['openssl_windows']['url'],
                "hashes":[{"alg":"SHA-256","content":pipeline.lock['openssl_windows']['sha256']}]}],
            "properties": [{"name": "qcws:linkage", "value": "dynamic-bundled"},
                           {"name": "qcws:assembly", "value": "no-asm"}]})
        # File version is taken from the deployed DLL, not its VS directory name.
        dll = pipeline.install / "bin/vcruntime140.dll"
        quoted = str(dll).replace("'", "''")
        runtime_version = pipeline.run(["powershell.exe", "-NoProfile", "-Command",
            f"(Get-Item -LiteralPath '{quoted}').VersionInfo.FileVersion"], True).strip()
        components.append({"type": "library", "bom-ref": "library:msvc-runtime", "name": "Microsoft Visual C++ Runtime",
                           "version": runtime_version,
                           "licenses": [{"license": {"name": "LicenseRef-Microsoft-Visual-Studio-2022-Redistributable"}}]})
    inventories, sboms, metadata_files = {}, {}, []
    for package in packages:
        if package.suffix == '.msi':
            entries, tables = inventory_msi(package, pipeline.tool_paths.get('wix') or 'wix', pipeline.env)
            metadata = destination / (package.name + '.tables.json')
            metadata.write_text(json.dumps(tables, indent=2) + '\n', encoding='utf-8')
            metadata_files.append(metadata.name)
        else:
            entries = package_files(package)
        inventories[package.name] = entries
        document = {"bomFormat":"CycloneDX", "specVersion":"1.6", "version":1,
            "serialNumber":"urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, commit + target + package_hashes[package.name])),
            "metadata":{"timestamp":now,"component":{"type":"application","bom-ref":"qcws",
                "name":"QCopilots-WebSearch","version":version,"licenses":[{"license":{"id":"Apache-2.0"}}]},
                "properties":[{"name":"qcws:inventory-package","value":package.name},
                              {"name":"qcws:inventory-sha256","value":package_hashes[package.name]}]},
            "components":components + entries,
            "dependencies":[{"ref":"qcws","dependsOn":[c['bom-ref'] for c in components]}]}
        sbom = destination / (package.name + '.cdx.json')
        sbom.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        sboms[package.name] = sbom.name
    consistency = None
    if os.name == 'nt' and any(p.suffix == '.msi' for p in packages):
        msi = next(p for p in packages if p.suffix == '.msi')
        consistency = require_windows_payload_match(inventories[primary.name], inventories[msi.name])
    executable_suffix = '/bin/qcopilots-websearch.exe' if os.name == 'nt' else '/bin/qcopilots-websearch'
    packaged_exes = [entry for entry in inventories[primary.name]
                    if ('/' + entry['name']).endswith(executable_suffix)]
    if len(packaged_exes) != 1:
        raise RuntimeError('Ambiguous packaged executable')
    relative = executable_suffix.lstrip('/')
    runtime_files = installed['runtime']['files']
    installed_exe = next(entry for entry in runtime_files if entry['path'] == relative)
    if packaged_exes[0]['hashes'][0]['content'] != installed_exe['sha256']:
        raise RuntimeError('Packaged executable differs from validated install')
    prefix = packaged_exes[0]['name'][:-len(relative)]
    payload = {entry['name'][len(prefix):]: entry for entry in inventories[primary.name]
               if entry['name'].startswith(prefix)}
    def content_hash(name, visited=()):
        if name in visited or name not in payload:
            raise RuntimeError('Missing or cyclic runtime package link')
        item = payload[name]
        if item.get('hashes'):
            return item['hashes'][0]['content']
        properties = {value['name']:value['value'] for value in item.get('properties', [])}
        link = properties['qcws:link-target']
        # Runtime Qt links are relative within their library directory.
        if link.startswith('/') or properties.get('qcws:link-kind') != 'symbolic':
            raise RuntimeError('Unexpected runtime link')
        import posixpath
        return content_hash(posixpath.normpath(posixpath.join(posixpath.dirname(name), link)), visited + (name,))
    for entry in runtime_files:
        if content_hash(entry['path']) != entry['sha256']:
            raise RuntimeError('Packaged runtime differs from validated installation')
    qt_source = pipeline.download(pipeline.lock["qt_source"])
    qt_name = pipeline.lock["qt_source"]["url"].rsplit("/", 1)[1]
    shutil.copyfile(qt_source, destination / qt_name)
    extra_sources = {}
    if os.name == "nt":
        spec = pipeline.lock["openssl_windows"]
        archive = pipeline.download(spec)
        name = spec["url"].rsplit("/", 1)[1]
        shutil.copyfile(archive, destination / name)
        extra_sources[name] = spec["sha256"]
    source = destination / f"QCopilots-WebSearch-{commit}-source.tar.gz"
    pipeline.run(["git", "archive", "--format=tar.gz", "--prefix=QCopilots-WebSearch/", "-o", source, commit])
    info = {"schema_version": 1, "project_version": version, "source_commit": commit,
            "working_tree_dirty": dirty, "target": target, "build_os": platform.platform(),
            "created_at": now, "signed": False, "package_sha256": package_hashes,
            "sboms": sboms, "inventory_files": {name:len(entries) for name,entries in inventories.items()},
            "metadata_files": metadata_files, "payload_consistency": consistency,
            "installed_runtime": installed['runtime'], "build_identity": installed['identity'],
            "junit_sha256": tested['junit_sha256'], "executable_sha256": installed_exe['sha256'],
            "project_source": source.name, "qt_source": qt_name,
            "qt_source_sha256": pipeline.lock["qt_source"]["sha256"], "extra_sources": extra_sources,
            "source_scope": "Git commit only. Dirty builds are local test artifacts and cannot be promoted."}
    (destination / f"build-info-{target}.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    for name in ('build-receipt.json', 'test-receipt.json', 'install-receipt.json', 'test-results.xml'):
        shutil.copyfile(pipeline.build / name, destination / (target + '-' + name))
    assets = sorted(p for p in destination.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (destination / "SHA256SUMS").write_text(
        "".join(f"{pipeline.digest(p)}  {p.name}\n" for p in assets), encoding="utf-8")
