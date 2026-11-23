"""Verify real startup failures without using an existing deployment or its ports."""
import argparse
import asyncio
from contextlib import closing, contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

if os.name == "nt":
    import _winapi

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from testkit import BREAK_HELPER, hidden_console


MIB = 1024 * 1024
DEFAULT_PORTS = {3181, 3182, 3183}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def cli_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def available_ports():
    reservations = []
    try:
        while len(reservations) < 3:
            listener = socket.socket()
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
            if listener.getsockname()[1] in DEFAULT_PORTS:
                listener.close()
            else:
                reservations.append(listener)
        return [listener.getsockname()[1] for listener in reservations]
    finally:
        for listener in reservations:
            listener.close()


class Fixture:
    def __init__(self, executable, root):
        self.root = root.resolve()
        self.root.mkdir()
        self.runtime = self.root / "runtime.json"
        self.cache = self.root / "cache"
        self.database = self.cache / "cache-v1.sqlite"
        self.lock = self.cache / "cache-v1.sqlite.lock"
        self.elsewhere = self.root / "other-cwd"
        self.elsewhere.mkdir()
        self.command = [executable, "--ConfigPath", str(self.runtime)]
        initialized = self.cli("--InitializeConfig")
        require(initialized.returncode == 0, "Startup diagnostic fixture initialization failed")
        self.config = json.loads(self.runtime.read_text(encoding="utf-8"))
        for role, port in zip(("business", "control", "admin"), available_ports()):
            self.config[role].update(port=port, allowed_hosts=[f"127.0.0.1:{port}"])
        # Settings and registry retain their own writable state directory. A
        # denied cache directory must fail at cache locking, not settings load.
        self.config["cache"].update(directory="cache", max_bytes=16 * MIB)
        self.runtime.write_text(json.dumps(self.config), encoding="utf-8")
        self.client_token = (self.root / "client.token").read_text(encoding="ascii").strip()
        self.admin_token = (self.root / "admin.token").read_text(encoding="ascii").strip()

    def cli(self, *arguments):
        return subprocess.run([*self.command, *arguments], cwd=self.elsewhere,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=12, **cli_options())

    def healthy(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.config["control"]["port"], timeout=0.5)
        try:
            connection.request("GET", "/health/live",
                               headers={"Authorization": "Bearer " + self.client_token})
            response = connection.getresponse()
            content = response.read(65537)
            return response.status == 200 and len(content) <= 65536 and json.loads(content).get("live") is True
        finally:
            connection.close()

    def no_credentials(self, content):
        require(all(token.encode("ascii") not in content for token in (self.client_token, self.admin_token)),
                "A startup diagnostic disclosed a credential")

    def failure(self, reason):
        result = self.cli()
        require(result.returncode == 1, "Startup failure did not preserve exit code 1")
        require(not result.stdout, "Startup failure polluted stdout")
        self.no_credentials(result.stderr)
        text = result.stderr.decode("utf-8", errors="replace")
        require(re.search(r"^" + re.escape(reason) + r":", text, re.MULTILINE) is not None,
                "Startup failure did not identify " + reason)
        require("Cache is unavailable." not in text and " ready." not in text,
                "Startup failure used a generic message or claimed readiness")
        self.diagnostic_path(text, "Cache database", self.database)
        self.diagnostic_path(text, "Cache lock", self.lock)
        return text

    @staticmethod
    def diagnostic_path(text, label, expected):
        values = re.findall(r"^" + re.escape(label) + r": (.+)$", text, re.MULTILINE)
        require(len(values) == 1, "Startup diagnostic omitted or duplicated " + label)
        actual = values[0].rstrip("\r")
        require(os.path.isabs(actual) and os.path.normcase(os.path.abspath(actual)) ==
                os.path.normcase(str(expected)), "Startup diagnostic has an incorrect " + label)


class Running:
    """Own exactly the Popen created here, including the deliberate crash case."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.process = None
        self.stdout_path = fixture.root / "child.stdout"
        self.stderr_path = fixture.root / "child.stderr"

    def __enter__(self):
        with self.stdout_path.open("wb") as output, self.stderr_path.open("wb") as errors:
            self.process = subprocess.Popen(self.fixture.command, cwd=self.fixture.elsewhere,
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                            **hidden_console())
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                require(self.process.poll() is None, "Isolated startup fixture exited before readiness")
                try:
                    if self.fixture.healthy() and b"Press Ctrl+C to stop." in self.stderr_path.read_bytes():
                        break
                except (OSError, http.client.HTTPException, ValueError):
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError("Isolated startup fixture did not become ready")
            self.fixture.no_credentials(self.stderr_path.read_bytes())
            require(not self.stdout_path.read_bytes(), "HTTP startup polluted stdout")
            return self
        except BaseException:
            self.stop(graceful=False)
            raise

    def stop(self, graceful=True):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            if not graceful:
                self.process.kill()
            elif os.name == "nt":
                # The target has its own hidden console, so this event cannot
                # signal the test runner, a user terminal or another service.
                signaler = subprocess.run([sys.executable, "-c", BREAK_HELPER, str(self.process.pid)],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, timeout=3, **cli_options())
                require(signaler.returncode == 0, "Could not signal the owned startup fixture")
            else:
                self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=8)
            if graceful:
                require(self.process.returncode == 0, "The owned startup fixture did not exit cleanly")
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)

    def __exit__(self, kind, _value, _traceback):
        self.stop(graceful=kind is None)


def lock_owner_pid(path):
    first = path.read_text(encoding="utf-8").splitlines()[0]
    require(first.isdecimal(), "Fixture lock does not contain a numeric owner PID")
    return int(first)


def duplicate_and_stale(executable, root):
    fixture = Fixture(executable, root / "lock")
    with Running(fixture) as owner:
        require(lock_owner_pid(fixture.lock) == owner.process.pid, "First instance does not own its cache lock")
        original_lock = fixture.lock.read_bytes()
        original_database = hashlib.sha256(fixture.database.read_bytes()).digest()
        text = fixture.failure("instance_already_running")
        pids = re.findall(r"^Lock owner PID: ([0-9]+)\r?$", text, re.MULTILINE)
        require(pids == [str(owner.process.pid)], "Repeated startup did not identify the first instance PID")
        require("Cannot bind" not in text, "Cache sharing was incorrectly reported as a port conflict")
        require(owner.process.poll() is None and fixture.healthy(), "Repeated startup disturbed the first instance")
        require(fixture.lock.read_bytes() == original_lock, "Repeated startup replaced the active cache lock")
        require(hashlib.sha256(fixture.database.read_bytes()).digest() == original_database,
                "Repeated startup modified the first instance database")
    require(not fixture.lock.exists(), "A cleanly stopped instance did not release its lock")
    with Running(fixture) as owner:
        require(fixture.healthy(), "Restart after clean shutdown failed")
        crashed_pid = owner.process.pid
        owner.stop(graceful=False)
        require(fixture.lock.exists() and lock_owner_pid(fixture.lock) == crashed_pid,
                "The deliberate crash did not leave the expected stale lock fixture")
    # Do not remove the lock manually. The product must recognize its dead owner.
    with Running(fixture) as recovered:
        require(lock_owner_pid(fixture.lock) == recovered.process.pid and fixture.healthy(),
                "The product did not recover the dead owner's cache lock")
    require(not fixture.lock.exists(), "The recovered instance did not release its lock")
    print("PASS repeated startup owner PID/path, original health, clean restart and stale lock recovery")


@contextmanager
def prevent_lock_removal(path):
    # This is exclusively a lock left in our own TemporaryDirectory. On
    # Windows READONLY prevents stale-file deletion even for an administrator.
    target = path if os.name == "nt" else path.parent
    original_mode = target.stat().st_mode
    target.chmod(stat.S_IREAD if os.name == "nt" else 0o500)
    try:
        yield
    finally:
        target.chmod(original_mode)


def unconfirmed_lock_owners(executable, root):
    unknown = Fixture(executable, root / "unknown-owner")
    unknown.cache.mkdir()
    unknown_contents = b"not valid lock metadata\n"
    unknown.lock.write_bytes(unknown_contents)
    text = unknown.failure("cache_locked")
    require("Lock owner PID: unknown" in text and "instance_already_running" not in text,
            "Unknown lock metadata was reported as a running instance")
    require(unknown.lock.read_bytes() == unknown_contents and not unknown.database.exists(),
            "An unknown owner's lock was removed or its database was opened")

    if os.name != "nt" and os.geteuid() == 0:
        print("PASS unknown lock owner; SKIP undeletable stale lock under root")
        return
    stale = Fixture(executable, root / "undeletable-stale")
    with Running(stale) as owner:
        crashed_pid = owner.process.pid
        owner.stop(graceful=False)
    require(stale.lock.exists() and lock_owner_pid(stale.lock) == crashed_pid,
            "The owned crash did not leave its lock fixture")
    original_lock = stale.lock.read_bytes()
    original_database = hashlib.sha256(stale.database.read_bytes()).digest()
    with prevent_lock_removal(stale.lock):
        text = stale.failure("cache_locked")
        require("instance_already_running" not in text and
                re.findall(r"^Lock owner PID: ([0-9]+)\r?$", text, re.MULTILINE) == [str(crashed_pid)],
                "An undeletable dead owner's lock was reported as a running instance")
        require(stale.lock.read_bytes() == original_lock and
                hashlib.sha256(stale.database.read_bytes()).digest() == original_database,
                "Rejecting an undeletable stale lock changed the lock or cache")

    # A live PID also needs to belong to this executable. Keep authentic lock
    # metadata except for the PID of this Python runner, without signaling it.
    foreign_lock = str(os.getpid()).encode("ascii") + b"\n" + original_lock.split(b"\n", 1)[1]
    stale.lock.write_bytes(foreign_lock)
    try:
        with prevent_lock_removal(stale.lock):
            text = stale.failure("cache_locked")
            require("instance_already_running" not in text,
                    "A live PID with a different executable was reported as this instance")
            require(stale.lock.read_bytes() == foreign_lock and
                    hashlib.sha256(stale.database.read_bytes()).digest() == original_database,
                    "Rejecting a different executable changed the lock or cache")
    finally:
        stale.lock.write_bytes(original_lock)
    # Restored permissions let QLockFile recover the dead owner's lock itself.
    with Running(stale) as recovered:
        require(lock_owner_pid(stale.lock) == recovered.process.pid and stale.healthy(),
                "Restoring lock permissions did not recover normal startup")
    require(not stale.lock.exists(), "The recovered stale-lock fixture did not release its lock")
    print("PASS unknown, undeletable dead and mismatched executable lock-owner diagnostics")


def independent_cache_port_conflict(executable, root):
    first = Fixture(executable, root / "port-owner")
    with Running(first) as owner:
        # Reserve the other fixture's dynamic ports only after the first
        # listener is live, so only the intentionally reused business port
        # conflicts. Each configuration resolves its own cache directory.
        second = Fixture(executable, root / "port-contender")
        require(first.database != second.database and first.lock != second.lock,
                "Port conflict fixture unexpectedly shares a cache")
        port = first.config["business"]["port"]
        second.config["business"].update(port=port, allowed_hosts=[f"127.0.0.1:{port}"])
        second.runtime.write_text(json.dumps(second.config), encoding="utf-8")
        require(lock_owner_pid(first.lock) == owner.process.pid,
                "First instance does not own its independent cache lock")
        original_lock = first.lock.read_bytes()
        original_database = hashlib.sha256(first.database.read_bytes()).digest()

        result = second.cli()
        require(result.returncode == 1, "Port conflict did not preserve exit code 1")
        require(not result.stdout, "Port conflict polluted stdout")
        first.no_credentials(result.stderr)
        second.no_credentials(result.stderr)
        text = result.stderr.decode("utf-8", errors="replace")
        require("Cannot bind business listener." in text,
                "Independent cache port conflict did not identify the business listener")
        require(all(marker not in text for marker in
                    ("cache_unavailable", "instance_already_running", "cache_locked", "Cache is unavailable.")),
                "Independent cache port conflict was misreported as a cache failure")
        require(" ready." not in text and "Press Ctrl+C to stop." not in text,
                "Port conflict incorrectly claimed readiness")
        require(second.database.is_file() and not second.lock.exists(),
                "Port conflict did not initialize its independent cache or release its lock")
        require(owner.process.poll() is None and first.healthy(),
                "Independent cache port conflict disturbed the first instance")
        require(first.lock.read_bytes() == original_lock,
                "Independent cache port conflict replaced the first instance cache lock")
        require(hashlib.sha256(first.database.read_bytes()).digest() == original_database,
                "Independent cache port conflict modified the first instance database")
    require(not first.lock.exists() and not second.lock.exists(),
            "Port conflict fixtures did not release their cache locks")
    print("PASS independent-cache business port conflict, original health and lock cleanup")


def failed_database_cases(executable, root):
    directory = Fixture(executable, root / "directory")
    directory.database.mkdir(parents=True)
    directory.failure("cache_database_open_failed")
    require(not directory.lock.exists(), "Database open failure leaked the cache lock")

    corrupt = Fixture(executable, root / "corrupt")
    corrupt.cache.mkdir()
    original = b"not a SQLite database\n"
    corrupt.database.write_bytes(original)
    corrupt.failure("cache_database_corrupt")
    require(corrupt.database.read_bytes() == original and not corrupt.lock.exists(),
            "Corruption failure modified the database or leaked its lock")

    capacity = Fixture(executable, root / "capacity")
    capacity.cache.mkdir()
    with capacity.database.open("wb") as stream:
        stream.truncate(16 * MIB + 1)
    capacity.failure("cache_capacity_exceeded")
    require(capacity.database.stat().st_size == 16 * MIB + 1 and not capacity.lock.exists(),
            "Capacity rejection modified the database or leaked its lock")

    incompatible = Fixture(executable, root / "version")
    incompatible.cache.mkdir()
    with closing(sqlite3.connect(incompatible.database)) as connection:
        connection.execute("PRAGMA auto_vacuum=FULL")
        connection.execute("PRAGMA user_version=99")
    incompatible.failure("cache_database_incompatible")
    with closing(sqlite3.connect(incompatible.database)) as connection:
        require(connection.execute("PRAGMA user_version").fetchone()[0] == 99,
                "Unsupported cache format was silently migrated")
    require(not incompatible.lock.exists(), "Unsupported cache format leaked its lock")
    print("PASS distinct database open, corruption, capacity and format diagnostics")


@contextmanager
def deny_fixture_access(path, windows_rights, unix_mode):
    if os.name == "nt":
        system = Path(os.environ["SystemRoot"]) / "System32"
        identity = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, timeout=8, **cli_options())
        match = re.search(rb'"(S-1-[0-9-]+)"', identity.stdout)
        require(identity.returncode == 0 and match is not None, "Cannot identify the ACL fixture account")
        principal = "*" + match[1].decode("ascii")
        command = [str(system / "icacls.exe"), str(path)]
        result = subprocess.run([*command, "/deny", f"{principal}:({windows_rights})"],
                                capture_output=True, timeout=8, **cli_options())
        require(result.returncode == 0, "Cannot apply the fixture ACL")
        try:
            yield
        finally:
            result = subprocess.run([*command, "/remove:d", principal],
                                    capture_output=True, timeout=8, **cli_options())
            require(result.returncode == 0, "Cannot restore the fixture ACL")
    else:
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(unix_mode)
        try:
            yield
        finally:
            path.chmod(mode)


def permission_failure(executable, root):
    if os.name != "nt" and os.geteuid() == 0:
        print("SKIP cache permission fixture under root (Windows ACL coverage remains required)")
        return
    fixture = Fixture(executable, root / "permission")
    fixture.cache.mkdir()
    with deny_fixture_access(fixture.cache, "WD,AD", 0o500):
        probe = fixture.cache / "probe"
        try:
            probe.touch(exist_ok=False)
        except PermissionError:
            pass
        else:
            probe.unlink()
            raise RuntimeError("Cache permission fixture is still writable")
        fixture.failure("cache_lock_permission_denied")
        require(not fixture.lock.exists(), "Denied lock creation unexpectedly created a lock file")
    # Restoring this test-only ACL must make precisely the same configuration usable.
    with Running(fixture):
        require(fixture.healthy(), "Restored cache directory permissions did not recover startup")
    print("PASS cache lock permission diagnostic and permission restoration")


async def stdio_policy(command, cwd, env, search_enabled, change_settings=None):
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=MIB + 1, **cli_options())

    async def rpc(request_id, method, params):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()
        if request_id is not None:
            line = await process.stdout.readline()
            require(line, "Stdio closed before sending a protocol reply")
            reply = json.loads(line)
            require(reply["id"] == request_id and "result" in reply, "Unexpected stdio reply")
            return reply["result"]

    try:
        async with asyncio.timeout(10):
            await rpc(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "clientInfo": {"name": "startup-check", "version": "1"}})
            await rpc(None, "notifications/initialized", {})
            request_id = 2
            params = {"name": "web_search_status", "arguments": {}}
            result = await rpc(request_id, "tools/call", params)
            policy = result["structuredContent"]["settings"]
            require(policy["provider"] == "auto" and policy["search_enabled"] is search_enabled,
                    "Stdio did not use the selected settings")
            if change_settings:
                change_settings()
                while policy["search_enabled"] is search_enabled:
                    await asyncio.sleep(0.15)
                    request_id += 1
                    result = await rpc(request_id, "tools/call", params)
                    policy = result["structuredContent"]["settings"]
            process.stdin.close()
            await process.stdin.wait_closed()
            require(await process.wait() == 0 and not await process.stdout.read(),
                    "Stdio did not exit cleanly at EOF")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def stdio_configuration(executable, root):
    # Stage only the executable. Its own installation, adjacent files and
    # service credentials must remain untouched even when these cases fail.
    source = Path(executable)
    staged = root / "standalone bin" / source.name
    staged.parent.mkdir()
    shutil.copy2(source, staged)
    runtime = staged.parent / "data" / "runtime.json"
    private_temp = root / "process-temp"
    private_temp.mkdir()
    cwd = root / "stdio-cwd"
    inherited = cwd / "state"
    inherited.mkdir(parents=True)
    settings = json.loads((Path(__file__).resolve().parents[2] / "config/settings.json").read_text("utf-8"))
    settings["tools"]["search"]["enabled"] = False
    (inherited / "settings.json").write_text(json.dumps(settings), "utf-8")
    env = dict(os.environ, STATE_DIRECTORY=str(inherited), TMPDIR=str(private_temp),
               TEMP=str(private_temp), TMP=str(private_temp))
    for variable, directory in (("PATH", source.parent), ("QT_PLUGIN_PATH", source.parent.parent / "plugins"),
                                ("LD_LIBRARY_PATH", source.parent.parent / "lib")):
        if directory.is_dir():
            env[variable] = str(directory) + (os.pathsep + env[variable] if env.get(variable) else "")
    env.pop("QCWS_CLIENT_TOKEN", None)
    bare = [str(staged), "--Stdio"]
    explicit = [*bare, "--ConfigPath", str(runtime)]

    def rejected(*arguments):
        result = subprocess.run([*bare, *arguments], cwd=cwd, env=env, input=b"",
                                capture_output=True, timeout=6, **cli_options())
        require(result.returncode == 1 and not result.stdout and b"invalid_configuration" in result.stderr,
                "Invalid stdio configuration did not fail on stderr")

    asyncio.run(stdio_policy(bare, cwd, env, True))
    require(not runtime.parent.exists(), "Bare stdio created a service deployment")
    runtime.parent.mkdir()
    asyncio.run(stdio_policy(bare, cwd, env, True))
    require(not any(runtime.parent.iterdir()) and not any(private_temp.iterdir()),
            "Bare stdio left service files or private process state")
    rejected("--ConfigPath", str(cwd / "missing.json"))

    fixture = Fixture(executable, root / "stdio-configured")
    shared = fixture.root / "state"
    shared.mkdir(exist_ok=True)
    settings_file = shared / "settings.json"
    settings_file.write_text(json.dumps(settings), "utf-8")
    fixture.config["settings"]["directory"] = str(shared)
    fixture.config["cache"]["directory"] = str(fixture.cache)
    runtime.write_text(json.dumps(fixture.config), "utf-8")

    def enable_search():
        settings["tools"]["search"]["enabled"] = True
        settings_file.write_text(json.dumps(settings), "utf-8")

    asyncio.run(stdio_policy(bare, cwd, env, False, enable_search))
    require(not fixture.cache.exists(), "Stdio used the HTTP instance's persistent cache")
    runtime.write_text("{", "utf-8")
    rejected()
    rejected("--ConfigPath", str(runtime))
    asyncio.run(stdio_policy([*bare, "--ConfigPath", str(fixture.runtime)], cwd, env, True))
    runtime.write_text(json.dumps(fixture.config | {"project_version": "999.0.0"}), "utf-8")
    rejected()
    rejected("--ConfigPath", str(runtime))
    runtime.write_text(json.dumps({k: v for k, v in fixture.config.items() if k != "project_version"}), "utf-8")
    rejected()
    asyncio.run(stdio_policy(explicit, cwd, env, True))
    runtime.write_text(json.dumps(fixture.config), "utf-8")
    fixture.runtime.write_text(json.dumps(fixture.config), "utf-8")
    settings["tools"]["search"]["enabled"] = False
    settings_file.write_text(json.dumps(settings), "utf-8")
    if os.name == "nt" or os.geteuid() != 0:
        with deny_fixture_access(runtime, "RD", 0):
            rejected()
            rejected("--ConfigPath", str(runtime))
        with deny_fixture_access(runtime.parent, "RD", 0o100):
            rejected()
            asyncio.run(stdio_policy(explicit, cwd, env, False))
        runtime.unlink()
        with deny_fixture_access(runtime.parent, "RD", 0o100):
            rejected()
    else:
        runtime.unlink()
        print("SKIP stdio permission fixture under root")

    try:
        runtime.symlink_to(root / "missing-runtime-target")
    except OSError as error:
        if os.name != "nt" or error.winerror != 1314:
            raise
        # An unprivileged Windows account can still create a dangling reparse
        # point at the runtime filename, even with file symlinks disabled.
        target = root / "runtime-junction-target"
        target.mkdir()
        _winapi.CreateJunction(str(target), str(runtime))
        target.rmdir()
        try:
            rejected()
        finally:
            runtime.rmdir()
    else:
        try:
            rejected()
        finally:
            runtime.unlink()
        runtime.symlink_to(fixture.runtime)
        try:
            rejected()
            asyncio.run(stdio_policy(explicit, cwd, env, False))
        finally:
            runtime.unlink()
    runtime.parent.rmdir()
    if os.name == "nt":
        _winapi.CreateJunction(str(fixture.root), str(runtime.parent))
    else:
        runtime.parent.symlink_to(fixture.root, target_is_directory=True)
    try:
        rejected()
        asyncio.run(stdio_policy(explicit, cwd, env, False))
    finally:
        if os.name == "nt":
            runtime.parent.rmdir()
        else:
            runtime.parent.unlink()
    print("PASS stdio discovery, explicit paths, directory permissions, private state and settings reload")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", type=Path, required=True)
    arguments = parser.parse_args()
    executable = str(arguments.Executable.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="qsd-") as temporary:
        root = Path(temporary).resolve()
        stdio_configuration(executable, root)
        duplicate_and_stale(executable, root)
        unconfirmed_lock_owners(executable, root)
        independent_cache_port_conflict(executable, root)
        failed_database_cases(executable, root)
        permission_failure(executable, root)
    print("PASS startup diagnostics and owned-process cleanup")


if __name__ == "__main__":
    main()
