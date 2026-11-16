#!/usr/bin/env python3
"""Shared, hash-pinned Windows and Ubuntu build pipeline."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import venv
from build_evidence import identity, require_receipt, write_receipt, build_artifacts
from runtime_evidence import fingerprint

ROOT = Path(__file__).resolve().parents[1]
WIN = sys.platform == "win32"
PLATFORM = "windows-x64" if WIN else "ubuntu-x64"
STAGES = ("doctor", "bootstrap", "configure", "build", "test", "install", "publish")


class ConfiguredProxyHandler(urllib.request.ProxyHandler):
    def proxy_open(self, request, proxy, protocol):
        # The profile is authoritative. urllib's default ProxyHandler consults
        # process/system bypass rules even when given an explicit proxy map.
        parsed = urllib.parse.urlsplit(proxy)
        original = request.type
        request.set_proxy(parsed.netloc, parsed.scheme)
        if original != parsed.scheme and original != 'https':
            return self.parent.open(request, timeout=request.timeout)
        return None


def apt_command(proxy, root):
    if not isinstance(proxy, str) or not re.fullmatch(r"(?:|https?://[^\s@\x00-\x1f\x7f]+)", proxy):
        raise RuntimeError("Invalid bootstrap download proxy.")
    command = [] if root else ["sudo", "-n", "--"]
    command += ["/usr/bin/env"]
    for key in ("APT_CONFIG", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy", "no_proxy", "NO_PROXY"):
        command += ["-u", key]
    return command + ["DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get",
                      "-c", str(ROOT / "scripts/apt-proxy-reset.conf"),
                      "-o", "Acquire::http::Proxy=" + (proxy or "DIRECT"),
                      "-o", "Acquire::https::Proxy=" + (proxy or "DIRECT")]


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.config = json.loads(Path(args.ConfigPath).read_text(encoding="utf-8-sig"))
        self.profile = self.config["profiles"][args.Profile]
        self.lock = json.loads((ROOT / "config/dependencies.lock.json").read_text(encoding="utf-8"))
        self.env = dict(os.environ)
        for key in ("ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "PIP_PROXY"):
            self.env.pop(key, None)
        # Also suppress pip proxy settings in machine/user configuration files.
        self.env["PIP_CONFIG_FILE"] = os.devnull
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            self.env[key] = self.profile.get("proxy", "")
        for key in ("NO_PROXY", "no_proxy"):
            self.env[key] = "" if self.profile.get("proxy") else "*"
        self.env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        self.dry = args.DryRun
        self.build = self.path("buildDir")
        self.install = self.path("installDir")
        self.cache = self.path("cacheDir")
        self.qt_root = self.path("qtRoot")
        self.qt = self.lock["qt"][PLATFORM]
        self.qt_prefix = self.qt_root / self.qt["version"] / self.qt["directory"]
        self.kind = self.profile["buildType"]
        self.jobs = str(self.profile["jobs"])
        self.offline = self.profile.get("offline", False)
        self.tool_paths = self.profile.get("tools", {})
        if self.profile["platform"] != PLATFORM:
            raise RuntimeError("Selected profile does not match this operating system.")
        for path in (self.build, self.install, self.cache):
            if path in (ROOT, ROOT.parent, Path(path.anchor), self.qt_root, self.qt_prefix):
                raise RuntimeError("Build, install and cache paths must be dedicated directories.")

    def path(self, key):
        p = Path(self.profile[key]).expanduser()
        return (p if p.is_absolute() else ROOT / p).resolve()

    def run(self, command, capture=False, cwd=None):
        command = list(map(str, command))
        if command[0] in self.tool_paths and self.tool_paths[command[0]]:
            command[0] = str(Path(self.tool_paths[command[0]]).expanduser().resolve())
        print("+", subprocess.list2cmdline(command), flush=True)
        if self.dry:
            return ""
        return subprocess.run(command, cwd=cwd or ROOT, env=self.env, check=True, text=True,
                              encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE if capture else None).stdout or ""

    def tools(self):
        directory = self.cache / "tools" / f"{PLATFORM}-py{sys.version_info.major}.{sys.version_info.minor}"
        python = directory / ("Scripts/python.exe" if WIN else "bin/python")
        if os.path.normcase(str(Path(sys.prefix).resolve())) == os.path.normcase(str(directory)):
            return
        if not python.is_file():
            if self.dry:
                print("Tool environment missing. Dry run will not install it.")
                return
            if "bootstrap" not in self.args.Stages or self.args.CheckOnly:
                raise RuntimeError("Tooling is missing. Run --Stages bootstrap first.")
            directory.parent.mkdir(parents=True, exist_ok=True)
            venv.EnvBuilder(with_pip=True).create(directory)
        if "bootstrap" in self.args.Stages and not self.args.CheckOnly:
            wheels = self.cache / "wheels" / f"{PLATFORM}-py{sys.version_info.major}.{sys.version_info.minor}"
            if not self.dry:
                wheels.mkdir(parents=True, exist_ok=True)
            if not self.offline:
                self.run([python, "-m", "pip", "download", "--only-binary=:all:", "--require-hashes",
                          "--cache-dir", self.cache / "pip", "--dest", wheels,
                          "-r", ROOT / "scripts/requirements-tools.lock"])
            self.run([python, "-m", "pip", "install", "--no-index", "--find-links", wheels,
                      "--only-binary=:all:", "--require-hashes", "-r", ROOT / "scripts/requirements-tools.lock"])
        if not self.dry:
            raise SystemExit(subprocess.call([str(python), __file__, *sys.argv[1:]], cwd=ROOT, env=self.env))

    def validate(self):
        try:
            import jsonschema
        except ImportError:
            if self.dry:
                print("Dry run: full schema validation unavailable until bootstrap.")
                return
            raise RuntimeError("Missing pinned schema validator. Run bootstrap.") from None
        schema = json.loads((ROOT / "config/BuildPipeline.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft7Validator.check_schema(schema)
        if not jsonschema.Draft7Validator(schema).is_valid(self.config):
            raise RuntimeError("Build configuration does not match BuildPipeline.schema.json.")

    def windows_environment(self):
        if not WIN:
            return
        where = Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio/Installer/vswhere.exe"
        studio = self.tool_paths.get("visualStudio", "")
        if not studio and not where.is_file():
            raise RuntimeError("Install VS2022 with the Desktop development with C++ workload.")
        if not studio:
            studio = subprocess.check_output([str(where), "-latest", "-products", "*", "-version", "[17.0,18.0)",
                "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], text=True).strip()
        if not studio:
            raise RuntimeError("VS2022 x64 C++ tools were not found. Install them manually.")
        bat = Path(studio) / "Common7/Tools/VsDevCmd.bat"
        command = f'cmd.exe /d /u /s /c ""{bat}" -no_logo -arch=amd64 >nul && set"'
        output = subprocess.check_output(command, env=self.env).decode("utf-16le")
        for line in output.splitlines():
            key, sep, value = line.partition("=")
            if sep and key:
                self.env[key] = value
        cmake = Path(studio) / "Common7/IDE/CommonExtensions/Microsoft/CMake"
        self.env["PATH"] = os.pathsep.join([str(cmake / "CMake/bin"), str(cmake / "Ninja"), self.env.get("PATH", "")])

    def configured_tools(self):
        directories = []
        for name, value in self.tool_paths.items():
            if not value or name == "visualStudio":
                continue
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise RuntimeError(f"Configured {name} executable does not exist: {path}")
            directories.append(str(path.parent))
        self.env["PATH"] = os.pathsep.join([*directories, self.env.get("PATH", "")])

    def ubuntu_dependencies(self):
        if WIN:
            return
        packages = ["build-essential", "cmake", "ninja-build", "git", "python3", "python3-venv", "python3-pip",
                    "pkg-config", "dpkg-dev", "libssl-dev", "ca-certificates", "libglib2.0-0t64", "libzstd1", "zlib1g",
                    "libgssapi-krb5-2", "libbrotli1", "libssl3t64"]
        missing = []
        for package in packages:
            result = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package], capture_output=True, text=True)
            if result.returncode or "install ok installed" not in result.stdout:
                missing.append(package)
        if missing:
            if self.offline:
                raise RuntimeError("Offline bootstrap requires preinstalled system packages: " + ", ".join(missing))
            command = apt_command(self.profile["proxy"], os.geteuid() == 0)
            self.run([*command, "update", "--error-on=any"])
            self.run([*command, "install", "-y", "--no-install-recommends", *missing])

    @staticmethod
    def digest(path):
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def download(self, artifact):
        sha, url = artifact["sha256"], artifact["url"]
        if not re.fullmatch("[0-9a-f]{64}", sha) or not url.startswith("https://"):
            raise RuntimeError("Invalid dependency lock entry.")
        target = self.cache / "downloads" / sha
        if target.is_file() and self.digest(target) == sha:
            return target
        if self.offline:
            raise RuntimeError("Offline cache is missing verified artifact " + sha)
        print(f"Download pinned artifact {sha[:16]}", flush=True)
        if self.dry:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        proxy = self.profile.get("proxy", "")
        opener = urllib.request.build_opener(ConfiguredProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
        for attempt in range(3):
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
                    temporary = Path(out.name)
                    with opener.open(url, timeout=60) as response:
                        if not response.url.startswith("https://"):
                            raise RuntimeError("Dependency redirected away from HTTPS.")
                        shutil.copyfileobj(response, out, 1024 * 1024)
                if self.digest(temporary) != sha:
                    raise RuntimeError("Dependency SHA-256 mismatch.")
                os.replace(temporary, target)
                return target
            except (OSError, RuntimeError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        raise AssertionError("Unreachable")

    def verify_qt(self):
        qmake = self.qt_prefix / "bin" / ("qmake.exe" if WIN else "qmake")
        version = self.run([qmake, "-query", "QT_VERSION"], True).strip()
        prefix = self.run([qmake, "-query", "QT_INSTALL_PREFIX"], True).strip()
        if not self.dry and (version != "6.12.0" or os.path.normcase(str(Path(prefix).resolve())) != os.path.normcase(str(self.qt_prefix))):
            raise RuntimeError("Qt version or relocated install prefix is incorrect.")
        self.env["PATH"] = str(self.qt_prefix / "bin") + os.pathsep + self.env.get("PATH", "")
        if not WIN:
            self.env["LD_LIBRARY_PATH"] = str(self.qt_prefix / "lib")

    def bootstrap(self):
        if self.qt_prefix.is_dir():
            self.verify_qt()
            return
        artifacts = [(a, self.download(a)) for a in self.qt["artifacts"]]
        if self.dry:
            print(f"Would extract verified Qt into {self.qt_prefix}")
            return
        import py7zr
        from aqt.archives import TargetConfig
        from aqt.updater import Updater
        self.qt_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".qcws-", dir=self.qt_root) as tmp:
            stage = Path(tmp) / "prefix"
            stage.mkdir()
            for spec, archive in artifacts:
                destination = stage / spec["destination"]
                destination.mkdir(exist_ok=True)
                with py7zr.SevenZipFile(archive, "r", max_extract_size=2 * 1024**3) as packed:
                    for member in packed.files:
                        name = member.filename.replace("\\", "/")
                        path = PurePosixPath(name)
                        if path.is_absolute() or ".." in path.parts or ":" in name:
                            raise RuntimeError("Unsafe Qt archive member.")
                        if WIN and not (member.is_file or member.is_directory):
                            raise RuntimeError("Unexpected Windows Qt member type.")
                    packed.extractall(destination)
            for entry in stage.rglob("*"):
                if entry.is_symlink() and not entry.resolve().is_relative_to(stage.resolve()):
                    raise RuntimeError("Qt archive link escapes its installation prefix.")
            self.qt_prefix.parent.mkdir(parents=True, exist_ok=True)
            stage.rename(self.qt_prefix)
            try:
                Updater.update(TargetConfig("6.12.0", "desktop", self.qt["arch"], "windows" if WIN else "linux"), self.qt_root, None)
                self.verify_qt()
            except BaseException:
                self.qt_prefix.rename(stage)
                raise

    def doctor(self):
        if platform.machine().lower() not in ("amd64", "x86_64"):
            raise RuntimeError("Only amd64/x86_64 is supported.")
        missing = [t for t in ("cmake", "ninja", "git") if not self.tool_paths.get(t) and not shutil.which(t, path=self.env.get("PATH"))]
        if missing:
            raise RuntimeError("Install missing build tools: " + ", ".join(missing))
        output = self.run(["cmake", "--version"], True)
        match = re.search(r"cmake version (\d+)\.(\d+)", output)
        if not self.dry and (not match or tuple(map(int, match.groups())) < (3, 28)):
            raise RuntimeError("CMake 3.28 or newer is required.")
        for tool in ("ninja", "git"):
            self.run([tool, "--version"])
        if WIN:
            self.native_perl()
        print("Host:", platform.platform())
        self.verify_qt()

    def source(self, artifact):
        archive = self.download(artifact)
        destination = self.cache / "sources" / artifact["sha256"]
        marker = destination / ".complete"
        if not marker.is_file() and not self.dry:
            if destination.exists():
                raise RuntimeError("Incomplete source cache. Move it aside before retrying: " + str(destination))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".source-", dir=destination.parent) as tmp:
                with tarfile.open(archive, "r:*") as packed:
                    packed.extractall(tmp, filter="data")
                Path(tmp, ".complete").write_text(artifact["sha256"], encoding="ascii")
                Path(tmp).rename(destination)
        return destination / artifact["sourceDir"]

    def native_perl(self):
        if not self.tool_paths.get("perl") and not shutil.which("perl", path=self.env.get("PATH")):
            raise RuntimeError("Install native Strawberry Perl or set tools.perl. Windows bootstrap only installs Qt, not compiler tools.")
        if self.run(["perl", "-e", "print $^O"], True).strip() != "MSWin32" and not self.dry:
            raise RuntimeError("OpenSSL requires native Windows Perl. Git/MSYS/Cygwin Perl is not supported.")

    def patched_httplib(self, source):
        script = ROOT / "cmake/patches/httplib_input_lifetime.py"
        patch_hash = self.digest(script)
        destination = self.build / "dependencies" / ("httplib-" + patch_hash[:16])
        expected = "533bd907cfb5e018a11bcc1bf0f87983015c0a8b8ee6677d1540265535f02414"
        if not self.dry:
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".httplib-", dir=destination.parent) as tmp:
                    stage = Path(tmp) / "source"
                    shutil.copytree(source, stage)
                    spec = importlib.util.spec_from_file_location("qcws_httplib_patch", script)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    header = stage / "httplib.h"
                    header.write_bytes(module.patch(header.read_bytes()))
                    if self.digest(header) != expected:
                        raise RuntimeError("Patched cpp-httplib checksum mismatch")
                    stage.rename(destination)
            if self.digest(destination / "httplib.h") != expected:
                raise RuntimeError("Modified cpp-httplib build copy")
        return destination

    def windows_openssl(self):
        if not WIN:
            return []
        self.native_perl()
        spec = self.lock["openssl_windows"]
        verified_source = self.source(spec)
        directory = self.build / "dependencies" / ("openssl-" + spec["sha256"][:12])
        source = directory / "source"
        build = directory / "build"
        prefix = directory / "installed"
        if not self.dry:
            build.mkdir(parents=True, exist_ok=True)
            # Native Perl resolves relative source paths before normalizing
            # parent segments. Keep this build's source adjacent to its output
            # instead of traversing back into the long SHA-keyed download cache.
            if not source.is_dir():
                shutil.copytree(verified_source, source)
        marker = directory / "qcws-complete.json"
        identity = {"source": spec["sha256"], "compiler": self.env.get("VCToolsVersion", ""),
                    "sdk": self.env.get("WindowsSDKVersion", ""),
                    "options": ["VC-WIN64A", "shared", "no-asm", "no-autoload-config", "no-module", "no-engine", "no-tests"]}
        complete = marker.is_file() and json.loads(marker.read_text()) == identity
        complete = complete and all((prefix / f"bin/{name}-3-x64.dll").is_file() for name in ("libssl", "libcrypto"))
        if not complete:
            self.run(["perl", source / "Configure", *identity["options"], f"--prefix={prefix}", "--libdir=lib"], cwd=build)
            self.run(["nmake", "/nologo", "build_libs"], cwd=build)
            self.run(["nmake", "/nologo", "install_dev"], cwd=build)
            if not self.dry:
                marker.write_text(json.dumps(identity), encoding="utf-8")
        return [f"-DQCWS_OPENSSL_PREFIX={prefix}", f"-DQCWS_OPENSSL_SOURCE_DIR={source}"]

    def configure(self):
        self.invalidate_receipts('configure', 'build', 'test', 'install')
        current = identity(ROOT, self.profile) if not self.dry else None
        self.verify_qt()
        qt_source = self.source(self.lock["qt_source"])
        redistribution = [f"-DQCWS_{name.upper()}={self.download(spec)}"
                          for name, spec in self.lock["redistribution"].items()]
        source = self.source(self.lock["zlib"])
        dep_build, prefix = self.build / "dependencies/zlib-build", self.build / "dependencies/prefix"
        common = [f"-DCMAKE_BUILD_TYPE={self.kind}", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON"]
        if self.tool_paths.get("ninja"):
            common.append(f"-DCMAKE_MAKE_PROGRAM={Path(self.tool_paths['ninja']).expanduser().resolve()}")
        self.run(["cmake", "-S", source, "-B", dep_build, "-G", "Ninja", *common, f"-DCMAKE_INSTALL_PREFIX={prefix}",
                  "-DCMAKE_INSTALL_LIBDIR=lib", "-DZLIB_BUILD_SHARED=OFF", "-DZLIB_BUILD_STATIC=ON", "-DZLIB_BUILD_TESTING=OFF", "-DZLIB_INSTALL=ON"])
        self.run(["cmake", "--build", dep_build, "--parallel", self.jobs])
        self.run(["cmake", "--install", dep_build])
        sources = []
        for name, spec in self.lock["sources"].items():
            prepared = self.source(spec)
            if name == "httplib":
                prepared = self.patched_httplib(prepared)
            sources.append(f"-DFETCHCONTENT_SOURCE_DIR_{name.upper()}={prepared}")
        tls = self.windows_openssl()
        self.run(["cmake", "-S", ROOT, "-B", self.build, "-G", "Ninja", *common, f"-DCMAKE_PREFIX_PATH={self.qt_prefix}",
                  f"-DCMAKE_INSTALL_PREFIX={self.install}", f"-DQCWS_DEPENDENCY_PREFIX={prefix}", "-DQCWS_BUILD_TESTING=ON",
                  f"-DPython3_EXECUTABLE={sys.executable}",
                  f"-DQCWS_QT_SOURCE_DIR={qt_source}",
                  f"-DQCWS_ZLIB_SOURCE_DIR={source}",
                  *sources, *redistribution, *tls, *self.profile.get("cmakeArgs", [])])
        if not self.dry:
            if identity(ROOT,self.profile) != current:
                raise RuntimeError('Source changed during configure')
            write_receipt(self.build / 'configure-receipt.json', current)

    def invalidate_receipts(self, *stages):
        if not self.dry:
            for stage in stages:
                (self.build / (stage + '-receipt.json')).unlink(missing_ok=True)

    def build_stage(self):
        self.invalidate_receipts('build', 'test', 'install')
        current = identity(ROOT, self.profile) if not self.dry else None
        if not self.dry:
            require_receipt(self.build / 'configure-receipt.json', current)
        self.run(["cmake", "--build", self.build, "--parallel", self.jobs])
        if not self.dry:
            if identity(ROOT, self.profile) != current:
                raise RuntimeError("Source changed during build")
            write_receipt(self.build / "build-receipt.json", current,
                          artifacts=build_artifacts(self.build,self.qt_prefix,WIN))

    def test(self):
        self.invalidate_receipts('test', 'install')
        current = identity(ROOT, self.profile) if not self.dry else None
        if not self.dry:
            built = require_receipt(self.build / "build-receipt.json", current)
            if built['artifacts'] != build_artifacts(self.build,self.qt_prefix,WIN):
                raise RuntimeError('Build artifacts changed before tests')
        self.verify_qt()
        self.run(["ctest", "--test-dir", self.build, "--output-on-failure", "--no-tests=error", "--parallel", self.jobs,
                  "--output-junit", self.build / "test-results.xml"])
        if not self.dry:
            if identity(ROOT, self.profile) != current:
                raise RuntimeError("Source changed during tests")
            if built['artifacts'] != build_artifacts(self.build,self.qt_prefix,WIN):
                raise RuntimeError('Build artifacts changed during tests')
            write_receipt(self.build / "test-receipt.json", current,
                          artifacts=built['artifacts'],
                          junit_sha256=self.digest(self.build / "test-results.xml"))

    def install_stage(self):
        self.invalidate_receipts('install')
        current = identity(ROOT, self.profile) if not self.dry else None
        if not self.dry:
            tested = require_receipt(self.build / "test-receipt.json", current)
            if tested['artifacts'] != build_artifacts(self.build,self.qt_prefix,WIN):
                raise RuntimeError('Build artifacts changed after tests')
        self.run(["cmake", "--install", self.build])
        if not self.dry:
            if identity(ROOT, self.profile) != current:
                raise RuntimeError("Source changed during installation")
            if tested['artifacts'] != build_artifacts(self.build,self.qt_prefix,WIN):
                raise RuntimeError('Build artifacts changed during installation')
            relative = "bin/qcopilots-websearch.exe" if WIN else "opt/qcopilots-websearch/bin/qcopilots-websearch"
            write_receipt(self.build / "install-receipt.json", current,
                          tested_artifacts=tested['artifacts'],
                          runtime=fingerprint(self.install / relative))

    def publish(self):
        if not self.dry:
            current = identity(ROOT, self.profile)
            installed = require_receipt(self.build / "install-receipt.json", current)
            relative = "bin/qcopilots-websearch.exe" if WIN else "opt/qcopilots-websearch/bin/qcopilots-websearch"
            if installed['runtime'] != fingerprint(self.install / relative):
                raise RuntimeError("Installed runtime changed after validation")
        if WIN and "WIX" in self.profile["packageGenerators"]:
            version = self.run(["wix", "--version"], True).strip()
            if not self.dry and not version.startswith("7.0.1"):
                raise RuntimeError("Packaging requires official WiX 7.0.1. See docs/build.md.")
            self.run(["dotnet", "--list-runtimes"])
        destination = self.build / "packages" / time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        for generator in self.profile["packageGenerators"]:
            self.run(["cpack", "--config", self.build / "CPackConfig.cmake", "-G", generator, "-B", destination])
        if not self.dry:
            packages = sorted(p for p in destination.iterdir() if p.is_file() and p.suffix.lower() in (".zip", ".msi", ".deb"))
            if len(packages) != len(self.profile["packageGenerators"]):
                raise RuntimeError("Incomplete package set.")
            from release_assets import generate
            generate(self, destination, packages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ConfigPath", default=str(ROOT / "config/BuildPipeline.json"))
    parser.add_argument("--Profile", required=True)
    parser.add_argument("--Stages", default="doctor,configure,build,test")
    parser.add_argument("--CheckOnly", action="store_true")
    parser.add_argument("--DryRun", action="store_true")
    args = parser.parse_args()
    args.Stages = args.Stages.split(",")
    if len(set(args.Stages)) != len(args.Stages) or any(s not in STAGES for s in args.Stages):
        parser.error("Stages must be unique names from: " + ",".join(STAGES))
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required.")
    pipeline = Pipeline(args)
    pipeline.windows_environment()
    pipeline.configured_tools()
    if "bootstrap" in args.Stages and not args.CheckOnly:
        pipeline.ubuntu_dependencies()
    pipeline.tools()
    pipeline.validate()
    actions = {"doctor": pipeline.doctor, "bootstrap": pipeline.bootstrap, "configure": pipeline.configure,
               "build": pipeline.build_stage, "test": pipeline.test, "install": pipeline.install_stage, "publish": pipeline.publish}
    for stage in ("doctor",) if args.CheckOnly else args.Stages:
        print("Stage:", stage, flush=True)
        actions[stage]()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print("Pipeline failed:", error, file=sys.stderr)
        raise SystemExit(1)
