"""Verify self-contained MCP icons over HTTP and stdio, without an admin listener."""
import argparse
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

from test_admin_http import request


VERSIONS = ("2025-06-18", "2025-11-25")
WINDOWS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def initialize(version):
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "embedded-icon-test", "version": "1"}}}


def validate_svg(data):
    root = ET.fromstring(data)
    namespace = "{http://www.w3.org/2000/svg}"
    require(root.tag == namespace + "svg" and root.attrib.get("viewBox") == "0 0 64 64",
            "The canonical icon must be a scalable 64-unit SVG")
    allowed = {namespace + tag for tag in ("svg", "g", "rect", "circle", "ellipse", "path")}
    for element in root.iter():
        require(element.tag in allowed, "The icon contains non-geometry content")
        require(not element.text or not element.text.strip(), "The icon contains text or embedded content")
        for name, value in element.attrib.items():
            require(not name.lower().startswith("on") and name not in ("href", "style")
                    and "url(" not in value.lower() and "://" not in value,
                    "The icon contains an external or active resource")


def validate_response(reply, version, expected):
    require(reply.get("id") == 1 and "result" in reply, "MCP initialize did not return a result")
    result = reply["result"]
    require(result["protocolVersion"] == version, "Icon metadata changed protocol negotiation")
    info = result["serverInfo"]
    require(info["name"] == "qcopilots-websearch" and bool(info["version"]), "Server identity changed")
    icons = info.get("icons")
    require(isinstance(icons, list) and len(icons) == 1, "Initialize must advertise one server icon")
    icon = icons[0]
    require(set(icon) == {"src", "mimeType", "sizes"} and icon["mimeType"] == "image/svg+xml"
            and icon["sizes"] == ["any"], "The scalable icon descriptor is invalid")
    prefix = "data:image/svg+xml;base64,"
    require(icon["src"].startswith(prefix), "The icon must not require a second HTTP request")
    decoded = base64.b64decode(icon["src"][len(prefix):], validate=True)
    require(decoded == expected, "The executable's icon differs from the canonical SVG")
    validate_svg(decoded)
    return icon


def reserve_ports(stack, config):
    sockets = [stack.enter_context(socket.socket()) for _ in range(3)]
    for role, listener in zip(("business", "control", "admin"), sockets):
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config[role]["port"] = port
        if role != "admin":
            config[role]["allowed_hosts"] = [f"127.0.0.1:{port}"]
    return sockets


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def http_icons(executable, runtime, config, token, expected):
    with ExitStack() as stack:
        reservations = reserve_ports(stack, config)
        config["admin"]["enabled"] = False
        runtime.write_text(json.dumps(config), encoding="utf-8")
        # Keep the admin port occupied: initialize cannot depend on mounting its assets.
        for listener in reservations[:2]:
            listener.close()
        output = stack.enter_context(tempfile.TemporaryFile())
        process = subprocess.Popen([executable, "--ConfigPath", str(runtime)], cwd=runtime.parent,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output, **WINDOWS)
        try:
            port = config["business"]["port"]
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                       "Content-Type": "application/json"}
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                require(process.poll() is None, "The isolated HTTP fixture exited before becoming ready")
                try:
                    if request(port, "GET", "/v1/status", headers)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("The isolated HTTP fixture did not become ready")
            icons = []
            for version in VERSIONS:
                status, response, body = request(port, "POST", "/mcp", headers, initialize(version))
                require(status == 200, "HTTP initialize failed")
                session = response.get("Mcp-Session-Id")
                require(bool(session), "HTTP initialize omitted its session")
                try:
                    icons.append(validate_response(body, version, expected))
                finally:
                    status, _, body = request(port, "DELETE", "/mcp", headers | {
                        "Mcp-Session-Id": session, "MCP-Protocol-Version": version})
                    require(status == 204 and not body, "The icon test session did not close")
            return icons
        finally:
            stop(process)


def stdio_icon(executable, runtime, config, version, expected):
    with ExitStack() as stack:
        reserve_ports(stack, config)
        config["admin"]["enabled"] = True
        runtime.write_text(json.dumps(config), encoding="utf-8")
        errors = stack.enter_context(tempfile.TemporaryFile())
        process = subprocess.Popen([executable, "--ConfigPath", str(runtime), "--Stdio"], cwd=runtime.parent,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, **WINDOWS)
        response = queue.Queue()
        reader = threading.Thread(target=lambda: response.put(process.stdout.readline(1024 * 1024 + 1)),
                                  daemon=True)
        try:
            reader.start()
            process.stdin.write((json.dumps(initialize(version)) + "\n").encode("utf-8"))
            process.stdin.flush()
            try:
                line = response.get(timeout=8)
            except queue.Empty:
                raise AssertionError("Stdio initialize did not return within its deadline") from None
            require(line.endswith(b"\n") and len(line) <= 1024 * 1024, "Stdio emitted an invalid protocol line")
            icon = validate_response(json.loads(line), version, expected)
            process.stdin.close()
            require(process.wait(timeout=5) == 0, "Stdio did not exit normally after EOF")
            require(process.stdout.read() == b"", "Stdio emitted non-protocol output after its response")
            return icon
        finally:
            stop(process)
            reader.join(timeout=2)
            process.stdin.close()
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", required=True)
    parser.add_argument("--Icon", type=Path, required=True)
    args = parser.parse_args()
    executable = str(Path(args.Executable).resolve(strict=True))
    expected = args.Icon.read_bytes()
    validate_svg(expected)
    with tempfile.TemporaryDirectory(prefix="qcws-icon-") as temporary:
        runtime = Path(temporary) / "runtime.json"
        initialized = subprocess.run([executable, "--ConfigPath", str(runtime), "--InitializeConfig"],
                                     capture_output=True, timeout=10, **WINDOWS)
        require(initialized.returncode == 0, "The isolated configuration could not be initialized")
        config = json.loads(runtime.read_text(encoding="utf-8"))
        token = runtime.with_name("client.token").read_text(encoding="ascii").strip()
        icons = http_icons(executable, runtime, config, token, expected)
        icons.extend(stdio_icon(executable, runtime, config, version, expected) for version in VERSIONS)
        require(all(icon == icons[0] for icon in icons), "Transports or protocol versions advertised different icons")
    print("PASS self-contained SVG icons over HTTP and stdio for both MCP versions without an admin listener")


if __name__ == "__main__":
    main()
