"""CodeCommitProvider tests.

moto's CodeCommit backend doesn't implement `create_commit` (raises
NotImplementedError) — CodeCommitProvider's core operations (commit_files,
and everything downstream that needs a branch with content) can't be
exercised against moto at all. This uses a small hand-rolled fake boto3
client instead, verifying our code builds the right call shapes even though
it can't validate against real CodeCommit semantics the way moto would.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.modules.git_provider.codecommit import CodeCommitProvider

REPO_NAME = "panasa-agent-iac"


class _FakeCodeCommitClient:
    def __init__(self) -> None:
        self.branches: dict[str, str] = {"main": "commit-0"}
        self.pull_requests: dict[str, dict[str, Any]] = {}
        self._commit_counter = 0
        self._pr_counter = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_branch(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_branch", kwargs))
        branch_name = kwargs["branchName"]
        return {"branch": {"branchName": branch_name, "commitId": self.branches[branch_name]}}

    def create_branch(self, **kwargs: Any) -> None:
        self.calls.append(("create_branch", kwargs))
        self.branches[kwargs["branchName"]] = kwargs["commitId"]

    def create_commit(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_commit", kwargs))
        self._commit_counter += 1
        new_commit_id = f"commit-{self._commit_counter}"
        self.branches[kwargs["branchName"]] = new_commit_id
        return {"commitId": new_commit_id}

    def create_pull_request(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_pull_request", kwargs))
        self._pr_counter += 1
        pr_id = str(self._pr_counter)
        target = kwargs["targets"][0]
        pr = {
            "pullRequestId": pr_id,
            "title": kwargs["title"],
            "description": kwargs["description"],
            "pullRequestStatus": "OPEN",
            "pullRequestTargets": [
                {
                    **target,
                    "sourceCommit": self.branches[target["sourceReference"]],
                    "destinationCommit": self.branches[target["destinationReference"]],
                }
            ],
        }
        self.pull_requests[pr_id] = pr
        return {"pullRequest": pr}

    def get_pull_request(self, **kwargs: Any) -> dict[str, Any]:
        return {"pullRequest": self.pull_requests[kwargs["pullRequestId"]]}

    def merge_pull_request_by_squash(self, **kwargs: Any) -> None:
        self.calls.append(("merge_pull_request_by_squash", kwargs))
        self.pull_requests[kwargs["pullRequestId"]]["pullRequestStatus"] = "CLOSED"

    def post_comment_for_pull_request(self, **kwargs: Any) -> None:
        self.calls.append(("post_comment_for_pull_request", kwargs))

    def update_pull_request_status(self, **kwargs: Any) -> None:
        self.calls.append(("update_pull_request_status", kwargs))
        self.pull_requests[kwargs["pullRequestId"]]["pullRequestStatus"] = kwargs[
            "pullRequestStatus"
        ]


@pytest.fixture
def fake_client() -> _FakeCodeCommitClient:
    return _FakeCodeCommitClient()


@pytest.fixture
def provider(fake_client: _FakeCodeCommitClient) -> CodeCommitProvider:
    return CodeCommitProvider(fake_client)


async def test_create_branch(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    await provider.create_branch(REPO_NAME, "deploy-branch", from_branch="main")

    assert fake_client.branches["deploy-branch"] == "commit-0"
    create_call = next(c for c in fake_client.calls if c[0] == "create_branch")
    assert create_call[1] == {
        "repositoryName": REPO_NAME,
        "branchName": "deploy-branch",
        "commitId": "commit-0",
    }


async def test_commit_files(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    fake_client.branches["deploy-branch"] = "commit-0"

    commit_id = await provider.commit_files(
        REPO_NAME, "deploy-branch", {"terraform/main.tf": "resource ..."}, message="generated IaC"
    )

    assert commit_id == "commit-1"
    assert fake_client.branches["deploy-branch"] == "commit-1"

    commit_call = next(c for c in fake_client.calls if c[0] == "create_commit")
    assert commit_call[1]["parentCommitId"] == "commit-0"
    assert commit_call[1]["putFiles"] == [
        {"filePath": "terraform/main.tf", "fileContent": b"resource ..."}
    ]
    assert commit_call[1]["commitMessage"] == "generated IaC"


async def test_create_and_merge_pull_request(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    fake_client.branches["deploy-branch"] = "commit-5"

    pr_id = await provider.create_pull_request(REPO_NAME, "deploy-branch", "title", "description")
    assert pr_id == "1"
    assert fake_client.pull_requests["1"]["pullRequestStatus"] == "OPEN"

    await provider.merge_pull_request(REPO_NAME, pr_id)
    assert fake_client.pull_requests["1"]["pullRequestStatus"] == "CLOSED"


async def test_close_pull_request_comments_then_closes(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    fake_client.branches["deploy-branch"] = "commit-5"
    pr_id = await provider.create_pull_request(REPO_NAME, "deploy-branch", "title", "description")

    await provider.close_pull_request(REPO_NAME, pr_id, "critical security finding")

    comment_call = next(c for c in fake_client.calls if c[0] == "post_comment_for_pull_request")
    assert comment_call[1]["content"] == "critical security finding"
    assert fake_client.pull_requests[pr_id]["pullRequestStatus"] == "CLOSED"
