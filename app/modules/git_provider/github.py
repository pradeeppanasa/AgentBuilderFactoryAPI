"""GitHub implementation of GitProvider — REST API v3 (Git Data API for commits).

GitHub's simple contents API only supports one file per commit; a multi-file
IaC commit needs the lower-level blob -> tree -> commit -> ref-update dance,
so that's what commit_files() does here.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.modules.git_provider._util import repo_slug_from_url
from app.modules.git_provider.base import GitProvider

_API_BASE = "https://api.github.com"

# A repo created moments ago via create_repository()'s auto_init=True has
# its initial commit written, but the Git Data API (blobs/trees/commits)
# can in principle briefly 404 on that commit's tree sha while it
# propagates, before it's queryable — this retry exists for that. Widened
# twice chasing what turned out to be a DIFFERENT, permanent cause hiding
# behind the same generic 404 (see MissingWorkflowScopeError below) — a
# git token missing the 'workflow' OAuth scope makes GitHub's trees
# endpoint 404 on ANY tree containing a .github/workflows/ path, on every
# attempt, with no amount of retrying ever succeeding. That's now checked
# for explicitly and fails fast instead of silently eating a ~75s retry
# budget first. This retry stays as defense-in-depth for the genuinely
# transient case the widening was originally aimed at, real or not.
_TREE_PROPAGATION_ATTEMPTS = 9
_TREE_PROPAGATION_BASE_DELAY_SECONDS = 1.0
_TREE_PROPAGATION_MAX_DELAY_SECONDS = 15.0


class MissingWorkflowScopeError(RuntimeError):
    """The configured git token can't create/update files under
    .github/workflows/ — GitHub's Git Data API rejects this with a plain
    404 on the trees endpoint, indistinguishable from any other 404 unless
    checked for explicitly (commit_files' _check_workflow_scope call)."""

    def __init__(self) -> None:
        super().__init__(
            "The configured git token is missing the 'workflow' OAuth scope "
            "required to create or update files under .github/workflows/. "
            "Regenerate the token with the 'workflow' scope enabled (classic "
            "PAT) or 'Workflows: Read and write' permission (fine-grained "
            "PAT), then update GIT_CREDENTIALS_SECRET."
        )


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

    @staticmethod
    async def _post_retrying_404(
        call: Callable[[], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        for attempt in range(_TREE_PROPAGATION_ATTEMPTS):
            response = await call()
            is_last_attempt = attempt == _TREE_PROPAGATION_ATTEMPTS - 1
            if response.status_code != 404 or is_last_attempt:
                response.raise_for_status()
                return response
            delay = min(
                _TREE_PROPAGATION_BASE_DELAY_SECONDS * (2**attempt),
                _TREE_PROPAGATION_MAX_DELAY_SECONDS,
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # loop always returns or raises

    async def repository_exists(self, repo: str) -> bool:
        slug = self._repo(repo)
        response = await self._client.get(f"/repos/{slug}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def create_repository(self, repo: str) -> None:
        slug = self._repo(repo)
        org, _, name = slug.partition("/")
        # auto_init=True gives the repo an initial commit on its default
        # branch — otherwise there is no ref for commit_files()'s blob/tree/
        # commit dance (or create_branch()) to build on top of.
        response = await self._client.post(
            f"/orgs/{org}/repos", json={"name": name, "private": True, "auto_init": True}
        )
        if response.status_code == 404:
            # GIT_ORG isn't always a real GitHub Organization — many
            # customers configure their own personal account/username
            # there. /orgs/{org}/repos 404s for a personal account; the
            # equivalent personal-account endpoint is /user/repos, which
            # creates under whichever account the token belongs to.
            response = await self._client.post(
                "/user/repos", json={"name": name, "private": True, "auto_init": True}
            )
        response.raise_for_status()

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

    async def file_exists(self, repo: str, path: str, branch: str = "main") -> bool:
        slug = self._repo(repo)
        response = await self._client.get(
            f"/repos/{slug}/contents/{path}", params={"ref": branch}
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def _check_workflow_scope(self) -> None:
        """Best-effort: classic PATs report their granted scopes on every
        API response via the X-OAuth-Scopes header; fine-grained PATs and
        GitHub App tokens don't send it at all, so this silently no-ops for
        those rather than false-flagging a token GitHub itself doesn't
        describe this way — such tokens still fail normally (just without
        this specific diagnosis) if they truly lack the permission."""
        response = await self._client.get("/user")
        response.raise_for_status()
        scopes_header = response.headers.get("x-oauth-scopes")
        if scopes_header is None:
            return
        scopes = {s.strip() for s in scopes_header.split(",") if s.strip()}
        if "workflow" not in scopes:
            raise MissingWorkflowScopeError()

    async def commit_files(
        self,
        repo: str,
        branch: str,
        files: dict[str, str],
        message: str,
        omit_base_tree: bool = False,
    ) -> str:
        if any(path.startswith(".github/workflows/") for path in files):
            await self._check_workflow_scope()

        slug = self._repo(repo)

        branch_ref = await self._client.get(f"/repos/{slug}/git/ref/heads/{branch}")
        branch_ref.raise_for_status()
        parent_commit_sha = branch_ref.json()["object"]["sha"]

        base_tree_sha: str | None = None
        if not omit_base_tree:
            parent_commit = await self._client.get(
                f"/repos/{slug}/git/commits/{parent_commit_sha}"
            )
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

        tree_payload: dict[str, Any] = {"tree": tree_entries}
        if base_tree_sha is not None:
            tree_payload["base_tree"] = base_tree_sha

        tree = await self._post_retrying_404(
            lambda: self._client.post(f"/repos/{slug}/git/trees", json=tree_payload)
        )
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
