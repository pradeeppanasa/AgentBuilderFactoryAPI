"""Shared helpers for git provider implementations."""

from __future__ import annotations

from urllib.parse import urlparse


def repo_slug_from_url(url: str) -> str:
    """Extract 'owner/repo' from a git remote URL.

    Works for https:// URLs (or a bare "owner/repo" already). The last path
    segments are used verbatim — that's exactly what GitHub/GitLab/
    Bitbucket's REST APIs expect as their repo-identifying path parameter.
    """
    path = urlparse(url).path if "://" in url else url
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path
