"""Bootstrap decision parsing and privileged APT argument boundaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline


class PipelineTests(unittest.TestCase):
    def test_apt_proxy_survives_sudo_and_overrides_environment(self):
        for root in (True, False):
            for proxy in ("", "http://127.0.0.1:7897", "https://proxy.example:443"):
                with self.subTest(root=root, proxy=proxy):
                    command = pipeline.apt_command(proxy, root)
                    self.assertEqual(command[:1 if root else 3], ["/usr/bin/env"] if root else ["sudo", "-n", "--"])
                    self.assertIn("Acquire::http::Proxy=" + (proxy or "DIRECT"), command)
                    self.assertIn("Acquire::https::Proxy=" + (proxy or "DIRECT"), command)
                    self.assertIn(str(ROOT / "scripts/apt-proxy-reset.conf"), command)
                    for name in ("APT_CONFIG", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "no_proxy", "NO_PROXY"):
                        self.assertEqual(command[command.index(name) - 1], "-u")
        for value in (None, False, "http://user:pass@host", "http://host\n-oX=Y"):
            with self.assertRaises(RuntimeError):
                pipeline.apt_command(value, False)

    def test_offline_system_packages_never_install(self):
        instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
        instance.offline = True
        instance.run = lambda *_args: self.fail("offline bootstrap executed command")
        with patch.object(pipeline, "WIN", False), patch.object(pipeline.subprocess, "run") as query:
            query.return_value.returncode = 1
            query.return_value.stdout = ""
            with self.assertRaisesRegex(RuntimeError, "Offline bootstrap"):
                instance.ubuntu_dependencies()

    def parse_bootstrap(self, document, *args, encoding="utf-8"):
        with tempfile.TemporaryDirectory(prefix="qcws bootstrap '") as temporary:
            path = Path(temporary) / "configuration '.json"
            path.write_bytes(document.encode(encoding))
            env = {key: value for key, value in os.environ.items() if key not in
                   {"PERL5OPT", "PERL5LIB", "PERLLIB", "PERL_UNICODE", "PERL_JSON_PP_USE_B"}}
            return subprocess.run([PERL, "-I", str(ROOT / "scripts/vendor/perl"),
                str(ROOT / "scripts/bootstrap_config.pl"), str(path), "--Profile", "ubuntu-x64", *args],
                env=env, capture_output=True, timeout=5)

    def test_json_bootstrap_decisions(self):
        document = json.loads((ROOT / "config/BuildPipeline.json").read_text(encoding="utf-8"))
        profile = document["profiles"]["ubuntu-x64"]
        for offline in (False, True):
            profile.update(offline=offline, proxy="http://127.0.0.1:7897")
            result = self.parse_bootstrap(json.dumps(document), "--Stages=bootstrap,doctor", "--DryRun", "--CheckOnly")
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertEqual(result.stdout.split(b"\0"), [b"1", b"1" if offline else b"0", b"1", b"1", b"http://127.0.0.1:7897", b""])
        profile.update(offline=False, proxy="")
        result = self.parse_bootstrap(json.dumps(document), "--Stages", "bootstrap", encoding="utf-8-sig")
        self.assertEqual(result.returncode, 0)
        for field, value in (("offline", "false"), ("proxy", False), ("proxy", "http://u:p@host"), ("platform", "windows-x64")):
            bad = json.loads(json.dumps(document))
            bad["profiles"]["ubuntu-x64"][field] = value
            result = self.parse_bootstrap(json.dumps(bad), "--Stages", "bootstrap")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
        for raw in ("{", "[]", "[" * 65 + "0" + "]" * 65):
            self.assertNotEqual(self.parse_bootstrap(raw).returncode, 0)
        for args in (("--Stages", "bootstrap,bootstrap"), ("--Unknown", "x")):
            self.assertNotEqual(self.parse_bootstrap(json.dumps(document), *args).returncode, 0)

    def test_vendored_parser_integrity(self):
        files = {
            "JSON/PP.pm": "f7b434438a8b114421248807864aefb54755cf163b42d23778a3d3eadbbda055",
            "JSON/PP/Boolean.pm": "9331451a2000bf82d4fbb9fb97df6d429298fec20294ee927654b72ccfdd2006",
            "LICENSE-Artistic": "dd90d4f42e4dcadf5a7c09eea0189d93c7b37ae560c91f0f6d5233ed3b9292a2",
            "README.upstream": "224a2ee5d0c24d3cae0243c1fbafdbccdcd0b7294e7e8b4f3589d194f32c5eb3",
        }
        for name, digest in files.items():
            self.assertEqual(hashlib.sha256((ROOT / "scripts/vendor/perl" / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--Perl", required=True)
    args, rest = parser.parse_known_args()
    PERL = args.Perl
    unittest.main(argv=[sys.argv[0], *rest])
