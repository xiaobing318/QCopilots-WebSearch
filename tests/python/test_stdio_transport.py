#!/usr/bin/env python3
"""Process tests, including stdout pipes that the parent deliberately never drains."""
import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time


def require(condition, message):
    # Explicit checks keep working under python -O, unlike assert.
    if not condition:
        raise RuntimeError(message)


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
    pressure(args.Executable, args.Template, False)
    pressure(args.Executable, args.Template, True)
    startup_summary(args.Executable)
    print("PASS stdio isolation, shutdown, fixed defaults, startup summary and listener conflicts")
