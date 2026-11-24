#!/usr/bin/env python3
"""Test a candidate DEB on a fresh disposable Ubuntu systemd runner."""
import argparse
from contextlib import closing, contextmanager
import fcntl
import grp
import hashlib
import http.client
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
import tempfile
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


class Installation:
    def __init__(self, archive, expected_sha256):
        require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None, "Invalid expected SHA-256")
        require(archive.is_file() and sha(archive) == expected_sha256, "Package SHA-256 mismatch")
        self.archive = archive
        self.expected_sha256 = expected_sha256
        self.version = metadata(archive, "Version")
        require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version) is not None, "Wrong version format")
        self.name = f"{APP_NAME}-{self.version}"
        require(archive.name == f"{self.name}_amd64.deb", "Unexpected DEB filename")
        require(metadata(archive, "Package") == self.name and metadata(archive, "Architecture") == "amd64",
                "Wrong package identity")
        self.unit = self.name + ".service"
        self.account = "qcws-" + hashlib.sha256(self.version.encode("ascii")).hexdigest()[:24]
        self.app = Path("/opt") / self.name
        self.exe = self.app / "bin" / APP_NAME
        self.data = self.exe.parent / "data"
        self.state = self.data / "state"
        self.config = self.data / "runtime.json"
        self.tokens = (self.data / "client.token", self.data / "admin.token")
        self.db = self.state / "cache/cache-v1.sqlite"
        self.installed_ids = None

    def service_ids(self):
        entry = pwd.getpwnam(self.account)
        require(entry.pw_uid > 0 and entry.pw_gid > 0 and entry.pw_dir == str(self.state)
                and grp.getgrnam(self.account).gr_gid == entry.pw_gid,
                "Unexpected service account, group or home directory")
        return entry.pw_uid, entry.pw_gid

    def package_state(self):
        result = run("dpkg-query", "-W", "-f=${Status}\\n${Version}", self.name, check=False)
        return result.stdout.splitlines() if result.returncode == 0 else []

    def install(self, reinstall=False):
        run("systemctl", "reset-failed", self.unit, check=False)
        options = ["--reinstall"] if reinstall else []
        result = apt("install", "--no-install-recommends", *options, self.archive, check=False)
        if self.installed_ids is None:
            try:
                self.installed_ids = self.service_ids()
            except KeyError:
                require(result.returncode != 0, "Configured package did not create the service account and group")
        require(result.returncode == 0, "Package installation failed")
        require(self.package_state() == ["install ok installed", self.version],
                "Package was not configured at the expected version")
        require(self.service_ids() == self.installed_ids, "Installation changed the service account identity")
        unit = (Path("/usr/lib/systemd/system") / self.unit).read_text()
        require([line for line in unit.splitlines() if line.startswith("ExecStart=")]
                == [f"ExecStart={self.exe} --ConfigPath {self.config}"],
                "The installed unit uses an unexpected runtime path")

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
        paths = [self.config, *self.tokens]
        registry = self.state / "client-registry.json"
        if registry.is_file():
            paths.append(registry)
        return {path: sha(path) for path in paths}

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
    read_write = run("systemctl", "show", installation.unit, "-p", "ReadWritePaths", "--value").stdout.split()
    require(read_write == [str(installation.state)], "Service must write only to its own state directory")
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


@contextmanager
def fixture_lock(path):
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | flags)
    try:
        parent = os.fstat(directory)
        require(parent.st_uid == os.geteuid() and not parent.st_mode & 0o022, "Unsafe fixture lock directory")
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | flags, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077, "Unsafe fixture lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BlockingIOError("Another DEB acceptance process holds the host lock") from exc
        yield
    finally:
        # Keep the inode in /run so every process locks the same file.
        os.close(fd)


def preflight(installation):
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    require(release.get("ID", "").strip('"') == "ubuntu"
            and release.get("VERSION_ID", "").strip('"') == "26.04"
            and os.uname().machine == "x86_64", "Requires Ubuntu 26.04 amd64")
    require(Path("/run/systemd/system").is_dir()
            and Path("/proc/1/comm").read_text().strip() == "systemd", "Requires systemd as PID 1")
    require(not run("dpkg", "--audit").stdout.strip(), "Another package needs repair")
    for path in (installation.app, Path("/etc/systemd/system") / installation.unit,
                 Path("/usr/lib/systemd/system") / installation.unit):
        require(not path.exists() and not path.is_symlink(), "Pre-existing package state refused")
    require(not installation.package_state(), "Pre-existing dpkg state refused")
    require(run("systemctl", "show", installation.unit, "-p", "LoadState", "--value", check=False)
            .stdout.strip() == "not-found", "Pre-existing systemd unit refused")
    for lookup in (pwd.getpwnam, grp.getgrnam):
        try:
            lookup(installation.account)
        except KeyError:
            continue
        raise RuntimeError("Pre-existing service account or group refused")


def check_cached_entry(installation, entry):
    key, payload = entry
    with closing(sqlite3.connect(f"file:{installation.db}?mode=ro", uri=True)) as database:
        require(database.execute("SELECT payload FROM entries WHERE key=?", (key,)).fetchone() == (payload,),
                "The package operation changed retained cache data")


def check_reinstall(installation, saved_key_state, passed):
    expected = installation.snapshots()
    settings = installation.state / "settings.json"
    expected[settings] = sha(settings)
    elapsed = installation.stop()
    key = os.urandom(32).hex()
    payload = json.dumps({"acceptance": key}).encode()
    with closing(sqlite3.connect(installation.db)) as database:
        database.execute("INSERT INTO entries VALUES(?,?,?,?,?)",
                         (key, payload, len(payload), int(time.time()) + 86400, 0))
        database.commit()
    run("systemctl", "start", installation.unit)
    health(installation)
    passed("stop-start", stopSeconds=elapsed)
    installation.install(reinstall=True)
    health(installation)
    preserved(expected)
    check_cached_entry(installation, (key, payload))
    require(provider_key_snapshot(installation) == saved_key_state, "Reinstall changed provider key data or permissions")
    with admin_session(installation, []) as (exchange, cookie, _):
        _, settings = exchange("GET", "/settings", headers=cookie)
        require(settings["providers"]["brave"] == {"key_set": True}, "Reinstalled service lost its provider key")
    passed("same-version-reinstall", providerKeyRetained=True)
    return key, payload


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


def verify_mcp(port, token, package_version):
    status, session, data = mcp_request(port, token, INITIALIZE)
    require(status == 200 and session and data["result"]["protocolVersion"] == PROTOCOL
            and data["result"]["serverInfo"]["version"] == package_version,
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


def check_stdio_accounts(installation, passed):
    service = pwd.getpwnam(installation.account)
    ordinary = pwd.getpwnam("nobody")
    require(ordinary.pw_uid not in (0, service.pw_uid) and ordinary.pw_gid != service.pw_gid,
            "The acceptance client must be unprivileged and outside the service group")
    version = "2024-11-05"
    initialize = {**INITIALIZE, "params": {**INITIALIZE["params"], "protocolVersion": version}}
    messages = [initialize, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    request = "".join(json.dumps(message) + "\n" for message in messages)

    def probe(account, *options, denied=False):
        # Drop supplementary groups as well as uid and gid before opening any
        # configuration. No service credentials or inherited deployment state are supplied.
        result = subprocess.run([installation.exe, "--Stdio", *options], user=account.pw_uid,
                                group=account.pw_gid, extra_groups=(), cwd="/tmp",
                                env={"PATH": ENV["PATH"], "LC_ALL": "C", "HOME": account.pw_dir},
                                input=request, capture_output=True, text=True, encoding="utf-8", timeout=15)
        if denied:
            require(result.returncode != 0 and not result.stdout and "invalid_configuration:" in result.stderr,
                    "An ordinary user must receive a configuration error on the private service directory")
            return
        require(result.returncode == 0, "Installed stdio process failed for its configuration owner")
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        require(len(replies) == 2 and replies[0].get("id") == 1 and replies[1].get("id") == 2
                and replies[0]["result"]["protocolVersion"] == version
                and replies[0]["result"]["serverInfo"]["version"] == installation.version
                and {tool["name"] for tool in replies[1]["result"]["tools"]}
                == {"web_search", "web_open", "web_find", "web_search_status"},
                "Installed stdio process did not negotiate the expected version and four tools")

    probe(service)
    probe(ordinary, denied=True)
    # Use the shipped public template so the low-privilege fixture never
    # receives material from the real service's private runtime directory.
    template = installation.app / "share/qcopilots-websearch/config/runtime.json"
    with tempfile.TemporaryDirectory(prefix="qcws-stdio-user-", dir="/tmp") as directory:
        root = Path(directory)
        runtime = root / "runtime.json"
        runtime.write_bytes(template.read_bytes())
        runtime.chmod(0o600)
        os.chown(runtime, ordinary.pw_uid, ordinary.pw_gid)
        os.chown(root, ordinary.pw_uid, ordinary.pw_gid)
        probe(ordinary, "--ConfigPath", runtime)
    passed("stdio-accounts", protocol=version)


def check_invalid_startup(installation, passed):
    account = pwd.getpwnam(installation.account)
    for name in ("settings.json", "secrets.json"):
        installation.stop()
        target = installation.state / name
        # Retaining the original inode preserves its ownership, mode and ACL.
        with tempfile.TemporaryDirectory(prefix="startup-", dir=installation.state) as directory:
            scratch = Path(directory)
            backup = scratch / name
            invalid = scratch / "malformed"
            invalid.write_bytes(b"{")
            invalid.chmod(0o600)
            os.chown(invalid, account.pw_uid, account.pw_gid)
            exists = target.exists()
            if exists:
                target.rename(backup)
            try:
                os.replace(invalid, target)
                result = run("systemctl", "start", installation.unit, check=False)
                require(result.returncode != 0 and not installation.active() and installation.pid() == 0,
                        f"Installed service accepted malformed {name}")
            finally:
                # Cancel any queued automatic restart before restoring the file.
                run("systemctl", "stop", installation.unit, check=False)
                if exists:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
        run("systemctl", "reset-failed", installation.unit, check=False)
        run("systemctl", "start", installation.unit)
        health(installation)
    passed("invalid-startup-recovery")


def check_token_rotation(installation, passed):
    port = installation.read_config()["business"]["port"]
    unchanged_files = installation.snapshots()
    runtime_access = access_rules(installation.config)
    client = "deb-acceptance"
    args = [installation.exe, "--ConfigPath", installation.config, "--ClientId", client]
    # Credential output is captured here and is intentionally excluded from run() diagnostics.
    generated = subprocess.run([*args, "--GenerateToken"], input="yes\n", capture_output=True,
                               text=True, encoding="utf-8", env=ENV, timeout=10)
    token = generated.stdout.strip()
    require(generated.returncode == 0 and re.fullmatch(r"[a-f0-9]{64}", token) and token not in generated.stderr,
            "Installed CLI did not generate a client credential")
    check_registry_ownership(installation)
    require(mcp_request(port, token, INITIALIZE)[0] == 401, "Generated token became active without restart")
    run("systemctl", "restart", installation.unit)
    health(installation)
    verify_mcp(port, token, installation.version)

    replacement = secrets.token_hex(32)
    rotated = subprocess.run([*args, "--SetToken"], input=replacement + "\nyes\n", capture_output=True,
                             text=True, encoding="utf-8", env=ENV, timeout=10)
    require(rotated.returncode == 0 and not rotated.stdout and replacement not in rotated.stderr,
            "Installed CLI did not rotate its client credential safely")
    check_registry_ownership(installation)
    require(mcp_request(port, replacement, INITIALIZE)[0] == 401, "Rotated token became active without restart")
    run("systemctl", "restart", installation.unit)
    health(installation)
    require(mcp_request(port, token, INITIALIZE)[0] == 401, "Previous token still works after rotation")
    verify_mcp(port, replacement, installation.version)

    check_stored_client(installation, client, replacement, retired=(token,))
    preserved(unchanged_files)
    require(access_rules(installation.config) == runtime_access,
            "Client rotation changed runtime ownership, permissions or ACL")
    passed("client-token-rotation")


def check_registry_ownership(installation):
    registry = installation.state / "client-registry.json"
    account = pwd.getpwnam(installation.account)
    info = registry.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == account.pw_uid and info.st_gid == account.pw_gid,
            "The client registry must remain private and owned by the service account")


def access_rules(path):
    info = path.stat()
    attribute = "system.posix_acl_access"
    acl = os.getxattr(path, attribute) if attribute in os.listxattr(path) else None
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), acl


def check_stored_client(installation, client, token, *, retired=()):
    check_registry_ownership(installation)
    raw = (installation.state / "client-registry.json").read_bytes()
    entries = [entry for entry in json.loads(raw)["clients"] if entry["id"] == client]
    require(len(entries) == 1 and entries[0].get("token_value") == token
            and entries[0]["token_sha256"] == hashlib.sha256(token.encode("ascii")).hexdigest()
            and re.fullmatch(r"[a-f0-9]{32}", entries[0].get("generation", "")),
            "The private client registry did not retain matching credential, digest and identity")
    require(all(value.encode("ascii") not in raw for value in retired),
            "The private client registry retained a rotated credential")
    runtime = installation.config.read_bytes()
    require(all(value.encode("ascii") not in runtime for value in (token, *retired)),
            "A managed client credential leaked into runtime configuration")


def leave_crashed_cli_lock(installation):
    """Hold a root CLI transaction on a fixture FIFO, then verify its recoverable lock."""
    token = installation.tokens[0]
    backup = token.with_name("client.token.registry-test-backup")
    lock = installation.state / "client-registry.json.lock"
    account = pwd.getpwnam(installation.account)
    require(token.is_file() and not token.is_symlink() and not backup.exists() and not lock.exists(),
            "The CLI crash fixture requires untouched private installation files")
    process = None
    os.replace(token, backup)
    try:
        os.mkfifo(token, 0o600)
        os.chown(token, account.pw_uid, account.pw_gid)
        # The inherited file credential is resolved inside the locked transaction.
        # A FIFO with no writer holds that read without a production test hook.
        process = subprocess.Popen(
            [str(installation.exe), "--ConfigPath", str(installation.config), "--ClientId", "deb-crash-fixture", "--SetToken"],
            env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        process.stdin.write("crash/" + secrets.token_hex(6) + "\nyes\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            require(process.poll() is None, "The CLI crash fixture exited before holding its transaction")
            if lock.is_file():
                info = lock.lstat()
                if info.st_uid == account.pw_uid and info.st_gid == account.pw_gid and stat.S_IMODE(info.st_mode) == 0o600:
                    break
            time.sleep(0.02)
        else:
            raise RuntimeError("The root CLI lock did not acquire private service-account ownership")
        process.kill()
        process.communicate(timeout=5)
        require(lock.is_file(), "The crash fixture did not retain its transaction lock")
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        if token.exists():
            require(stat.S_ISFIFO(token.lstat().st_mode), "The crash fixture token path changed unexpectedly")
            token.unlink()
        os.replace(backup, token)


@contextmanager
def admin_session(installation, credentials):
    port = installation.read_config()["admin"]["port"]
    origin = f"http://127.0.0.1:{port}"
    base = "/admin/api/v1"
    admin_token = installation.tokens[1].read_text(encoding="ascii").strip()
    credentials.extend((installation.token(), admin_token))

    def exchange(method, path, body=None, headers=None, *, expected_status=200):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, base + path, json.dumps(body) if body is not None else None, headers or {})
            response = connection.getresponse()
            data = response.read(MIB + 1)
            require(len(data) <= MIB, "Installed client administration response exceeded its limit")
            require(response.status == expected_status, "Installed client administration returned an unexpected status")
            if path == "/clients/reveal":
                require(response.headers.get("Cache-Control") == "no-store",
                        "Installed credential reveal response could be cached")
            if path != "/clients/reveal" or expected_status != 200:
                require(all(token.encode("ascii") not in data for token in credentials),
                        "Installed administration disclosed a credential outside successful reveal")
            return response.headers, json.loads(data)
        finally:
            connection.close()

    login = {"Origin": origin, "Content-Type": "application/json"}
    headers, session = exchange("POST", "/session",
                                {"token": admin_token}, login)
    cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
    write = login | cookie | {"X-QCWS-CSRF": session["csrf"]}
    try:
        yield exchange, cookie, write
    finally:
        exchange("DELETE", "/session", {}, write)


def provider_key_snapshot(installation):
    path = installation.state / "secrets.json"
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and (info.st_uid, info.st_gid) == installation.installed_ids
            and stat.S_IMODE(info.st_mode) == 0o600, "Unsafe provider key storage")
    return sha(path), access_rules(path)


def check_client_registry(installation, passed):
    """Exercise writes through the actual sandboxed service after root CLI updates."""
    runtime = installation.read_config()
    runtime_hash = sha(installation.config)
    runtime_access = access_rules(installation.config)
    previous_pid = installation.pid()
    port = runtime["admin"]["port"]
    origin = f"http://127.0.0.1:{port}"
    # Capture the one-time login link without adding it to the acceptance log.
    link = subprocess.run([installation.exe, "--SettingsUrl"], env=ENV, capture_output=True, text=True, timeout=8)
    require(link.returncode == 0 and re.fullmatch(re.escape(origin) + r"/settings/#login=[a-f0-9]{64}",
                                                  link.stdout.strip()), "Wrong default settings URL")
    credentials = []

    def check_view(view):
        require(set(view) == {"revision", "clients", "pending_restart"}
                and all(set(client) == {"id", "source", "can_reveal"}
                        and isinstance(client["can_reveal"], bool) for client in view["clients"]),
                "Installed client list exposed credential or internal identity fields")

    def reveal(client, token, revision):
        _, shown = exchange("POST", "/clients/reveal", {"id": client, "revision": revision}, write)
        require(shown == {"id": client, "token": token, "revision": revision, "active": True},
                "Installed credential reveal did not return the active selected credential")
        _, view = exchange("GET", "/clients", headers=cookie)
        check_view(view)
        require(view["revision"] == revision, "Viewing a credential changed the client registry")

    with admin_session(installation, credentials) as (exchange, cookie, write):
        _, view = exchange("GET", "/clients", headers=cookie)
        check_view(view)
        require(any(client["id"] == "local" for client in view["clients"]),
                "The initial local identity is missing from administration")
        stale_revision = view["revision"]
        bearer = "web/" + secrets.token_hex(6)
        credentials.append(bearer)
        _, view = exchange("POST", "/clients", {"revision": view["revision"], "id": "deb-web-acceptance",
                                                "token": bearer}, write)
        check_view(view)
        require(not view["pending_restart"] and bearer not in json.dumps(view),
                "Web enrollment was pending or disclosed its credential")
        check_stored_client(installation, "deb-web-acceptance", bearer)
        require(not (installation.state / "client-registry.json.lock").exists(),
                "The service could not recover a crashed root CLI transaction")
        body = {"id": "deb-web-acceptance", "revision": view["revision"]}
        exchange("POST", "/clients/reveal", body, cookie | {"Origin": origin, "Content-Type": "application/json"},
                 expected_status=403)
        _, error = exchange("POST", "/clients/reveal", body | {"revision": stale_revision},
                            write, expected_status=409)
        require(error["error"]["code"] == "revision_conflict", "Credential reveal ignored a stale revision")
        reveal("deb-web-acceptance", bearer, view["revision"])
        business = runtime["business"]["port"]
        verify_mcp(business, bearer, installation.version)
        replacement = "web/" + secrets.token_hex(6)
        credentials.append(replacement)
        _, view = exchange("PUT", "/clients/deb-web-acceptance",
                           {"revision": view["revision"], "token": replacement}, write)
        check_view(view)
        require(not view["pending_restart"] and mcp_request(business, bearer, INITIALIZE)[0] == 401,
                "Web rotation did not immediately revoke the previous credential")
        check_stored_client(installation, "deb-web-acceptance", replacement, retired=(bearer,))
        reveal("deb-web-acceptance", replacement, view["revision"])
        verify_mcp(business, replacement, installation.version)
        _, view = exchange("DELETE", "/clients/deb-web-acceptance", {"revision": view["revision"]}, write)
        check_view(view)
        require(not view["pending_restart"] and mcp_request(business, replacement, INITIALIZE)[0] == 401,
                "Web deletion did not immediately revoke its credential")
        _, error = exchange("POST", "/clients/reveal", {"id": "deb-web-acceptance", "revision": view["revision"]},
                            write, expected_status=404)
        require(error["error"].get("reason") == "client_not_found", "Deleted client credential remained viewable")
        check_registry_ownership(installation)
        raw = (installation.state / "client-registry.json").read_bytes()
        require(all(token.encode("ascii") not in raw for token in (bearer, replacement)),
                "Deleted client credentials remained in the private registry")
        require(installation.pid() == previous_pid and sha(installation.config) == runtime_hash
                and access_rules(installation.config) == runtime_access,
                "Web client changes restarted the service or changed deployment configuration or ACL")
        provider_key = secrets.token_hex(32)
        credentials.append(provider_key)
        _, settings = exchange("PUT", "/providers/brave/key", {"key": provider_key}, write)
        require(settings["providers"]["brave"] == {"key_set": True}
                and json.loads((installation.state / "secrets.json").read_bytes())["keys"]["brave"] == provider_key,
                "The installed service did not persist the supplied provider key")
        require(all(token not in "\n".join(LOG) for token in credentials),
                "Installed client administration exposed a credential in acceptance diagnostics")
    passed("client-registry-service-writes")
    return provider_key_snapshot(installation)


def remove_and_purge(installation, cache_entry, saved_key_state, passed):
    expected = installation.snapshots()
    settings = installation.state / "settings.json"
    expected[settings] = sha(settings)

    def check_retained():
        require(provider_key_snapshot(installation) == saved_key_state, "Package removal changed protected provider keys")
        preserved(expected)
        check_cached_entry(installation, cache_entry)
        require(installation.service_ids() == installation.installed_ids
                and installation.state.stat().st_uid == installation.installed_ids[0],
                "The retained service account or data owner changed")

    old = installation.pid()
    apt("remove", installation.name)
    require(installation.package_state() == ["deinstall ok config-files", installation.version]
            and not installation.exe.exists() and not installation.active()
            and not Path(f"/proc/{old}").exists(), "Removal left service")
    check_retained()
    passed("uninstall", providerKeyRetained=True)
    apt("purge", installation.name)
    require(not installation.package_state() and not installation.config.exists(),
            "Purge did not remove the package and its runtime configuration")
    del expected[installation.config]
    check_retained()
    passed("purge", providerKeyRetained=True)


def cleanup(installation):
    run("journalctl", "--unit", installation.unit, "--no-pager", "-n", "100", check=False)
    if installation.active():
        installation.stop()
    require(installation.pid() == 0, "Acceptance left service running")
    if installation.package_state():
        apt("purge", installation.name)
    require(not installation.package_state() and not installation.exe.exists(), "Acceptance left package installed")
    directory = installation.app
    if directory.exists() or directory.is_symlink():
        require(directory == Path("/opt") / installation.name and directory.resolve(strict=True) == directory
                and directory.is_dir() and not directory.is_symlink() and shutil.rmtree.avoids_symlink_attacks,
                f"Refusing to delete unsafe fixture directory: {directory}")
        shutil.rmtree(directory)
    # The preflight refused any existing user or group. Remove only the
    # identity we actually observed during installation on this disposable host.
    if installation.installed_ids is not None:
        require(installation.service_ids() == installation.installed_ids,
                "Refusing cleanup of a changed service identity")
        run("userdel", installation.account)
        try:
            group = grp.getgrnam(installation.account)
        except KeyError:
            return
        require(group.gr_gid == installation.installed_ids[1] and not group.gr_mem,
                "Refusing cleanup of a changed service group")
        run("groupdel", installation.account)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Package", type=Path, required=True)
    parser.add_argument("--PackageSHA256", required=True)
    parser.add_argument("--OutputPath", type=Path, required=True)
    parser.add_argument("--DisposableMachine", action="store_true", required=True)
    args = parser.parse_args()
    require(args.DisposableMachine and os.geteuid() == 0, "Requires root and disposable VM opt-in")
    with fixture_lock(Path("/run/qcopilots-websearch-deb-acceptance.lock")):
        accept_package(args)


def accept_package(args):
    candidate = Installation(args.Package.resolve(strict=True), args.PackageSHA256)
    output = args.OutputPath.resolve()
    require(output not in (Path(__file__).resolve(), candidate.archive), "Output would overwrite input")
    require(not output.is_relative_to(candidate.app), "Output would share a directory owned by the fixture")
    preflight(candidate)
    report = {"schemaVersion": 1, "status": "failed", "candidateSha256": args.PackageSHA256, "checks": {}}

    def passed(name, **details):
        report["checks"]["ubuntu26.deb." + name] = {"status": "passed", "exitCode": 0, **details}

    failure = None
    installation_attempted = False
    opt_fd = None
    original_opt_mode = None
    try:
        # The disposable hosted image opens /opt to tooling installations. The
        # package itself requires protected ancestry, which this fixture preserves.
        opt_fd = os.open("/opt", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opt_info = os.fstat(opt_fd)
        require(opt_info.st_uid == 0, "Disposable /opt must be root-owned")
        if stat.S_IMODE(opt_info.st_mode) & 0o022:
            original_opt_mode = stat.S_IMODE(opt_info.st_mode)
            os.fchmod(opt_fd, original_opt_mode & ~0o022)
            LOG.append(f"Protected disposable /opt (original mode {original_opt_mode:o})")
        installation_attempted = True
        candidate.install()
        health(candidate)
        passed("install")
        check_stdio_accounts(candidate, passed)
        settings = admin_smoke(candidate.exe, candidate.config, ENV, "auto", persist_in=candidate.state)
        passed("admin", provider=settings["provider"])
        check_invalid_startup(candidate, passed)
        check_token_rotation(candidate, passed)
        leave_crashed_cli_lock(candidate)
        saved_key_state = check_client_registry(candidate, passed)
        cache_entry = check_reinstall(candidate, saved_key_state, passed)
        remove_and_purge(candidate, cache_entry, saved_key_state, passed)
        require(sha(candidate.archive) == candidate.expected_sha256, "Original package changed during acceptance")
        report["status"] = "passed"
    except Exception as error:
        failure = error
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        cleanup_errors = []
        if installation_attempted:
            try:
                cleanup(candidate)
            except Exception as error:
                cleanup_errors.append(str(error))
                failure = failure or error
        if opt_fd is not None:
            try:
                if original_opt_mode is not None:
                    os.fchmod(opt_fd, original_opt_mode)
                    require(stat.S_IMODE(os.fstat(opt_fd).st_mode) == original_opt_mode, "Could not restore /opt mode")
                    LOG.append(f"Restored disposable /opt mode {original_opt_mode:o}")
            except Exception as error:
                cleanup_errors.append(str(error))
                failure = failure or error
            finally:
                os.close(opt_fd)
        if cleanup_errors:
            report["status"], report["cleanupError"] = "failed", "\n".join(cleanup_errors)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".log").write_text("\n".join(LOG), encoding="utf-8")
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if failure:
        raise SystemExit(str(failure))


if __name__ == "__main__":
    main()
