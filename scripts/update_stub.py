"""Update the stub binary revision and hash entries in briefcase.toml.

Usage::

    python scripts/update_stub.py <template-dir>

`<template-dir>` is the path to one of the four Briefcase template repo
checkouts. The platform (macOS / Windows) is inferred from the directory's
name (see platforms.py):

- macOS: the latest GitHub release of `beeware/briefcase-macOS-Xcode-template`
  (tag `b<revision>`), which publishes the stub binaries as release assets
  named `<AppPrefix>-<L?>Stub-<python-tag>-b<revision>.zip`, one for each
  combination of framework/non-framework (`use_framework`) and console/GUI
  app (`console_app`).
- Windows: the latest GitHub release of
  `beeware/briefcase-windows-VisualStudio-template` (tag `b<revision>`),
  which publishes assets named
  `<AppPrefix>-Stub-<python-tag>-<amd64|arm64>-b<revision>.zip`, one for
  each combination of host architecture (`host_arch`) and console/GUI app
  (`console_app`).

Neither iOS nor Linux (flatpak) templates have a `stub_binary_revision` /
`stub_binary_hash` structure in their `briefcase.toml` at all, so running
this script against one of those template directories fails with the same
"Could not find stub_binary_revision in briefcase.toml" error it would raise
for any other briefcase.toml that's missing that structure -- there is no
separate platform allow-list.

For every Python tag already listed in a `stub_binary_hash` dict, if a
matching upstream asset is found its hash is updated; if not, that tag's
entry is removed entirely (a missing stub binary means that variant isn't
available for that Python version yet, unlike a missing support package,
which is left unchanged instead).

A GitHub personal access token can be provided via the `GITHUB_TOKEN`
environment variable to raise the GitHub API's unauthenticated rate limit.
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

from _briefcase_toml import (
    entry_regex,
    find_conditional_blocks,
    read_toml,
    render,
    render_entry,
    scalar_present,
    update_scalar,
    write_toml,
)
from _github import fetch_latest_release
from _platforms import briefcase_toml_path, detect_platform

HASH_KEY = "stub_binary_hash"
REVISION_KEY = "stub_binary_revision"
HASH_ENTRY_RE = entry_regex(HASH_KEY)

# Matches release tags of the form "b<revision>", e.g. "b16".
_TAG_RE = re.compile(r"^b(?P<revision>\d+)$")


class StubSource(NamedTuple):
    repo: str
    block_flags: tuple[str, str]
    arch_alternatives: dict[str, dict[str, str]] | None
    parse_variants: Callable[[dict], dict[tuple, dict[str, str]]]
    variant_name: Callable[[tuple], str]


# --- macOS: beeware/briefcase-macOS-Xcode-template --------------------------

# Matches an asset name like "Console-LStub-3.13-b16.zip" or "GUI-Stub-3.14-b16.zip".
_MACOS_ASSET_RE = re.compile(
    r"^(?P<app_prefix>Console|GUI)-(?P<stub_prefix>L?)Stub-"
    r"(?P<py_tag>\d+\.\d+)-b(?P<revision>\d+)\.zip$"
)


def _macOS_variants(release: dict) -> dict[tuple[bool, bool], dict[str, str]]:
    """Map (use_framework, console_app) -> {python_tag: digest}."""
    variants: dict[tuple[bool, bool], dict[str, str]] = {}
    for asset in release.get("assets", []):
        match = _MACOS_ASSET_RE.match(asset["name"])
        if not match:
            continue

        use_framework = match.group("stub_prefix") == ""
        console_app = match.group("app_prefix") == "Console"
        python_tag = match.group("py_tag")

        digest = asset.get("digest")
        if not digest:
            raise ValueError(
                f"Asset {asset['name']} has no digest reported by the GitHub API"
            )
        variants.setdefault((use_framework, console_app), {})[python_tag] = digest
    return variants


def _macos_variant_name(key: tuple[bool, bool]) -> str:
    use_framework, console_app = key
    return (
        f"{'framework' if use_framework else 'non-framework'}, "
        f"{'console' if console_app else 'GUI'}"
    )


# --- Windows: beeware/briefcase-windows-VisualStudio-template ---------------

# Matches an asset name like "Console-Stub-3.11-amd64-b13.zip" or
# "GUI-Stub-3.14-arm64-b13.zip".
_WINDOWS_ASSET_RE = re.compile(
    r"^(?P<app_prefix>Console|GUI)-Stub-(?P<py_tag>\d+\.\d+)-"
    r"(?P<arch>amd64|arm64)-b(?P<revision>\d+)\.zip$"
)

_WINDOWS_ARCH_ALTERNATIVES = {"host_arch": {"AMD64": "ARM64", "ARM64": "AMD64"}}


def _windows_variants(release: dict) -> dict[tuple[str, bool], dict[str, str]]:
    """Map (host_arch, console_app) -> {python_tag: digest}."""
    variants: dict[tuple[str, bool], dict[str, str]] = {}
    for asset in release.get("assets", []):
        match = _WINDOWS_ASSET_RE.match(asset["name"])
        if not match:
            continue

        arch = match.group("arch").upper()
        console_app = match.group("app_prefix") == "Console"
        python_tag = match.group("py_tag")

        digest = asset.get("digest")
        if not digest:
            raise ValueError(
                f"Asset {asset['name']} has no digest reported by the GitHub API"
            )
        variants.setdefault((arch, console_app), {})[python_tag] = digest
    return variants


def _windows_variant_name(key: tuple[str, bool]) -> str:
    arch, console_app = key
    return f"{arch}, {'console' if console_app else 'GUI'}"


STUB_SOURCES: dict[str, StubSource] = {
    "macOS": StubSource(
        repo="beeware/briefcase-macOS-Xcode-template",
        block_flags=("use_framework", "console_app"),
        arch_alternatives=None,
        parse_variants=_macOS_variants,
        variant_name=_macos_variant_name,
    ),
    "windows": StubSource(
        repo="beeware/briefcase-windows-VisualStudio-template",
        block_flags=("host_arch", "console_app"),
        arch_alternatives=_WINDOWS_ARCH_ALTERNATIVES,
        parse_variants=_windows_variants,
        variant_name=_windows_variant_name,
    ),
}


def _extract_revision(release: dict, repo: str) -> str:
    match = _TAG_RE.match(release["tag_name"])
    if not match:
        raise ValueError(
            f"Latest release tag {release['tag_name']!r} of {repo} doesn't "
            "look like a stub binary revision (expected 'b<revision>')"
        )
    return match.group("revision")


def update(template_dir: Path, opener=urllib.request.urlopen) -> None:
    platform = detect_platform(template_dir)
    toml_path = briefcase_toml_path(template_dir)
    text = read_toml(toml_path)

    # Platforms with no stub binary structure at all (iOS, linux-flatpak)
    # naturally fail here with the same error a real mismatch would produce
    # -- there is no separate "unsupported platform" check.
    if not scalar_present(text, REVISION_KEY):
        raise ValueError(f"Could not find {REVISION_KEY} in briefcase.toml")

    try:
        source = STUB_SOURCES[platform]
    except KeyError:
        raise ValueError(f"Don't know how to resolve stub binaries for {platform}")

    release = fetch_latest_release(source.repo, opener=opener)
    revision = _extract_revision(release, source.repo)
    variants = source.parse_variants(release)

    print(f"Updating to revision {revision}")
    text = update_scalar(text, REVISION_KEY, revision)
    lines = text.splitlines(keepends=True)

    blocks = find_conditional_blocks(
        lines, *source.block_flags, arch_alternatives=source.arch_alternatives
    )

    to_delete: set[int] = set()
    for key, (start, end) in blocks.items():
        values = variants.get(key, {})
        variant_name = source.variant_name(key)

        for i in range(start, end + 1):
            match = HASH_ENTRY_RE.match(lines[i])
            if not match:
                continue

            python_tag = match.group("tag")
            digest = values.get(python_tag)

            if digest is None:
                print(f"{variant_name} {python_tag}: no asset found; removing entry")
                to_delete.add(i)
            else:
                print(f"{variant_name} {python_tag}: {digest}")
                lines[i] = render_entry(
                    match.group("indent"), python_tag, HASH_KEY, digest
                )

    write_toml(toml_path, render(lines, to_delete))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <template-dir>", file=sys.stderr)
        return 2

    update(Path(argv[1]))
    print("Updated stub binary revision and hash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
