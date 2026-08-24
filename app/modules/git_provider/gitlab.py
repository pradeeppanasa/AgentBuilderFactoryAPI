"""GitLab implementation of GitProvider — REST API v4.

Unlike GitHub, GitLab's commits API supports multi-file atomic commits
directly via an `actions` array — no blob/tree dance needed. Each action
must say "create" or "update"; since a deploy branch is freshly cut from
main, a file only needs "update" if a prior deployment already merged it.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.modules.git_provider._util import repo_slug_from_url
from app.modules.git_provider.base import GitProvider

_API_BASE = "https://gitlab.com/api/v4"


class GitLabProvider(GitProvider):
    def __init__(
        self,
        token: str,
        base_url: str = _API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"PRIVATE-TOKEN": token},
            timeout=30.0,
            transport=transport,
        )

    @staticmethod
    def _project_id(repo: str) -> str:
        return quote(repo_slug_from_url(repo), safe="")

    async def repository_exists(self, repo: str) -> bool:
        project_id = self._project_id(repo)
        response = await self._client.get(f"/projects/{project_id}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def create_repository(self, repo: str) -> None:
        namespace, _, name = repo_slug_from_url(repo).rpartition("/")
        # initialize_with_readme gives the project an initial commit on its
        # default branch — same reason as GitHub's auto_init (see there).
        payload: dict[str, str | bool | int] = {"name": name, "initialize_with_readme": True}
        if namespace:
            namespace_lookup = await self._client.get(
                "/namespaces", params={"search": namespace}
            )
            namespace_lookup.raise_for_status()
            matches = [n for n in namespace_lookup.json() if n["full_path"] == namespace]
            if matches:
                payload["namespace_id"] = matches[0]["id"]
        response = await self._client.post("/projects", json=payload)
        response.raise_for_status()

    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        project_id = self._project_id(repo)
        response = await self._client.post(
            f"/projects/{project_id}/repository/branches",
            params={"branch": branch, "ref": from_branch},
        )
        response.raise_for_status()

    async def _action_for_path(self, project_id: str, branch: str, path: str) -> str:
        encoded_path = quote(path, safe="")
        response = await self._client.head(
            f"/projects/{project_id}/repository/files/{encoded_path}",
            params={"ref": branch},
        )
        return "update" if response.status_code == 200 else "create"

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        project_id = self._project_id(repo)
        actions = [
            {
                "action": await self._action_for_path(project_id, branch, path),
                "file_path": path,
                "content": content,
            }
            for path, content in files.items()
        ]

        response = await self._client.post(
            f"/projects/{project_id}/repository/commits",
            json={"branch": branch, "commit_message": message, "actions": actions},
        )
        response.raise_for_status()
        return str(response.json()["id"])

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        project_id = self._project_id(repo)
        response = await self._client.post(
            f"/projects/{project_id}/merge_requests",
            json={
                "source_branch": branch,
                "target_branch": "main",
                "title": title,
                "description": description,
            },
        )
        response.raise_for_status()
        return str(response.json()["iid"])

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        project_id = self._project_id(repo)
        response = await self._client.put(f"/projects/{project_id}/merge_requests/{pr_id}/merge")
        response.raise_for_status()

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        project_id = self._project_id(repo)
        note = await self._client.post(
            f"/projects/{project_id}/merge_requests/{pr_id}/notes", json={"body": reason}
        )
        note.raise_for_status()

        close = await self._client.put(
            f"/projects/{project_id}/merge_requests/{pr_id}", json={"state_event": "close"}
        )
        close.raise_for_status()
