#!/usr/bin/env python3
"""Measure cold/hot stability against a local 100 ms search fixture."""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
from process_cleanup import wait_owned
from runtime_evidence import fingerprint, host_info

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
BREAK_HELPER = r"""
import ctypes, sys, time
from ctypes import wintypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
Handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
for name, args in (
    ("FreeConsole", []), ("AttachConsole", [wintypes.DWORD]),
    ("SetConsoleCtrlHandler", [Handler, wintypes.BOOL]),
    ("GenerateConsoleCtrlEvent", [wintypes.DWORD, wintypes.DWORD]),
):
    getattr(k, name).argtypes = args
    getattr(k, name).restype = wintypes.BOOL
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    sys.exit(21)
handler = Handler(lambda event: event == 1)
try:
    if not k.SetConsoleCtrlHandler(handler, True):
        sys.exit(22)
    if not k.GenerateConsoleCtrlEvent(1, 0):
        sys.exit(23)
    time.sleep(0.1)
finally:
    k.FreeConsole()
"""


class Fault(Exception):
    """Fixed diagnostic codes only, never remote content."""


def digest(path):
    with path.open("rb") as source:
        value = hashlib.sha256()
        for block in iter(lambda: source.read(MIB), b""):
            value.update(block)
        return value.hexdigest()


def request(port, token, path="/health/live", payload=None, timeout=2, measurement=None, origin=0):
    started = time.monotonic()
    if measurement is not None:
        measurement["actual_start_offset_ms"] = round((started - origin) * 1000, 3)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request("GET" if body is None else "POST", path, body, headers)
        response = connection.getresponse()
        if measurement is not None:
            measurement["http_status"] = response.status
        if response.status != 200:
            raise Fault(f"http_{response.status}")
        if response.getheader("Content-Type", "").split(";")[0] != "application/json":
            raise Fault("content_type")
        raw = response.read(MIB + 1)
        if len(raw) > MIB:
            raise Fault("response_too_large")
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("contract_version")) is not int
                or value["contract_version"] != 1):
            raise Fault("contract_version")
        return value, round((time.monotonic() - started) * 1000, 3)
    except Fault:
        raise
    except (ValueError, UnicodeError):
        raise Fault("invalid_json") from None
    except (TimeoutError, socket.timeout):
        raise Fault("timeout") from None
    except (OSError, http.client.HTTPException):
        raise Fault("transport") from None
    finally:
        if measurement is not None:
            measurement["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        connection.close()


class Fixture(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.calls, self.lock = 0, threading.Lock()

    def count(self):
        with self.lock:
            return self.calls

    def handle_error(self, request, client_address):
        pass


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            self.connection.settimeout(2)
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/search" or not 0 < length <= 65536:
                self.send_error(400)
                return
            if len(self.rfile.read(length)) != length:
                return
            with self.server.lock:
                self.server.calls += 1
            time.sleep(0.1)
            body = json.dumps({"unresponsive_engines": [], "results": [
                {"url": f"https://example.org/fixture/{i}", "title": f"Fixture {i}",
                 "content": "Synthetic acceptance content.", "engines": ["fixture"]}
                for i in range(20)]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ValueError):
            pass


def environment(tokens):
    excluded = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ld_library_path",
                "qt_plugin_path", "qml2_import_path", "qml_import_path",
                "qt_qpa_platform_plugin_path"}
    env = {k: v for k, v in os.environ.items() if k.lower() not in excluded}
    env.update({f"QCWS_SOAK_TOKEN_{i}": token for i, token in enumerate(tokens)})
    return env


def hidden_console():
    if os.name != "nt":
        return {"start_new_session": True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NEW_CONSOLE}


def stop_process(proc):
    started, error, forced, children = time.monotonic(), None, False, []
    # A reaped Popen no longer owns its numeric PID. Never reopen that PID to
    # enumerate or signal descendants belonging to a replacement process.
    if proc.poll() is not None:
        return {"seconds": round(time.monotonic() - started, 3), "returncode": proc.returncode,
                "forced": False, "error": "early_exit", "passed": False}
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        pass
    try:
        if proc.poll() is not None:
            raise Fault("early_exit")
        if os.name == "nt":
            helper = subprocess.run([sys.executable, "-c", BREAK_HELPER, str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=2, check=False)
            if helper.returncode:
                raise Fault("signal_failed")
        else:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=max(0.01, 10 - (time.monotonic() - started)))
    except Fault as exc:
        error = exc.args[0]
    except subprocess.TimeoutExpired:
        error = "stop_timeout"
    except OSError:
        error = "signal_failed"
    finally:
        if proc.poll() is None:
            forced = True
            proc.kill()
            proc.wait(timeout=5)
        for child in children:
            try:
                if child.is_running():
                    forced = True
                    child.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                error = "child_cleanup_failed"
        if wait_owned(children, timeout=3):
            error = "child_cleanup_failed"
    elapsed = round(time.monotonic() - started, 3)
    return {"seconds": elapsed, "returncode": proc.returncode, "forced": forced,
            "error": error, "passed": error is None and not forced
            and proc.returncode == 0 and elapsed <= 10}


def start_process(executable, template, folder, fixture, tokens, ttl):
    for attempt in range(1, 4):
        sockets = []
        try:
            for _ in range(2):
                sock = socket.socket()
                sockets.append(sock)
                if os.name == "nt":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", 0))
            ports = [sock.getsockname()[1] for sock in sockets]
            config = copy.deepcopy(template)
            for name, port in zip(("business", "control"), ports):
                config[name] = {"host": "127.0.0.1", "port": port, "origins": [],
                                "allowed_hosts": [f"127.0.0.1:{port}"]}
            config["clients"] = [{"id": f"soak-{i}", "token_env": f"QCWS_SOAK_TOKEN_{i}"}
                                 for i in range(5)]
            config["provider"] = {"url": f"http://127.0.0.1:{fixture.server_port}/search",
                                  "ca_file": "", "engines": ["fixture"]}
            config["cache"].update(directory=str(folder / f"cache-{attempt}"),
                                   max_bytes=256 * MIB, max_entries=10000,
                                   search_ttl_seconds=ttl, page_ttl_seconds=1800)
            config["limits"] = {"max_concurrent": 5, "search_timeout_seconds": 15,
                                "open_timeout_seconds": 20}
            path = folder / "runtime.json"
            path.write_text(json.dumps(config), encoding="utf-8")
        finally:
            for sock in sockets:
                sock.close()
        proc = subprocess.Popen([str(executable), "--ConfigPath", str(path)],
            env=environment(tokens), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **hidden_console())
        ready = False
        try:
            deadline = time.monotonic() + 20
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    ready = request(ports[1], tokens[0], timeout=0.5)[0].get("live") is True
                    if ready:
                        return proc, ports, attempt
                except Fault:
                    pass
                time.sleep(0.05)
        finally:
            if not ready:
                stop_process(proc)
    raise Fault("startup_failed")


def stats(rows):
    values = sorted(row["ms"] for row in rows if "ms" in row)
    return {"attempted": len(rows), "successful": len(values),
            "p95_ms": values[math.ceil(len(values) * 0.95) - 1] if values else None,
            "errors": dict(collections.Counter(row["error"] for row in rows if "error" in row)),
            "latency_ms": values, "samples": rows}


def phase(name, executable, template, folder, fixture, seconds):
    folder.mkdir()
    hot, tokens = name == "hot", [secrets.token_hex(32) for _ in range(5)]
    ttl = max(300, seconds + 60) if hot else 300
    proc, ports, attempts = start_process(executable, template, folder, fixture, tokens, ttl)
    halt, threads = threading.Event(), []
    result = {"name": name, "seconds": seconds, "startup_attempts": attempts,
              "search_ttl_seconds": ttl, "workload_passed": False}
    rows, health, rss = [[] for _ in range(5)], [], []

    def search(index, sequence, expected_hit, measurement=None, origin=0):
        query = f"hot-{index}" if hot else f"cold-{index}-{sequence}"
        value, elapsed = request(ports[0], tokens[index], "/v1/search", {"query": query, "limit": 10},
                                 measurement=measurement, origin=origin)
        hit = value.get("cache", {}).get("hit")
        if measurement is not None:
            measurement["cache_hit"] = hit if type(hit) is bool else None
        if value.get("status") != "ok" or len(value.get("results", [])) != 10:
            raise Fault("search_payload")
        if type(hit) is not bool or hit != expected_hit:
            raise Fault("cache_behavior")
        return {"ms": elapsed}

    try:
        native = psutil.Process(proc.pid)
        if hot:
            for index in range(5):
                search(index, 0, False)
        before, start = fixture.count(), time.monotonic() + 0.25
        end = start + seconds

        def client(index):
            sequence = 0
            while not halt.is_set():
                due = start + index * 0.5 + sequence * 2.5
                if due >= end or halt.wait(max(0, due - time.monotonic())):
                    return
                attempt = time.monotonic()
                sample = {"client_index": index, "sequence": sequence,
                          "scheduled_start_offset_ms": round((due - start) * 1000, 3),
                          "attempt_start_offset_ms": round((attempt - start) * 1000, 3),
                          "schedule_lag_ms": round((attempt - due) * 1000, 3),
                          "actual_start_offset_ms": None, "duration_ms": None,
                          "http_status": None, "cache_hit": None}
                try:
                    if attempt - due > 0.25:
                        raise Fault("schedule_missed")
                    sample.update(search(index, sequence, hot, sample, start))
                except Fault as exc:
                    sample["error"] = exc.args[0]
                except Exception:
                    sample["error"] = "client_failed"
                rows[index].append(sample)
                sequence += 1

        def monitor():
            while not halt.is_set():
                sample = {"sequence": len(health), "actual_start_offset_ms": None,
                          "duration_ms": None, "http_status": None}
                try:
                    rss.append(native.memory_info().rss)
                    value, elapsed = request(ports[1], tokens[0], timeout=1, measurement=sample, origin=start)
                    if value.get("live") is not True:
                        raise Fault("not_live")
                    sample["ms"] = elapsed
                except Fault as exc:
                    sample["error"] = exc.args[0]
                except Exception:
                    sample["error"] = "monitor_failed"
                health.append(sample)
                halt.wait(1)

        threads = [threading.Thread(target=client, args=(i,)) for i in range(5)]
        threads.append(threading.Thread(target=monitor))
        for thread in threads:
            thread.start()
        next_update = start + 60
        while time.monotonic() < end:
            halt.wait(min(0.5, max(0, end - time.monotonic())))
            if time.monotonic() >= next_update:
                print(f"{name}: {int(time.monotonic() - start)} seconds", flush=True)
                next_update += 60
        for thread in threads[:5]:
            thread.join(timeout=3)
        halt.set()
        threads[-1].join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise Fault("thread_did_not_stop")
        requests, live = stats([r for client_rows in rows for r in client_rows]), stats(health)
        expected, calls, peak = seconds * 2, fixture.count() - before, max(rss, default=0)
        result.update(requests=requests, health=live, peak_rss_bytes=peak, rss_samples_bytes=rss,
                      fixture_calls=calls, expected_requests=expected, measured_seconds=time.monotonic() - start)
        result["workload_passed"] = (
            requests["attempted"] == requests["successful"] == expected
            and requests["p95_ms"] <= 1000
            and live["attempted"] == live["successful"] > 0 and live["p95_ms"] <= 200
            and 0 < peak <= 512 * MIB and calls == (0 if hot else expected))
    except Fault as exc:
        result["error"] = exc.args[0]
    finally:
        halt.set()
        result["stop"] = stop_process(proc)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=3)
        if any(thread.is_alive() for thread in threads):
            result["workload_passed"], result["error"] = False, "thread_cleanup_failed"
    result["passed"] = result["workload_passed"] and result["stop"]["passed"]
    return result


def resources():
    try:
        cpus = len(psutil.Process().cpu_affinity())
    except (AttributeError, psutil.Error):
        cpus = psutil.cpu_count() or 0
    memory = psutil.virtual_memory().total
    # Account for cgroup v2 and v1 limits when running the runner in a container.
    for name in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        path = Path(name)
        if path.is_file():
            value = path.read_text().strip()
            if value.isdigit():
                memory = min(memory, int(value))
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    if cpu_max.is_file():
        quota, period = cpu_max.read_text().split()
        if quota != "max":
            cpus = min(cpus, int(quota) / int(period))
    else:
        quota_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period_path = quota_path.with_name("cpu.cfs_period_us")
        if quota_path.is_file() and period_path.is_file():
            quota, period = int(quota_path.read_text()), int(period_path.read_text())
            if quota > 0:
                cpus = min(cpus, quota / period)
    return {"available_cpus": cpus, "memory_bytes": memory,
            "minimum_met": cpus >= 2 and memory >= 4 * 1024**3}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", required=True, type=Path)
    parser.add_argument("--Seconds", type=int, default=1800)
    parser.add_argument("--Output", required=True, type=Path)
    args = parser.parse_args()
    if not 5 <= args.Seconds <= 86340:
        parser.error("--Seconds must be 5–86340 per phase")
    executable, output = args.Executable.resolve(strict=True), args.Output.resolve()
    template_path = ROOT / "config/runtime.json"
    if output in (executable, template_path.resolve(), Path(__file__).resolve()):
        parser.error("--Output must be a separate report file")
    report = {"report_version": 1, "samples_schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "run_kind": "acceptance" if args.Seconds >= 1800 else "smoke",
              "executable": str(executable), "executable_sha256": digest(executable),
              "runtime": fingerprint(executable), "host": host_info(),
              "template_sha256": digest(template_path), "os": platform.platform(),
              "architecture": platform.machine(), "resources": resources(), "psutil": psutil.__version__,
              "seconds_per_phase": args.Seconds, "clients": 5, "requests_per_second": 2,
              "fixture_delay_ms": 100, "rss_sampling_seconds": 1,
              "phases": [], "checks_passed": False, "full_acceptance": False}
    fixture, fixture_thread, code = None, None, 1
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
        work = ROOT / "work"
        work.mkdir(exist_ok=True)
        fixture = Fixture()
        fixture_thread = threading.Thread(target=fixture.serve_forever)
        fixture_thread.start()
        with tempfile.TemporaryDirectory(prefix="qcws-soak-", dir=work) as temporary:
            folder = Path(temporary).resolve()
            if not folder.is_relative_to(work.resolve()):
                raise Fault("temporary_directory_scope")
            for name in ("cold", "hot"):
                print(f"{name}: starting", flush=True)
                report["phases"].append(phase(name, executable, template, folder / name, fixture, args.Seconds))
        report["executable_unchanged"] = digest(executable) == report["executable_sha256"]
        report["runtime_unchanged"] = fingerprint(executable) == report["runtime"]
        report["checks_passed"] = (len(report["phases"]) == 2 and all(p["passed"] for p in report["phases"])
                                   and report["executable_unchanged"] and report["runtime_unchanged"])
        report["full_acceptance"] = (report["checks_passed"] and args.Seconds >= 1800
                                     and report["resources"]["minimum_met"])
        code = 0 if report["checks_passed"] and (args.Seconds < 1800 or report["full_acceptance"]) else 1
    except KeyboardInterrupt:
        report["error"], code = "interrupted", 130
    except Fault as exc:
        report["error"] = exc.args[0]
    except Exception:
        report["error"] = "runner_failed"
    finally:
        if fixture is not None:
            fixture.shutdown()
            fixture.server_close()
        if fixture_thread is not None:
            fixture_thread.join(timeout=3)
            if fixture_thread.is_alive():
                report["checks_passed"] = report["full_acceptance"] = False
                report["error"], code = "fixture_cleanup_failed", 1
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=output.name + ".", delete=False) as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            pending = Path(target.name)
        try:
            os.replace(pending, output)
        finally:
            pending.unlink(missing_ok=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
