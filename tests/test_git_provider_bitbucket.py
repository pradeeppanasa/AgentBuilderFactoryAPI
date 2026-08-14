"""BitbucketProvider tests using httpx.MockTransport — no real network calls."""

from __future__ import annotations

import json

import httpx
import pytest

from app.modules.git_provider.bitbucket import BitbucketProvider

REPO = "https://bitbucket.org/acme/panasa-agent-iac"
SLUG = "acme/panasa-agent-iac"


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # base_url already contributes a "/2.0" path prefix, so match on
        # suffix rather than the exact path (mirrors the GitLab test, whose
        # base_url similarly has a non-empty path component "/api/v4").
        method, path = request.method, request.url.path

        if method == "POST" and path.endswith(f"/repositories/{SLUG}/refs/branches"):
            return httpx.Response(201, json={"name": "deploy-branch"})

        if method == "POST" and path.endswith(f"/repositories/{SLUG}/src"):
            return httpx.Response(201, text="")

        if method == "GET" and path.endswith(f"/repositories/{SLUG}/commits/deploy-branch"):
            return httpx.Response(200, json={"values": [{"hash": "bitbucket-commit-hash"}]})

        if method == "POST" and path.endswith(f"/repositories/{SLUG}/pullrequests"):
            return httpx.Response(201, json={"id": 5})

        if method == "POST" and path.endswith(f"/repositories/{SLUG}/pullrequests/5/merge"):
            return httpx.Response(200, json={"state": "MERGED"})

        if method == "POST" and path.endswith(f"/repositories/{SLUG}/pullrequests/5/comments"):
            return httpx.Response(201, json={})

        if method == "POST" and path.endswith(f"/repositories/{SLUG}/pullrequests/5/decline"):
            return httpx.Response(200, json={"state": "DECLINED"})

        raise AssertionError(f"Unexpected request: {method} {path}")


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def provider(recorder: _Recorder) -> BitbucketProvider:
    transport = httpx.MockTransport(recorder.handler)
    return BitbucketProvider(token="fake-token", transport=transport)


async def test_create_branch(provider: BitbucketProvider, recorder: _Recorder) -> None:
    await provider.create_branch(REPO, "deploy-branch", from_branch="main")
    body = json.loads(recorder.requests[-1].content)
    assert body == {"name": "deploy-branch", "target": {"hash": "main"}}


async def test_commit_files_pushes_then_reads_back_commit_hash(
    provider: BitbucketProvider, recorder: _Recorder
) -> None:
    commit_hash = await provider.commit_files(
        REPO, "deploy-branch", {"a.tf": "content-a"}, message="generated IaC"
    )
    assert commit_hash == "bitbucket-commit-hash"

    src_request = next(r for r in recorder.requests if r.url.path.endswith("/src"))
    assert src_request.method == "POST"


async def test_create_pull_request_returns_id_as_string(
    provider: BitbucketProvider, recorder: _Recorder
) -> None:
    pr_id = await provider.create_pull_request(REPO, "deploy-branch", "title", "description")
    assert pr_id == "5"

    body = json.loads(recorder.requests[-1].content)
    assert body["title"] == "title"
    assert body["source"]["branch"]["name"] == "deploy-branch"
    assert body["destination"]["branch"]["name"] == "main"


async def test_merge_pull_request(provider: BitbucketProvider, recorder: _Recorder) -> None:
    await provider.merge_pull_request(REPO, "5")
    assert recorder.requests[-1].url.path.endswith("/pullrequests/5/merge")


async def test_close_pull_request_comments_then_declines(
    provider: BitbucketProvider, recorder: _Recorder
) -> None:
    await provider.close_pull_request(REPO, "5", "security scan failed")

    comment_request, decline_request = recorder.requests[-2], recorder.requests[-1]
    assert json.loads(comment_request.content) == {"content": {"raw": "security scan failed"}}
    assert decline_request.url.path.endswith("/pullrequests/5/decline")
