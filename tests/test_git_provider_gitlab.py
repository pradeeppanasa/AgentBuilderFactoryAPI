"""GitLabProvider tests using httpx.MockTransport — no real network calls."""

from __future__ import annotations

import json

import httpx
import pytest

from app.modules.git_provider.gitlab import GitLabProvider

REPO = "https://gitlab.com/acme/panasa-agent-iac"


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.file_exists: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path

        if method == "POST" and path.endswith("/repository/branches"):
            return httpx.Response(201, json={"name": "deploy-branch"})

        if method == "HEAD" and "/repository/files/" in path:
            exists = any(name in path for name in self.file_exists)
            return httpx.Response(200 if exists else 404)

        if method == "POST" and path.endswith("/repository/commits"):
            return httpx.Response(201, json={"id": "gitlab-commit-sha"})

        if method == "POST" and path.endswith("/merge_requests"):
            return httpx.Response(201, json={"iid": 7})

        if method == "PUT" and path.endswith("/merge_requests/7/merge"):
            return httpx.Response(200, json={"state": "merged"})

        if method == "POST" and path.endswith("/merge_requests/7/notes"):
            return httpx.Response(201, json={})

        if method == "PUT" and path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"state": "closed"})

        raise AssertionError(f"Unexpected request: {method} {path}")


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def provider(recorder: _Recorder) -> GitLabProvider:
    transport = httpx.MockTransport(recorder.handler)
    return GitLabProvider(token="fake-token", transport=transport)


async def test_create_branch(provider: GitLabProvider, recorder: _Recorder) -> None:
    await provider.create_branch(REPO, "deploy-branch", from_branch="main")
    request = recorder.requests[-1]
    assert request.method == "POST"
    assert dict(request.url.params) == {"branch": "deploy-branch", "ref": "main"}


async def test_commit_files_marks_existing_files_as_update(
    provider: GitLabProvider, recorder: _Recorder
) -> None:
    recorder.file_exists.add("existing.tf")

    commit_sha = await provider.commit_files(
        REPO,
        "deploy-branch",
        {"existing.tf": "old content, new value", "new.tf": "brand new"},
        message="generated IaC",
    )

    assert commit_sha == "gitlab-commit-sha"
    commit_request = next(
        r for r in recorder.requests if r.url.path.endswith("/repository/commits")
    )
    body = json.loads(commit_request.content)
    actions_by_path = {a["file_path"]: a["action"] for a in body["actions"]}
    assert actions_by_path == {"existing.tf": "update", "new.tf": "create"}
    assert body["branch"] == "deploy-branch"
    assert body["commit_message"] == "generated IaC"


async def test_create_pull_request_returns_iid_as_string(
    provider: GitLabProvider, recorder: _Recorder
) -> None:
    mr_id = await provider.create_pull_request(REPO, "deploy-branch", "title", "description")
    assert mr_id == "7"

    request = recorder.requests[-1]
    body = json.loads(request.content)
    assert body == {
        "source_branch": "deploy-branch",
        "target_branch": "main",
        "title": "title",
        "description": "description",
    }


async def test_merge_pull_request(provider: GitLabProvider, recorder: _Recorder) -> None:
    await provider.merge_pull_request(REPO, "7")
    assert recorder.requests[-1].method == "PUT"
    assert recorder.requests[-1].url.path.endswith("/merge_requests/7/merge")


async def test_close_pull_request_notes_then_closes(
    provider: GitLabProvider, recorder: _Recorder
) -> None:
    await provider.close_pull_request(REPO, "7", "blocked by policy gate")

    note_request, close_request = recorder.requests[-2], recorder.requests[-1]
    assert note_request.url.path.endswith("/merge_requests/7/notes")
    assert json.loads(note_request.content) == {"body": "blocked by policy gate"}
    assert json.loads(close_request.content) == {"state_event": "close"}
