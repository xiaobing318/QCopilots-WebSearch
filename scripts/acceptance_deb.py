#!/usr/bin/env python3
"""Test release-specific DEB installations on a fresh disposable Ubuntu systemd VM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import subprocess
import time
import urllib.error
import urllib.request

from testkit import admin_smoke, digest as sha

APP_NAME = "qcopilots-websearch"
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


class Installation:
    def __init__(self, archive, expected_sha256):
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
        self.etc = Path("/etc") / self.name
        self.state = Path("/var/lib") / self.name
        self.exe = self.app / "bin" / APP_NAME
        self.config = self.etc / "runtime.json"
        self.tokens = (self.etc / "client.token", self.etc / "admin.token")
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
    if len(installations) == 2:
        candidate, baseline = installations
        run("dpkg", "--compare-versions", baseline.version, "lt", candidate.version)
    for installation in installations:
        unit_paths = [Path(root) / installation.unit for root in ("/etc/systemd/system", "/usr/lib/systemd/system")]
        for path in (installation.app, installation.etc, installation.state, *unit_paths):
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


def check_isolation(candidate, baseline, passed):
    baseline.install()
    health(baseline)
    health(candidate)
    require(candidate.pid() != baseline.pid()
            and pwd.getpwnam(candidate.account).pw_uid != pwd.getpwnam(baseline.account).pw_uid,
            "Installed versions share a process or account")
    require(not candidate.check_ports().intersection(baseline.check_ports()), "Versions share a listener port")
    for own, other in ((candidate, baseline), (baseline, candidate)):
        reject_token(own, other.token())
        mismatch = run(own.exe, "--ConfigPath", other.config, "--CheckConfig", check=False)
        require(mismatch.returncode != 0 and "version" in mismatch.stdout.lower(),
                "An installed binary accepted another version's configuration")
        # Unlike run(), this call must not put the temporary login link in the log.
        link = subprocess.run([str(own.exe), "--SettingsUrl"], env=ENV, capture_output=True,
                              text=True, timeout=8)
        port = own.read_config()["admin"]["port"]
        require(link.returncode == 0 and re.fullmatch(
            rf"http://127\.0\.0\.1:{port}/settings/#login=[a-f0-9]{{64}}", link.stdout.strip()),
            "Default settings command did not select its own version's listener")
    passed("side-by-side", candidate=candidate.version, baseline=baseline.version)


def remove_and_purge(installation, passed, primary=True):
    expected = installation.snapshots()
    old = installation.pid()
    apt("remove", installation.name)
    require(not installation.exe.exists() and not installation.active()
            and not Path(f"/proc/{old}").exists(), "Removal left service")
    preserved(expected)
    if primary:
        passed("uninstall")
    apt("purge", installation.name)
    require(not installation.config.exists(), "Purge did not remove this version's runtime configuration")
    del expected[installation.config]
    preserved(expected)
    if primary:
        passed("purge")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Package", type=Path, required=True)
    parser.add_argument("--PackageSHA256", required=True)
    parser.add_argument("--BaselinePackage", type=Path)
    parser.add_argument("--BaselineSHA256")
    parser.add_argument("--OutputPath", type=Path, required=True)
    parser.add_argument("--DisposableMachine", action="store_true", required=True)
    args = parser.parse_args()
    if bool(args.BaselinePackage) != bool(args.BaselineSHA256):
        parser.error("Provide both --BaselinePackage and --BaselineSHA256 for side-by-side checks")
    candidate = Installation(args.Package.resolve(strict=True), args.PackageSHA256)
    installations = [candidate]
    if args.BaselinePackage:
        installations.append(Installation(args.BaselinePackage.resolve(strict=True), args.BaselineSHA256))
    output = args.OutputPath.resolve()
    require(output not in (Path(__file__).resolve(), *(item.archive for item in installations)),
            "Output would overwrite input")
    preflight(args, installations)
    report = {"schemaVersion": 1, "status": "failed", "candidateSha256": args.PackageSHA256,
              "baselineSha256": args.BaselineSHA256, "checks": {}}

    def passed(name, **details):
        report["checks"]["ubuntu26.deb." + name] = {"status": "passed", "exitCode": 0, **details}

    failure = None
    try:
        candidate.install()
        health(candidate)
        passed("install")
        settings = admin_smoke(candidate.exe, candidate.config, ENV, "auto", persist_in=candidate.state)
        passed("admin", provider=settings["provider"])
        if len(installations) == 2:
            baseline = installations[1]
            check_isolation(candidate, baseline, passed)
            baseline_snapshot, previous_pid = baseline.snapshots(), baseline.pid()
            check_reinstall(candidate, passed)
            remove_and_purge(candidate, passed)
            health(baseline)
            require(baseline.pid() == previous_pid, "Candidate removal or reinstall restarted the other service")
            preserved(baseline_snapshot)
            passed("uninstall-isolation")
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
        try:
            for installation in installations:
                run("journalctl", "--unit", installation.unit, "--no-pager", "-n", "100", check=False)
                if installation.active():
                    installation.stop()
                require(installation.pid() == 0, "Acceptance left service running")
                if installation.package_state():
                    apt("purge", installation.name)
        except Exception as error:
            report["status"], report["cleanupError"] = "failed", str(error)
            failure = failure or error
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".log").write_text("\n".join(LOG), encoding="utf-8")
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if failure:
        raise SystemExit(str(failure))


if __name__ == "__main__":
    main()
