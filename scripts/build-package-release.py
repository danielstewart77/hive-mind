#!/usr/bin/env python3
"""Build the wheel and sdist of a hive-mind package for a release tag.

    scripts/build-package-release.py hive-surfaces-v0.2.0 [--out dist]

The tag is `<package>-vX.Y.Z`. The package is the folder `modules/<package>`.
The build refuses unless the number in that folder's pyproject.toml equals the
tag's number, so a tag can never name a version the files do not hold. Prints
the paths of what it built, one per line, and their sha256.

Needs `pip` and nothing else (it runs `pip wheel` and builds the sdist with
setuptools through the same isolated build backend).
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^(?P<package>[a-z][a-z0-9-]*)-v(?P<version>\d+\.\d+\.\d+)$")


def parse_tag(tag: str) -> tuple[str, str]:
    m = TAG.match(tag)
    if not m:
        raise SystemExit(f"{tag!r} is not <package>-vX.Y.Z")
    return m["package"], m["version"]


def package_version(package: str, root: Path = ROOT) -> str:
    path = root / "modules" / package / "pyproject.toml"
    if not path.is_file():
        raise SystemExit(f"{path} does not exist: no package {package!r} in modules/")
    return tomllib.loads(path.read_text())["project"]["version"]


def check_tag(tag: str, root: Path = ROOT) -> Path:
    """The package folder, once the tag's number equals pyproject's."""
    package, version = parse_tag(tag)
    have = package_version(package, root)
    if have != version:
        raise SystemExit(f"tag {tag} says {version} but modules/{package}/pyproject.toml says {have}")
    return root / "modules" / package


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tag")
    ap.add_argument("--out", default="dist")
    args = ap.parse_args(argv)
    folder = check_tag(args.tag)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    subprocess.run([py, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(out), str(folder)], check=True)
    subprocess.run([py, "-m", "pip", "install", "--quiet", "--target", str(out / ".sdist-tool"), "build"], check=True)
    env = {**__import__("os").environ, "PYTHONPATH": str(out / ".sdist-tool")}
    subprocess.run([py, "-m", "build", "--sdist", "--outdir", str(out), str(folder)], check=True, env=env)
    import shutil
    shutil.rmtree(out / ".sdist-tool", ignore_errors=True)
    for f in sorted(out.iterdir()):
        print(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
