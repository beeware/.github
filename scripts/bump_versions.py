# bump_versions.py - Bumps versions for Python packages not managed by Dependabot
#
# Usage
# -----
# $ python bump_versions.py [subdirectory]
#
# Finds pinned dependencies in pyproject.toml and tox.ini and updates them to the
# latest version available on PyPI.
#
# positional arguments:
#   subdirectory   Directory that contains pyproject.toml/tox.ini; defaults to
#                  current directory
# Dependencies
# ------------
# configupdater packaging requests tomlkit

from __future__ import annotations

import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from functools import lru_cache
from pathlib import Path
from shutil import get_terminal_size

import configupdater
import requests
import tomlkit
from packaging.requirements import InvalidRequirement, Requirement, SpecifierSet
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BumpVersionError(Exception):
    def __init__(self, msg: str, error_no: int):
        self.msg = msg
        self.error_no = error_no


def validate_directory(subdirectory: str) -> Path:
    subdirectory = Path.cwd() / subdirectory

    if subdirectory == Path.cwd() or Path.cwd() in subdirectory.parents:
        return subdirectory

    raise BumpVersionError(
        f"{subdirectory} is not a subdirectory of {Path.cwd()}", error_no=10
    )


def parse_args():
    width = max(min(get_terminal_size().columns, 80) - 2, 20)
    parser = ArgumentParser(
        description="Bumps versions for Python packages not managed by Dependabot",
        formatter_class=lambda prog: RawDescriptionHelpFormatter(prog, width=width),
    )
    parser.add_argument(
        "subdirectory",
        default=".",
        type=validate_directory,
        nargs="?",
        help=(
            "Directory that contains pyproject.toml/tox.ini; "
            "defaults to current directory"
        ),
    )

    args = parser.parse_args()
    print(f"\nEvaluating {args.subdirectory}")

    return args


def is_filepath_exist(filepath: Path) -> bool:
    if not filepath.exists():
        print(f"\nSkipping {filepath.relative_to(Path.cwd())}; not found")
        return False

    print(f"\n{filepath.relative_to(Path.cwd())}")
    return True


def read_toml_file(file_path: Path) -> tomlkit.TOMLDocument:
    with open(file_path, encoding="utf=8") as f:
        return tomlkit.load(f)


def read_ini_file(file_path: Path) -> configupdater.ConfigUpdater:
    config = configupdater.ConfigUpdater()
    with open(file_path, encoding="utf=8") as f:
        config.read_file(f)
    return config


@lru_cache
def http_session() -> requests.Session:
    sess = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(status_forcelist={500, 502, 504}))
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


@lru_cache
def latest_pypi_version(name: str) -> str | None:
    """Fetch the latest version for a package from PyPI."""
    resp = http_session().get(f"https://pypi.org/pypi/{name}/json", timeout=(3.1, 30))
    try:
        return resp.json()["info"]["version"]
    except KeyError:
        return None


def bump_version(req: str) -> str:
    """Bump the version for a requirement to its latest version.

    Requires the requirement only uses == operator for version.

    :param req: requirement to bump, e.g. build==1.0.5
    :returns: requirement with bumped version or input requirement if cannot bump
    """
    if req.startswith("#"):
        return req

    try:
        req_parsed = Requirement(req)
    except InvalidRequirement:
        print(f"  𐄂 {req}; invalid requirement")
        return req

    if not (latest_version := latest_pypi_version(req_parsed.name)):
        print(f"  𐄂 {req}; cannot determine latest version")
        return req

    if len(req_parsed.specifier) != 1:
        print(f"  𐄂 {req}; requires exactly one specifier (latest: {latest_version})")
        return req

    spec = next(iter(req_parsed.specifier))

    if spec.operator != "==":
        print(f"  𐄂 {req}; must use == operator (latest: {latest_version})")
        return req

    if spec.version != latest_version:
        print(f"  ↑ {req_parsed.name} from {spec.version} to {latest_version}")
        req_parsed.specifier = SpecifierSet(f"=={latest_version}")
        return str(req_parsed)
    else:
        print(f"  ✓ {req} is already the latest version")

    return req


def update_pyproject_toml(base_dir: Path):
    """Update pinned build-system requirements in pyproject.toml."""
    pyproject_path = base_dir / "pyproject.toml"

    if not is_filepath_exist(pyproject_path):
        return

    pyproject_toml = read_toml_file(pyproject_path)

    if build_requires := pyproject_toml.get("build-system", {}).get("requires", []):
        print(" build-system.requires")
        for idx, req in enumerate(build_requires.copy()):
            # update list directly to avoid losing existing formatting/comments
            build_requires[idx] = bump_version(req)

        pyproject_toml["build-system"]["requires"] = build_requires

    with open(pyproject_path, "w") as f:
        tomlkit.dump(pyproject_toml, f)


def update_tox_ini(base_dir: Path):
    """Update pinned requirements in tox.ini."""
    tox_ini_path = base_dir / "tox.ini"

    if not is_filepath_exist(tox_ini_path):
        return

    tox_ini = read_ini_file(tox_ini_path)

    for section in tox_ini:
        if reqs := tox_ini[section].get("deps"):
            print(f" {section.split('{')[0]}")
            tox_ini[section]["deps"].set_values(
                bump_version(req) for req in reqs.value.splitlines() if req
            )

    with open(tox_ini_path, "w", encoding="utf-8") as f:
        tox_ini.write(f)


def main():
    ret_code = 0
    try:
        args = parse_args()
        update_pyproject_toml(base_dir=args.subdirectory)
        update_tox_ini(base_dir=args.subdirectory)
    except BumpVersionError as e:
        print(e.msg)
        ret_code = e.error_no
    return ret_code


if __name__ == "__main__":
    sys.exit(main())
