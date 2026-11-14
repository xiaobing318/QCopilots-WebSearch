#!/usr/bin/env python3
"""Process tests, including stdout pipes that the parent deliberately never drains."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time


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
            assert json.loads(child.stdout.readline())["id"] == 1
            if close_reader:
                child.stdout.close()
            payload = memoryview((json.dumps({"jsonrpc": "2.0", "id": "p" * 262144,
                                             "method": "ping"}) + "\n").encode())
            while payload:
                written = child.stdin.write(payload)
                assert written
                payload = payload[written:]
            # Popen.wait does not pump stdout, unlike QProcess.waitForFinished.
            started = time.monotonic()
            if not close_reader:
                child.stdin.close()
            result = child.wait(timeout=5)
            assert result in (0, 1), f"abnormal stdio exit: {result}"
            if not close_reader:
                assert result == 0, "EOF should cancel backpressure without reporting input failure"
            else:
                assert result == 1, "A broken stdout must stop input even while stdin stays open"
            assert time.monotonic() - started < 5
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--CppTests", required=True)
    parser.add_argument("--Executable", required=True)
    parser.add_argument("--Template", required=True)
    args = parser.parse_args()
    subprocess.run([args.CppTests, args.Executable, args.Template], check=True, timeout=70)
    pressure(args.Executable, args.Template, False)
    pressure(args.Executable, args.Template, True)
    print("PASS non-consuming stdout and closed-reader shutdown")
