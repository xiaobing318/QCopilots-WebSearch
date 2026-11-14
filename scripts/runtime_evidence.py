"""Fingerprint the deployed executable and private runtime, independently of its path."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import platform
import sys


def host_info():
    """Record the actual kernel/platform, without a hostname or user identity."""
    result = {"system": platform.system(), "architecture": platform.machine()}
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        result["windows"] = {"major": version.major, "minor": version.minor,
                             "build": version.build, "product_type": version.product_type}
    elif sys.platform == "linux":
        release = platform.freedesktop_os_release()
        result["linux"] = {key: release.get(key, "") for key in ("ID", "VERSION_ID")}
    return result


def fingerprint(executable):
    executable = Path(executable).resolve(strict=True)
    root = executable.parent.parent
    entries = []
    candidates = {executable}
    for name in ("bin", "lib", "plugins"):
        folder = root / name
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file() and (path.suffix.lower() == ".dll" or ".so" in path.name
                                       or path.name == "qt.conf"):
                    candidates.add(path)
    for path in sorted(candidates, key=lambda value: value.relative_to(root).as_posix()):
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Runtime symlink escapes the deployment")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        entry = {"path": path.relative_to(root).as_posix(), "sha256": digest}
        if path.is_symlink():
            entry["link"] = path.readlink().as_posix()
        entries.append(entry)
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": entries}
