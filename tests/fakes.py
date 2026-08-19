"""Shared test doubles — no real network/AWS calls behind these.

FakeGitProvider was originally local to test_deploy_api.py; moved here once
test_versions_api.py's rollback tests needed the exact same double (Phase 13:
rollback now triggers a real deployment too).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from app.modules.git_provider.base import GitProvider


class FakeGitProvider(GitProvider):
    """Records calls instead of talking to a real git host."""

    def __init__(self) -> None:
        self.created_branches: list[tuple[str, str, str]] = []
        self.committed_files: list[tuple[str, str, dict[str, str], str]] = []
        self.opened_prs: list[tuple[str, str, str, str]] = []
        self.merged: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str, str]] = []

    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        self.created_branches.append((repo, branch, from_branch))

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        self.committed_files.append((repo, branch, files, message))
        return "fake-commit-sha"

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        self.opened_prs.append((repo, branch, title, description))
        return "99"

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        self.merged.append((repo, pr_id))

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        self.closed.append((repo, pr_id, reason))


class FakeToxicityClassifier:
    """Stands in for app.modules.guardrails.bert_classifier.
    ONNXBertClassifier — never loads a real model, used by GuardrailEngine
    tests (test_guardrail_engine.py) where the classifier's score is just an
    input, not the thing under test. Fixed score set at construction time;
    tests instantiate one per desired confidence band (block / escalate /
    safe) rather than parameterising `score()` itself, since GuardrailEngine's
    classifier_factory is `Callable[[str], ...]` (called with just the model
    name). See tests/guardrail_onnx_fixtures.py for the *real* ONNX
    classifier's own test coverage — that one runs actual onnxruntime
    inference against a small synthetic model, not this fake."""

    def __init__(self, fixed_score: float) -> None:
        self._fixed_score = fixed_score

    def score(self, text: str) -> float:  # noqa: ARG002 — fixed regardless of input
        return self._fixed_score


class FakeBedrockGuardrailClient:
    """Stands in for the boto3 bedrock-runtime client's apply_guardrail
    call. `intervene` controls whether every call reports
    action=GUARDRAIL_INTERVENED; `sanitised_text` is returned as the
    output-path's redacted text when set."""

    def __init__(self, *, intervene: bool = False, sanitised_text: str | None = None) -> None:
        self.intervene = intervene
        self.sanitised_text = sanitised_text
        self.calls: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        response: dict[str, Any] = {"action": "GUARDRAIL_INTERVENED" if self.intervene else "NONE"}
        if self.sanitised_text is not None:
            response["outputs"] = [{"text": self.sanitised_text}]
        return response


class FailingBedrockGuardrailClient:
    """apply_guardrail always raises — exercises GuardrailEngine's
    fail-closed path."""

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated Bedrock outage")


class FakeBedrockControlPlaneClient:
    """Stands in for the boto3 `bedrock` (control-plane) client's
    create_guardrail/update_guardrail calls — moto 5.0.28 doesn't implement
    either (confirmed: raises NotImplementedError), so this is the only way
    to test app.modules.guardrails.provisioner.BedrockGuardrailProvisioner
    without a real AWS call."""

    def __init__(self, *, guardrail_id: str = "gr-fake-123", version: str = "DRAFT") -> None:
        self._guardrail_id = guardrail_id
        self._version = version
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def create_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        arn = f"arn:aws:bedrock:eu-west-2:123456789012:guardrail/{self._guardrail_id}"
        return {
            "guardrailId": self._guardrail_id,
            "guardrailArn": arn,
            "version": self._version,
            "createdAt": "2026-08-16T00:00:00Z",
        }

    def update_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        guardrail_id = kwargs["guardrailIdentifier"]
        arn = f"arn:aws:bedrock:eu-west-2:123456789012:guardrail/{guardrail_id}"
        return {
            "guardrailId": guardrail_id,
            "guardrailArn": arn,
            "version": self._version,
            "updatedAt": "2026-08-16T00:00:00Z",
        }

    def delete_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(kwargs)
        return {}


class FailingBedrockControlPlaneClient:
    """create_guardrail/update_guardrail/delete_guardrail all raise —
    exercises BedrockGuardrailProvisioner.deprovision()'s best-effort
    swallow-and-log path (it must never propagate an AWS-side failure)."""

    def create_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated Bedrock control-plane outage")

    def update_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated Bedrock control-plane outage")

    def delete_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated Bedrock control-plane outage")


class InvalidCredentialsBedrockControlPlaneClient:
    """create_guardrail/update_guardrail raise a real botocore ClientError
    shaped like AWS's actual response to an unrecognized/invalid access
    key — QA A-05's exact reported failure mode, distinct from
    FailingBedrockControlPlaneClient's generic RuntimeError above."""

    def create_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            {
                "Error": {
                    "Code": "UnrecognizedClientException",
                    "Message": "The security token included in the request is invalid.",
                }
            },
            "CreateGuardrail",
        )

    def update_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            {
                "Error": {
                    "Code": "UnrecognizedClientException",
                    "Message": "The security token included in the request is invalid.",
                }
            },
            "UpdateGuardrail",
        )


class FakeSTSClient:
    """Stands in for the boto3 `sts` client's assume_role call — records
    every call and returns fixed, deterministic temporary credentials
    (never real AWS credentials) so BedrockGuardrailProvisioner's
    credential-resolution path (Section 37.15 STS AssumeRole design) is
    testable without a real AWS call."""

    def __init__(
        self,
        *,
        access_key_id: str = "ASIAFAKEACCESSKEY",
        secret_access_key: str = "fake-secret-access-key",  # noqa: S107
        session_token: str = "fake-session-token",  # noqa: S107
    ) -> None:
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self.assume_role_calls: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.assume_role_calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": self._access_key_id,
                "SecretAccessKey": self._secret_access_key,
                "SessionToken": self._session_token,
                "Expiration": "2026-08-16T01:00:00Z",
            }
        }
