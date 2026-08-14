"""AWS CodeCommit implementation of GitProvider — boto3, not a REST token.

CodeCommit authenticates with the Runtime's own AWS credentials (SigV4) —
there is no personal access token to fetch from Secrets Manager for this
provider (see git_provider/secrets.py).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.modules.git_provider._util import repo_slug_from_url
from app.modules.git_provider.base import GitProvider


class CodeCommitProvider(GitProvider):
    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _repo_name(repo: str) -> str:
        return repo_slug_from_url(repo).rstrip("/").split("/")[-1]

    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        repo_name = self._repo_name(repo)

        def _create() -> None:
            base = self._client.get_branch(repositoryName=repo_name, branchName=from_branch)
            commit_id = base["branch"]["commitId"]
            self._client.create_branch(
                repositoryName=repo_name, branchName=branch, commitId=commit_id
            )

        await asyncio.to_thread(_create)

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        repo_name = self._repo_name(repo)

        def _commit() -> str:
            branch_info = self._client.get_branch(repositoryName=repo_name, branchName=branch)
            parent_commit_id = branch_info["branch"]["commitId"]
            put_files = [
                {"filePath": path, "fileContent": content.encode("utf-8")}
                for path, content in files.items()
            ]
            response = self._client.create_commit(
                repositoryName=repo_name,
                branchName=branch,
                parentCommitId=parent_commit_id,
                putFiles=put_files,
                commitMessage=message,
            )
            commit_id: str = response["commitId"]
            return commit_id

        return await asyncio.to_thread(_commit)

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        repo_name = self._repo_name(repo)

        def _create() -> str:
            response = self._client.create_pull_request(
                title=title,
                description=description,
                targets=[
                    {
                        "repositoryName": repo_name,
                        "sourceReference": branch,
                        "destinationReference": "main",
                    }
                ],
            )
            pr_id: str = response["pullRequest"]["pullRequestId"]
            return pr_id

        return await asyncio.to_thread(_create)

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        repo_name = self._repo_name(repo)
        await asyncio.to_thread(
            self._client.merge_pull_request_by_squash,
            pullRequestId=pr_id,
            repositoryName=repo_name,
        )

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        repo_name = self._repo_name(repo)

        def _close() -> None:
            pr = self._client.get_pull_request(pullRequestId=pr_id)
            target = pr["pullRequest"]["pullRequestTargets"][0]
            self._client.post_comment_for_pull_request(
                pullRequestId=pr_id,
                repositoryName=repo_name,
                beforeCommitId=target["destinationCommit"],
                afterCommitId=target["sourceCommit"],
                content=reason,
            )
            self._client.update_pull_request_status(pullRequestId=pr_id, pullRequestStatus="CLOSED")

        await asyncio.to_thread(_close)
