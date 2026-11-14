"""Prepare fixed packaging tools in CI. The general bootstrap does not call this."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

PINS = {
    "sdk": ("https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.404/dotnet-sdk-10.0.404-win-x64.zip", "sha512", "330ddd2665b43cd2e428216a0d8ea1ac7bb19b5434fdc530e62010dc3b52823a620bf95489d628e1ed6f5122401793ee4887cca4a019122361a1231497f632ee"),
    "wix": ("https://api.nuget.org/v3-flatcontainer/wix/7.0.1/wix.7.0.1.nupkg", "sha512", "9c72a17d435e0c642f2ba6d78424a49526fcd743cc2d04bd1e053d1ecae2dd12d63faa6201c79577490e37756677f5a8d7df7f5498e64bf996018ebe17b37d70"),
    "ui": ("https://api.nuget.org/v3-flatcontainer/wixtoolset.ui.wixext/7.0.1/wixtoolset.ui.wixext.7.0.1.nupkg", "sha512", "b6b3e90e6a5b2c1fc8526a6defab69b2007cf984e99af009c748a1c72f47539d7dd9f853c57ac5384d6b9c2deaa9dffe2fe37d0e351d7f575e5be028de911d07"),
    "perl": ("https://github.com/StrawberryPerl/Perl-Dist-Strawberry/releases/download/SP_54231_64bit/strawberry-perl-5.42.3.1-64bit-portable.zip", "sha256", "6a081a811781c30aca51dbc036afd93092af91e3297901f02c17043795a10690"),
}

class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise RuntimeError("Refusing non-HTTPS download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def digest(path, algorithm):
    with path.open("rb") as source:
        return hashlib.file_digest(source, algorithm).hexdigest()

def download(key, cache):
    url, algorithm, expected = PINS[key]
    target = cache / url.rsplit("/", 1)[1]
    if target.exists():
        if digest(target, algorithm) != expected:
            raise RuntimeError("Corrupt cached tool: " + target.name)
        return target
    opener = urllib.request.build_opener(HttpsRedirect())
    for attempt in range(3):
        fd, name = tempfile.mkstemp(prefix=".download-", dir=cache)
        pending = Path(name)
        try:
            with os.fdopen(fd, "wb") as out, opener.open(url, timeout=90) as response:
                shutil.copyfileobj(response, out, length=1024 * 1024)
            if digest(pending, algorithm) != expected:
                raise RuntimeError("Checksum mismatch: " + target.name)
            pending.replace(target)
            return target
        except Exception:
            pending.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(attempt + 1)

def extract(archive, target, expected, prefix=None):
    marker = target / ".qcws-archive.json"
    identity = {"digest": expected, "prefix": prefix}
    if marker.is_file() and json.loads(marker.read_text()) == identity:
        return
    if target.exists():
        raise RuntimeError("Refusing unrecognized tool directory: " + str(target))
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=target.parent) as temporary:
        stage = Path(temporary).resolve() / "payload"
        stage.mkdir()
        seen = set()
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                raw = entry.filename
                if prefix and not raw.startswith(prefix):
                    continue
                part = PurePosixPath(raw)
                if any(c in raw for c in ("\\", ":", "\x00")) or part.is_absolute() or ".." in part.parts:
                    raise RuntimeError("Unsafe tool archive path")
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise RuntimeError("Unexpected tool archive link")
                key = str(part).casefold()
                if key in seen:
                    raise RuntimeError("Duplicate tool archive path")
                seen.add(key)
                output = stage.joinpath(*part.parts)
                if entry.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, output.open("xb") as out:
                        shutil.copyfileobj(source, out)
        if not seen:
            raise RuntimeError("Empty tool archive selection")
        (stage / ".qcws-archive.json").write_text(json.dumps(identity))
        stage.rename(target)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Root", type=Path)
    parser.add_argument("--IncludePerl", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows is required")
    root = (args.Root or Path(os.environ["RUNNER_TEMP"]) / "qcws-windows-tools").resolve()
    root.mkdir(parents=True, exist_ok=True)
    feed, sdk, tool = root / "feed", root / "dotnet-10.0.404", root / "wix-7.0.1"
    feed.mkdir(exist_ok=True)
    extract(download("sdk", feed), sdk, PINS["sdk"][2])
    download("wix", feed)
    download("ui", feed)
    from xml.sax.saxutils import escape
    nuget = root / "NuGet.Config"
    nuget.write_text('<configuration><packageSources><clear/><add key="verified-local" value="' +
        escape(str(feed), {'"': '&quot;'}) +
        '"/></packageSources><auditSources><clear/></auditSources></configuration>', encoding="utf-8")
    values = {
        "DOTNET_ROOT": str(sdk), "DOTNET_ROOT_X64": str(sdk),
        "DOTNET_CLI_HOME": str(root / "cli"), "NUGET_PACKAGES": str(root / "nuget"),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "true",
        "WIX": str(tool), "WIX_EXTENSIONS": str(root / "extensions"),
        "LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C",
    }
    env = {**os.environ, **values}
    env["PATH"] = os.pathsep.join([str(sdk), str(tool), env["PATH"]])
    def run(command):
        return subprocess.run(list(map(str, command)), cwd=root, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True).stdout.strip()
    dotnet, wix = sdk / "dotnet.exe", tool / "wix.exe"
    if run([dotnet, "--version"]) != "10.0.404":
        raise RuntimeError("Unexpected .NET SDK version")
    if not wix.is_file():
        run([dotnet, "tool", "install", "wix", "--version", "7.0.1", "--tool-path", tool,
             "--configfile", nuget, "--allow-roll-forward"])
    if not re.fullmatch(r"7\.0\.1(?:\+[^\s]+)?", run([wix, "--version"])):
        raise RuntimeError("Unexpected WiX version")
    extension = Path(values["WIX_EXTENSIONS"]) / ".wix/extensions/WixToolset.UI.wixext/7.0.1/wixext7/WixToolset.UI.wixext.dll"
    if not extension.is_file():
        run([wix, "extension", "add", "--global", "WixToolset.UI.wixext/7.0.1", "-acceptEula", "wix7"])
    if not extension.is_file():
        raise RuntimeError("Missing pinned WiX UI extension")
    tools = {"dotnet": str(dotnet), "wix": str(wix)}
    paths = [sdk, tool]
    if args.IncludePerl:
        perl_root = root / "strawberry-5.42.3.1"
        extract(download("perl", feed), perl_root, PINS["perl"][2], "perl/")
        perl = perl_root / "perl/bin/perl.exe"
        if run([perl, "-e", 'print "$^O|$^V"']) != "MSWin32|v5.42.3":
            raise RuntimeError("Unexpected native Perl version")
        tools["perl"] = str(perl)
        paths.append(perl.parent)
    output = root / "tools.json"
    output.write_text(json.dumps(tools, indent=2) + "\n", encoding="utf-8")
    values["QCWS_WINDOWS_TOOLS"] = str(output)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as out:
            for name, value in values.items():
                if "\n" in value or "\r" in value:
                    raise RuntimeError("Invalid GitHub environment value")
                out.write(f"{name}={value}\n")
        with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as out:
            for path in paths:
                out.write(str(path) + "\n")
    print(output)

if __name__ == "__main__":
    main()
