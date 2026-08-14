"""GitHub implementation of GitProvider — REST API v3 (Git Data API for commits).

GitHub's simple contents API only supports one file per commit; a multi-file
IaC commit needs the lower-level blob -> tree -> commit -> ref-update dance,
so that's what commit_files() does here.
"""

from __future__ import annotations

import base64

import httpx

from app.modules.git_provider._util import repo_slug_from_url
from app.modules.git_provider.base import GitProvider

_API_BASE = "https://api.github.com"


class GitHubProvider(GitProvider):
    def __init__(
        self,
        token: str,
        base_url: str = _API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            transport=transport,
        )

    @staticmethod
    def _repo(repo: str) -> str:
        return repo_slug_from_url(repo)

    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        slug = self._repo(repo)
        base_ref = await self._client.get(f"/repos/{slug}/git/ref/heads/{from_branch}")
        base_ref.raise_for_status()
        base_sha = base_ref.json()["object"]["sha"]

        response = await self._client.post(
            f"/repos/{slug}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        response.raise_for_status()

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        slug = self._repo(repo)

        branch_ref = await self._client.get(f"/repos/{slug}/git/ref/heads/{branch}")
        branch_ref.raise_for_status()
        parent_commit_sha = branch_ref.json()["object"]["sha"]

        parent_commit = await self._client.get(f"/repos/{slug}/git/commits/{parent_commit_sha}")
        parent_commit.raise_for_status()
        base_tree_sha = parent_commit.json()["tree"]["sha"]

        tree_entries = []
        for path, content in files.items():
            blob = await self._client.post(
                f"/repos/{slug}/git/blobs",
                json={
                    "content": base64.b64encode(content.encode()).decode(),
                    "encoding": "base64",
                },
            )
            blob.raise_for_status()
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}
            )

        tree = await self._client.post(
            f"/repos/{slug}/git/trees",
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        )
        tree.raise_for_status()
        new_tree_sha = tree.json()["sha"]

        commit = await self._client.post(
            f"/repos/{slug}/git/commits",
            json={"message": message, "tree": new_tree_sha, "parents": [parent_commit_sha]},
        )
        commit.raise_for_status()
        new_commit_sha: str = commit.json()["sha"]

        update_ref = await self._client.patch(
            f"/repos/{slug}/git/refs/heads/{branch}",
            json={"sha": new_commit_sha},
        )
        update_ref.raise_for_status()

        return new_commit_sha

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        slug = self._repo(repo)
        response = await self._client.post(
            f"/repos/{slug}/pulls",
            json={"title": title, "head": branch, "base": "main", "body": description},
        )
        response.raise_for_status()
        return str(response.json()["number"])

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        slug = self._repo(repo)
        response = await self._client.put(
            f"/repos/{slug}/pulls/{pr_id}/merge", json={"merge_method": "squash"}
        )
        response.raise_for_status()

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        slug = self._repo(repo)
        comment = await self._client.post(
            f"/repos/{slug}/issues/{pr_id}/comments", json={"body": reason}
        )
        comment.raise_for_status()

        close = await self._client.patch(f"/repos/{slug}/pulls/{pr_id}", json={"state": "closed"})
        close.raise_for_status()
