"""Exercise CLI credential enrollment and the public listener contract without an upstream."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time

from test_admin_http import ports, request


HOST = "search.example.test"
ORIGIN = "https://chat.example.test"
PROTOCOL_VERSION = "2025-06-18"
PUBLIC_PATH = re.compile(r"^/(mcp|v1/(status|search|open|find))$")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def generated_token(result):
    require(result.returncode == 0 and re.fullmatch(r"[a-f0-9]{64}\n", result.stdout),
            "Successful generation must return exactly one raw token line")
    token = result.stdout.rstrip("\n")
    require(token not in result.stderr, "The generated token appeared on stderr")
    return token


def runtime_access(path):
    if os.name == "nt":
        icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
        return subprocess.check_output([icacls, path], timeout=5)
    metadata = path.stat()
    return metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)


class BusinessProxy(BaseHTTPRequestHandler):
    def forward(self):
        if not PUBLIC_PATH.fullmatch(self.path.partition("?")[0]):
            self.send_error(404)
            return
        payload = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or None
        hop_headers = {"connection", "keep-alive", "transfer-encoding"}
        forwarded = {key: value for key, value in self.headers.items() if key.lower() not in hop_headers}
        port = self.server.upstream_port
        context = self.server.origin_tls
        connection = (HTTPSConnection("127.0.0.1", port, timeout=3, context=context) if context
                      else HTTPConnection("127.0.0.1", port, timeout=3))
        with closing(connection) as upstream:
            upstream.request(self.command, self.path, body=payload, headers=forwarded)
            response = upstream.getresponse()
            content = response.read()
            self.send_response_only(response.status)
            for key, value in response.getheaders():
                if key.lower() not in hop_headers | {"content-length"}:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)

    do_GET = do_POST = do_DELETE = do_OPTIONS = forward

    def log_message(self, *_args):
        pass


class Fixture:
    def __init__(self, executable, root):
        self.executable = executable
        self.runtime = root / "runtime.json"
        self.base = [executable, "--ConfigPath", str(self.runtime)]
        self.business, self.control, admin = ports()
        self.environment_token = secrets.token_hex(32)
        self.env = dict(os.environ, QCWS_TEST_ENROLLMENT_TOKEN=self.environment_token)
        require(self.command("--InitializeConfig").returncode == 0, "Fixture initialization failed")
        self.file_token = (root / "client.token").read_text(encoding="ascii").strip()
        config = self.config()
        config["business"].update(port=self.business, allowed_hosts=[HOST], origins=[ORIGIN])
        config["control"].update(port=self.control, allowed_hosts=[f"127.0.0.1:{self.control}"])
        config["admin"] = {"enabled": True, "port": admin}
        config["clients"].append({"id": "legacy-env", "token_env": "QCWS_TEST_ENROLLMENT_TOKEN"})
        self.write(config)
        if os.name != "nt":
            self.runtime.chmod(0o640)

    def config(self):
        return json.loads(self.runtime.read_text(encoding="utf-8"))

    @property
    def registry(self):
        directory = Path(self.config().get("settings", {}).get("directory", "state"))
        if not directory.is_absolute():
            directory = self.runtime.parent / directory
        return directory / "client-registry.json"

    def registry_config(self):
        return json.loads(self.registry.read_text(encoding="utf-8"))

    def write_registry(self, value):
        self.registry.write_text(json.dumps(value), encoding="utf-8")

    def write(self, config):
        self.runtime.write_text(json.dumps(config), encoding="utf-8")

    def command(self, *options, user_input="", configured=True):
        base = self.base if configured else [self.executable]
        return subprocess.run([*base, *options], input=user_input, capture_output=True,
                              text=True, encoding="utf-8", env=self.env, timeout=8)

    def public(self, method, path, token=None, headers=None, body=None):
        forwarded = {"Host": HOST, "Origin": ORIGIN}
        if body is not None:
            forwarded["Content-Type"] = "application/json"
        if token is not None:
            forwarded["Authorization"] = f"Bearer {token}"
        return request(self.proxy_port, method, path, forwarded | (headers or {}), body)

    @contextmanager
    def running(self, origin_tls=None):
        windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with (
            ThreadingHTTPServer(("127.0.0.1", 0), BusinessProxy) as proxy,
            subprocess.Popen(self.base, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **windows) as service,
        ):
            proxy.upstream_port = self.business
            proxy.origin_tls = origin_tls
            self.proxy_port = proxy.server_port
            proxy_thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.05})
            try:
                proxy_thread.start()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    require(service.poll() is None, "The temporary service failed to start")
                    try:
                        status = request(self.control, "GET", "/health/live",
                                         {"Authorization": f"Bearer {self.file_token}"})[0]
                        if status == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("The temporary service did not become ready")
                yield
            finally:
                if proxy_thread.ident is not None:
                    proxy.shutdown()
                    proxy_thread.join()
                if service.poll() is None:
                    service.terminate()
                    try:
                        service.wait(5)
                    except subprocess.TimeoutExpired:
                        service.kill()
                        service.wait(5)


def verify_draft(fixture):
    generated_token(fixture.command("--GenerateToken", configured=False))
    result = fixture.command("--GenerateToken", "--ClientId", "extra", configured=False)
    require(result.returncode != 0 and not result.stdout, "A client ID requires a config path")

    config = fixture.config()
    unset_env = "QCWS_TEST_MISSING_ENROLLMENT_TOKEN"
    fixture.env.pop(unset_env, None)
    config["clients"][0] = {"id": "local", "token_env": unset_env}
    fixture.write(config)
    runtime = fixture.runtime.read_bytes()
    require(fixture.command("--CheckConfig").returncode != 0, "The default token environment variable was unset")
    token = generated_token(fixture.command("--GenerateToken", user_input="y\n"))
    clients = [dict(entry) for entry in config["clients"]]
    clients[0] = {"id": "local", "token_sha256": digest(token), "token_value": token}
    require(fixture.registry_config() == {"schema_version": 1, "clients": clients} and
            fixture.runtime.read_bytes() == runtime and fixture.command("--CheckConfig").returncode == 0,
            "Default enrollment failed to replace the unset environment source")

    fixture.write_registry({"schema_version": 1, "clients": []})
    original = fixture.registry.read_bytes()
    require(fixture.command("--CheckConfig").returncode == 0,
            "An empty authoritative registry should remain valid without reviving runtime clients")
    for answer in ("", "\n", " n \n"):
        result = fixture.command("--GenerateToken", user_input=answer)
        require(result.returncode == 0 and not result.stdout and result.stderr.strip(),
                "Declining generation must only prompt on stderr")
        require(fixture.registry.read_bytes() == original and fixture.runtime.read_bytes() == runtime,
                "Declining generation modified the registry or runtime")

    token = generated_token(fixture.command("--GenerateToken", user_input="\ufeff YeS \n"))
    enrolled = fixture.registry_config()
    require(enrolled["schema_version"] == 1 and len(enrolled["clients"]) == 1,
            "Generation restored an unrelated runtime client")
    entry = enrolled["clients"][0]
    generation = entry.get("generation", "")
    require(re.fullmatch(r"[a-f0-9]{32}", generation) and
            entry == {"id": "local", "token_sha256": digest(token), "token_value": token,
                      "generation": generation},
            "Generation did not provision one recoverable credential with a new internal identity")
    require(fixture.command("--CheckConfig").returncode == 0, "Provisioned runtime failed validation")
    committed = fixture.registry.read_bytes()
    result = fixture.command("--GenerateToken", "--ClientId", "invalid/client", user_input="yes\n")
    require(result.returncode != 0 and not result.stdout and fixture.registry.read_bytes() == committed and
            fixture.runtime.read_bytes() == runtime,
            "Failed generation published a token or changed persistent configuration")

    for invalid in ({"token_sha256": "G" * 64}, {"token_env": "QCWS_TEST_ENROLLMENT_TOKEN"},
                    {"token_value": "different-valid-value"}, {"token_value": "line\nbreak"}):
        fixture.write_registry(enrolled | {"clients": [entry | invalid]})
        require(fixture.command("--CheckConfig").returncode != 0,
                "A malformed, mismatched or conflicting credential source passed validation")
    fixture.write_registry(enrolled | {"clients": [{"id": "local", "token_value": token,
                                                     "token_file": "client.token"}]})
    require(fixture.command("--CheckConfig").returncode != 0,
            "A recoverable value without its authentication digest passed validation")

    config["clients"][0] = {"id": "local", "token_file": "client.token"}
    fixture.write(config)
    fixture.write_registry(enrolled)
    require(fixture.command("--CheckConfig").returncode == 0, "The corruption fixture must start valid")
    fixture.registry.write_text("{", encoding="utf-8")
    require(fixture.command("--CheckConfig").returncode != 0,
            "A corrupt authoritative registry fell back to runtime credentials")


def verify_business(fixture, token):
    status, response_headers, value = fixture.public("GET", "/v1/status", token)
    require(status == 200 and value["contract_version"] == 1, "Bearer REST status failed")
    require(response_headers.get("Access-Control-Allow-Origin") == ORIGIN, "Allowed Origin was not echoed")
    for absent_or_invalid in (None, secrets.token_hex(32), digest(token)):
        status, response_headers, _ = fixture.public("GET", "/v1/status", absent_or_invalid)
        require(status == 401 and response_headers.get("WWW-Authenticate") == "Bearer",
                "The listener accepted a missing or invalid credential")
    require(fixture.public("GET", "/v1/status", token, {"Host": "wrong.example.test"})[0] == 403,
            "The business listener accepted an unconfigured public Host")
    require(fixture.public("GET", "/v1/status", token, {"Origin": "https://wrong.example.test"})[0] == 403,
            "The business listener accepted an unconfigured Origin")
    for path in ("/health/live", "/health/ready", "/metrics", "/settings/", "/admin/api/v1/settings", "/mcp/"):
        require(fixture.public("GET", path, token)[0] == 404, "The proxy exposed a path outside the business allowlist")
    for path in ("/v1/search", "/v1/open", "/v1/find"):
        status, _, value = fixture.public("POST", path, token, body={"unexpected": True})
        require(status == 400 and value["error"]["code"] == "invalid_argument",
                "The forwarded REST business route did not reach tool validation")

    status, response_headers, _ = fixture.public("OPTIONS", "/mcp", headers={
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,mcp-session-id,mcp-protocol-version",
    })
    allowed = {item.strip().lower() for item in response_headers.get("Access-Control-Allow-Headers", "").split(",")}
    require(status == 204 and response_headers.get("Access-Control-Allow-Origin") == ORIGIN and
            {"authorization", "content-type", "mcp-session-id", "mcp-protocol-version"} <= allowed,
            "Browser MCP preflight failed")

    headers = {"Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "proxy-fixture", "version": "1"},
    }}
    require(fixture.public("POST", "/mcp", headers=headers, body=init)[0] == 401,
            "MCP initialization did not require a Bearer credential")
    status, response_headers, value = fixture.public("POST", "/mcp", token, headers, init)
    session = response_headers.get("Mcp-Session-Id")
    require(status == 200 and session and value["result"]["protocolVersion"] == PROTOCOL_VERSION and
            response_headers.get("Access-Control-Expose-Headers", "").lower() == "mcp-session-id",
            "Browser MCP negotiation did not expose a valid session")
    headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL_VERSION})
    ready = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    require(fixture.public("POST", "/mcp", token, headers, ready)[0] == 202,
            "MCP initialization notification failed")
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    require(fixture.public("POST", "/mcp", fixture.file_token, headers, tools_list)[0] == 404,
            "A second client could use the first client's session")
    status, _, value = fixture.public("POST", "/mcp", token, headers, tools_list)
    require(status == 200 and {tool["name"] for tool in value["result"]["tools"]} == {
        "web_search", "web_open", "web_find", "web_search_status",
    }, "Browser MCP tool discovery failed")
    status, _, value = fixture.public("POST", "/mcp", token, headers, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "web_search_status", "arguments": {}},
    })
    require(status == 200 and value["result"]["structuredContent"]["contract_version"] == 1,
            "Browser MCP status call failed")
    require(fixture.public("DELETE", "/mcp", token, headers)[0] == 204, "MCP session cleanup failed")


def verify_rotation(fixture):
    access = runtime_access(fixture.runtime)
    original = fixture.config()
    runtime = fixture.runtime.read_bytes()
    token = generated_token(fixture.command("--GenerateToken", "--ClientId", "browser", user_input="y\n"))
    enrolled = fixture.registry_config()
    browser = next(entry for entry in enrolled["clients"] if entry["id"] == "browser")
    generation = browser.get("generation", "")
    require(re.fullmatch(r"[a-f0-9]{32}", generation) and
            browser == {"id": "browser", "token_sha256": digest(token), "token_value": token,
                        "generation": generation},
            "New enrollment needs a persistent internal identity")
    require(enrolled == {"schema_version": 1, "clients": original["clients"] + [browser]} and
            fixture.runtime.read_bytes() == runtime, "Named enrollment changed unrelated configuration")
    snapshot = fixture.registry.read_bytes()
    registry_access = runtime_access(fixture.registry)
    require(browser["token_value"] == token and token.encode("ascii") not in runtime,
            "The generated credential was not saved only in its protected registry")

    replacement = "qcopilots-websearch/xbyang"
    for answer in ("", "\n", "no\n"):
        result = fixture.command("--SetToken", "--ClientId", "browser", user_input=replacement + "\n" + answer)
        require(result.returncode == 0 and not result.stdout and result.stderr.strip() and
                replacement not in result.stderr, "Declining rotation must only prompt on stderr")
        require(fixture.registry.read_bytes() == snapshot and fixture.runtime.read_bytes() == runtime,
                "Declining rotation modified persistent configuration")

    for invalid in (fixture.environment_token, "", "x y", "x\ty", "x\x00y", "授权码", "x" * 8193):
        result = fixture.command("--SetToken", "--ClientId", "browser", user_input=invalid + "\nyes\n")
        require(result.returncode != 0 and not result.stdout and (not invalid or invalid not in result.stderr),
                "Invalid or duplicate credentials were accepted or exposed in output")
        require(fixture.registry.read_bytes() == snapshot and fixture.runtime.read_bytes() == runtime,
                "An unsuccessful rotation modified persistent configuration")

    with fixture.running():
        verify_business(fixture, token)
        require(fixture.public("GET", "/v1/status", fixture.environment_token)[0] == 200,
                "An environment credential stopped working alongside hash credentials")
        result = fixture.command("--SetToken", "--ClientId", "browser", user_input="\ufeff" + replacement + "\n Y \n")
        require(result.returncode == 0 and replacement not in result.stdout + result.stderr,
                "Accepted rotation failed or exposed the operator's token")
        require(fixture.public("GET", "/v1/status", token)[0] == 200 and
                fixture.public("GET", "/v1/status", replacement)[0] == 401,
                "Rotation changed active credentials before restart")

    browser["token_sha256"] = digest(replacement)
    browser["token_value"] = replacement
    require(fixture.registry_config() == enrolled and fixture.runtime.read_bytes() == runtime,
            "Rotation changed unrelated configuration or the client's internal identity")
    require(replacement.encode("ascii") not in fixture.runtime.read_bytes(),
            "Rotation exposed a raw credential in the runtime configuration")
    require(runtime_access(fixture.runtime) == access, "Rotation changed runtime-file permissions")
    require(runtime_access(fixture.registry) == registry_access, "Rotation changed registry-file permissions")
    with fixture.running():
        require(fixture.public("GET", "/v1/status", token)[0] == 401 and
                fixture.public("GET", "/v1/status", replacement)[0] == 200,
                "Restart did not apply the credential rotation")

    custom_tokens = {"short-one": "a", "short-name": "xbyang", "short-project": "qcopilots-websearch",
                     "longest": "t" * 8192}
    for client_id, custom_token in custom_tokens.items():
        result = fixture.command("--SetToken", "--ClientId", client_id, user_input=custom_token + "\nyes\n")
        require(result.returncode == 0 and not result.stdout and custom_token not in result.stderr.splitlines(),
                "A custom credential was rejected or exposed in output")
    with fixture.running():
        for custom_token in custom_tokens.values():
            require(fixture.public("GET", "/v1/status", custom_token)[0] == 200,
                    "A valid custom credential could not authenticate after restart")


def verify_concurrent_enrollment(fixture):
    gate = threading.Barrier(2, timeout=3)

    def enroll(client):
        gate.wait()
        token = generated_token(fixture.command("--GenerateToken", "--ClientId", client, user_input="yes\n"))
        return {"id": client, "token_sha256": digest(token), "token_value": token}

    original = fixture.registry_config()["clients"]
    runtime = fixture.runtime.read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        added = list(pool.map(enroll, ("concurrent-a", "concurrent-b")))
    actual = fixture.registry_config()["clients"]
    additions = {entry["id"]: entry for entry in actual if entry["id"] in {item["id"] for item in added}}
    require(len(additions) == len(added) and
            all(re.fullmatch(r"[a-f0-9]{32}", entry.get("generation", "")) for entry in additions.values()) and
            len({entry["generation"] for entry in additions.values()}) == len(added),
            "Concurrent new clients did not receive distinct persistent identities")
    expected = original + [item | {"generation": additions[item["id"]]["generation"]} for item in added]
    require(sorted(actual, key=lambda entry: entry["id"]) == sorted(expected, key=lambda entry: entry["id"]) and
            fixture.runtime.read_bytes() == runtime,
            "Concurrent enrollment lost or replaced a client")
    require(fixture.command("--CheckConfig").returncode == 0, "Concurrent enrollment left an invalid runtime")


def verify_release_config(executable, folder):
    def command(path, *options, user_input=""):
        return subprocess.run([executable, "--ConfigPath", str(path), *options], input=user_input,
                              capture_output=True, text=True, encoding="utf-8", timeout=8)

    seed = folder / "portable" / "runtime.json"
    version_result = command(seed, "--Version")
    match = re.match(r"^QCopilots-WebSearch ([0-9]+\.[0-9]+\.[0-9]+),", version_result.stdout)
    require(version_result.returncode == 0 and match, "Native release version is unavailable")
    version = match[1]
    require(command(seed, "--InitializeConfig").returncode == 0, "Portable config initialization failed")
    value = json.loads(seed.read_text(encoding="utf-8"))
    require(value["project_version"] == version and value["schema_version"] == 1,
            "Initialized config should carry the native release and the independent schema version")

    peer_version = "999.999.999" if version != "999.999.999" else "1.2.3"
    peer = folder / f"qcopilots-websearch-{peer_version}" / "runtime.json"
    peer.parent.mkdir()
    peer.write_text(json.dumps(value | {"project_version": peer_version}), encoding="utf-8")
    peer_bytes = peer.read_bytes()
    result = command(peer, "--CheckConfig")
    require(result.returncode != 0 and "different project release" in result.stderr,
            "An executable accepted another release's config")
    result = command(peer, "--GenerateToken", user_input="yes\n")
    require(result.returncode != 0 and not result.stdout and peer.read_bytes() == peer_bytes,
            "The token command changed another release's config")

    managed = folder / f"qcopilots-websearch-{version}" / "runtime.json"
    state = folder / "separate-state"
    options = ("--InitializeConfig", "--SelectAvailablePorts", "--StateDirectory", str(state))
    require(command(managed, *options).returncode == 0, "Managed config initialization failed")
    actual = json.loads(managed.read_text(encoding="utf-8"))
    listeners = {actual[role]["port"] for role in ("business", "control", "admin")}
    known = {value["business"]["port"], value["control"]["port"], value.get("admin", {}).get("port", 8091)}
    require(len(listeners) == 3 and not (known & listeners), "An idle release's ports were reused")
    require(Path(actual["cache"]["directory"]) == state / "cache" and
            Path(actual["settings"]["directory"]) == state, "Managed state directory is wrong")
    managed_bytes = managed.read_bytes()
    require(command(managed, *options).returncode == 0 and managed.read_bytes() == managed_bytes,
            "Initializing the same release reset its configuration")
    require(command(managed, "--CheckConfig").returncode == 0, "Managed configuration is invalid")

    del actual["project_version"]
    managed.write_text(json.dumps(actual), encoding="utf-8")
    require(command(managed, "--CheckConfig").returncode != 0, "Managed config requires a release stamp")
    del value["project_version"]
    seed.write_text(json.dumps(value), encoding="utf-8")
    require(command(seed, "--CheckConfig").returncode == 0, "An explicit legacy portable config stopped working")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", required=True)
    executable = parser.parse_args().Executable
    with tempfile.TemporaryDirectory(prefix="qcws token enrollment ") as folder:
        verify_draft(Fixture(executable, Path(folder)))
    with tempfile.TemporaryDirectory(prefix="qcws token rotation ") as folder:
        fixture = Fixture(executable, Path(folder))
        verify_rotation(fixture)
        verify_concurrent_enrollment(fixture)
    with tempfile.TemporaryDirectory(prefix="qcws release configuration ") as folder:
        verify_release_config(executable, Path(folder))


if __name__ == "__main__":
    main()
