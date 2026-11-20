"""Exercise live client management without an external search provider."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import threading
import time

from test_admin_http import ports, request


API = "/admin/api/v1"
PROTOCOL_VERSION = "2025-06-18"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class Fixture:
    def __init__(self, executable, root):
        self.runtime = root / "runtime.json"
        self.registry = root / "state" / "client-registry.json"
        self.base = [executable, "--ConfigPath", str(self.runtime)]
        self.business, self.control, self.admin = ports()
        self.environment_token = secrets.token_hex(32)
        self.env = dict(os.environ, QCWS_REGISTRY_TEST_TOKEN=self.environment_token)
        require(self.command("--InitializeConfig").returncode == 0, "Initialization failed")
        self.file_token = (root / "client.token").read_text(encoding="ascii").strip()
        self.admin_token = (root / "admin.token").read_text(encoding="ascii").strip()
        config = json.loads(self.runtime.read_text(encoding="utf-8"))
        for label, port in (("business", self.business), ("control", self.control)):
            config[label].update(port=port, allowed_hosts=[f"127.0.0.1:{port}"])
        config["admin"] = {"enabled": True, "port": self.admin}
        config["clients"].append({"id": "legacy-env", "token_env": "QCWS_REGISTRY_TEST_TOKEN"})
        self.runtime.write_text(json.dumps(config), encoding="utf-8")
        self.runtime_bytes = self.runtime.read_bytes()
        self.runtime_mode = stat.S_IMODE(self.runtime.stat().st_mode)
        # Installed runtime configuration can be read-only while state stays writable.
        self.runtime.chmod(stat.S_IREAD if os.name == "nt" else 0o400)
        self.read_headers = {}
        self.write_headers = {}

    def close(self):
        self.runtime.chmod(self.runtime_mode)

    def command(self, *options, user_input=""):
        windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        return subprocess.run([*self.base, *options], input=user_input, capture_output=True,
                              text=True, encoding="utf-8", env=self.env, timeout=10, **windows)

    def unchanged_runtime(self):
        require(self.runtime.read_bytes() == self.runtime_bytes,
                "Client management modified the installed runtime configuration")

    def public(self, token, path="/v1/status", method="GET", body=None, extra=None, control=False):
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return request(self.control if control else self.business, method, path,
                       headers | (extra or {}), body)

    def login(self):
        headers = {"Origin": f"http://127.0.0.1:{self.admin}", "Content-Type": "application/json"}
        status, response, body = request(self.admin, "POST", API + "/session", headers,
                                         {"token": self.admin_token})
        require(status == 200 and "csrf" in body, "The administrator could not sign in")
        self.read_headers = {"Cookie": response["Set-Cookie"].split(";", 1)[0]}
        self.write_headers = headers | self.read_headers | {"X-QCWS-CSRF": body["csrf"]}

    def admin_request(self, method="GET", suffix="", body=None):
        return request(self.admin, method, API + "/clients" + suffix,
                       self.read_headers if method == "GET" else self.write_headers, body)

    def view(self):
        status, _, view = self.admin_request()
        require(status == 200, "Client list is unavailable")
        self.validate_view(view)
        return view

    @staticmethod
    def validate_view(view):
        require(isinstance(view, dict) and set(view) == {"revision", "clients", "pending_restart"},
                "Client view exposed unexpected fields")
        require(isinstance(view["revision"], str) and bool(view["revision"]), "Missing client revision")
        require(isinstance(view["pending_restart"], bool) and isinstance(view["clients"], list),
                "Invalid client list metadata")
        for client in view["clients"]:
            require(set(client) == {"id", "source"} and
                    client["source"] in ("manual", "file", "environment"),
                    "Client view exposed credential or internal identity data")
        require(len({entry["id"] for entry in view["clients"]}) == len(view["clients"]),
                "Client list contains duplicate identities")

    def change(self, method, suffix="", fields=None):
        status, _, view = self.admin_request(method, suffix, {"revision": self.view()["revision"]} | (fields or {}))
        require(status == 200, "Client change failed")
        self.validate_view(view)
        require(not view["pending_restart"], "A web change did not apply the complete registry")
        self.unchanged_runtime()
        return view

    @contextmanager
    def running(self):
        windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with subprocess.Popen(self.base, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **windows) as process:
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    require(process.poll() is None, "The temporary service failed to start")
                    try:
                        if request(self.admin, "GET", "/settings/")[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("The temporary administration listener did not become ready")
                self.login()
                yield
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(5)

    def mcp(self, token, message, session=None):
        headers = {"Accept": "application/json, text/event-stream"}
        if session:
            headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL_VERSION})
        return self.public(token, "/mcp", "POST", message, headers)

    def new_session(self, token):
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "client-registry-test", "version": "1"},
        }}
        status, headers, body = self.mcp(token, message)
        session = headers.get("Mcp-Session-Id")
        require(status == 200 and session and body.get("result", {}).get("protocolVersion") == PROTOCOL_VERSION,
                "Client could not create an MCP session")
        require(self.mcp(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)[0] == 202,
                "Client could not initialize its MCP session")
        return session


def require_sources(view, expected):
    require({entry["id"]: entry["source"] for entry in view["clients"]} == expected,
            "Registry lost a client or changed a credential source")


def verify_body_id_routes(fixture):
    # Browser fetch normalizes dot path segments, so these legal IDs must stay in JSON.
    for client_id in (".", ".."):
        token = f"dot/{len(client_id)}/initial"
        replacement = f"dot/{len(client_id)}/rotated"
        before = fixture.view()["revision"]
        added = fixture.change("POST", fields={"id": client_id, "token": token})
        require(fixture.public(token)[0] == 200, "A dot client could not authenticate after creation")
        require(fixture.admin_request("PUT", body={"revision": before, "id": client_id,
                "token": replacement})[0] == 409, "Body-ID replacement ignored a stale revision")
        for method, body in (
            ("PUT", {"revision": added["revision"], "token": replacement}),
            ("DELETE", {"revision": added["revision"]}),
            ("PUT", {"revision": added["revision"], "id": [], "token": replacement}),
            ("DELETE", {"revision": added["revision"], "id": []}),
        ):
            require(fixture.admin_request(method, body=body)[0] == 400,
                    "A body-ID route accepted a missing or invalid client ID")
        updated = fixture.change("PUT", fields={"id": client_id, "token": replacement})
        require(fixture.public(token)[0] == 401 and fixture.public(replacement)[0] == 200,
                "Body-ID replacement did not rotate a dot client's credential immediately")
        require(fixture.admin_request("DELETE", body={"revision": added["revision"],
                "id": client_id})[0] == 409, "Body-ID deletion ignored a stale revision")
        require(fixture.view()["revision"] == updated["revision"] and fixture.public(replacement)[0] == 200,
                "Rejected body-ID changes altered the active registry")
        fixture.change("DELETE", fields={"id": client_id})
        require(fixture.public(replacement)[0] == 401, "Body-ID deletion left a dot client's credential active")
        for method, fields in (("PUT", {"token": replacement}), ("DELETE", {})):
            require(fixture.admin_request(method, body={"revision": fixture.view()["revision"],
                    "id": client_id} | fields)[0] == 404, "A missing body-ID client did not return 404")


def verify_crud(fixture):
    manual = "xbyang/qcopilots-websearch"
    replacement = "rotated/" + secrets.token_hex(16)
    initial = {"local": "file", "legacy-env": "environment"}
    with fixture.running():
        view = fixture.view()
        require_sources(view, initial)
        require(not view["pending_restart"], "Inherited credentials unexpectedly need a restart")
        require(request(fixture.admin, "GET", API + "/clients")[0] == 401,
                "Client list did not require an administrator session")
        body = {"revision": view["revision"], "id": "browser", "token": manual}
        require(request(fixture.admin, "POST", API + "/clients",
                        {key: value for key, value in fixture.write_headers.items() if key != "X-QCWS-CSRF"},
                        body)[0] == 403, "Client changes did not require CSRF protection")
        require(request(fixture.admin, "POST", API + "/clients",
                        fixture.write_headers | {"Origin": "http://foreign.invalid"}, body)[0] == 403,
                "A foreign origin could change client credentials")
        added = fixture.change("POST", fields={"id": "browser", "token": manual})
        require_sources(added, initial | {"browser": "manual"})
        require(fixture.registry.is_file(), "The first change did not create the client registry")
        require(fixture.public(manual)[0] == 200, "A short custom token was not immediately usable")
        for token in (fixture.file_token, fixture.environment_token):
            require(fixture.public(token)[0] == 200, "Migration broke an inherited credential")
        require(fixture.admin_request("POST", body=body)[0] == 409,
                "A stale revision was allowed to overwrite the registry")
        current = added["revision"]
        for method, suffix, fields, expected in (
            ("POST", "", {"id": "browser", "token": replacement}, 400),
            ("POST", "", {"id": "duplicate-token", "token": manual}, 400),
            ("PUT", "/absent", {"token": replacement}, 404),
            ("DELETE", "/absent", {}, 404),
        ):
            result = fixture.admin_request(method, suffix, {"revision": current} | fields)
            require(result[0] == expected, "Client identity conflict returned the wrong status")
            require(manual not in json.dumps(result[2]) and replacement not in json.dumps(result[2]),
                    "An error response exposed a client credential")
        for invalid in ("", "contains space", "line\nbreak", "tab\tvalue", "\x00", "\x7f", "非ASCII", "a" * 8193):
            status, _, _ = fixture.admin_request("PUT", "/browser", {"revision": current, "token": invalid})
            require(status == 400, "An invalid business credential was accepted")
        require(fixture.view()["revision"] == current, "Rejected changes modified the registry")

        session = fixture.new_session(manual)
        fixture.change("PUT", "/browser", {"token": replacement})
        require(fixture.public(manual)[0] == 401 and fixture.public(replacement)[0] == 200,
                "Token rotation did not apply immediately")
        status, _, reply = fixture.mcp(replacement, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        require(status == 200 and bool(reply.get("result", {}).get("tools")),
                "Token rotation discarded the existing client's MCP identity")
        fixture.change("DELETE", "/browser")
        require(fixture.public(replacement)[0] == 401, "A deleted credential remained active")
        fixture.change("POST", fields={"id": "browser", "token": replacement})
        require(fixture.public(replacement)[0] == 200, "A recreated client could not authenticate")
        require(fixture.mcp(replacement, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, session)[0] == 404,
                "Recreating a client inherited the deleted client's MCP session")
        require(fixture.new_session(replacement) != session, "A recreated client reused a retired session ID")

        fixture.change("POST", fields={"id": "one-character", "token": "/"})
        require(fixture.public("/")[0] == 200, "A one-character visible ASCII credential was rejected")
        fixture.change("PUT", "/one-character", {"token": "a" * 8192})
        require(fixture.public("a" * 8192)[0] == 200, "The documented maximum credential length was rejected")
        fixture.change("DELETE", "/one-character")
        encoded = fixture.registry.read_bytes()
        require(manual.encode() not in encoded and replacement.encode() not in encoded,
                "The registry stored a manual credential in plaintext")
        response = json.dumps(fixture.view())
        require(digest(manual) not in response and digest(replacement) not in response,
                "The client view exposed a credential digest")
        # Existing path-based PUT and DELETE calls above remain compatibility coverage.
        verify_body_id_routes(fixture)

    with fixture.running():
        require_sources(fixture.view(), initial | {"browser": "manual"})
        for token in (replacement, fixture.file_token, fixture.environment_token):
            require(fixture.public(token)[0] == 200, "A registry credential did not survive restart")
        require(fixture.public(manual)[0] == 401, "Restart restored an obsolete credential")
        fixture.unchanged_runtime()


def verify_cli_pending(fixture):
    tokens = {name: secrets.token_hex(24) for name in ("concurrent-a", "concurrent-b")}
    replacement = secrets.token_hex(24)
    with fixture.running():
        previous = fixture.view()
        gate = threading.Barrier(2, timeout=5)

        def enroll(item):
            name, token = item
            gate.wait()
            result = fixture.command("--SetToken", "--ClientId", name, user_input=token + "\nyes\n")
            require(result.returncode == 0 and not result.stdout and token not in result.stderr,
                    "CLI enrollment failed or exposed a manual token")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(enroll, tokens.items()))
        pending = fixture.view()
        require_sources(pending, {"local": "file", "legacy-env": "environment"} |
                        {name: "manual" for name in tokens})
        require(pending["pending_restart"], "CLI changes were not reported as pending")
        for token in tokens.values():
            require(fixture.public(token)[0] == 401, "CLI enrollment changed the active registry before apply")
        require(fixture.admin_request("POST", body={"revision": previous["revision"],
                "id": "stale", "token": "stale/input"})[0] == 409,
                "A stale web update overwrote concurrent CLI enrollment")
        fixture.change("POST", fields={"id": "web-apply", "token": "web/apply"})
        for token in tokens.values():
            require(fixture.public(token)[0] == 200, "Web apply omitted pending CLI changes")
        result = fixture.command("--SetToken", "--ClientId", "concurrent-a", user_input=replacement + "\ny\n")
        require(result.returncode == 0 and replacement not in result.stdout + result.stderr,
                "CLI rotation failed or exposed a manual token")
        require(fixture.view()["pending_restart"] and fixture.public(tokens["concurrent-a"])[0] == 200 and
                fixture.public(replacement)[0] == 401, "CLI rotation did not remain pending until restart")
        encoded = fixture.registry.read_bytes()
        require(all(token.encode() not in encoded for token in [*tokens.values(), replacement]),
                "CLI enrollment stored a manual credential in plaintext")
        fixture.unchanged_runtime()

    with fixture.running():
        require(not fixture.view()["pending_restart"], "Restart did not consume pending CLI changes")
        require(fixture.public(replacement)[0] == 200 and fixture.public(tokens["concurrent-a"])[0] == 401 and
                fixture.public(tokens["concurrent-b"])[0] == 200,
                "Restart did not apply the CLI registry without losing another client")
        fixture.unchanged_runtime()


def verify_zero_clients(fixture):
    with fixture.running():
        for client in fixture.view()["clients"]:
            fixture.change("DELETE", "/" + client["id"])
        require(fixture.view()["clients"] == [], "Deleting the last client was rejected")
        for token in (fixture.file_token, fixture.environment_token):
            require(fixture.public(token)[0] == 401 and
                    fixture.public(token, "/health/live", control=True)[0] == 401,
                    "An empty registry restored runtime credentials or disabled authentication")
        fixture.login()
        require(fixture.view()["clients"] == [], "Zero clients prevented administration")
        require(fixture.command("--CheckConfig").returncode == 0, "An empty registry failed configuration validation")

    fixture.env.pop("QCWS_REGISTRY_TEST_TOKEN")
    (fixture.runtime.parent / "client.token").unlink()
    require(fixture.command("--CheckConfig").returncode == 0,
            "An authoritative registry still required retired runtime credential sources")
    committed = fixture.registry.read_bytes()
    try:
        fixture.registry.write_bytes(b'{"schema_version":1,"clients":"invalid"}')
        require(fixture.command("--CheckConfig").returncode != 0,
                "An invalid authoritative registry silently fell back to runtime credentials")
    finally:
        fixture.registry.write_bytes(committed)

    with fixture.running():
        require(fixture.view()["clients"] == [] and fixture.public(fixture.file_token)[0] == 401,
                "Restart restored runtime clients after deleting the authoritative registry's last client")
        fixture.change("POST", fields={"id": "recovered", "token": "recover/access"})
        require(fixture.public("recover/access")[0] == 200, "The administrator could not recover an empty registry")
        fixture.unchanged_runtime()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", required=True)
    executable = parser.parse_args().Executable
    for verify in (verify_crud, verify_cli_pending, verify_zero_clients):
        with tempfile.TemporaryDirectory(prefix="qcws client registry ") as folder:
            fixture = Fixture(executable, Path(folder))
            try:
                verify(fixture)
            finally:
                fixture.close()


if __name__ == "__main__":
    main()
