"""Verify self-contained MCP icons over HTTP and stdio, without an admin listener."""
import argparse
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from mcp_http_fixture import mcp_probe, request
from test_stdio_transport import LEGACY_STDIO_VERSION, VERSIONS, check_tool_wire, initialize, stdio_peer


WINDOWS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


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


def http_tool_wire(port, headers, version):
    def exchange(message):
        status, _, body = request(port, "POST", "/mcp", headers, message)
        if "id" not in message:
            require(status == 202 and not body, "HTTP notification did not return an empty 202")
            return None
        require(status == 200 and body.get("jsonrpc") == "2.0" and body.get("id") == message["id"],
                "HTTP reply did not match its request")
        return body

    check_tool_wire(exchange, version)


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
                    if mcp_probe(port, headers)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("The isolated HTTP fixture did not become ready")
            icons = []

            def close_session(session_headers):
                status, _, body = request(port, "DELETE", "/mcp", session_headers)
                require(status == 204 and not body, "The HTTP test session did not close")

            with ExitStack() as clients:
                sessions = []
                for requested in (*VERSIONS, LEGACY_STDIO_VERSION):
                    status, response, body = request(port, "POST", "/mcp", headers, initialize(requested))
                    require(status == 200, "HTTP initialize failed")
                    session = response.get("Mcp-Session-Id")
                    require(bool(session), "HTTP initialize omitted its session")
                    # A 2024 body without a version header negotiates up on HTTP.
                    version = "2025-11-25" if requested == LEGACY_STDIO_VERSION else requested
                    session_headers = headers | {"Mcp-Session-Id": session, "MCP-Protocol-Version": version}
                    clients.callback(close_session, session_headers)
                    icons.append(validate_response(body, version, expected))
                    sessions.append((session_headers, version))
                for session_headers, version in sessions:
                    http_tool_wire(port, session_headers, version)
                status, response, _ = request(port, "POST", "/mcp",
                                              headers | {"MCP-Protocol-Version": LEGACY_STDIO_VERSION},
                                              initialize(LEGACY_STDIO_VERSION))
                require(status == 400 and "Mcp-Session-Id" not in response,
                        "HTTP accepted the 2024 stdio-only protocol header")
            return icons
        finally:
            stop(process)


def stdio_icon(executable, runtime, config, version, expected):
    with ExitStack() as stack:
        reserve_ports(stack, config)
        config["admin"]["enabled"] = True
        runtime.write_text(json.dumps(config), encoding="utf-8")
        with stdio_peer(executable, runtime) as exchange:
            return validate_response(exchange(initialize(version)), version, expected)


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
    print("PASS self-contained SVG icons for both 2025 versions, HTTP tool results and version negotiation")


if __name__ == "__main__":
    main()
