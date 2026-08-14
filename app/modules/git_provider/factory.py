"""Selects the GitProvider implementation for GIT_PROVIDER (Section 10)."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.modules.git_provider.base import GitProvider
from app.modules.git_provider.bitbucket import BitbucketProvider
from app.modules.git_provider.codecommit import CodeCommitProvider
from app.modules.git_provider.github import GitHubProvider
from app.modules.git_provider.gitlab import GitLabProvider


def create_git_provider(
    settings: Settings, token: str | None, codecommit_client: Any
) -> GitProvider:
    if settings.git_provider == "github":
        assert token is not None
        return GitHubProvider(token)
    if settings.git_provider == "gitlab":
        assert token is not None
        return GitLabProvider(token)
    if settings.git_provider == "bitbucket":
        assert token is not None
        return BitbucketProvider(token)
    if settings.git_provider == "codecommit":
        return CodeCommitProvider(codecommit_client)
    raise ValueError(f"Unknown GIT_PROVIDER: {settings.git_provider!r}")
