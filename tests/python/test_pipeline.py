"""Bootstrap decision parsing and privileged APT argument boundaries."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import types
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline


class PipelineTests(unittest.TestCase):
    def test_profile_proxy_overrides_all_inherited_routes(self):
        for proxy in ('', 'http://proxy.example:7897'):
            with self.subTest(proxy=proxy), tempfile.TemporaryDirectory() as temporary:
                document = json.loads((ROOT / 'config/BuildPipeline.json').read_text(encoding='utf-8'))
                document['profiles'][pipeline.PLATFORM]['proxy'] = proxy
                config = Path(temporary) / 'build.json'
                config.write_text(json.dumps(document), encoding='utf-8')
                inherited = {key: 'http://stale.example:1234' for key in
                             ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy', 'PIP_PROXY')}
                inherited.update(NO_PROXY='*', no_proxy='*', NPM_CONFIG_HTTPS_PROXY='http://stale.example:1234',
                                 NPM_CONFIG_OFFLINE='true')
                with patch.dict(os.environ, inherited):
                    instance = pipeline.Pipeline(types.SimpleNamespace(ConfigPath=config, Profile=pipeline.PLATFORM, DryRun=False))
                self.assertEqual(instance.env.get('http_proxy'), proxy)
                self.assertEqual(instance.env.get('https_proxy'), proxy)
                for key in ('ALL_PROXY', 'all_proxy', 'PIP_PROXY'):
                    self.assertFalse(instance.env.get(key), key)
                for key in ('NO_PROXY', 'no_proxy'):
                    self.assertEqual(instance.env.get(key), '' if proxy else '*')
                self.assertEqual(instance.env['PIP_CONFIG_FILE'], os.devnull)
                self.assertEqual(instance.env['npm_config_https_proxy'], proxy)
                self.assertEqual(instance.env['npm_config_offline'], 'false')
                self.assertNotIn('NPM_CONFIG_HTTPS_PROXY', instance.env)

    def test_ci_profile_expands_web_artifact_path_in_one_argument(self):
        with tempfile.TemporaryDirectory(prefix="qcws web '") as directory:
            with patch.dict(os.environ, {"QCWS_WEB_DIST_DIR": directory}):
                profile = pipeline.Pipeline(types.SimpleNamespace(
                    ConfigPath=ROOT / "config/BuildPipeline.json",
                    Profile="ci-" + pipeline.PLATFORM,
                    DryRun=False,
                ))
            self.assertIn("-DQCWS_BUILD_WEB=OFF", profile.profile["cmakeArgs"])
            self.assertIn(f"-DQCWS_WEB_DIST_DIR={directory}", profile.profile["cmakeArgs"])
            self.assertIsNone(profile.node_environment(install=True))
            self.assertEqual(profile.preset, pipeline.PLATFORM)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bash"), "Requires native Bash")
    def test_bash_forwards_quoted_arguments_and_refuses_missing_python(self):
        wrapper = ROOT / "scripts/BuildPipeline.sh"
        with tempfile.TemporaryDirectory(prefix="qcws bash ") as directory:
            interpreter = Path(directory) / "python shim"
            interpreter.write_text(
                '#!/usr/bin/env bash\n'
                'if [[ "$1" == -I ]]; then exit 0; fi\n'
                'printf "%s\\0" "$@"\n',
                encoding="utf-8",
            )
            interpreter.chmod(0o700)
            options = ["--ConfigPath", "path with spaces/config.json", "--Profile", "ubuntu-x64"]
            environment = dict(os.environ, PYTHON=str(interpreter))
            completed = subprocess.run(["bash", wrapper, *options], env=environment,
                                       capture_output=True, check=True, timeout=10)
            self.assertEqual(completed.stdout.decode().split("\\0")[:-1],
                             [str(ROOT / "scripts/pipeline.py"), *options])
            environment["PYTHON"] = str(interpreter) + ".missing"
            missing = subprocess.run(["bash", wrapper, "--Stages", "bootstrap"], env=environment,
                                     capture_output=True, timeout=10)
            self.assertEqual(missing.returncode, 2)
            self.assertIn(b"Python 3.12 or newer is required", missing.stderr)

    def test_download_ignores_process_and_system_proxy_bypass(self):
        content = b'controlled archive'
        for proxy in ('', 'http://proxy.example:7897', 'https://proxy.example:7897'):
            with self.subTest(proxy=proxy), tempfile.TemporaryDirectory() as temporary:
                instance = pipeline.Pipeline.__new__(pipeline.Pipeline)
                instance.profile, instance.cache = {'proxy': proxy}, Path(temporary)
                instance.offline = instance.dry = False
                seen = []
                url = 'https://artifact.example/file'
                def capture(_handler, request):
                    seen.append((request.host, request._tunnel_host))
                    response = urllib.response.addinfourl(io.BytesIO(content), {}, url, 200)
                    response.msg = 'OK'
                    return response
                with patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*', 'ALL_PROXY': 'http://stale.example:1234'}), \
                        patch.object(urllib.request.HTTPSHandler, 'https_open', capture):
                    result = instance.download({'url': url, 'sha256': hashlib.sha256(content).hexdigest()})
                self.assertEqual(result.read_bytes(), content)
                self.assertEqual(seen, [('proxy.example:7897', 'artifact.example')] if proxy else [('artifact.example', None)])

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

if __name__ == "__main__":
    unittest.main()
