"""Update the support package revision and hash entries in briefcase.toml.

Usage::

    python scripts/update_support.py <template-dir>

`<template-dir>` is the path to one of the four Briefcase template repo
checkouts (e.g. `~/beeware/templates/briefcase-macOS-app-template`). The
platform (macOS / iOS / windows / linux) is inferred from the directory's name
(see platforms.py), and used to select the correct upstream data source:

- macOS / iOS: GitHub releases of `beeware/Python-Apple-support`
  (per-Python-version release tags, e.g. `3.14-b11`).
- Windows: the Windows embeddable-package index published at
  https://www.python.org/ftp/python/index-windows.json, per AMD64/ARM64 host
  architecture.
- Linux: the latest GitHub release of
  `astral-sh/python-build-standalone`, per x86_64/aarch64 host architecture.

For every Python major.minor tag already listed in briefcase.toml's
`support_revision` / `support_package_hash` entries, this looks up the matching
upstream revision/hash and rewrites the entry in the normalized format (see
briefcase_toml.py). Tags not already present in briefcase.toml are never added.
If no matching upstream data is found for an already-listed tag, that tag is
left unchanged and a warning is printed to stderr.

A GitHub personal access token can be provided via the `GITHUB_TOKEN`
environment variable to raise the GitHub API's unauthenticated rate limit.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from _briefcase_toml import (
    apply_updates,
    entry_regex,
    find_conditional_blocks,
    read_toml,
    render,
    tags_present,
    write_toml,
)
from _github import asset_digest, fetch_all_releases, fetch_latest_release
from _platforms import briefcase_toml_path, detect_platform

REVISION_KEY = "support_revision"
HASH_KEY = "support_package_hash"

REVISION_ENTRY_RE = entry_regex(REVISION_KEY)
HASH_ENTRY_RE = entry_regex(HASH_KEY)

# --- macOS / iOS: beeware/Python-Apple-support ------------------------------

APPLE_SUPPORT_REPO = "beeware/Python-Apple-support"

# Matches release tags of the form "<python-version>-b<revision>", e.g. "3.14-b11".
APPLE_TAG_RE = re.compile(r"^(?P<py_version>\d+\.\d+)-b(?P<revision>\d+)$")


def _apple_support(
    platform: str,
    tags: set[str],
    opener,
) -> tuple[dict[str, str], dict[str, str]]:
    """Flat (no per-architecture split) revisions/hashes for macOS and iOS."""
    releases = fetch_all_releases(APPLE_SUPPORT_REPO, opener=opener)

    latest: dict[str, tuple[int, dict]] = {}
    for release in releases:
        match = APPLE_TAG_RE.match(release["tag_name"])
        if not match:
            continue
        py_version = match.group("py_version")
        revision = int(match.group("revision"))
        if py_version not in latest or revision > latest[py_version][0]:
            latest[py_version] = (revision, release)

    revisions: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for tag in sorted(tags):
        if tag not in latest:
            print(
                f"warning: no releases found for Python {tag}; leaving unchanged",
                file=sys.stderr,
            )
            continue
        revision, release = latest[tag]
        expected_name = f"Python-{tag}-{platform}-support.b{revision}.tar.gz"
        digest = asset_digest(release, expected_name)
        revisions[tag] = str(revision)
        hashes[tag] = digest
        print(f"{tag}: support_revision = {revision}, {digest}")
    return revisions, hashes


# --- Windows: python.org embeddable-package index ----------------------------

WINDOWS_INDEX_URL = "https://www.python.org/ftp/python/index-windows.json"

# "pythonembed-<major.minor>-<suffix>" suffix -> host_arch value.
WINDOWS_ARCH_FOR_SUFFIX = {"64": "AMD64", "arm64": "ARM64"}

WINDOWS_ID_RE = re.compile(r"^pythonembed-(?P<tag>\d+\.\d+)-(?P<suffix>64|arm64)$")
WINDOWS_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<micro>\d+)"
    r"(?:(?P<pre>a|b|rc)(?P<preno>\d+))?$"
)

WINDOWS_ARCH_ALTERNATIVES = {"host_arch": {"AMD64": "ARM64", "ARM64": "AMD64"}}


def _windows_version_sort_key(
    version: str,
) -> tuple[int, int, int, int, int] | None:
    """Sortable key for a CPython version string such as "3.13.9" or
    "3.15.0rc2". Final releases sort higher than pre-releases (alpha < beta
    < rc < final) for the same major.minor.micro. None if unparseable."""
    match = WINDOWS_VERSION_RE.match(version)
    if not match:
        return None
    pre_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[match.group("pre")]
    preno = int(match.group("preno")) if match.group("preno") else 0
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("micro")),
        pre_rank,
        preno,
    )


def _windows_revision_for(version: str) -> str:
    """The support revision component of a version string; e.g. "9" for
    "3.13.9", or "0rc2" for "3.15.0rc2"."""
    parts = version.split(".", 2)
    return parts[2] if len(parts) > 2 else "0"


def _windows_highest_versions(opener) -> dict[str, dict[str, tuple[str, str]]]:
    """Return the highest available micro version (and its hash) for every
    major.minor/host_arch combination found in the Windows embeddable
    package index. Result: {python_tag: {host_arch: (version, "algo:hexdigest")}}."""
    request = urllib.request.Request(WINDOWS_INDEX_URL)
    with opener(request) as response:
        index = json.load(response)

    best: dict[tuple[str, str], tuple[tuple, str, str]] = {}
    for entry in index["versions"]:
        match = WINDOWS_ID_RE.match(entry["id"])
        if not match:
            continue

        tag = match.group("tag")
        arch = WINDOWS_ARCH_FOR_SUFFIX[match.group("suffix")]
        version = entry["sort-version"]

        key = _windows_version_sort_key(version)
        if key is None:
            print(
                f"  -> skipping unparseable version {version!r} for {entry['id']}",
                file=sys.stderr,
            )
            continue

        algo, digest = next(iter(entry["hash"].items()))
        hash_str = f"{algo}:{digest}"

        current = best.get((tag, arch))
        if current is None or key > current[0]:
            best[(tag, arch)] = (key, version, hash_str)

    result: dict[str, dict[str, tuple[str, str]]] = {}
    for (tag, arch), (_, version, hash_str) in best.items():
        result.setdefault(tag, {})[arch] = (version, hash_str)
    return result


def _windows_support(
    tags: set[str], opener
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Flat revisions, plus per-architecture (AMD64/ARM64) hashes."""
    highest = _windows_highest_versions(opener)

    revisions: dict[str, str] = {}
    hashes_by_arch: dict[str, dict[str, str]] = {"AMD64": {}, "ARM64": {}}

    for tag in sorted(tags):
        variants = highest.get(tag)
        if not variants:
            print(
                f"warning: no index data found for Python {tag}; leaving unchanged",
                file=sys.stderr,
            )
            continue

        versions = {version for version, _ in variants.values()}
        if len(versions) > 1:
            print(
                f"warning: Python {tag} has differing highest versions "
                f"across architectures: {sorted(versions)}",
                file=sys.stderr,
            )
        version = next(iter(versions))
        revisions[tag] = _windows_revision_for(version)

        for arch, (_, hash_str) in variants.items():
            hashes_by_arch[arch][tag] = hash_str
            print(f"{tag} ({arch}): support_package_hash -> {hash_str} ({version})")

    return revisions, hashes_by_arch


# --- Linux: astral-sh/python-build-standalone ---------------------

PYTHON_BUILD_STANDALONE_REPO = "astral-sh/python-build-standalone"

LINUX_ARCH_TRIPLE = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}

LINUX_ARCH_ALTERNATIVES = {"host_arch": {"x86_64": "aarch64", "aarch64": "x86_64"}}


def _linux_support(
    tags: set[str], opener
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Flat revisions (full version string), plus per-architecture
    (x86_64/aarch64) hashes."""
    release = fetch_latest_release(PYTHON_BUILD_STANDALONE_REPO, opener=opener)
    print(f"Latest release of {PYTHON_BUILD_STANDALONE_REPO}: {release['tag_name']}")

    revisions: dict[str, str] = {}
    hashes_by_arch: dict[str, dict[str, str]] = {arch: {} for arch in LINUX_ARCH_TRIPLE}

    for tag in sorted(tags):
        versions_found: set[str] = set()
        for arch, triple in LINUX_ARCH_TRIPLE.items():
            pattern = re.compile(
                rf"^cpython-(?P<version>{re.escape(tag)}\.[^-]+)-{re.escape(triple)}"
                rf"-install_only_stripped\.tar\.gz$"
            )
            for asset in release.get("assets", []):
                match = pattern.match(asset["name"])
                if not match:
                    continue
                digest = asset.get("digest")
                if not digest:
                    raise ValueError(
                        f"Asset {asset['name']} has no digest reported by "
                        "the GitHub API"
                    )
                hashes_by_arch[arch][tag] = digest
                version = match.group("version")
                versions_found.add(version)
                print(f"{tag} ({arch}): support_package_hash -> {digest} ({version})")
                break
            else:
                print(
                    f"  -> no {triple} asset found for Python {tag} "
                    f"in release {release['tag_name']}",
                    file=sys.stderr,
                )

        if not versions_found:
            print(
                f"warning: no release data found for Python {tag}; leaving unchanged",
                file=sys.stderr,
            )
            continue
        if len(versions_found) > 1:
            print(
                f"warning: Python {tag} has differing versions across "
                f"architectures: {sorted(versions_found)}",
                file=sys.stderr,
            )
        revisions[tag] = next(iter(versions_found))

    return revisions, hashes_by_arch


# --- Dispatch -----------------------------------------------------------------


def update(template_dir: Path, opener=urllib.request.urlopen) -> None:
    platform = detect_platform(template_dir)
    toml_path = briefcase_toml_path(template_dir)
    text = read_toml(toml_path)
    lines = text.splitlines(keepends=True)

    tags = tags_present(lines, REVISION_ENTRY_RE, HASH_ENTRY_RE)

    to_delete: set[int] = set()

    if platform in {"macOS", "iOS"}:
        revisions, hashes = _apple_support(platform, tags, opener)
        to_delete |= apply_updates(lines, REVISION_ENTRY_RE, REVISION_KEY, revisions)
        to_delete |= apply_updates(lines, HASH_ENTRY_RE, HASH_KEY, hashes)

    elif platform == "windows":
        revisions, hashes_by_arch = _windows_support(tags, opener)
        to_delete |= apply_updates(lines, REVISION_ENTRY_RE, REVISION_KEY, revisions)
        blocks = find_conditional_blocks(
            lines, "host_arch", arch_alternatives=WINDOWS_ARCH_ALTERNATIVES
        )
        for (arch,), (start, end) in blocks.items():
            values = hashes_by_arch.get(arch, {})
            to_delete |= apply_updates(
                lines,
                HASH_ENTRY_RE,
                HASH_KEY,
                values,
                line_range=range(start, end + 1),
            )

    elif platform == "linux":
        revisions, hashes_by_arch = _linux_support(tags, opener)
        to_delete |= apply_updates(lines, REVISION_ENTRY_RE, REVISION_KEY, revisions)
        blocks = find_conditional_blocks(
            lines, "host_arch", arch_alternatives=LINUX_ARCH_ALTERNATIVES
        )
        for (arch,), (start, end) in blocks.items():
            values = hashes_by_arch.get(arch, {})
            to_delete |= apply_updates(
                lines,
                HASH_ENTRY_RE,
                HASH_KEY,
                values,
                line_range=range(start, end + 1),
            )

    else:
        raise ValueError(f"Unsupported platform for update_support.py: {platform!r}")

    write_toml(toml_path, render(lines, to_delete))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <template-dir>", file=sys.stderr)
        return 2

    update(Path(argv[1]))
    print("Updated support package revision and hash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
