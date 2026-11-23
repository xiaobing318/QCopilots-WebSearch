"""Exercise the installed listener contract with real local HTTP."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from mcp_http_fixture import call_tool, mcp_probe, mcp_request, ports, request, start_session


GENERATED_PUBLIC_URL = object()


def require_error(response, status, code, reason=None):
    assert response[0] == status
    error = response[2]["error"]
    assert error["code"] == code
    if reason is not None:
        assert error["reason"] == reason
    assert "diagnostic-secret-sentinel" not in json.dumps(error)
    return error


def read_admin_status(port, token):
    """Read status through an authenticated administration session."""
    origin = f"http://127.0.0.1:{port}"
    headers = {"Origin": origin, "Content-Type": "application/json"}
    code, response, session = request(port, "POST", "/admin/api/v1/session", headers, {"token": token})
    assert code == 200
    cookie = {"Cookie": response["Set-Cookie"].split(";", 1)[0]}
    try:
        code, _, status = request(port, "GET", "/admin/api/v1/status", cookie)
        assert code == 200
        return status
    finally:
        assert request(port, "DELETE", "/admin/api/v1/session",
                       headers | cookie | {"X-QCWS-CSRF": session["csrf"]})[0] == 200


def browser(port, executable, runtime, token, expected_mcp_url):
    origin = f"http://127.0.0.1:{port}"
    base = "/admin/api/v1"
    raw = subprocess.check_output([executable, "--ConfigPath", str(runtime), "--SettingsUrl"],
                                  text=True, timeout=6).strip()
    link = urlsplit(raw)
    assert link.netloc == f"127.0.0.1:{port}" and link.path == "/settings/"
    login_code = link.fragment.removeprefix("login=")
    assert len(login_code) == 64
    assert request(port, "GET", "/")[0] == 302
    code, headers, page = request(port, "GET", "/settings/")
    assert code == 200 and b"html" in page
    assert headers["Content-Security-Policy"] and "Access-Control-Allow-Origin" not in headers
    require_error(request(port, "GET", "/settings/", {"Host": "other.invalid"}),
                  403, "forbidden", "admin_host_rejected")
    require_error(request(port, "GET", base + "/settings"), 401, "unauthorized", "admin_session_expired")
    require_error(request(port, "GET", base + "/no-such-route"), 404, "not_found", "admin_endpoint_not_found")
    require_error(request(port, "POST", base + "/login-links", {"Authorization": f"Bearer {token}",
                                                               "Origin": origin}),
                  403, "forbidden", "admin_native_login_required")
    bad = {"Origin": "http://other.invalid", "Content-Type": "application/json"}
    require_error(request(port, "POST", base + "/session", bad, {"code": login_code}),
                  403, "forbidden", "admin_origin_rejected")
    login = {"Origin": origin, "Content-Type": "application/json"}
    require_error(request(port, "POST", base + "/session", login, {"unexpected": "diagnostic-secret-sentinel"}),
                  400, "invalid_argument", "admin_login_request_invalid")
    require_error(request(port, "POST", base + "/session", login | {"Content-Type": "text/plain"},
                          {"code": login_code}), 415, "invalid_argument", "admin_content_type_invalid")
    code, headers, data = request(port, "POST", base + "/session", login, {"code": login_code})
    assert code == 200
    full_cookie = headers["Set-Cookie"]
    assert "HttpOnly" in full_cookie and "SameSite=Strict" in full_cookie
    require_error(request(port, "POST", base + "/session", login, {"code": login_code}),
                  401, "unauthorized", "admin_login_code_invalid")
    require_error(request(port, "POST", base + "/session", login, {"token": "diagnostic-secret-sentinel"}),
                  401, "unauthorized", "admin_credential_invalid")
    cookie = {"Cookie": full_cookie.split(";", 1)[0]}
    assert request(port, "GET", base + "/session", cookie)[2] == data
    old = request(port, "GET", base + "/settings", cookie)[2]
    patch = {"revision": old["revision"], "patch": {"tools": {
        "search": {"enabled": False}, "open": {"enabled": False}}}}
    require_error(request(port, "PUT", base + "/settings", login | cookie, patch),
                  403, "forbidden", "admin_csrf_rejected")
    write = login | cookie | {"X-QCWS-CSRF": data["csrf"]}
    require_error(request(port, "PUT", base + "/settings", write | {"X-QCWS-CSRF": "wrong"}, patch),
                  403, "forbidden", "admin_csrf_rejected")
    require_error(request(port, "PUT", base + "/settings", write,
                          {"revision": old["revision"], "patch": {"unexpected": "diagnostic-secret-sentinel"}}),
                  400, "invalid_argument", "settings_patch_invalid")
    updated = request(port, "PUT", base + "/settings", write, patch)
    assert updated[0] == 200 and updated[2]["tools"]["search"]["enabled"] is False
    require_error(request(port, "PUT", base + "/settings", write, patch),
                  409, "revision_conflict", "settings_revision_conflict")
    secret = "test-provider-key"
    key_path = base + "/providers/brave/key"
    response = request(port, "PUT", key_path, write, {"key": secret})
    assert response[0] == 200 and response[2]["providers"]["brave"]["key_set"] is True
    assert secret not in json.dumps(response[2])
    assert request(port, "DELETE", key_path, write)[2]["providers"]["brave"]["key_set"] is False
    require_error(request(port, "PUT", key_path, write, {"key": "diagnostic-secret-sentinel invalid"}),
                  400, "invalid_argument", "provider_key_invalid")
    require_error(request(port, "DELETE", base + "/providers/unknown/key", write),
                  400, "invalid_argument", "provider_unknown")
    test = request(port, "POST", base + "/providers/brave/test", write, {})
    assert test[0] == 200 and test[2]["error"]["code"] == "provider_not_configured"
    status, _, service = request(port, "GET", base + "/status", cookie)
    assert status == 200 and service["listeners"]["admin"]["port"] == port
    assert service["mcp_url"] == expected_mcp_url, "Administration status returned the wrong client MCP endpoint"
    assert service["settings_error"] is None and service["settings_error_detail"] is None

    # The detailed status must describe the failed reload while the last valid
    # settings remain usable, then clear after the file is repaired.
    settings_path = runtime.parent / "state" / "settings.json"
    valid_settings = settings_path.read_bytes()
    active_settings = request(port, "GET", base + "/settings", cookie)[2]
    try:
        settings_path.write_bytes(b"{")
        for _ in range(60):
            observed = request(port, "GET", base + "/status", cookie)[2]
            if observed["settings_error"] is not None:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Invalid settings were not reported by administration status")
        assert observed["settings_error"] == "settings_error"
        assert observed["settings_error_detail"]["reason"] == "settings_json_invalid"
        assert request(port, "GET", base + "/settings", cookie)[2] == active_settings
    finally:
        settings_path.write_bytes(valid_settings)
    for _ in range(60):
        observed = request(port, "GET", base + "/status", cookie)[2]
        if observed["settings_error"] is None and observed["settings_error_detail"] is None:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("Repaired settings did not clear the administration diagnostic")

    view = request(port, "GET", base + "/clients", cookie)[2]
    client = next(item for item in view["clients"] if item["id"] == "local")
    assert client["can_reveal"] is True and "token" not in client
    reveal = {"id": "local", "revision": view["revision"]}
    require_error(request(port, "POST", base + "/clients/reveal", login, reveal),
                  403, "forbidden", "admin_csrf_rejected")
    require_error(request(port, "POST", base + "/clients/reveal", write | {"Origin": "http://foreign.invalid"}, reveal),
                  403, "forbidden", "admin_origin_rejected")
    require_error(request(port, "POST", base + "/clients/reveal", write, reveal | {"revision": "0" * 64}),
                  409, "revision_conflict")
    require_error(request(port, "POST", base + "/clients/reveal", write, reveal | {"id": "absent"}),
                  404, "not_found", "client_not_found")
    require_error(request(port, "POST", base + "/clients/reveal", write,
                          reveal | {"unexpected": "diagnostic-secret-sentinel"}),
                  400, "invalid_argument", "admin_request_invalid")
    code, revealed_headers, revealed = request(port, "POST", base + "/clients/reveal", write, reveal)
    assert code == 200 and revealed_headers["Cache-Control"] == "no-store"
    assert revealed == {"id": "local", "revision": view["revision"], "active": True,
                        "token": (runtime.parent / "client.token").read_text(encoding="ascii").strip()}
    assert request(port, "DELETE", base + "/session", write)[0] == 200
    require_error(request(port, "GET", base + "/settings", cookie),
                  401, "unauthorized", "admin_session_expired")
    require_error(request(port, "POST", base + "/clients/reveal", write, reveal),
                  401, "unauthorized", "admin_session_expired")
    code, new_headers, new_session = request(port, "POST", base + "/session", login, {"token": token})
    assert code == 200
    assert request(port, "DELETE", base + "/session",
                   login | {"Cookie": new_headers["Set-Cookie"].split(";", 1)[0],
                            "X-QCWS-CSRF": new_session["csrf"]})[0] == 200
    for _ in range(8):
        assert request(port, "POST", base + "/login-links", {"Authorization": f"Bearer {token}"})[0] == 200
    require_error(request(port, "POST", base + "/login-links", {"Authorization": f"Bearer {token}"}),
                  429, "rate_limited", "admin_login_links_exhausted")
    for _ in range(8):
        require_error(request(port, "POST", base + "/session", login, {"token": "diagnostic-secret-sentinel"}),
                      401, "unauthorized", "admin_credential_invalid")
    limited = require_error(request(port, "POST", base + "/session", login, {"token": token}),
                            429, "rate_limited", "admin_login_rate_limited")
    assert 1 <= limited["retry_after_seconds"] <= 600


def verify_public_url_configuration(base, runtime, config):
    accepted = (
        "https://search.example.test/mcp",
        "https://search.example.test:443/mcp",
        "https://search.example.test:65535/mcp",
        "https://192.0.2.1:8443/mcp",
        "https://[2001:db8::1]:8443/mcp",
    )
    rejected = (
        "http://search.example.test/mcp",
        "https://search.example.test/",
        "https://search.example.test/mcp/",
        "https://search.example.test/v1/status",
        "https://search.example.test/%6Dcp",
        "https://search.example.test/mc%70",
        "https://user@search.example.test/mcp",
        "https://user:password@search.example.test/mcp",
        "https://@search.example.test/mcp",
        "https://search.example.test/mcp?query=value",
        "https://search.example.test/mcp?",
        "https://search.example.test/mcp#fragment",
        "https://search.example.test/mcp#",
        "https:///mcp",
        "https://search.example.test:0/mcp",
        "https://search.example.test:65536/mcp",
        "https://bad_host.example/mcp",
        "https://-invalid.example/mcp",
        "https://invalid-.example/mcp",
        "https://bad..example/mcp",
        "https://" + "a" * 64 + ".example/mcp",
    )
    try:
        for expected, urls in ((True, accepted), (False, rejected)):
            for url in urls:
                candidate = config | {"business": config["business"] | {"public_url": url}}
                runtime.write_text(json.dumps(candidate), encoding="utf-8")
                result = subprocess.run([*base, "--CheckConfig"], capture_output=True, text=True, timeout=5)
                assert (result.returncode == 0) == expected, f"Unexpected public_url validation result: {url}"
                if not expected:
                    assert "invalid_configuration" in result.stderr
    finally:
        runtime.write_text(json.dumps(config), encoding="utf-8")


def verify_http(executable, public_url):
    with tempfile.TemporaryDirectory(prefix="qcws-admin-") as temp:
        root = Path(temp)
        runtime = root / "runtime.json"
        base = [executable, "--ConfigPath", str(runtime)]
        subprocess.run([*base, "--InitializeConfig"], check=True, capture_output=True, timeout=5)
        config = json.loads(runtime.read_text(encoding="utf-8"))
        generated_url = config["business"]["public_url"]
        assert generated_url == "https://web-search.vastarmorai.com/mcp"
        business, control, admin = ports()
        for label, port in (("business", business), ("control", control)):
            config[label].update(port=port, allowed_hosts=[f"127.0.0.1:{port}"])
        config["admin"] = {"enabled": True, "port": admin}
        if public_url is None:
            config["business"].pop("public_url")
        elif public_url is not GENERATED_PUBLIC_URL:
            config["business"]["public_url"] = public_url
        advertised_url = config["business"].get("public_url")
        runtime.write_text(json.dumps(config), encoding="utf-8")
        if public_url is None:
            verify_public_url_configuration(base, runtime, config)
        token = (root / "admin.token").read_text(encoding="ascii").strip()
        if public_url is GENERATED_PUBLIC_URL:
            assert subprocess.run([*base, "--SettingsUrl"], capture_output=True, timeout=6).returncode == 1
        subprocess.run([*base, "--CheckConfig"], check=True, capture_output=True, timeout=5)
        windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with subprocess.Popen(base, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **windows) as service:
            try:
                for _ in range(50):
                    if service.poll() is not None:
                        raise RuntimeError(service.stderr.read().decode(errors="replace"))
                    try:
                        if request(admin, "GET", "/")[0] == 302:
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Administration listener did not start")
                business_token = (root / "client.token").read_text(encoding="ascii").strip()
                headers = {"Authorization": f"Bearer {business_token}"}
                expected_url = advertised_url or f"http://127.0.0.1:{business}/mcp"
                public_host = urlsplit(advertised_url or generated_url).netloc
                assert mcp_probe(business, headers | {"Host": public_host})[0] == 403, \
                    "Endpoint display metadata silently expanded the business Host allowlist"
                if public_url is not GENERATED_PUBLIC_URL:
                    assert read_admin_status(admin, token)["mcp_url"] == expected_url
                    assert mcp_probe(business, headers)[0] == 200, "The configured local Host must remain usable"
                    return
                browser(admin, executable, runtime, token, expected_url)
                session = start_session(business, headers)
                try:
                    status = call_tool(business, headers, session, 2, "web_search_status", {})
                    assert not status["isError"] and status["structuredContent"]["settings"]["search_enabled"] is False
                    for req_id, (tool, arguments) in enumerate((
                        ("web_search", {"query": "QGIS"}), ("web_open", {"page_id": "cached-id"}),
                    ), 3):
                        result = call_tool(business, headers, session, req_id, tool, arguments)
                        assert result["isError"] and result["structuredContent"]["error"]["code"] == "tool_disabled"
                finally:
                    assert mcp_request(business, headers, session=session)[0] == 204

                # Probe the actual business listener. A proxy allowlist could hide a surviving REST route.
                origin = headers | {"Origin": "https://client.example.test", "Content-Type": "application/json"}
                for path in ("/v1/status", "/v1/search", "/v1/open", "/v1/find"):
                    method = "GET" if path == "/v1/status" else "POST"
                    assert request(business, method, path, origin, {} if method == "POST" else None)[0] == 404
                    assert request(business, "OPTIONS", path, {
                        "Origin": origin["Origin"], "Access-Control-Request-Method": method,
                        "Access-Control-Request-Headers": "authorization,content-type",
                    })[0] == 404
            finally:
                if service.poll() is None:
                    service.terminate()
                    try:
                        service.wait(5)
                    except subprocess.TimeoutExpired:
                        service.kill()
                        service.wait(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--Executable", required=True)
    args = parser.parse_args()
    for public_url in (GENERATED_PUBLIC_URL, None, "https://search.example.test/mcp"):
        verify_http(args.Executable, public_url)


if __name__ == "__main__":
    main()
