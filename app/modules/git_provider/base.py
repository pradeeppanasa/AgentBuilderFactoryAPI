"""Git provider abstraction (CLAUDE.md Section 10, updated by F5).

F5 supersedes Section 10's original contract: the PR a GitProvider opens is
a pipeline trigger, not a human code-review request. Customer CI/CD runs on
PR creation; the Factory Runtime later calls merge_pull_request() on
POLICY_CHECK=PASS or close_pull_request() on POLICY_CHECK=BLOCK. No human
ever needs to touch the PR. get_pull_request_status() from the original
Section 10 draft is dropped here — status comes from the customer CI/CD
writing to DynamoDB (F2), which the Runtime polls; it was never meant to
come from asking the git host itself.

All methods are async — Rule 6 (all async FastAPI routes, await all I/O).
CLAUDE.md's pseudocode shows plain `def`; every concrete implementation here
does real network I/O, so async is the correct shape for this codebase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class GitProvider(ABC):
    @abstractmethod
    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None: ...

    @abstractmethod
    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        """Commit files to branch. Returns the new commit SHA/id."""
        ...

    @abstractmethod
    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        """Open a PR from branch into the repo's default branch. Returns the PR id."""
        ...

    @abstractmethod
    async def merge_pull_request(self, repo: str, pr_id: str) -> None: ...

    @abstractmethod
    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None: ...
