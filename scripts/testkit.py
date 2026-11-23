"""Shared fixtures and process cleanup for package acceptance checks."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

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


def request(port, token, path="/health/live", payload=None, timeout=2):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request("GET" if body is None else "POST", path, body, headers)
        response = connection.getresponse()
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
        return value
    except Fault:
        raise
    except (ValueError, UnicodeError):
        raise Fault("invalid_json") from None
    except (TimeoutError, socket.timeout):
        raise Fault("timeout") from None
    except (OSError, http.client.HTTPException):
        raise Fault("transport") from None
    finally:
        connection.close()


def environment(tokens):
    excluded = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ld_library_path",
                "qt_plugin_path", "qml2_import_path", "qml_import_path",
                "qt_qpa_platform_plugin_path"}
    env = {k: v for k, v in os.environ.items() if k.lower() not in excluded}
    env.update({f"QCWS_FIXTURE_TOKEN_{i}": token for i, token in enumerate(tokens[:5])})
    if len(tokens) == 6:
        env["QCWS_FIXTURE_ADMIN_TOKEN"] = tokens[5]
    return env


def hidden_console():
    if os.name != "nt":
        return {"start_new_session": True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NEW_CONSOLE}


def stop_process(proc):
    import psutil
    from process_cleanup import wait_owned

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
            for _ in range(3):
                sock = socket.socket()
                sockets.append(sock)
                if os.name == "nt":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", 0))
            ports = [sock.getsockname()[1] for sock in sockets]
            config = copy.deepcopy(template)
            for name, port in zip(("business", "control"), ports):
                config[name] = {"host": "127.0.0.1", "port": port,
                                "allowed_hosts": [f"127.0.0.1:{port}"]}
                if name == "control":
                    config[name]["origins"] = []
            config["clients"] = [{"id": f"fixture-{i}", "token_env": f"QCWS_FIXTURE_TOKEN_{i}"}
                                 for i in range(5)]
            config["admin"] = {"enabled": True, "port": ports[2], "token_env": "QCWS_FIXTURE_ADMIN_TOKEN"}
            if fixture is not None:
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
                    ready = request(ports[1], tokens[0], timeout=0.5).get("live") is True
                    if ready:
                        return proc, ports, attempt
                except Fault:
                    pass
                time.sleep(0.05)
        finally:
            if not ready:
                stop_process(proc)
    raise Fault("startup_failed")


def host_info():
    """Record the actual kernel/platform, without a hostname or user identity."""
    result = {"system": platform.system(), "architecture": platform.machine()}
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        result["windows"] = {"major": version.major, "minor": version.minor,
                             "build": version.build, "product_type": version.product_type}
    elif sys.platform == "linux":
        release = platform.freedesktop_os_release()
        result["linux"] = {key: release.get(key, "") for key in ("ID", "VERSION_ID")}
    return result


def fingerprint(executable):
    executable = Path(executable).resolve(strict=True)
    root = executable.parent.parent
    entries = []
    candidates = {executable}
    for name in ("bin", "lib", "plugins"):
        folder = root / name
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file() and (path.suffix.lower() == ".dll" or ".so" in path.name
                                       or path.name == "qt.conf"):
                    candidates.add(path)
    for path in sorted(candidates, key=lambda value: value.relative_to(root).as_posix()):
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Runtime symlink escapes the deployment")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        entry = {"path": path.relative_to(root).as_posix(), "sha256": digest}
        if path.is_symlink():
            entry["link"] = path.readlink().as_posix()
        entries.append(entry)
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": entries}


def need(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def admin_smoke(executable, config, env, provider, persist_in=None):
    """Check the browser entry and native link against a running local service."""
    options = json.loads(config.read_text(encoding="utf-8")).get("admin", {})
    port = options.get("port", 8091)
    origin = f"http://127.0.0.1:{port}"
    base = "/admin/api/v1"
    def exchange(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
            response = connection.getresponse()
            data = response.read(MIB + 1)
            if len(data) > MIB:
                raise Fault("admin_response_size")
            return response.status, response.headers, data
        finally:
            connection.close()

    deadline = time.monotonic() + 4
    while True:
        try:
            status, headers, html = exchange("GET", "/settings/")
            if status == 200 and html and headers.get("Content-Security-Policy"):
                break
        except (OSError, http.client.HTTPException):
            pass
        if time.monotonic() >= deadline:
            raise Fault("settings_page")
        time.sleep(0.05)
    output = subprocess.run(
        [str(executable), "--ConfigPath", str(config), "--SettingsUrl"], env=env,
        check=False, capture_output=True, text=True, timeout=8,
    )
    if output.returncode:
        raise Fault("settings_url_cli")
    link = urlsplit(output.stdout.strip())
    code = link.fragment.removeprefix("login=")
    if (f"{link.scheme}://{link.netloc}" != origin or link.path != "/settings/" or link.query
            or link.fragment != "login=" + code or not re.fullmatch(r"[a-f0-9]{64}", code)):
        raise Fault("unexpected_settings_url")

    status, headers, asset = exchange("GET", "/settings/assets/app.js")
    if status != 200 or not asset:
        raise Fault("settings_js_asset")
    status, _, _ = exchange("GET", base + "/settings")
    if status != 401:
        raise Fault("unauthenticated_settings_read")
    status, headers, session_data = exchange(
        "POST", base + "/session", {"code": code},
        {"Origin": origin, "Content-Type": "application/json"},
    )
    cookie = headers.get("Set-Cookie", "")
    if status != 200 or "HttpOnly" not in cookie or "SameSite=Strict" not in cookie:
        raise Fault("settings_session")
    session = json.loads(session_data)
    status, _, settings_data = exchange("GET", base + "/settings", headers={"Cookie": cookie.split(";", 1)[0]})
    if status != 200:
        raise Fault("read_settings")
    settings = json.loads(settings_data)
    if settings["search"]["provider"] != provider:
        raise Fault("settings_provider")
    write_headers = {"Origin": origin, "Content-Type": "application/json", "Cookie": cookie.split(";", 1)[0],
                     "X-QCWS-CSRF": session["csrf"]}
    if persist_in is not None:
        status, _, _ = exchange("PUT", base + "/settings",
                                {"revision": settings["revision"], "patch": {"search_without_asking": True}},
                                write_headers)
        if status != 200 or not json.loads((persist_in / "settings.json").read_bytes())["search_without_asking"]:
            raise Fault("persist_settings")
    status, _, _ = exchange("DELETE", base + "/session", {}, write_headers)
    if status != 200:
        raise Fault("settings_logout")
    return {"passed": True, "provider": provider}
