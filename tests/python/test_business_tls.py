"""Verify the business listener and a certificate-verifying HTTPS origin connection."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import http.client
from pathlib import Path
import socket
import ssl
import tempfile

from test_admin_http import request
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
    fixture.write(config)
    require(fixture.command("--CheckConfig").returncode == 0, "Valid TLS configuration was rejected")
    trusted = ssl.create_default_context(cafile=str(root / "ca.pem"))
    unrelated = ssl.create_default_context(cafile=str(root / "other-ca.pem"))
    with fixture.running(origin_tls=trusted):
        rejected_handshake(fixture.business, unrelated, "127.0.0.1")
        rejected_handshake(fixture.business, trusted, "wrong-name.invalid")
        try:
            status, _, _ = request(fixture.business, "GET", "/v1/status", {"Host": HOST})
        except (OSError, http.client.HTTPException):
            pass
        else:
            require(status == 400, "The TLS origin answered an ordinary plaintext business request")
        verify_business(fixture, fixture.environment_token)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(fixture.public, "GET", "/v1/status", fixture.file_token) for _ in range(4)]
            require(all(call.result()[0] == 200 for call in pending), "Concurrent TLS status requests failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", required=True, type=Path)
    parser.add_argument("--OpenSSL", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="qcws-origin-tls-") as temporary:
        verify(args.Executable.resolve(strict=True), args.OpenSSL.resolve(strict=True), Path(temporary))
