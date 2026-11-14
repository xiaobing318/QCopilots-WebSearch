#!/usr/bin/env python3
"""Real local TLS checks without changing the machine trust store."""
import argparse
from contextlib import contextmanager
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid


class Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, context=None, stall=False):
        self.context = context
        self.stall = stall
        self.lock = threading.Lock()
        self.accepted = self.posts = self.crl_reads = 0
        self.crl_path, self.crl_data = "", b""
        super().__init__(("127.0.0.1", 0), Handler)

    def finish_request(self, request, address):
        if not self.stall:
            return super().finish_request(request, address)
        try:
            while request.recv(4096):
                pass
        except OSError:
            pass

    def get_request(self):
        raw, address = super().get_request()
        raw.settimeout(3)
        with self.lock:
            self.accepted += 1
        if self.context is None:
            return raw, address
        try:
            return self.context.wrap_socket(raw, server_side=True), address
        except BaseException:
            raw.close()
            raise

    def counters(self):
        with self.lock:
            return self.accepted, self.posts, self.crl_reads


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def reply(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        with self.server.lock:
            match = self.path == self.server.crl_path and bool(self.server.crl_data)
            data = self.server.crl_data if match else b"not found"
            if match:
                self.server.crl_reads += 1
        self.reply(200 if match else 404, data, "application/pkix-crl")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/search" or not 0 <= length <= 1024:
            self.reply(400, b"invalid request", "text/plain")
            return
        body = self.rfile.read(length)
        with self.server.lock:
            self.server.posts += 1
        self.reply(200, body, "text/plain")


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        if thread.is_alive():
            raise RuntimeError("TLS fixture did not stop")


def certificates(directory, openssl, crl_url):
    def write(name, data):
        (directory / name).write_text(data, encoding="ascii")

    environment = dict(os.environ, OPENSSL_CONF=str(directory / "request.cnf"))

    def run(*arguments):
        completed = subprocess.run([openssl, *arguments], cwd=directory, env=environment,
                                   capture_output=True, text=True, timeout=15)
        if completed.returncode:
            raise RuntimeError(f"OpenSSL {arguments[0]} failed: {completed.stderr}")

    write("request.cnf", "[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=QCWS fixture leaf\n")
    for name in ("ca", "other-ca"):
        text = (f"[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=QCWS {uuid.uuid4().hex}\n"
                "[root]\nbasicConstraints=critical,CA:TRUE,pathlen:1\n"
                "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n"
                "authorityKeyIdentifier=keyid:always\n")
        if name == "ca":
            text += f"crlDistributionPoints=URI:{crl_url}\n"
        write(name + ".cnf", text)
        run("req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3",
            "-config", name + ".cnf", "-extensions", "root", "-keyout", name + ".key", "-out", name + ".pem")
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-config", "request.cnf",
        "-keyout", "leaf.key", "-out", "leaf.csr")
    (directory / "newcerts").mkdir()
    write("index.txt", "")
    write("serial", "01\n")
    write("crlnumber", "01\n")
    write("ca.cnf", """[ca]
default_ca=issuer
[issuer]
database=index.txt
serial=serial
crlnumber=crlnumber
new_certs_dir=newcerts
certificate=ca.pem
private_key=ca.key
default_md=sha256
default_days=1
default_crl_days=1
policy=policy
unique_subject=no
copy_extensions=none
crl_extensions=crl
[policy]
commonName=supplied
[crl]
authorityKeyIdentifier=keyid:always
""")
    now = dt.datetime.now(dt.timezone.utc)
    hour, day = dt.timedelta(hours=1), dt.timedelta(days=1)
    cases = {"valid": ("IP:127.0.0.1", now-hour, now+day),
             "wrong-san": ("DNS:fixture.invalid", now-hour, now+day),
             "expired": ("IP:127.0.0.1", now-2*day, now-day),
             "future": ("IP:127.0.0.1", now+day, now+2*day),
             "client-only": ("IP:127.0.0.1", now-hour, now+day)}
    for name, (san, start, end) in cases.items():
        write(name + ".cnf", "[leaf]\nbasicConstraints=critical,CA:FALSE\n"
              "keyUsage=critical,digitalSignature,keyEncipherment\n"
              f"extendedKeyUsage={'clientAuth' if name == 'client-only' else 'serverAuth'}\n"
              "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n"
              f"subjectAltName={san}\ncrlDistributionPoints=URI:{crl_url}\n"
              f"authorityInfoAccess=caIssuers;URI:{crl_url}.issuer,OCSP;URI:{crl_url}.ocsp\n")
        run("ca", "-batch", "-notext", "-config", "ca.cnf", "-in", "leaf.csr", "-out", name + ".pem",
            "-extfile", name + ".cnf", "-extensions", "leaf", "-startdate", start.strftime("%Y%m%d%H%M%SZ"),
            "-enddate", end.strftime("%Y%m%d%H%M%SZ"))
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-config", "request.cnf",
        "-subj", "/CN=QCWS fixture intermediate", "-keyout", "intermediate.key", "-out", "intermediate.csr")
    write("intermediate.cnf", "basicConstraints=critical,CA:TRUE,pathlen:0\n"
          "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n"
          "authorityKeyIdentifier=keyid:always\n")
    run("x509", "-req", "-in", "intermediate.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-set_serial", "101", "-days", "1", "-sha256", "-extfile", "intermediate.cnf", "-out", "intermediate.pem")
    run("x509", "-req", "-in", "leaf.csr", "-CA", "intermediate.pem", "-CAkey", "intermediate.key",
        "-set_serial", "102", "-days", "1", "-sha256", "-extfile", "valid.cnf", "-extensions", "leaf", "-out", "missing-chain.pem")
    write("full-chain.pem", (directory / "missing-chain.pem").read_text() +
          (directory / "intermediate.pem").read_text())
    run("ca", "-gencrl", "-config", "ca.cnf", "-out", "ca.crl.pem")
    run("crl", "-in", "ca.crl.pem", "-outform", "DER", "-out", "ca.crl")
    run("verify", "-CAfile", "ca.pem", "-CRLfile", "ca.crl.pem", "-crl_check_all", "-purpose",
        "sslserver", "-verify_ip", "127.0.0.1", "valid.pem")


def tls_server(directory, name):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(directory / (name + ".pem"), directory / "leaf.key")
    return Server(context)


def probe(executable, server, ca, expected, label):
    before = server.counters()
    result = subprocess.run([executable, "--tls-probe", f"https://127.0.0.1:{server.server_port}/search",
                             str(ca), expected], capture_output=True, text=True, timeout=12)
    after = server.counters()
    if result.returncode or after[0] <= before[0] or after[1]-before[1] != (expected == "success"):
        raise RuntimeError(f"{label}: exit={result.returncode} counters={before}->{after} {result.stderr}")
    print("PASS tls", label, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--OpenSSL", required=True)
    parser.add_argument("--Probe", required=True)
    args = parser.parse_args()
    openssl, executable = (str(Path(p).resolve(strict=True)) for p in (args.OpenSSL, args.Probe))
    with tempfile.TemporaryDirectory(prefix="qcws-tls-") as temporary:
        directory = Path(temporary)
        with serving(Server()) as crl:
            crl.crl_path = "/" + uuid.uuid4().hex + ".crl"
            certificates(directory, openssl, f"http://127.0.0.1:{crl.server_port}{crl.crl_path}")
            crl.crl_data = (directory / "ca.crl").read_bytes()
            ca = directory / "ca.pem"
            with serving(tls_server(directory, "valid")) as good:
                probe(executable, good, ca, "success", "trusted CA and IP SAN")
                if crl.counters()[0] != 0:
                    raise RuntimeError("Certificate verification bypassed the outbound policy")
                probe(executable, good, "", "reject", "missing CA")
                probe(executable, good, directory / "other-ca.pem", "reject", "unrelated CA")
                for name in ("wrong-san", "expired", "future", "client-only", "missing-chain"):
                    with serving(tls_server(directory, name)) as bad:
                        probe(executable, bad, ca, "reject", name)
                    probe(executable, good, ca, "success", "control after " + name)
                with serving(tls_server(directory, "full-chain")) as chained:
                    probe(executable, chained, ca, "success", "complete intermediate chain")
                if crl.counters()[0] != 0:
                    raise RuntimeError("AIA, OCSP or CRL caused an implicit network request")
                print("PASS tls no implicit AIA/OCSP/CRL requests", flush=True)
            # A real TCP peer accepts TLS ClientHello but never replies. The
            # library must still obey cancellation and its total deadline.
            with serving(Server(stall=True)) as stalled:
                for mode in ("timeout", "cancel"):
                    started = time.monotonic()
                    result = subprocess.run([executable, "--tls-" + mode + "-probe",
                        f"https://127.0.0.1:{stalled.server_port}/search", str(ca)],
                        capture_output=True, text=True, timeout=4)
                    if result.returncode or time.monotonic() - started > 2.5:
                        raise RuntimeError("Stalled TLS did not honor " + mode)
                    print("PASS tls stalled handshake " + mode, flush=True)


if __name__ == "__main__":
    main()
