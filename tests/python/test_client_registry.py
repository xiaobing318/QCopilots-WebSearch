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

from mcp_http_fixture import mcp_probe, mcp_request, ports, request, start_session


API = "/admin/api/v1"


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
        config["clients"].append({"id": "environment-client", "token_env": "QCWS_REGISTRY_TEST_TOKEN"})
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

    def probe(self, token):
        return mcp_probe(self.business, {"Authorization": f"Bearer {token}"})

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
            require(set(client) == {"id", "source", "can_reveal"} and isinstance(client["can_reveal"], bool) and
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

    def reveal(self, client_id, token, active=True):
        revision = self.view()["revision"]
        status, headers, result = self.admin_request("POST", "/reveal", {"id": client_id, "revision": revision})
        require(status == 200 and result == {"id": client_id, "token": token, "revision": revision,
                                            "active": active}, "Single-client reveal returned incorrect data")
        require(headers.get("Cache-Control") == "no-store", "Revealed credentials could be cached")
        require(self.view()["revision"] == revision, "Revealing a credential modified the registry")

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

    def mcp(self, token, message, session):
        return mcp_request(self.business, {"Authorization": f"Bearer {token}"}, message, session)

    def new_session(self, token):
        return start_session(self.business, {"Authorization": f"Bearer {token}"})


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
        require(fixture.probe(token)[0] == 200, "A dot client could not authenticate after creation")
        fixture.reveal(client_id, token)
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
        require(fixture.probe(token)[0] == 401 and fixture.probe(replacement)[0] == 200,
                "Body-ID replacement did not rotate a dot client's credential immediately")
        require(fixture.admin_request("DELETE", body={"revision": added["revision"],
                "id": client_id})[0] == 409, "Body-ID deletion ignored a stale revision")
        require(fixture.view()["revision"] == updated["revision"] and fixture.probe(replacement)[0] == 200,
                "Rejected body-ID changes altered the active registry")
        fixture.change("DELETE", fields={"id": client_id})
        require(fixture.probe(replacement)[0] == 401, "Body-ID deletion left a dot client's credential active")
        for method, fields in (("PUT", {"token": replacement}), ("DELETE", {})):
            require(fixture.admin_request(method, body={"revision": fixture.view()["revision"],
                    "id": client_id} | fields)[0] == 404, "A missing body-ID client did not return 404")


def verify_crud(fixture):
    manual = "xbyang/qcopilots-websearch"
    replacement = "rotated/" + secrets.token_hex(16)
    initial = {"local": "file", "environment-client": "environment"}
    with fixture.running():
        view = fixture.view()
        require_sources(view, initial)
        fixture.reveal("local", fixture.file_token)
        fixture.reveal("environment-client", fixture.environment_token)
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
        require(fixture.probe(manual)[0] == 200, "A short custom token was not immediately usable")
        fixture.reveal("browser", manual)
        for token in (fixture.file_token, fixture.environment_token):
            require(fixture.probe(token)[0] == 200, "Creating the registry disabled a configured credential")
        require(fixture.admin_request("POST", body=body)[0] == 409,
                "A stale revision was allowed to overwrite the registry")
        current = added["revision"]
        for method, suffix, fields, expected, reason, client in (
            ("POST", "", {"id": "browser", "token": replacement}, 400, "client_id_exists", "browser"),
            ("POST", "", {"id": "duplicate-token", "token": manual}, 400, "client_credential_duplicate", "browser"),
            ("PUT", "/absent", {"token": replacement}, 404, "client_not_found", "absent"),
            ("DELETE", "/absent", {}, 404, "client_not_found", "absent"),
        ):
            result = fixture.admin_request(method, suffix, {"revision": current} | fields)
            require(result[0] == expected, "Client identity conflict returned the wrong status")
            require(result[2]["error"].get("reason") == reason and
                    result[2]["error"].get("parameters") == {"client_id": client},
                    "A client conflict did not identify its safe, specific cause")
            require(manual not in json.dumps(result[2]) and replacement not in json.dumps(result[2]),
                    "An error response exposed a client credential")
        for method, client_id, duplicate_token, owner in (
            ("POST", "duplicate-file", fixture.file_token, "local"),
            ("POST", "duplicate-env", fixture.environment_token, "environment-client"),
            ("PUT", "browser", fixture.file_token, "local"),
            ("PUT", "local", manual, "browser"),
        ):
            status, _, result = fixture.admin_request(method, body={"revision": current, "id": client_id,
                                                                   "token": duplicate_token})
            require(status == 400 and result["error"].get("reason") == "client_credential_duplicate" and
                    result["error"].get("parameters") == {"client_id": owner} and
                    duplicate_token not in json.dumps(result), "Duplicate source or replacement identified the wrong client")
        require(fixture.view()["revision"] == current and fixture.probe(manual)[0] == 200 and
                fixture.probe(fixture.file_token)[0] == 200, "A duplicate-credential error changed active authorization")
        for invalid in ("", "contains space", "line\nbreak", "tab\tvalue", "\x00", "\x7f", "非ASCII", "a" * 8193):
            status, _, _ = fixture.admin_request("PUT", "/browser", {"revision": current, "token": invalid})
            require(status == 400, "An invalid business credential was accepted")
        require(fixture.view()["revision"] == current, "Rejected changes modified the registry")

        session = fixture.new_session(manual)
        fixture.change("PUT", "/browser", {"token": replacement})
        fixture.reveal("browser", replacement)
        require(fixture.probe(manual)[0] == 401 and fixture.probe(replacement)[0] == 200,
                "Token rotation did not apply immediately")
        status, _, reply = fixture.mcp(replacement, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        require(status == 200 and bool(reply.get("result", {}).get("tools")),
                "Token rotation discarded the existing client's MCP identity")
        fixture.change("DELETE", "/browser")
        require(fixture.probe(replacement)[0] == 401, "A deleted credential remained active")
        fixture.change("POST", fields={"id": "browser", "token": replacement})
        require(fixture.probe(replacement)[0] == 200, "A recreated client could not authenticate")
        require(fixture.mcp(replacement, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, session)[0] == 404,
                "Recreating a client inherited the deleted client's MCP session")
        require(fixture.new_session(replacement) != session, "A recreated client reused a retired session ID")

        fixture.change("POST", fields={"id": "one-character", "token": "/"})
        require(fixture.probe("/")[0] == 200, "A one-character visible ASCII credential was rejected")
        fixture.change("PUT", "/one-character", {"token": "a" * 8192})
        require(fixture.probe("a" * 8192)[0] == 200, "The documented maximum credential length was rejected")
        fixture.change("DELETE", "/one-character")
        encoded = fixture.registry.read_bytes()
        stored = next(entry for entry in json.loads(encoded)["clients"] if entry["id"] == "browser")
        require(stored["token_value"] == replacement and stored["token_sha256"] == digest(replacement) and
                manual.encode() not in encoded, "The protected registry retained an obsolete credential")
        response = json.dumps(fixture.view())
        require(all(value not in response for value in (manual, replacement, digest(manual), digest(replacement))),
                "The client view exposed a credential or digest")
        verify_body_id_routes(fixture)

    with fixture.running():
        require_sources(fixture.view(), initial | {"browser": "manual"})
        for token in (replacement, fixture.file_token, fixture.environment_token):
            require(fixture.probe(token)[0] == 200, "A registry credential did not survive restart")
        fixture.reveal("browser", replacement)
        require(fixture.probe(manual)[0] == 401, "Restart restored an obsolete credential")
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
        require_sources(pending, {"local": "file", "environment-client": "environment"} |
                        {name: "manual" for name in tokens})
        require(pending["pending_restart"], "CLI changes were not reported as pending")
        for name, token in tokens.items():
            fixture.reveal(name, token, active=False)
        for token in tokens.values():
            require(fixture.probe(token)[0] == 401, "CLI enrollment changed the active registry before apply")
        require(fixture.admin_request("POST", body={"revision": previous["revision"],
                "id": "stale", "token": "stale/input"})[0] == 409,
                "A stale web update overwrote concurrent CLI enrollment")
        fixture.change("POST", fields={"id": "web-apply", "token": "web/apply"})
        for name, token in tokens.items():
            require(fixture.probe(token)[0] == 200, "Web apply omitted pending CLI changes")
            fixture.reveal(name, token)
        result = fixture.command("--SetToken", "--ClientId", "concurrent-a", user_input=replacement + "\ny\n")
        require(result.returncode == 0 and replacement not in result.stdout + result.stderr,
                "CLI rotation failed or exposed a manual token")
        require(fixture.view()["pending_restart"] and fixture.probe(tokens["concurrent-a"])[0] == 200 and
                fixture.probe(replacement)[0] == 401, "CLI rotation did not remain pending until restart")
        fixture.reveal("concurrent-a", replacement, active=False)
        fixture.reveal("concurrent-b", tokens["concurrent-b"])
        encoded = json.loads(fixture.registry.read_bytes())
        stored = {entry["id"]: entry.get("token_value") for entry in encoded["clients"]}
        require(stored["concurrent-a"] == replacement and stored["concurrent-b"] == tokens["concurrent-b"],
                "CLI enrollment did not retain the recoverable values in its protected registry")
        fixture.unchanged_runtime()

    with fixture.running():
        require(not fixture.view()["pending_restart"], "Restart did not consume pending CLI changes")
        require(fixture.probe(replacement)[0] == 200 and fixture.probe(tokens["concurrent-a"])[0] == 401 and
                fixture.probe(tokens["concurrent-b"])[0] == 200,
                "Restart did not apply the CLI registry without losing another client")
        fixture.reveal("concurrent-a", replacement)
        fixture.unchanged_runtime()


def verify_zero_clients(fixture):
    with fixture.running():
        for client in fixture.view()["clients"]:
            fixture.change("DELETE", "/" + client["id"])
        require(fixture.view()["clients"] == [], "Deleting the last client was rejected")
        for token in (fixture.file_token, fixture.environment_token):
            require(fixture.probe(token)[0] == 401 and
                    request(fixture.control, "GET", "/health/live", {"Authorization": f"Bearer {token}"})[0] == 401,
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
        require(fixture.view()["clients"] == [] and fixture.probe(fixture.file_token)[0] == 401,
                "Restart restored runtime clients after deleting the authoritative registry's last client")
        fixture.change("POST", fields={"id": "recovered", "token": "recover/access"})
        require(fixture.probe("recover/access")[0] == 200, "The administrator could not recover an empty registry")
        fixture.unchanged_runtime()


def verify_reveal(fixture):
    token = "reveal/" + secrets.token_hex(24)
    with fixture.running():
        stale = fixture.view()["revision"]
        fixture.change("POST", fields={"id": "visible", "token": token})
        revision = fixture.view()["revision"]
        body = {"id": "visible", "revision": revision}
        for headers, expected in (
            ({key: value for key, value in fixture.write_headers.items() if key != "Cookie"}, 401),
            ({key: value for key, value in fixture.write_headers.items() if key != "X-QCWS-CSRF"}, 403),
            (fixture.write_headers | {"Origin": "http://foreign.invalid"}, 403),
        ):
            status, _, result = request(fixture.admin, "POST", API + "/clients/reveal", headers, body)
            require(status == expected and token not in json.dumps(result),
                    "Reveal bypassed authentication, CSRF or Origin protection")
        status, _, result = fixture.admin_request("POST", "/reveal", body | {"revision": stale})
        require(status == 409 and result["error"]["code"] == "revision_conflict" and
                token not in json.dumps(result), "Reveal ignored a stale client revision")
        status, _, result = fixture.admin_request("POST", "/reveal", body | {"id": "absent"})
        require(status == 404 and result["error"].get("reason") == "client_not_found",
                "Reveal did not identify a missing client")
        for invalid in ({"id": "visible"}, body | {"id": []}, body | {"token": token}):
            status, _, result = fixture.admin_request("POST", "/reveal", invalid)
            require(status == 400 and token not in json.dumps(result), "Reveal accepted or reflected an invalid body")
        fixture.reveal("visible", token)

        # File sources are shown without changing their ownership or provenance.
        # An externally rotated file is not the already-running authentication snapshot.
        source = fixture.runtime.parent / "client.token"
        original = source.read_bytes()
        replacement = "file/" + secrets.token_hex(24)
        try:
            source.write_text(replacement + "\n", encoding="ascii")
            fixture.reveal("local", replacement, active=False)
            require(fixture.probe(fixture.file_token)[0] == 200 and fixture.probe(replacement)[0] == 401,
                    "Reading a changed file source applied it to the active service")
            hidden = source.with_name("client.unavailable")
            source.rename(hidden)
            try:
                status, _, result = fixture.admin_request("POST", "/reveal", body | {"id": "local"})
                require(status == 400 and result["error"].get("reason") == "credential_source_unavailable" and
                        replacement not in json.dumps(result), "An unavailable source was not reported safely")
                before = fixture.view()
                saved = fixture.registry.read_bytes()
                candidate = "unapplied/" + secrets.token_hex(24)
                for method, fields in (
                    ("POST", {"id": "blocked-by-source", "token": candidate}),
                    ("PUT", {"id": "visible", "token": candidate}),
                    ("DELETE", {"id": "visible"}),
                ):
                    status, _, result = fixture.admin_request(method, body={"revision": before["revision"]} | fields)
                    require(status == 400 and result["error"]["code"] == "invalid_configuration" and
                            result["error"].get("reason") == "credential_source_unavailable" and
                            result["error"].get("parameters") == {"client_id": "local"},
                            "Client changes did not identify the unavailable credential source")
                    response = json.dumps(result)
                    require(all(value not in response for value in
                                (token, candidate, replacement, fixture.file_token, str(source))),
                            "An unavailable source error exposed a credential or source path")
                    require(fixture.view() == before and fixture.registry.read_bytes() == saved and
                            fixture.probe(token)[0] == 200 and fixture.probe(fixture.file_token)[0] == 200 and
                            fixture.probe(candidate)[0] == 401,
                            "A failed source lookup changed disk state or the authentication snapshot")
            finally:
                hidden.rename(source)
        finally:
            source.write_bytes(original)
        fixture.reveal("local", fixture.file_token)

        committed = fixture.registry.read_bytes()
        document = json.loads(committed)
        stored = next(item for item in document["clients"] if item["id"] == "visible")
        try:
            del stored["token_value"]
            fixture.registry.write_text(json.dumps(document), encoding="utf-8")
            digest_only = fixture.view()
            require(not next(item for item in digest_only["clients"] if item["id"] == "visible")["can_reveal"],
                    "A hash-only record was incorrectly described as recoverable")
            status, _, result = fixture.admin_request("POST", "/reveal",
                                                      {"id": "visible", "revision": digest_only["revision"]})
            require(status == 400 and result["error"].get("reason") == "credential_not_recoverable" and
                    fixture.probe(token)[0] == 200, "Hash-only reveal changed authentication or hid its cause")
            require(fixture.command("--CheckConfig").returncode == 0, "A valid digest-only record failed validation")
            stored["token_value"] = "incorrect/" + secrets.token_hex(24)
            fixture.registry.write_text(json.dumps(document), encoding="utf-8")
            status, _, result = fixture.admin_request("POST", "/reveal",
                                                      {"id": "visible", "revision": fixture.view()["revision"]})
            require(status == 400 and result["error"].get("reason") == "credential_digest_mismatch" and
                    stored["token_value"] not in json.dumps(result), "A mismatched credential was disclosed")
            checked = fixture.command("--CheckConfig")
            require(checked.returncode != 0 and token not in checked.stdout + checked.stderr and
                    stored["token_value"] not in checked.stdout + checked.stderr,
                    "Corrupt credential state passed validation or leaked values")
        finally:
            fixture.registry.write_bytes(committed)

        require(request(fixture.admin, "DELETE", API + "/session", fixture.write_headers)[0] == 200,
                "Administrator sign-out failed")
        status, _, result = fixture.admin_request("POST", "/reveal", body)
        require(status == 401 and token not in json.dumps(result), "A retired administrator session could reveal a code")
        fixture.login()
        fixture.reveal("visible", token)
        fixture.change("DELETE", fields={"id": "visible"})
        require(fixture.probe(token)[0] == 401, "Deleting a revealed credential did not revoke it")
        fixture.unchanged_runtime()


def verify_registry_capacity(fixture):
    # Obtain a registry with the product's actual protected ACL, then supply the
    # largest schema-valid document. Escaping doubles these credential bytes.
    require(fixture.command("--SetToken", "--ClientId", "capacity-seed", user_input="seed/code\nyes\n").returncode == 0,
            "Could not prepare a protected capacity fixture")
    tokens = {f"capacity-{index}": "\\" * 8188 + f"{index:04d}" for index in range(100)}
    document = {"schema_version": 1, "clients": [
        {"id": client, "token_sha256": digest(token), "token_value": token, "generation": secrets.token_hex(16)}
        for client, token in tokens.items()
    ]}
    encoded = json.dumps(document, indent=2).encode("utf-8")
    require(1024 * 1024 < len(encoded) < 2 * 1024 * 1024, "Capacity fixture does not exercise escaped credentials")
    fixture.registry.write_bytes(encoded)
    require(fixture.command("--CheckConfig").returncode == 0, "A valid maximum-size registry was rejected")
    with fixture.running():
        require(len(fixture.view()["clients"]) == 100, "The largest registry lost clients")
        fixture.reveal("capacity-99", tokens["capacity-99"])
        revision = fixture.view()["revision"]
        status, _, result = fixture.admin_request("POST", body={"id": "overflow", "token": "new/code",
                                                               "revision": revision})
        require(status == 400 and result["error"].get("reason") == "client_limit_reached" and
                fixture.view()["revision"] == revision, "The client limit was not reported without mutation")
        replacement = '"' * 8188 + "next"
        fixture.change("PUT", fields={"id": "capacity-0", "token": replacement})
        fixture.reveal("capacity-0", replacement)
        require(fixture.probe(replacement)[0] == 200, "Maximum escaped credential did not authenticate")
        committed = fixture.registry.read_bytes()
        try:
            fixture.registry.write_bytes(committed + b" " * (2 * 1024 * 1024 + 1 - len(committed)))
            status, _, result = fixture.admin_request()
            require(status == 400 and result["error"].get("reason") == "client_registry_too_large",
                    "An oversized registry was not explicitly rejected")
            require(fixture.probe(replacement)[0] == 200, "Oversized disk state replaced the valid active snapshot")
        finally:
            fixture.registry.write_bytes(committed)
        fixture.unchanged_runtime()
    with fixture.running():
        fixture.reveal("capacity-0", replacement)
        fixture.reveal("capacity-99", tokens["capacity-99"])


def verify_write_failure(fixture):
    with fixture.running():
        fixture.change("POST", fields={"id": "write-check", "token": "before/write"})
        committed = fixture.registry.read_bytes()
        revision = fixture.view()["revision"]
        replacement = "after/" + secrets.token_hex(16)
        handle = None
        directory_mode = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            # Permit reading, but prevent the atomic replacement of this file.
            handle = kernel.CreateFileW(str(fixture.registry), 0x80000000, 1, None, 3, 0x80, None)
            require(handle not in (None, ctypes.c_void_p(-1).value), "Could not create the Windows write-failure fixture")
        elif os.geteuid() != 0:
            directory_mode = stat.S_IMODE(fixture.registry.parent.stat().st_mode)
            fixture.registry.parent.chmod(0o500)
        else:
            # A privileged process bypasses chmod. Service-account write paths
            # are separately exercised by DEB acceptance under the real account.
            return
        try:
            status, _, result = fixture.admin_request("PUT", body={"id": "write-check", "token": replacement,
                                                                  "revision": revision})
            expected = "client_registry_write_failed" if os.name == "nt" else "client_registry_permissions"
            require(status == 400 and result["error"].get("reason") == expected and
                    replacement not in json.dumps(result), "A failed write had no safe, specific error")
            require(fixture.registry.read_bytes() == committed and fixture.probe("before/write")[0] == 200 and
                    fixture.probe(replacement)[0] == 401, "A failed write changed disk state or live authentication")
            fixture.reveal("write-check", "before/write")
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
            if directory_mode is not None:
                fixture.registry.parent.chmod(directory_mode)
        fixture.change("PUT", fields={"id": "write-check", "token": replacement})
        fixture.reveal("write-check", replacement)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", required=True)
    executable = parser.parse_args().Executable
    for verify in (verify_crud, verify_cli_pending, verify_zero_clients, verify_reveal,
                   verify_registry_capacity, verify_write_failure):
        with tempfile.TemporaryDirectory(prefix="qcws client registry ") as folder:
            fixture = Fixture(executable, Path(folder))
            try:
                verify(fixture)
            finally:
                fixture.close()


if __name__ == "__main__":
    main()
