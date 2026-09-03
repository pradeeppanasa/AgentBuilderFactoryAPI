"""MockGitProvider + create_git_provider's mock_git_provider branch
(TS02-A-03). Local dev's GIT_REPO_URL is a placeholder repo that doesn't
exist — a real GitHubProvider calling the real GitHub API against it 404s,
which deploy_agent had no try/except around, surfacing as a bare 500. This
mock is the same "skip the real network call" pattern as MOCK_LLM/
MOCK_BEDROCK_GUARDRAILS/MOCK_BEDROCK_KB.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.modules.git_provider.factory import create_git_provider
from app.modules.git_provider.github import GitHubProvider
from app.modules.git_provider.mock import MockGitProvider


async def test_mock_git_provider_never_raises() -> None:
    provider = MockGitProvider()

    assert await provider.file_exists("https://github.com/org/repo", "README.md") is False
    await provider.create_branch("https://github.com/org/repo", "feature/x")
    commit_sha = await provider.commit_files(
        "https://github.com/org/repo", "feature/x", {"main.tf": "# empty"}, "message"
    )
    assert commit_sha.startswith("mock-commit-")

    pr_id = await provider.create_pull_request(
        "https://github.com/org/repo", "feature/x", "title", "description"
    )
    assert pr_id.startswith("mock-pr-")

    await provider.merge_pull_request("https://github.com/org/repo", pr_id)
    await provider.close_pull_request("https://github.com/org/repo", pr_id, "blocked")


def test_factory_returns_mock_provider_when_flag_set() -> None:
    settings = Settings(mock_git_provider=True, git_provider="github")
    provider = create_git_provider(settings, token=None, codecommit_client=None)
    assert isinstance(provider, MockGitProvider)


def test_factory_ignores_mock_flag_when_unset() -> None:
    settings = Settings(mock_git_provider=False, git_provider="github")
    provider = create_git_provider(settings, token="a-real-token", codecommit_client=None)
    assert isinstance(provider, GitHubProvider)


def test_cors_allowed_origins_list_parses_comma_separated_value() -> None:
    settings = Settings(cors_allowed_origins="http://localhost:5173, https://app.panasa.io ,")
    assert settings.cors_allowed_origins_list == [
        "http://localhost:5173",
        "https://app.panasa.io",
    ]


def test_cors_allowed_origins_list_defaults_to_local_dev_origin() -> None:
    settings = Settings()
    assert settings.cors_allowed_origins_list == ["http://localhost:5173"]


def test_cors_allowed_origins_default_is_not_a_wildcard() -> None:
    """Not a hard validation rule (an operator could still set CORS_ALLOWED_
    ORIGINS=* deliberately) — just confirms the *default* isn't a wildcard,
    which is the TS02-A-02 regression this fix addresses: "*" paired with
    allow_credentials=True is a response combination browsers reject."""
    assert "*" not in Settings().cors_allowed_origins_list


@pytest.mark.parametrize("origins", ["*", "*,http://localhost:5173"])
def test_cors_allowed_origins_list_reflects_an_explicit_wildcard(origins: str) -> None:
    settings = Settings(cors_allowed_origins=origins)
    assert "*" in settings.cors_allowed_origins_list
