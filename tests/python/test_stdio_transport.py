#!/usr/bin/env python3
"""Process tests, including stdout pipes that the parent deliberately never drains."""
import argparse
from contextlib import contextmanager, ExitStack
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time


VERSIONS = ("2025-06-18", "2025-11-25")
LEGACY_STDIO_VERSION = "2024-11-05"
CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "v1"


def require(condition, message):
    # Explicit checks keep working under python -O, unlike assert.
    if not condition:
        raise RuntimeError(message)


def initialize(version):
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "wire-contract", "version": "1"}}}


@contextmanager
def stdio_peer(executable, runtime):
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
            [str(Path(executable).resolve(strict=True)), "--ConfigPath", str(runtime), "--Stdio"], cwd=runtime.parent,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process:
        # A blocking readline has no portable pipe timeout on Windows.
        deadline = threading.Timer(20, process.kill)
        deadline.start()
        try:
            def exchange(message):
                process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                process.stdin.flush()
                if "id" not in message:
                    return None
                line = process.stdout.readline(1024 * 1024 + 1)
                require(line.endswith(b"\n") and len(line) <= 1024 * 1024, "Invalid or missing stdio frame")
                reply = json.loads(line)
                require(reply.get("jsonrpc") == "2.0" and reply.get("id") == message["id"],
                        "Stdio reply did not match its request")
                return reply

            yield exchange
            process.stdin.close()
            require(process.wait(timeout=5) == 0, "Stdio failed to exit normally after EOF")
            require(not process.stdout.read(), "Stdio emitted unexpected output after its last reply")
        finally:
            deadline.cancel()
            if process.poll() is None:
                process.kill()
            deadline.join()


def check_tool_wire(exchange, version):
    legacy = version == LEGACY_STDIO_VERSION
    exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listing = exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]
    expected = []
    for metadata in json.loads((CONTRACTS / "tools.json").read_text("utf-8"))["tools"]:
        entry = {key: metadata[key] for key in ("name", "description")} if legacy else dict(metadata)
        entry["inputSchema"] = json.loads((CONTRACTS / (entry["name"] + ".schema.json")).read_text("utf-8"))
        expected.append(entry)
    require(listing == {"tools": expected}, f"Incorrect tool inventory or metadata for {version}")

    for request_id, name, failed in ((3, "web_search_status", False), (4, "web_search", True)):
        result = exchange({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                           "params": {"name": name, "arguments": {}}})["result"]
        keys = {"content", "isError"} if legacy else {"content", "isError", "structuredContent"}
        require(set(result) == keys and result["isError"] is failed, f"Incorrect {version} {name} result")
        content = result["content"]
        require(len(content) == 1 and set(content[0]) == {"type", "text"} and content[0]["type"] == "text",
                f"Missing {version} JSON text for {name}")
        payload = json.loads(content[0]["text"])
        if failed:
            require(payload["error"]["code"] == "invalid_argument", "The tool error lost its business code")
        else:
            require(payload["contract_version"] == 1 and payload["service"] == "ready", "Status payload changed")
        if not legacy:
            require(result["structuredContent"] == payload, f"Text and structured results differ for {version}")


def protocol_versions(executable, template):
    with tempfile.TemporaryDirectory(prefix="qcws stdio versions ") as temporary, ExitStack() as stack:
        root = Path(temporary)
        runtime = root / "runtime.json"
        config = json.loads(Path(template).read_text(encoding="utf-8"))
        config["settings"] = {"directory": str(root / "state")}
        runtime.write_text(json.dumps(config), encoding="utf-8")
        peers = {}
        instructions = json.loads((CONTRACTS / "tools.json").read_text("utf-8"))["instructions"]
        for version in (LEGACY_STDIO_VERSION, *VERSIONS):
            exchange = stack.enter_context(stdio_peer(executable, runtime))
            peers[version] = exchange
            result = exchange(initialize(version))["result"]
            require(set(result) == {"protocolVersion", "capabilities", "serverInfo", "instructions"} and
                    result["protocolVersion"] == version and result["capabilities"] == {"tools": {}} and
                    result["instructions"] == instructions, f"Incorrect stdio initialization for {version}")
            info = result["serverInfo"]
            fields = {"name", "version"} if version == LEGACY_STDIO_VERSION else {"name", "version", "icons"}
            require(set(info) == fields and info["name"] == "qcopilots-websearch" and bool(info["version"]),
                    f"Incorrect stdio server metadata for {version}")
        for version, exchange in peers.items():
            check_tool_wire(exchange, version)


def pressure(executable, template, close_reader):
    with tempfile.TemporaryDirectory(prefix="qcws stdio pipe ") as temporary:
        config = json.loads(Path(template).read_text(encoding="utf-8"))
        config["cache"]["directory"] = str(Path(temporary) / "cache")
        config["clients"] = [{"id": "pipe-test", "token_env": "QCWS_PIPE_TOKEN"}]
        path = Path(temporary) / "runtime.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        child = subprocess.Popen([executable, "--ConfigPath", str(path), "--Stdio"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 bufsize=0, env=dict(os.environ, QCWS_PIPE_TOKEN=secrets.token_hex(32)))
        try:
            initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "pipe-test", "version": "1"}}}
            child.stdin.write((json.dumps(initialize) + "\n").encode())
            require(json.loads(child.stdout.readline())["id"] == 1, "initialize reply has the wrong id")
            if close_reader:
                child.stdout.close()
            payload = memoryview((json.dumps({"jsonrpc": "2.0", "id": "p" * 262144,
                                             "method": "ping"}) + "\n").encode())
            while payload:
                written = child.stdin.write(payload)
                require(written, "stdin accepted no bytes")
                payload = payload[written:]
            # Popen.wait does not pump stdout, unlike QProcess.waitForFinished.
            started = time.monotonic()
            if not close_reader:
                child.stdin.close()
            result = child.wait(timeout=5)
            require(result in (0, 1), f"abnormal stdio exit: {result}")
            if not close_reader:
                require(result == 0, "EOF should cancel backpressure without reporting input failure")
            else:
                require(result == 1, "A broken stdout must stop input even while stdin stays open")
            require(time.monotonic() - started < 5, "stdio shutdown exceeded 5 seconds")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


def startup_summary(executable):
    with tempfile.TemporaryDirectory(prefix="qcws startup summary ") as temporary:
        root = Path(temporary)
        runtime = root / "runtime.json"
        command = [executable, "--ConfigPath", str(runtime)]
        initialized = subprocess.run([*command, "--InitializeConfig"], capture_output=True, timeout=8)
        require(initialized.returncode == 0, "Startup fixture initialization failed")
        config = json.loads(runtime.read_text(encoding="utf-8"))
        require(tuple(config[role]["port"] for role in ("business", "control", "admin")) == (3181, 3182, 3183),
                "New configuration does not use the documented fixed ports")
        reservations = [socket.socket() for _ in range(3)]
        try:
            for listener in reservations:
                listener.bind(("127.0.0.1", 0))
            for role, listener in zip(("business", "control", "admin"), reservations):
                port = listener.getsockname()[1]
                config[role].update(port=port, allowed_hosts=[f"127.0.0.1:{port}"])
        finally:
            for listener in reservations:
                listener.close()
        runtime.write_text(json.dumps(config), encoding="utf-8")
        client_token = (root / "client.token").read_text(encoding="ascii").strip()
        admin_token = (root / "admin.token").read_text(encoding="ascii").strip()
        checked = subprocess.run([*command, "--CheckConfig"], capture_output=True, timeout=8)
        require(checked.returncode == 0 and checked.stdout.strip() == b"Configuration valid.",
                "CheckConfig output changed or fixture configuration failed")
        for role in ("business", "control", "admin"):
            with socket.socket() as occupied:
                if os.name == "nt":
                    occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                occupied.bind(("127.0.0.1", config[role]["port"]))
                occupied.listen()
                blocked = subprocess.run(command, capture_output=True, timeout=8)
                require(blocked.returncode != 0 and b"Cannot bind" in blocked.stderr and b" ready." not in blocked.stderr,
                        f"A blocked {role} listener reported ready or failed to exit")
                require(not blocked.stdout, "Failed startup wrote to stdout")

        stderr_path = root / "startup.log"
        with stderr_path.open("wb") as errors, tempfile.TemporaryFile() as output:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    require(child.poll() is None, "HTTP process failed during startup")
                    summary = stderr_path.read_text(encoding="utf-8")
                    if "Press Ctrl+C to stop." in summary:
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Startup summary was not flushed after binding")
                for role in ("business", "control", "admin"):
                    require(f"127.0.0.1:{config[role]['port']}" in summary,
                            f"Startup summary omitted the actual {role} port")
                require(str(runtime) in summary and "Client registry:" in summary and "Cache directory:" in summary
                        and "/mcp" in summary and "/settings/" in summary and "--SettingsUrl" in summary,
                        "Startup summary omitted paths or connection guidance")
                require(client_token not in summary and admin_token not in summary,
                        "Startup summary disclosed credentials")
                connection = http.client.HTTPConnection("127.0.0.1", config["control"]["port"], timeout=3)
                try:
                    connection.request("GET", "/health/live", headers={"Authorization": "Bearer " + client_token})
                    response = connection.getresponse()
                    require(response.status == 200 and json.loads(response.read())["live"],
                            "Startup summary preceded listener readiness")
                finally:
                    connection.close()
                output.seek(0)
                require(not output.read(), "HTTP startup polluted stdout")
                # A real listener may enable socket reuse, unlike the exclusive
                # occupied sockets above. Independent state directories ensure
                # that a contender fails at bind, not at a cache or file lock.
                for role in ("business", "control", "admin"):
                    contender_root = root / ("conflict-" + role)
                    contender_runtime = contender_root / "runtime.json"
                    contender_command = [executable, "--ConfigPath", str(contender_runtime)]
                    initialized = subprocess.run([*contender_command, "--InitializeConfig"],
                                                 capture_output=True, timeout=8)
                    require(initialized.returncode == 0, f"{role} contender initialization failed")
                    contender_config = json.loads(contender_runtime.read_text(encoding="utf-8"))
                    reservations = [socket.socket() for _ in range(3)]
                    try:
                        for listener in reservations:
                            listener.bind(("127.0.0.1", 0))
                        for contender_role, listener in zip(("business", "control", "admin"), reservations):
                            port = config[role]["port"] if contender_role == role else listener.getsockname()[1]
                            contender_config[contender_role].update(port=port,
                                                                   allowed_hosts=[f"127.0.0.1:{port}"])
                    finally:
                        for listener in reservations:
                            listener.close()
                    contender_runtime.write_text(json.dumps(contender_config), encoding="utf-8")
                    blocked = subprocess.run(contender_command, capture_output=True, timeout=8)
                    require(blocked.returncode != 0 and b"Cannot bind" in blocked.stderr
                            and b" ready." not in blocked.stderr,
                            f"A second service sharing the {role} port did not fail at bind")
                    require(not blocked.stdout, "Conflicting service startup wrote to stdout")
                    require(child.poll() is None, f"The {role} contender stopped the first service")
                    connection = http.client.HTTPConnection("127.0.0.1", config["control"]["port"], timeout=3)
                    try:
                        connection.request("GET", "/health/live", headers={"Authorization": "Bearer " + client_token})
                        response = connection.getresponse()
                        require(response.status == 200 and json.loads(response.read())["live"],
                                f"The first service lost health after the {role} conflict")
                    finally:
                        connection.close()
            finally:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--CppTests", required=True)
    parser.add_argument("--Executable", required=True)
    parser.add_argument("--Template", required=True)
    args = parser.parse_args()
    subprocess.run([args.CppTests, args.Executable, args.Template], check=True, timeout=70)
    protocol_versions(args.Executable, args.Template)
    pressure(args.Executable, args.Template, False)
    pressure(args.Executable, args.Template, True)
    startup_summary(args.Executable)
    print("PASS stdio protocol versions, isolation, shutdown, fixed defaults, startup summary and listener conflicts")
