"""Mock Git provider (dev/test only — TS02-A-03).

Local/dev environments configure GIT_REPO_URL to a placeholder that doesn't
exist (e.g. https://github.com/org/panasa-agent-iac) — a real GitHubProvider
calling the real GitHub API against that repo 404s, and deploy_agent had no
try/except around _trigger_deployment, so that unhandled exception became a
bare 500 with no structured body. Same rationale as MOCK_LLM/
MOCK_BEDROCK_GUARDRAILS/MOCK_BEDROCK_KB (app/config.py) — a global opt-in
that lets a whole dev environment skip the real network call without every
caller passing a flag. Never set true in prototype/enterprise deployments.
"""

from __future__ import annotations

import uuid

from app.modules.git_provider.base import GitProvider
from app.shared.logging import get_logger

log = get_logger()


class MockGitProvider(GitProvider):
    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        log.info("git.mock.create_branch", repo=repo, branch=branch, from_branch=from_branch)

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        commit_sha = f"mock-commit-{uuid.uuid4().hex[:12]}"
        log.info(
            "git.mock.commit_files",
            repo=repo,
            branch=branch,
            file_count=len(files),
            commit_sha=commit_sha,
        )
        return commit_sha

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        pr_id = f"mock-pr-{uuid.uuid4().hex[:8]}"
        log.info("git.mock.create_pull_request", repo=repo, branch=branch, pr_id=pr_id)
        return pr_id

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        log.info("git.mock.merge_pull_request", repo=repo, pr_id=pr_id)

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        log.info("git.mock.close_pull_request", repo=repo, pr_id=pr_id, reason=reason)
