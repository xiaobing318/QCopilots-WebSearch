"""Exercise the installed listener contract with real local HTTP."""
import argparse
import http.client
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit


def ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for server in sockets:
            server.bind(("127.0.0.1", 0))
        return [server.getsockname()[1] for server in sockets]
    finally:
        for server in sockets:
            server.close()


def request(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse()
        content = response.read()
        if response.getheader("Content-Type", "").startswith("application/json"):
            content = json.loads(content)
        return response.status, response.headers, content
    finally:
        connection.close()


def browser(port, executable, runtime, token):
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
    assert request(port, "GET", "/settings/", {"Host": "other.invalid"})[0] == 403
    assert request(port, "GET", base + "/settings")[0] == 401
    assert request(port, "GET", base + "/no-such-route")[2]["error"]["code"] == "not_found"
    assert request(port, "POST", base + "/login-links", {"Authorization": f"Bearer {token}",
                                                        "Origin": origin})[0] == 403
    bad = {"Origin": "http://other.invalid", "Content-Type": "application/json"}
    assert request(port, "POST", base + "/session", bad, {"code": login_code})[0] == 403
    login = {"Origin": origin, "Content-Type": "application/json"}
    code, headers, data = request(port, "POST", base + "/session", login, {"code": login_code})
    assert code == 200
    full_cookie = headers["Set-Cookie"]
    assert "HttpOnly" in full_cookie and "SameSite=Strict" in full_cookie
    assert request(port, "POST", base + "/session", login, {"code": login_code})[0] == 401
    cookie = {"Cookie": full_cookie.split(";", 1)[0]}
    assert request(port, "GET", base + "/session", cookie)[2] == data
    old = request(port, "GET", base + "/settings", cookie)[2]
    patch = {"revision": old["revision"], "patch": {"tools": {
        "search": {"enabled": False}, "open": {"enabled": False}}}}
    assert request(port, "PUT", base + "/settings", login | cookie, patch)[0] == 403
    write = login | cookie | {"X-QCWS-CSRF": data["csrf"]}
    updated = request(port, "PUT", base + "/settings", write, patch)
    assert updated[0] == 200 and updated[2]["tools"]["search"]["enabled"] is False
    assert request(port, "PUT", base + "/settings", write, patch)[0] == 409
    secret = "test-provider-key"
    key_path = base + "/providers/brave/key"
    response = request(port, "PUT", key_path, write, {"key": secret})
    assert response[0] == 200 and response[2]["providers"]["brave"]["key_set"] is True
    assert secret not in json.dumps(response[2])
    assert request(port, "DELETE", key_path, write)[2]["providers"]["brave"]["key_set"] is False
    test = request(port, "POST", base + "/providers/brave/test", write, {})
    assert test[0] == 200 and test[2]["error"]["code"] == "provider_not_configured"
    assert request(port, "GET", base + "/status", cookie)[2]["listeners"]["admin"]["port"] == port
    assert request(port, "DELETE", base + "/session", write)[0] == 200
    assert request(port, "GET", base + "/settings", cookie)[0] == 401
    assert request(port, "POST", base + "/session", login, {"token": token})[0] == 200


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--Executable", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="qcws-admin-") as temp:
        root = Path(temp)
        runtime = root / "runtime.json"
        base = [args.Executable, "--ConfigPath", str(runtime)]
        subprocess.run([*base, "--InitializeConfig"], check=True, capture_output=True, timeout=5)
        config = json.loads(runtime.read_text(encoding="utf-8"))
        business, control, admin = ports()
        for label, port in (("business", business), ("control", control)):
            config[label].update(port=port, allowed_hosts=[f"127.0.0.1:{port}"])
        config["admin"] = {"enabled": True, "port": admin}
        runtime.write_text(json.dumps(config), encoding="utf-8")
        token = (root / "admin.token").read_text(encoding="ascii").strip()
        assert subprocess.run([*base, "--SettingsUrl"], capture_output=True, timeout=6).returncode == 1
        subprocess.run([*base, "--CheckConfig"], check=True, capture_output=True, timeout=5)
        with subprocess.Popen(base, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as service:
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
                browser(admin, args.Executable, runtime, token)
                business_token = (root / "client.token").read_text(encoding="ascii").strip()
                headers = {"Authorization": f"Bearer {business_token}", "Content-Type": "application/json"}
                assert request(business, "GET", "/v1/status", headers)[2]["settings"]["search_enabled"] is False
                status, _, body = request(business, "POST", "/v1/search", headers, {"query": "QGIS"})
                assert status == 403 and body["error"]["code"] == "tool_disabled"
                status, _, body = request(business, "POST", "/v1/open", headers, {"page_id": "cached-id"})
                assert status == 403 and body["error"]["code"] == "tool_disabled"
            finally:
                if service.poll() is None:
                    service.terminate()
                    try:
                        service.wait(5)
                    except subprocess.TimeoutExpired:
                        service.kill()


if __name__ == "__main__":
    main()
