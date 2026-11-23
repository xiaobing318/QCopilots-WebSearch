"""Verify the business listener and a certificate-verifying HTTPS origin connection."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import json
from pathlib import Path
import select
import socket
import ssl
import tempfile

from mcp_http_fixture import INITIALIZE, mcp_request
from test_admin_http import read_admin_status
from test_client_tokens import Fixture, HOST, require, verify_business
from tls_fixture import certificates


def rejected_handshake(port, context, hostname):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
        try:
            with context.wrap_socket(client, server_hostname=hostname):
                pass
        except ssl.SSLCertVerificationError:
            return
    raise AssertionError(f"The TLS client accepted an invalid certificate for {hostname}")


def verify_early_tls_response(fixture, trusted, *, send_body):
    body = json.dumps(INITIALIZE).encode()
    with closing(http.client.HTTPSConnection("127.0.0.1", fixture.business, context=trusted, timeout=3)) as client:
        client.putrequest("POST", "/mcp", skip_host=True)
        headers = fixture.headers(fixture.file_token) | {
            "Origin": "null", "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Accept": "application/json, text/event-stream",
        }
        for name, value in headers.items():
            client.putheader(name, value)
        client.endheaders()
        # Require the actual HTTP response before the listener's two-second request-body timeout.
        client.sock.settimeout(1)
        if send_body:
            require(select.select([client.sock], [], [], 1)[0], "No TLS data arrived before the delayed body")
            client.send(body)
        response = client.getresponse()
        require(response.status == 403 and json.loads(response.read())["error"]["code"] == "forbidden",
                "The origin did not return the complete HTTP rejection")


def verify(executable, openssl, root):
    certificates(root, openssl, "http://127.0.0.1:1/unused.crl")
    fixture = Fixture(executable, root)
    directory = root / "origin TLS 证书"
    directory.mkdir()
    chain = directory / "chain.pem"
    key = directory / "leaf.key"
    (root / "full-chain.pem").rename(chain)
    (root / "leaf.key").rename(key)
    config = fixture.config()
    for cert_path, key_path, label in ((chain, root / "other-ca.key", "mismatched key"),
                                       (chain, root / "missing.key", "missing key"),
                                       (root / "request.cnf", key, "malformed PEM certificate")):
        config["business"]["tls"] = {"certificate_chain_file": str(cert_path), "private_key_file": str(key_path)}
        fixture.write(config)
        for args in (("--CheckConfig",), ()):
            result = fixture.command(*args)
            require(result.returncode != 0 and "invalid_configuration" in result.stderr,
                    f"Invalid TLS configuration ({label}) was accepted or incorrectly diagnosed")

    config["business"]["tls"] = {"certificate_chain_file": str(chain.relative_to(root)),
                                 "private_key_file": str(key.relative_to(root))}
    config["business"].pop("public_url", None)
    fixture.write(config)
    require(fixture.command("--CheckConfig").returncode == 0, "Valid TLS configuration was rejected")
    trusted = ssl.create_default_context(cafile=str(root / "ca.pem"))
    unrelated = ssl.create_default_context(cafile=str(root / "other-ca.pem"))
    with fixture.running(origin_tls=trusted):
        admin_token = (root / "admin.token").read_text(encoding="ascii").strip()
        status = read_admin_status(config["admin"]["port"], admin_token)
        require(status["mcp_url"] == f"https://127.0.0.1:{fixture.business}/mcp",
                "TLS administration status advertised a plaintext business endpoint")
        rejected_handshake(fixture.business, unrelated, "127.0.0.1")
        rejected_handshake(fixture.business, trusted, "wrong-name.invalid")
        try:
            status, _, _ = mcp_request(fixture.business, {"Host": HOST}, INITIALIZE)
        except (OSError, http.client.HTTPException):
            pass
        else:
            require(status == 400, "The TLS origin answered an ordinary plaintext business request")
        verify_early_tls_response(fixture, trusted, send_body=False)
        verify_early_tls_response(fixture, trusted, send_body=True)
        verify_business(fixture, fixture.environment_token)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(fixture.probe, fixture.file_token) for _ in range(4)]
            require(all(call.result()[0] == 200 for call in pending), "Concurrent MCP requests over TLS failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", required=True, type=Path)
    parser.add_argument("--OpenSSL", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="qcws-origin-tls-") as temporary:
        verify(args.Executable.resolve(strict=True), args.OpenSSL.resolve(strict=True), Path(temporary))
