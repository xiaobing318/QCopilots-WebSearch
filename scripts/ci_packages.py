"""Select the exact package files produced by one CI build."""

import re
import subprocess
from pathlib import Path


def run_packaging(root, command):
    def directories():
        return {directory for directory in root.glob("*") if directory.is_dir()}

    existing = directories()
    subprocess.run(command, check=True)
    created = directories() - existing
    if len(created) != 1:
        raise RuntimeError(f"Expected one new package directory under {root}")
    directory = created.pop()
    if directory.is_symlink():
        raise RuntimeError(f"Package directory is a symbolic link: {directory}")
    return directory.resolve()


def select_packages(directory, *, windows, project=Path("CMakeLists.txt")):
    version = re.search(r'^set\(QCWS_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"', project.read_text(encoding="utf-8"),
                        re.MULTILINE)
    if version is None:
        raise RuntimeError(f"Cannot read QCWS_VERSION from {project}")

    prefix = f"QCopilots-WebSearch-{version[1]}"
    expected = {".zip": f"{prefix}-win-amd64.zip", ".msi": f"{prefix}-win-amd64.msi"} if windows else {
        ".deb": f"{prefix}-linux-amd64.deb",
    }
    packages = {path.name: path for path in directory.iterdir() if path.suffix.lower() in {".zip", ".msi", ".deb"}}
    if packages.keys() != set(expected.values()) or any(not path.is_file() or path.is_symlink()
                                                        for path in packages.values()):
        raise RuntimeError(f"Expected package files {sorted(expected.values())} under {directory}, found {sorted(packages)}")
    return {suffix: packages[name] for suffix, name in expected.items()}
