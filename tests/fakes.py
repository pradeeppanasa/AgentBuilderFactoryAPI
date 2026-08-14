"""Shared test doubles — no real network/AWS calls behind these.

FakeGitProvider was originally local to test_deploy_api.py; moved here once
test_versions_api.py's rollback tests needed the exact same double (Phase 13:
rollback now triggers a real deployment too).
"""

from __future__ import annotations

from app.modules.git_provider.base import GitProvider


class FakeGitProvider(GitProvider):
    """Records calls instead of talking to a real git host."""

    def __init__(self) -> None:
        self.created_branches: list[tuple[str, str, str]] = []
        self.committed_files: list[tuple[str, str, dict[str, str], str]] = []
        self.opened_prs: list[tuple[str, str, str, str]] = []
        self.merged: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str, str]] = []

    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        self.created_branches.append((repo, branch, from_branch))

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        self.committed_files.append((repo, branch, files, message))
        return "fake-commit-sha"

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        self.opened_prs.append((repo, branch, title, description))
        return "99"

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        self.merged.append((repo, pr_id))

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        self.closed.append((repo, pr_id, reason))
