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
from botocore.exceptions import ClientError

from app.modules.git_provider.codecommit import CodeCommitProvider

REPO_NAME = "panasa-agent-iac"


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeCodeCommitClient:
    def __init__(self, repositories: set[str] | None = None) -> None:
        self.repositories: set[str] = repositories if repositories is not None else {REPO_NAME}
        self.branches: dict[str, str] = (
            {"main": "commit-0"} if REPO_NAME in self.repositories else {}
        )
        self.pull_requests: dict[str, dict[str, Any]] = {}
        self.files_by_branch: dict[str, set[str]] = {}
        self._commit_counter = 0
        self._pr_counter = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_file(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_file", kwargs))
        branch = kwargs["commitSpecifier"]
        if branch not in self.branches:
            raise _client_error("CommitDoesNotExistException", "GetFile")
        if kwargs["filePath"] not in self.files_by_branch.get(branch, set()):
            raise _client_error("FileDoesNotExistException", "GetFile")
        return {"filePath": kwargs["filePath"]}

    def get_repository(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_repository", kwargs))
        name = kwargs["repositoryName"]
        if name not in self.repositories:
            raise _client_error("RepositoryDoesNotExistException", "GetRepository")
        return {"repositoryMetadata": {"repositoryName": name}}

    def create_repository(self, **kwargs: Any) -> None:
        self.calls.append(("create_repository", kwargs))
        self.repositories.add(kwargs["repositoryName"])

    def get_branch(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_branch", kwargs))
        branch_name = kwargs["branchName"]
        if branch_name not in self.branches:
            raise _client_error("BranchDoesNotExistException", "GetBranch")
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


async def test_repository_exists_true(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    assert await provider.repository_exists(REPO_NAME) is True


async def test_repository_exists_false(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    assert await provider.repository_exists("panasa-iac-new-agent") is False


async def test_create_repository(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    await provider.create_repository("panasa-iac-new-agent")

    assert "panasa-iac-new-agent" in fake_client.repositories
    create_call = next(c for c in fake_client.calls if c[0] == "create_repository")
    assert create_call[1] == {"repositoryName": "panasa-iac-new-agent"}


async def test_commit_files_into_brand_new_repo_omits_parent_commit_id(
    fake_client: _FakeCodeCommitClient,
) -> None:
    """Section 45.2 — a just-created repo has no branches at all yet;
    CodeCommit creates the branch implicitly when parentCommitId is
    omitted, rather than failing on a nonexistent "main"."""
    fake_client.repositories = {"panasa-iac-new-agent"}
    fake_client.branches = {}
    provider = CodeCommitProvider(fake_client)

    commit_id = await provider.commit_files(
        "panasa-iac-new-agent", "main", {"README.md": "hello"}, message="initial commit"
    )

    assert commit_id == "commit-1"
    assert fake_client.branches["main"] == "commit-1"
    commit_call = next(c for c in fake_client.calls if c[0] == "create_commit")
    assert "parentCommitId" not in commit_call[1]


async def test_file_exists_true_when_present_on_branch(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    fake_client.files_by_branch["main"] = {"README.md"}
    assert await provider.file_exists(REPO_NAME, "README.md", branch="main") is True


async def test_file_exists_false_when_absent_on_branch(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    exists = await provider.file_exists(REPO_NAME, "buildspec.yml", branch="main")
    assert exists is False


async def test_file_exists_false_when_branch_does_not_exist(
    provider: CodeCommitProvider, fake_client: _FakeCodeCommitClient
) -> None:
    exists = await provider.file_exists(REPO_NAME, "buildspec.yml", branch="does-not-exist")
    assert exists is False


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
