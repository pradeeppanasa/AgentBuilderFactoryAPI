"""GitHubProvider tests using httpx.MockTransport — no real network calls.

Verifies the actual REST call shapes (paths, methods, payloads), not just
that *some* HTTP call happened.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.modules.git_provider.github import GitHubProvider

REPO = "https://github.com/acme/panasa-agent-iac"


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._blob_counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path

        if method == "GET" and path == "/repos/acme/panasa-agent-iac":
            return httpx.Response(200, json={"full_name": "acme/panasa-agent-iac"})

        if method == "GET" and path == "/repos/acme/panasa-iac-new-agent":
            return httpx.Response(404, json={"message": "Not Found"})

        if method == "POST" and path == "/orgs/acme/repos":
            return httpx.Response(201, json={"full_name": "acme/panasa-iac-new-agent"})

        if method == "POST" and path == "/orgs/a-personal-account/repos":
            return httpx.Response(404, json={"message": "Not Found"})

        if method == "POST" and path == "/user/repos":
            return httpx.Response(
                201, json={"full_name": "a-personal-account/panasa-iac-new-agent"}
            )

        if method == "GET" and path == "/repos/acme/panasa-agent-iac/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})

        if method == "GET" and path == "/repos/acme/panasa-agent-iac/contents/README.md":
            return httpx.Response(200, json={"name": "README.md"})

        if (
            method == "GET"
            and path == "/repos/acme/panasa-agent-iac/contents/.github/workflows/panasa-deploy.yml"
        ):
            return httpx.Response(404, json={"message": "Not Found"})

        if method == "POST" and path == "/repos/acme/panasa-agent-iac/git/refs":
            return httpx.Response(201, json={"ref": "refs/heads/deploy-branch"})

        if method == "GET" and path == "/repos/acme/panasa-agent-iac/git/ref/heads/deploy-branch":
            return httpx.Response(200, json={"object": {"sha": "parent-commit-sha"}})

        if method == "GET" and path == "/repos/acme/panasa-agent-iac/git/commits/parent-commit-sha":
            return httpx.Response(200, json={"tree": {"sha": "base-tree-sha"}})

        if method == "POST" and path == "/repos/acme/panasa-agent-iac/git/blobs":
            self._blob_counter += 1
            return httpx.Response(201, json={"sha": f"blob-sha-{self._blob_counter}"})

        if method == "POST" and path == "/repos/acme/panasa-agent-iac/git/trees":
            return httpx.Response(201, json={"sha": "new-tree-sha"})

        if method == "POST" and path == "/repos/acme/panasa-agent-iac/git/commits":
            return httpx.Response(201, json={"sha": "new-commit-sha"})

        if (
            method == "PATCH"
            and path == "/repos/acme/panasa-agent-iac/git/refs/heads/deploy-branch"
        ):
            return httpx.Response(200, json={})

        if method == "POST" and path == "/repos/acme/panasa-agent-iac/pulls":
            return httpx.Response(201, json={"number": 42})

        if method == "PUT" and path == "/repos/acme/panasa-agent-iac/pulls/42/merge":
            return httpx.Response(200, json={"merged": True})

        if method == "POST" and path == "/repos/acme/panasa-agent-iac/issues/42/comments":
            return httpx.Response(201, json={})

        if method == "PATCH" and path == "/repos/acme/panasa-agent-iac/pulls/42":
            return httpx.Response(200, json={"state": "closed"})

        raise AssertionError(f"Unexpected request: {method} {path}")


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def provider(recorder: _Recorder) -> GitHubProvider:
    transport = httpx.MockTransport(recorder.handler)
    return GitHubProvider(token="fake-token", transport=transport)


async def test_repository_exists_true_for_200(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    assert await provider.repository_exists(REPO) is True


async def test_repository_exists_false_for_404(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    assert await provider.repository_exists("https://github.com/acme/panasa-iac-new-agent") is False


async def test_create_repository_posts_to_org_repos_with_auto_init(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    await provider.create_repository("acme/panasa-iac-new-agent")

    request = recorder.requests[-1]
    assert request.method == "POST"
    assert request.url.path == "/orgs/acme/repos"
    assert json.loads(request.content) == {
        "name": "panasa-iac-new-agent",
        "private": True,
        "auto_init": True,
    }


async def test_create_repository_falls_back_to_user_repos_for_personal_account(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    """GIT_ORG is often a personal GitHub username, not a real Organization —
    /orgs/{org}/repos 404s in that case. create_repository must retry
    against /user/repos (creates under the token's own account) instead of
    surfacing the 404."""
    await provider.create_repository("a-personal-account/panasa-iac-new-agent")

    request = recorder.requests[-1]
    assert request.method == "POST"
    assert request.url.path == "/user/repos"
    assert json.loads(request.content) == {
        "name": "panasa-iac-new-agent",
        "private": True,
        "auto_init": True,
    }


async def test_commit_files_retries_tree_creation_on_transient_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo created moments ago via auto_init=True can briefly 404 when
    building a tree on its initial commit — the Git Data API hasn't fully
    propagated it yet. commit_files must retry rather than fail the
    deploy."""
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _immediate())

    tree_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tree_attempts
        method, path = request.method, request.url.path

        if method == "GET" and path == "/repos/acme/panasa-agent-iac/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "parent-commit-sha"}})
        if method == "GET" and path == "/repos/acme/panasa-agent-iac/git/commits/parent-commit-sha":
            return httpx.Response(200, json={"tree": {"sha": "base-tree-sha"}})
        if method == "POST" and path == "/repos/acme/panasa-agent-iac/git/blobs":
            return httpx.Response(201, json={"sha": "blob-sha-1"})
        if method == "POST" and path == "/repos/acme/panasa-agent-iac/git/trees":
            tree_attempts += 1
            if tree_attempts < 3:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(201, json={"sha": "new-tree-sha"})
        if method == "POST" and path == "/repos/acme/panasa-agent-iac/git/commits":
            return httpx.Response(201, json={"sha": "new-commit-sha"})
        if method == "PATCH" and path == "/repos/acme/panasa-agent-iac/git/refs/heads/main":
            return httpx.Response(200, json={})

        raise AssertionError(f"Unexpected request: {method} {path}")

    provider = GitHubProvider(token="fake-token", transport=httpx.MockTransport(handler))

    commit_sha = await provider.commit_files(
        REPO, "main", {"a.tf": "content-a"}, message="generated IaC"
    )

    assert commit_sha == "new-commit-sha"
    assert tree_attempts == 3


async def _immediate() -> None:
    return None


async def test_file_exists_true_for_200(provider: GitHubProvider, recorder: _Recorder) -> None:
    assert await provider.file_exists(REPO, "README.md", branch="main") is True

    request = recorder.requests[-1]
    assert request.url.path == "/repos/acme/panasa-agent-iac/contents/README.md"
    assert dict(request.url.params) == {"ref": "main"}


async def test_file_exists_false_for_404(provider: GitHubProvider, recorder: _Recorder) -> None:
    exists = await provider.file_exists(
        REPO, ".github/workflows/panasa-deploy.yml", branch="main"
    )
    assert exists is False


async def test_create_branch_fetches_base_sha_and_creates_ref(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    await provider.create_branch(REPO, "deploy-branch", from_branch="main")

    create_ref_request = recorder.requests[-1]
    assert create_ref_request.method == "POST"
    body = json.loads(create_ref_request.content)
    assert body == {"ref": "refs/heads/deploy-branch", "sha": "base-sha"}


async def test_commit_files_does_blob_tree_commit_ref_dance(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    commit_sha = await provider.commit_files(
        REPO,
        "deploy-branch",
        {"a.tf": "content-a", "b.tf": "content-b"},
        message="generated IaC",
    )

    assert commit_sha == "new-commit-sha"
    methods_and_paths = [(r.method, r.url.path) for r in recorder.requests]
    assert ("POST", "/repos/acme/panasa-agent-iac/git/blobs") in methods_and_paths
    assert methods_and_paths.count(("POST", "/repos/acme/panasa-agent-iac/git/blobs")) == 2

    tree_request = next(r for r in recorder.requests if r.url.path.endswith("/git/trees"))
    tree_body = json.loads(tree_request.content)
    assert tree_body["base_tree"] == "base-tree-sha"
    assert {entry["path"] for entry in tree_body["tree"]} == {"a.tf", "b.tf"}

    commit_request = next(
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/git/commits")
    )
    commit_body = json.loads(commit_request.content)
    assert commit_body["message"] == "generated IaC"
    assert commit_body["parents"] == ["parent-commit-sha"]


async def test_create_pull_request_returns_number_as_string(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    pr_id = await provider.create_pull_request(REPO, "deploy-branch", "title", "description")
    assert pr_id == "42"

    request = recorder.requests[-1]
    body = json.loads(request.content)
    assert body == {
        "title": "title",
        "head": "deploy-branch",
        "base": "main",
        "body": "description",
    }


async def test_merge_pull_request_calls_merge_endpoint(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    await provider.merge_pull_request(REPO, "42")
    request = recorder.requests[-1]
    assert request.method == "PUT"
    assert request.url.path == "/repos/acme/panasa-agent-iac/pulls/42/merge"


async def test_close_pull_request_comments_then_closes(
    provider: GitHubProvider, recorder: _Recorder
) -> None:
    await provider.close_pull_request(REPO, "42", "critical security finding")

    comment_request, close_request = recorder.requests[-2], recorder.requests[-1]
    assert comment_request.url.path.endswith("/issues/42/comments")
    assert json.loads(comment_request.content) == {"body": "critical security finding"}
    assert close_request.method == "PATCH"
    assert json.loads(close_request.content) == {"state": "closed"}
