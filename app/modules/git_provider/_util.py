"""Shared helpers for git provider implementations."""

from __future__ import annotations

from urllib.parse import urlparse


def agent_repo_identifier(git_provider: str, git_org: str | None, agent_id: str) -> str | None:
    """Section 45.2 — one private repo per agent: panasa-iac-{agent_id}.

    Shared by app/api/v1/agents.py (the deploy trigger) and
    lambda_handlers/policy_check.py (which needs the same identifier to
    merge/close the PR generating_iac opened) so the naming can never drift
    between the two call sites.

    Returns None when a namespace is required (every provider except
    codecommit, whose repos aren't org/namespace-scoped) but not
    configured — callers raise their own domain-specific error for that
    (HTTPException vs StageFailure).
    """
    repo_name = f"panasa-iac-{agent_id}"
    if git_provider == "codecommit":
        return repo_name
    if not git_org:
        return None
    return f"{git_org}/{repo_name}"


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
