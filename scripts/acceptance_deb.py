#!/usr/bin/env python3
"""Test release-specific DEB installations on a fresh disposable Ubuntu systemd VM."""
import argparse
import hashlib
import http.client
from itertools import combinations
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

from testkit import MIB, admin_smoke, digest as sha

APP_NAME = "qcopilots-websearch"
PROTOCOL = "2025-06-18"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "deb-acceptance", "version": "1"},
}}
LOG = []
ENV = {k: v for k, v in os.environ.items() if not k.startswith(("QT_", "QML", "LD_"))}
ENV.update(PATH="/usr/sbin:/usr/bin:/sbin:/bin", LC_ALL="C", DEBIAN_FRONTEND="noninteractive")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*args, check=True, timeout=300):
    args = list(map(str, args))
    result = subprocess.run(args, env=ENV, text=True, encoding="utf-8", stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    LOG.append(json.dumps(args) + "\n" + result.stdout)
    if check:
        require(result.returncode == 0, f"Command failed ({result.returncode}): {args}")
    return result


def metadata(package, field):
    return run("dpkg-deb", "-f", package, field).stdout.strip()


def apt(*args, check=True):
    return run("apt-get", "-y", "-o", "DPkg::Lock::Timeout=60", *args, check=check, timeout=600)


def package_exec_start(archive, unit):
    member_name = f"usr/lib/systemd/system/{unit}"
    content = None
    with subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(archive)], env=ENV, stdout=subprocess.PIPE) as process:
        with tarfile.open(fileobj=process.stdout, mode="r|") as files:
            for member in files:
                if member.name.removeprefix("./") != member_name:
                    continue
                require(content is None and member.isfile() and member.size <= 16384, "Unsafe package unit")
                with files.extractfile(member) as source:
                    content = source.read().decode("utf-8")
        # tarfile may finish at EOF markers before dpkg closes its output.
        while process.stdout.read(65536):
            pass
        require(process.wait(timeout=30) == 0, "Could not read package unit")
    require(content is not None, "Expected a packaged service unit")
    command = [line.removeprefix("ExecStart=") for line in content.splitlines() if line.startswith("ExecStart=")]
    require(len(command) == 1, "Expected a single package service startup command")
    return command[0]


class Installation:
    def __init__(self, archive, expected_sha256, *, allow_legacy=False):
        require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None, "Invalid expected SHA-256")
        require(archive.is_file() and sha(archive) == expected_sha256, "Package SHA-256 mismatch")
        self.archive = archive
        self.expected_sha256 = expected_sha256
        self.version = metadata(archive, "Version")
        require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version) is not None, "Wrong version format")
        self.name = f"{APP_NAME}-{self.version}"
        require(metadata(archive, "Package") == self.name and metadata(archive, "Architecture") == "amd64",
                "Wrong package identity")
        self.unit = self.name + ".service"
        self.account = "qcws-" + hashlib.sha256(self.version.encode("ascii")).hexdigest()[:24]
        self.app = Path("/opt") / self.name
        self.exe = self.app / "bin" / APP_NAME
        self.legacy_roots = (Path("/etc") / self.name, Path("/var/lib") / self.name)
        adjacent = self.exe.parent / "data"
        legacy_config, legacy_state = self.legacy_roots
        exec_start = package_exec_start(archive, self.unit)
        self.legacy = exec_start == f"{self.exe} --ConfigPath {legacy_config / 'runtime.json'}"
        if self.legacy:
            require(allow_legacy, "The candidate must use the executable-adjacent data layout")
            self.data, self.state = legacy_config, legacy_state
        else:
            require(exec_start == f"{self.exe} --ConfigPath {adjacent / 'runtime.json'}",
                    "The package startup command does not use the expected runtime path")
            self.data, self.state = adjacent, adjacent / "state"
        self.config = self.data / "runtime.json"
        self.tokens = (self.data / "client.token", self.data / "admin.token")
        self.db = self.state / "cache/cache-v1.sqlite"

    def package_state(self):
        result = run("dpkg-query", "-W", "-f=${Status}\\n${Version}", self.name, check=False)
        return result.stdout.splitlines() if result.returncode == 0 else []

    def install(self, reinstall=False):
        run("systemctl", "reset-failed", self.unit, check=False)
        options = ["--reinstall"] if reinstall else []
        apt("install", "--no-install-recommends", *options, self.archive)
        require(self.package_state() == ["install ok installed", self.version],
                "Package was not configured at the expected version")

    def active(self):
        return run("systemctl", "is-active", "--quiet", self.unit, check=False).returncode == 0

    def pid(self):
        value = run("systemctl", "show", self.unit, "-p", "MainPID", "--value", check=False).stdout.strip()
        return int(value) if value.isdigit() else 0

    def stop(self):
        old, started = self.pid(), time.monotonic()
        run("systemctl", "stop", self.unit, timeout=15)
        elapsed = time.monotonic() - started
        require(elapsed <= 10 and not self.active(), "Stop exceeded ten seconds or stayed active")
        require(not old or not Path(f"/proc/{old}").exists(), "Main process survived stop")
        return elapsed

    def read_config(self):
        return json.loads(self.config.read_bytes())

    def token(self):
        return self.tokens[0].read_text(encoding="ascii").strip()

    def snapshots(self):
        return {path: sha(path) for path in (self.config, *self.tokens)}

    def check_ports(self):
        runtime = self.read_config()
        require(runtime.get("project_version") == self.version, "Configuration version does not match package")
        require(runtime["cache"]["directory"] == str(self.state / "cache")
                and runtime["settings"]["directory"] == str(self.state),
                "Runtime did not select version-specific mutable state")
        ports = set()
        for endpoint in ("business", "control", "admin"):
            config = runtime[endpoint]
            port = config["port"]
            require(config.get("host", "127.0.0.1") == "127.0.0.1" and 1024 <= port <= 65535,
                    "Expected an assigned loopback endpoint")
            ports.add(port)
        require(len(ports) == 3, "Version's listener ports overlap")
        return ports


def control_request(installation, token=None):
    port = installation.read_config()["control"]["port"]
    url = f"http://127.0.0.1:{port}/health/live"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Authorization": "Bearer " + token} if token else {}
    return opener.open(urllib.request.Request(url, headers=headers), timeout=1)


def reject_token(installation, token=None):
    try:
        with control_request(installation, token):
            raise RuntimeError("Health accepted missing or foreign credential")
    except urllib.error.HTTPError as error:
        with error:
            require(error.code == 401, "Missing or foreign credential was not rejected with 401")


def health(installation):
    deadline = time.monotonic() + 15
    token = installation.token()
    while True:
        try:
            with control_request(installation, token) as response:
                require(response.status == 200 and json.load(response).get("live") is True,
                        "Unexpected liveness response")
            break
        except (OSError, urllib.error.URLError):
            require(time.monotonic() < deadline, "Installed service did not become healthy")
            time.sleep(0.1)
    reject_token(installation)
    process = installation.pid()
    require(installation.active() and process > 0, "Health did not come from systemd service")
    account = pwd.getpwnam(installation.account)
    inactive_roots = (installation.exe.parent / "data",) if installation.legacy else installation.legacy_roots
    require(all(not path.exists() and not path.is_symlink() for path in inactive_roots),
            "Installation generated data outside its own expected layout")
    for directory, owner, mode in ((installation.data, 0, 0o750),
                                   (installation.state, account.pw_uid, 0o700)):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == mode
                and info.st_uid == owner and info.st_gid == account.pw_gid,
                f"Unsafe version data directory: {directory}")
    for path in (installation.exe.parent, installation.exe):
        info = path.lstat()
        require(not path.is_symlink() and info.st_uid == 0 and info.st_mode & 0o022 == 0,
                "Program files must remain protected from service writes")
    protect = run("systemctl", "show", installation.unit, "-p", "ProtectSystem", "--value").stdout.strip()
    require(protect == "strict", "Service must retain the filesystem sandbox")
    if installation.legacy:
        directory = run("systemctl", "show", installation.unit, "-p", "StateDirectory", "--value").stdout.strip()
        require(directory == installation.name, "Legacy service did not retain its own state directory")
    else:
        read_write = run("systemctl", "show", installation.unit, "-p", "ReadWritePaths", "--value").stdout.split()
        require(read_write == [str(installation.state)], "Service must write only to its own version state")
    for token_path in installation.tokens:
        info = token_path.stat()
        require(not token_path.is_symlink() and stat.S_ISREG(info.st_mode)
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == account.pw_uid,
                f"Unsafe credential ownership or mode: {token_path.name}")
    config = installation.config.stat()
    require(config.st_uid == 0 and config.st_gid == account.pw_gid
            and stat.S_IMODE(config.st_mode) == 0o640, "Unsafe runtime ownership or mode")
    require(os.readlink(f"/proc/{process}/exe") == str(installation.exe)
            and sha(Path(f"/proc/{process}/exe")) == sha(installation.exe), "Wrong installed executable bytes")
    maps = Path(f"/proc/{process}/maps").read_text().splitlines()
    libraries = {line.split(maxsplit=5)[5] for line in maps
                 if len(line.split(maxsplit=5)) == 6 and ".so" in line.split(maxsplit=5)[5]}
    qt = {path for path in libraries if "/libQt6" in path}
    require(qt and all(path.startswith(str(installation.app / "lib") + "/") for path in qt), "Qt is not private")
    roots = (str(installation.app) + "/", "/usr/lib/", "/lib/", "/lib64/")
    require(all(path.startswith(roots) for path in libraries), "A library came from an unexpected directory")
    version = run(installation.exe, "--Version").stdout
    require(f"QCopilots-WebSearch {installation.version}, Qt 6.12.0" in version, "Wrong installed version")
    installation.check_ports()


def preserved(expected):
    require(all(path.is_file() and sha(path) == digest for path, digest in expected.items()),
            "Configuration, token or retained file changed")


def preflight(args, installations):
    require(args.DisposableMachine and os.geteuid() == 0, "Requires root and disposable VM opt-in")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    require(release.get("ID", "").strip('"') == "ubuntu"
            and release.get("VERSION_ID", "").strip('"') == "26.04"
            and os.uname().machine == "x86_64", "Requires Ubuntu 26.04 amd64")
    require(Path("/run/systemd/system").is_dir()
            and Path("/proc/1/comm").read_text().strip() == "systemd", "Requires systemd as PID 1")
    require(not run("dpkg", "--audit").stdout.strip(), "Another package needs repair")
    candidate, *baselines = installations
    require(len({item.version for item in installations}) == len(installations), "Expected distinct package versions")
    for baseline in baselines:
        run("dpkg", "--compare-versions", baseline.version, "lt", candidate.version)
    for installation in installations:
        unit_paths = [Path(root) / installation.unit for root in ("/etc/systemd/system", "/usr/lib/systemd/system")]
        for path in (installation.app, *installation.legacy_roots, *unit_paths):
            require(not path.exists() and not path.is_symlink(), "Pre-existing package state refused")
        require(not installation.package_state(), "Pre-existing dpkg state refused")
        try:
            pwd.getpwnam(installation.account)
        except KeyError:
            pass
        else:
            raise RuntimeError("Pre-existing service account refused")


def check_reinstall(installation, passed):
    expected = installation.snapshots()
    settings = installation.state / "settings.json"
    expected[settings] = sha(settings)
    elapsed = installation.stop()
    key = os.urandom(32).hex()
    payload = json.dumps({"acceptance": key}).encode()
    with sqlite3.connect(installation.db) as database:
        database.execute("INSERT INTO entries VALUES(?,?,?,?,?)",
                         (key, payload, len(payload), int(time.time()) + 86400, 0))
    run("systemctl", "start", installation.unit)
    health(installation)
    passed("stop-start", stopSeconds=elapsed)
    installation.install(reinstall=True)
    health(installation)
    preserved(expected)
    with sqlite3.connect(f"file:{installation.db}?mode=ro", uri=True) as database:
        require(database.execute("SELECT payload FROM entries WHERE key=?", (key,)).fetchone() == (payload,),
                "Logical cached data did not survive reinstall")
    passed("same-version-reinstall")


def mcp_request(port, token, payload=None, session=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
    if session:
        headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL})
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request("POST" if payload is not None else "DELETE", "/mcp",
                           json.dumps(payload).encode() if payload is not None else None, headers)
        response = connection.getresponse()
        data = response.read(MIB + 1)
        require(len(data) <= MIB, "Installed MCP response exceeded the fixture limit")
        return response.status, response.getheader("Mcp-Session-Id"), json.loads(data) if response.status == 200 else {}
    finally:
        connection.close()


def verify_mcp(port, token):
    status, session, data = mcp_request(port, token, INITIALIZE)
    require(status == 200 and session and data["result"]["protocolVersion"] == PROTOCOL,
            "Installed MCP initialization failed")
    try:
        status, _, _ = mcp_request(port, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        require(status == 202, "Installed MCP initialized notification failed")
        status, _, data = mcp_request(port, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        require(status == 200 and any(tool["name"] == "web_search_status" for tool in data["result"]["tools"]),
                "Installed MCP tool discovery failed")
        status, _, data = mcp_request(port, token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "web_search_status", "arguments": {}}}, session)
        require(status == 200 and data["result"]["structuredContent"]["contract_version"] == 1,
                "Installed MCP tool call failed")
    finally:
        require(mcp_request(port, token, session=session)[0] == 204, "Installed MCP session cleanup failed")


def check_token_rotation(installation, passed):
    port = installation.read_config()["business"]["port"]
    token_files = {path: sha(path) for path in installation.tokens}
    client = "deb-acceptance"
    args = [installation.exe, "--ConfigPath", installation.config, "--ClientId", client]
    # Credential output is captured here and is intentionally excluded from run() diagnostics.
    generated = subprocess.run([*args, "--GenerateToken"], input="yes\n", capture_output=True,
                               text=True, encoding="utf-8", env=ENV, timeout=10)
    token = generated.stdout.strip()
    require(generated.returncode == 0 and re.fullmatch(r"[a-f0-9]{64}", token) and token not in generated.stderr,
            "Installed CLI did not generate a client credential")
    require(mcp_request(port, token, INITIALIZE)[0] == 401, "Generated token became active without restart")
    run("systemctl", "restart", installation.unit)
    health(installation)
    verify_mcp(port, token)

    replacement = secrets.token_hex(32)
    rotated = subprocess.run([*args, "--SetToken"], input=replacement + "\nyes\n", capture_output=True,
                             text=True, encoding="utf-8", env=ENV, timeout=10)
    require(rotated.returncode == 0 and not rotated.stdout and replacement not in rotated.stderr,
            "Installed CLI did not rotate its client credential safely")
    require(mcp_request(port, replacement, INITIALIZE)[0] == 401, "Rotated token became active without restart")
    run("systemctl", "restart", installation.unit)
    health(installation)
    require(mcp_request(port, token, INITIALIZE)[0] == 401, "Previous token still works after rotation")
    verify_mcp(port, replacement)

    expected = {"id": client, "token_sha256": hashlib.sha256(replacement.encode("ascii")).hexdigest()}
    require([entry for entry in installation.read_config()["clients"] if entry["id"] == client] == [expected],
            "Runtime did not retain just the rotated client digest")
    raw = installation.config.read_bytes()
    require(token.encode("ascii") not in raw and replacement.encode("ascii") not in raw,
            "Runtime exposed a plaintext client credential")
    preserved(token_files)
    passed("client-token-rotation")


def check_isolation(candidate, baselines, passed):
    installations = [candidate, *baselines]
    for first, second in combinations(installations, 2):
        require(not first.check_ports().intersection(second.check_ports()),
                "The new layout did not reserve the stopped versions' listener ports")
        require(pwd.getpwnam(first.account).pw_uid != pwd.getpwnam(second.account).pw_uid,
                "Installed versions share a service account")
    for baseline in baselines:
        run("systemctl", "start", baseline.unit)
        health(baseline)
    require(len({item.pid() for item in installations}) == len(installations), "Installed versions share a process")
    for own in installations:
        for other in installations:
            if other is own:
                continue
            reject_token(own, other.token())
            mismatch = run(own.exe, "--ConfigPath", other.config, "--CheckConfig", check=False)
            expected = "invalid_configuration: Runtime configuration belongs to a different project release."
            require(mismatch.returncode != 0 and any(line.startswith(expected) for line in mismatch.stdout.splitlines()),
                    "The installed executable did not return the expected cross-release configuration error")
        # Unlike run(), this call must not put the temporary login link in the log.
        link = subprocess.run([str(own.exe), "--SettingsUrl"], env=ENV, capture_output=True,
                              text=True, timeout=8)
        port = own.read_config()["admin"]["port"]
        require(link.returncode == 0 and re.fullmatch(
            rf"http://127\.0\.0\.1:{port}/settings/#login=[a-f0-9]{{64}}", link.stdout.strip()),
            "Default settings command did not select its own version's listener")
    passed("side-by-side", candidate=candidate.version, baseline=baselines[0].version,
           baselines=[{"version": item.version, "legacyLayout": item.legacy} for item in baselines])


def remove_and_purge(installation, passed, primary=True):
    expected = installation.snapshots()
    settings = installation.state / "settings.json"
    if settings.is_file():
        expected[settings] = sha(settings)
    old = installation.pid()
    apt("remove", installation.name)
    require(not installation.exe.exists() and not installation.active()
            and not Path(f"/proc/{old}").exists(), "Removal left service")
    preserved(expected)
    require(installation.db.is_file(), "Removal discarded the version's cache")
    if primary:
        passed("uninstall")
    apt("purge", installation.name)
    require(not installation.config.exists(), "Purge did not remove this version's runtime configuration")
    del expected[installation.config]
    preserved(expected)
    require(installation.db.is_file(), "Purge discarded the version's cache")
    if primary:
        passed("purge")


def cleanup(installation):
    run("journalctl", "--unit", installation.unit, "--no-pager", "-n", "100", check=False)
    if installation.active():
        installation.stop()
    require(installation.pid() == 0, "Acceptance left service running")
    if installation.package_state():
        apt("purge", installation.name)
    require(not installation.package_state() and not installation.exe.exists(), "Acceptance left package installed")
    for directory in (installation.app, *installation.legacy_roots):
        if not directory.exists() and not directory.is_symlink():
            continue
        require(directory.parent in (Path("/opt"), Path("/etc"), Path("/var/lib"))
                and directory.name == installation.name and directory.resolve(strict=True) == directory
                and directory.is_dir() and not directory.is_symlink() and shutil.rmtree.avoids_symlink_attacks,
                f"Refusing to delete unsafe fixture directory: {directory}")
        # The disposable-host preflight required every one of these roots to be absent.
        shutil.rmtree(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Package", type=Path, required=True)
    parser.add_argument("--PackageSHA256", required=True)
    parser.add_argument("--BaselinePackage", action="append", type=Path, default=[])
    parser.add_argument("--BaselineSHA256", action="append", default=[])
    parser.add_argument("--OutputPath", type=Path, required=True)
    parser.add_argument("--DisposableMachine", action="store_true", required=True)
    args = parser.parse_args()
    if len(args.BaselinePackage) != len(args.BaselineSHA256):
        parser.error("Provide a --BaselineSHA256 for each --BaselinePackage")
    candidate = Installation(args.Package.resolve(strict=True), args.PackageSHA256)
    baselines = [Installation(archive.resolve(strict=True), sha256, allow_legacy=True)
                 for archive, sha256 in zip(args.BaselinePackage, args.BaselineSHA256, strict=True)]
    installations = [candidate, *baselines]
    output = args.OutputPath.resolve()
    require(output not in (Path(__file__).resolve(), *(item.archive for item in installations)),
            "Output would overwrite input")
    require(all(not output.is_relative_to(root) for item in installations for root in (item.app, *item.legacy_roots)),
            "Output would share a directory owned by the installation fixture")
    preflight(args, installations)
    report = {"schemaVersion": 1, "status": "failed", "candidateSha256": args.PackageSHA256,
              "baselineSha256": args.BaselineSHA256[0] if baselines else None, "checks": {}}
    if len(baselines) > 1:
        report["baselineSha256s"] = args.BaselineSHA256

    def passed(name, **details):
        report["checks"]["ubuntu26.deb." + name] = {"status": "passed", "exitCode": 0, **details}

    failure = None
    try:
        for baseline in baselines:
            baseline.install()
            health(baseline)
            baseline.stop()
        candidate.install()
        health(candidate)
        passed("install")
        settings = admin_smoke(candidate.exe, candidate.config, ENV, "auto", persist_in=candidate.state)
        passed("admin", provider=settings["provider"])
        check_token_rotation(candidate, passed)
        if baselines:
            check_isolation(candidate, baselines, passed)
            peers = [(item, item.snapshots(), item.pid()) for item in baselines]
            check_reinstall(candidate, passed)
            remove_and_purge(candidate, passed)
            for baseline, snapshot, previous_pid in peers:
                health(baseline)
                require(baseline.pid() == previous_pid, "Candidate removal or reinstall restarted the other service")
                preserved(snapshot)
            passed("uninstall-isolation")
            for baseline in baselines:
                remove_and_purge(baseline, passed, primary=False)
        else:
            remove_and_purge(candidate, passed)
        require(all(sha(item.archive) == item.expected_sha256 for item in installations),
                "Original package changed during acceptance")
        report["status"] = "passed"
    except Exception as error:
        failure = error
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        cleanup_errors = []
        for installation in installations:
            try:
                cleanup(installation)
            except Exception as error:
                cleanup_errors.append(str(error))
                failure = failure or error
        if cleanup_errors:
            report["status"], report["cleanupError"] = "failed", "\n".join(cleanup_errors)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".log").write_text("\n".join(LOG), encoding="utf-8")
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if failure:
        raise SystemExit(str(failure))


if __name__ == "__main__":
    main()
