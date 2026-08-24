"""Bitbucket Cloud implementation of GitProvider — REST API 2.0.

Bitbucket's /src endpoint takes files as multipart form fields (field name
= file path) and pushes them as one commit; it doesn't return the new
commit hash directly, so commit_files() follows up with a GET against the
branch to read it back.
"""

from __future__ import annotations

import httpx

from app.modules.git_provider._util import repo_slug_from_url
from app.modules.git_provider.base import GitProvider

_API_BASE = "https://api.bitbucket.org/2.0"


class BitbucketProvider(GitProvider):
    def __init__(
        self,
        token: str,
        base_url: str = _API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            transport=transport,
        )

    @staticmethod
    def _repo(repo: str) -> str:
        return repo_slug_from_url(repo)

    async def repository_exists(self, repo: str) -> bool:
        slug = self._repo(repo)
        response = await self._client.get(f"/repositories/{slug}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def create_repository(self, repo: str) -> None:
        slug = self._repo(repo)
        # No auto-init flag here: unlike GitHub/GitLab, Bitbucket's /src
        # commit endpoint (commit_files below) creates a repo's very first
        # commit directly — an empty repo needs no special-casing.
        response = await self._client.post(f"/repositories/{slug}", json={"is_private": True})
        response.raise_for_status()

    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        slug = self._repo(repo)
        response = await self._client.post(
            f"/repositories/{slug}/refs/branches",
            json={"name": branch, "target": {"hash": from_branch}},
        )
        response.raise_for_status()

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        slug = self._repo(repo)
        form_files = {path: (None, content.encode("utf-8")) for path, content in files.items()}
        response = await self._client.post(
            f"/repositories/{slug}/src",
            data={"message": message, "branch": branch},
            files=form_files,
        )
        response.raise_for_status()

        latest = await self._client.get(
            f"/repositories/{slug}/commits/{branch}", params={"pagelen": 1}
        )
        latest.raise_for_status()
        return str(latest.json()["values"][0]["hash"])

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        slug = self._repo(repo)
        response = await self._client.post(
            f"/repositories/{slug}/pullrequests",
            json={
                "title": title,
                "description": description,
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": "main"}},
            },
        )
        response.raise_for_status()
        return str(response.json()["id"])

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        slug = self._repo(repo)
        response = await self._client.post(
            f"/repositories/{slug}/pullrequests/{pr_id}/merge",
            json={"merge_strategy": "squash"},
        )
        response.raise_for_status()

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        slug = self._repo(repo)
        comment = await self._client.post(
            f"/repositories/{slug}/pullrequests/{pr_id}/comments",
            json={"content": {"raw": reason}},
        )
        comment.raise_for_status()

        decline = await self._client.post(f"/repositories/{slug}/pullrequests/{pr_id}/decline")
        decline.raise_for_status()
