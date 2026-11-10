"""Shared helpers for querying the GitHub releases API.

Used by update_support.py and update_stub.py to find the latest published
release of an upstream repository (or every release, when a per-Python-tag
search across release history is needed), and to read the sha256 digest of a
release asset without having to download it.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable

API_ROOT = "https://api.github.com"

# An "opener" has the same shape as urllib.request.urlopen: given a Request
# (or URL string), it returns a context-manager-able response object with a
# .read() method. Tests substitute a fake opener here instead of hitting the
# network.
Opener = Callable[[urllib.request.Request], object]


def api_get(url: str, opener: Opener = urllib.request.urlopen) -> object:
    """Perform a GET request against the GitHub API and return the parsed JSON body.

    A GitHub personal access token can be provided via the `GITHUB_TOKEN`
    environment variable to avoid unauthenticated API rate limits.
    """
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"}
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with opener(request) as response:
        return json.load(response)


def fetch_all_releases(
    repo: str,
    opener: Opener = urllib.request.urlopen,
) -> list[dict]:
    """Fetch every release of `repo` (e.g. "beeware/Python-Apple-support")."""
    releases = []
    page = 1
    while True:
        batch = api_get(
            f"{API_ROOT}/repos/{repo}/releases?per_page=100&page={page}",
            opener=opener,
        )
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return releases


def fetch_latest_release(repo: str, opener: Opener = urllib.request.urlopen) -> dict:
    """Fetch the single most recent release of `repo`."""
    return api_get(f"{API_ROOT}/repos/{repo}/releases/latest", opener=opener)


def asset_digest(release: dict, asset_name: str) -> str:
    """Return the sha256 digest of `asset_name` in `release`, as reported by
    the GitHub API (the asset is never downloaded)."""
    for asset in release.get("assets", []):
        if asset["name"] != asset_name:
            continue
        digest = asset.get("digest")
        if not digest:
            raise ValueError(
                f"Asset {asset_name} has no digest reported by the GitHub API"
            )
        return digest

    raise ValueError(
        f"Could not find asset {asset_name} in release {release.get('tag_name')}"
    )
