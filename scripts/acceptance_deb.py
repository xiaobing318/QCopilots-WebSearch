#!/usr/bin/env python3
"""Test unchanged candidate/baseline DEBs on a fresh disposable Ubuntu systemd VM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import socket
import sqlite3
import stat
import subprocess
import time
import urllib.error
import urllib.request

PKG = "qcopilots-websearch"
UNIT = PKG + ".service"
APP, ETC, STATE = Path("/opt") / PKG, Path("/etc") / PKG, Path("/var/lib") / PKG
EXE, CONFIG, TOKEN = APP / "bin" / PKG, ETC / "runtime.json", ETC / "client.token"
DB = STATE / "cache/cache-v1.sqlite"
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


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def metadata(package, field):
    return run("dpkg-deb", "-f", package, field).stdout.strip()


def package_state():
    return run("dpkg-query", "-W", "-f=${Status}\n${Version}", PKG, check=False).stdout.splitlines()


def apt(*args, check=True):
    return run("apt-get", "-y", "-o", "DPkg::Lock::Timeout=60", "-o", "Dpkg::Options::=--force-confold",
               *args, check=check, timeout=600)


def install(package, check=True):
    result = apt("install", "--no-install-recommends", "--allow-downgrades", package, check=check)
    if check:
        require(package_state() == ["install ok installed", metadata(package, "Version")],
                "Package was not configured at the expected version")
    return result


def active():
    return run("systemctl", "is-active", "--quiet", UNIT, check=False).returncode == 0


def enabled():
    return run("systemctl", "is-enabled", UNIT, check=False).stdout.strip()


def pid():
    value = run("systemctl", "show", UNIT, "-p", "MainPID", "--value", check=False).stdout.strip()
    return int(value) if value.isdigit() else 0


def stop():
    old, started = pid(), time.monotonic()
    run("systemctl", "stop", UNIT, timeout=15)
    elapsed = time.monotonic() - started
    require(elapsed <= 10 and not active(), "Stop exceeded ten seconds or stayed active")
    require(not old or not Path(f"/proc/{old}").exists(), "Main process survived stop")
    return elapsed


def health():
    config = json.loads(CONFIG.read_bytes())
    url = f"http://127.0.0.1:{config['control']['port']}/health/live"
    token = TOKEN.read_text(encoding="ascii").strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    deadline = time.monotonic() + 15
    while True:
        try:
            with opener.open(request, timeout=1) as response:
                require(response.status == 200 and json.load(response).get("live") is True,
                        "Unexpected liveness response")
            break
        except (OSError, urllib.error.URLError):
            require(time.monotonic() < deadline, "Installed service did not become healthy")
            time.sleep(0.1)
    try:
        opener.open(url, timeout=1).close()
    except urllib.error.HTTPError as error:
        require(error.code == 401, "Unauthenticated health was not rejected with 401")
    else:
        raise RuntimeError("Health accepted no credential")
    require(active() and pid() > 0, "Health did not come from systemd service")
    info = TOKEN.stat()
    require(not TOKEN.is_symlink() and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == pwd.getpwnam(PKG).pw_uid, "Unsafe credential ownership/mode")
    require(os.readlink(f"/proc/{pid()}/exe") == str(EXE)
            and sha(Path(f"/proc/{pid()}/exe")) == sha(EXE), "Wrong installed executable bytes")
    maps = Path(f"/proc/{pid()}/maps").read_text().splitlines()
    libraries = {line.split(maxsplit=5)[5] for line in maps
                 if len(line.split(maxsplit=5)) == 6 and ".so" in line.split(maxsplit=5)[5]}
    qt = {path for path in libraries if "/libQt6" in path}
    require(qt and all(path.startswith(str(APP / "lib") + "/") for path in qt), "Qt is not private")
    require(all(path.startswith((str(APP) + "/", "/usr/lib/", "/lib/", "/usr/lib64/", "/lib64/"))
                for path in libraries), "A library came from a development or temporary directory")
    require("Qt 6.12.0" in run(EXE, "--Version").stdout, "Wrong installed Qt version")


def configure_dynamic_ports():
    require(not active(), "Initial mask did not prevent startup")
    config, info = json.loads(CONFIG.read_bytes()), CONFIG.stat()
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        for name, sock in (("business", first), ("control", second)):
            port = sock.getsockname()[1]
            config[name].update(host="127.0.0.1", port=port, allowed_hosts=[f"127.0.0.1:{port}"])
        config["cache"]["search_ttl_seconds"] = 321
        temporary = CONFIG.with_name("runtime.json.acceptance-new")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, info.st_uid, info.st_gid)
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.replace(temporary, CONFIG)
    run("systemctl", "unmask", UNIT)
    run("systemctl", "enable", "--now", UNIT)
    health()


def preserved(expected, key, payload):
    require(all(path.is_file() and sha(path) == digest for path, digest in expected.items()),
            "Configuration, token or retained file changed")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as database:
        require(database.execute("SELECT payload FROM entries WHERE key=?", (key,)).fetchone() == (payload,),
                "Logical cached data was lost")


def preflight(args, candidate, baseline):
    require(args.DisposableMachine and os.geteuid() == 0, "Requires root and disposable VM opt-in")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    require(release.get("ID", "").strip('"') == "ubuntu"
            and release.get("VERSION_ID", "").strip('"') == "26.04"
            and os.uname().machine == "x86_64", "Requires Ubuntu 26.04 amd64")
    require(Path("/run/systemd/system").is_dir()
            and Path("/proc/1/comm").read_text().strip() == "systemd", "Requires systemd as PID 1")
    for path in (APP, ETC, STATE, Path("/etc/systemd/system") / UNIT, Path("/usr/lib/systemd/system") / UNIT):
        require(not path.exists() and not path.is_symlink(), "Pre-existing package state refused")
    require(run("dpkg-query", "-W", PKG, check=False).returncode != 0, "Pre-existing dpkg state refused")
    require(not run("dpkg", "--audit").stdout.strip(), "Another package needs repair")
    try:
        pwd.getpwnam(PKG)
    except KeyError:
        pass
    else:
        raise RuntimeError("Pre-existing service account refused")
    for package, digest in ((candidate, args.PackageSHA256), (baseline, args.BaselineSHA256)):
        require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "Invalid expected SHA-256")
        require(package.is_file() and sha(package) == digest, "Package SHA-256 mismatch")
        require(metadata(package, "Package") == PKG and metadata(package, "Architecture") == "amd64",
                "Wrong package identity")
    run("dpkg", "--compare-versions", metadata(baseline, "Version"), "lt", metadata(candidate, "Version"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("Package", "BaselinePackage", "OutputPath"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("PackageSHA256", "BaselineSHA256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--DisposableMachine", action="store_true", required=True)
    args = parser.parse_args()
    candidate, baseline = args.Package.resolve(strict=True), args.BaselinePackage.resolve(strict=True)
    output = args.OutputPath.resolve()
    require(output not in (candidate, baseline, Path(__file__).resolve()), "Output would overwrite input")
    preflight(args, candidate, baseline)
    report = {"schemaVersion": 1, "status": "failed", "candidateSha256": args.PackageSHA256,
              "baselineSha256": args.BaselineSHA256, "checks": {}}

    def passed(name, **details):
        report["checks"]["ubuntu26.deb." + name] = {"status": "passed", "exitCode": 0, **details}

    failure = None
    try:
        run("systemctl", "mask", UNIT)
        install(candidate)
        configure_dynamic_ports()
        passed("install", initialStart="masked until dynamic ports were configured")
        elapsed = stop()
        sentinel, key = STATE / "acceptance-retained-data", os.urandom(32).hex()
        payload = json.dumps({"acceptance": key}).encode()
        sentinel.write_bytes(os.urandom(48))
        with sqlite3.connect(DB) as database:
            database.execute("INSERT INTO entries VALUES(?,?,?,?,?)",
                             (key, payload, len(payload), int(time.time()) + 86400, 0))
        expected = {path: sha(path) for path in (CONFIG, TOKEN, sentinel)}
        run("systemctl", "start", UNIT)
        health()
        passed("stop-start", stopSeconds=elapsed)
        install(baseline)
        health()
        install(candidate)
        health()
        preserved(expected, key, payload)
        passed("upgrade")
        install(baseline)
        health()
        stop()
        backup = TOKEN.with_name("client.token.acceptance-original")
        TOKEN.rename(backup)
        try:
            TOKEN.mkdir(mode=0o700)
            rejected = install(candidate, check=False)
            require(rejected.returncode != 0 and "Credential is not a regular file" in rejected.stdout,
                    "Intended postinst failure was not observed")
            require(package_state() == ["install ok half-configured", metadata(candidate, "Version")],
                    "Failed postinst did not leave expected dpkg state")
        finally:
            if TOKEN.is_dir() and not TOKEN.is_symlink():
                TOKEN.rmdir()
            require(not TOKEN.exists() and not TOKEN.is_symlink(), "Cannot safely restore token")
            backup.rename(TOKEN)
        install(baseline)
        health()
        preserved(expected, key, payload)
        passed("rollback", mode="original baseline reinstall after failed candidate postinst",
               failedInstallExitCode=rejected.returncode)
        run("systemctl", "disable", UNIT)
        require(active() and enabled() == "disabled", "Disabled-active setup failed")
        install(candidate)
        health()
        require(enabled() == "disabled", "Upgrade re-enabled disabled-active service")
        passed("disabled-active")
        stop()
        install(baseline)
        install(candidate)
        require(not active() and enabled() == "disabled", "Upgrade changed disabled-inactive state")
        preserved(expected, key, payload)
        passed("disabled-inactive")
        run("systemctl", "enable", "--now", UNIT)
        health()
        old = pid()
        apt("remove", PKG)
        require(not EXE.exists() and not active() and not Path(f"/proc/{old}").exists(), "Removal left service")
        preserved(expected, key, payload)
        passed("uninstall")
        apt("purge", PKG)
        require(not CONFIG.exists(), "Purge did not remove conffile")
        preserved({TOKEN: expected[TOKEN], sentinel: expected[sentinel]}, key, payload)
        passed("purge")
        require(sha(candidate) == args.PackageSHA256 and sha(baseline) == args.BaselineSHA256,
                "Original package changed during acceptance")
        report["status"] = "passed"
    except Exception as error:
        failure = error
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            run("systemctl", "stop", UNIT, check=False, timeout=15)
            if pid():
                run("systemctl", "kill", "--signal=SIGKILL", UNIT, check=False)
            require(pid() == 0, "Acceptance left service running")
            run("systemctl", "unmask", UNIT, check=False)
            run("journalctl", "--unit", UNIT, "--no-pager", "-n", "100", check=False)
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
