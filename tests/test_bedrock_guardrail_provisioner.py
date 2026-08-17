"""Unit tests for app.modules.guardrails.provisioner.BedrockGuardrailProvisioner
(CLAUDE.md Section 37.7: "Auto-provision Bedrock guardrail on save").

Uses FakeBedrockControlPlaneClient (tests/fakes.py) since moto 5.0.28 does
not implement create_guardrail/update_guardrail (confirmed: raises
NotImplementedError) — field shapes in provisioner.py were verified
directly against botocore's bundled Bedrock service model instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.bedrock_credentials.models import BedrockCredentialRecord
from app.modules.bedrock_credentials.store import (
    BedrockCredentialNotFoundError,
)
from app.modules.guardrails.models import (
    BedrockContentFilters,
    BedrockFilterConfig,
    BedrockStrength,
    GuardrailPolicy,
    KeywordPatternType,
    KeywordPolicy,
    KeywordRule,
    PiiAction,
    PiiConfig,
    PiiFieldConfig,
    TopicConfig,
)
from app.modules.guardrails.provisioner import BedrockGuardrailProvisioner
from tests.fakes import (
    FailingBedrockControlPlaneClient,
    FakeBedrockControlPlaneClient,
    FakeSTSClient,
)


def _policy(**overrides: object) -> GuardrailPolicy:
    now = datetime.now(UTC).isoformat()
    data: dict[str, object] = {
        "policy_id": "strict-a1b2c3",
        "tenant_id": "tenant-a",
        "name": "Strict",
        "description": "d",
        "created_by": "admin@example.com",
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return GuardrailPolicy(**data)


async def test_create_guardrail_called_when_no_existing_id() -> None:
    client = FakeBedrockControlPlaneClient(guardrail_id="gr-new-1")
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy()

    guardrail_id, version = await provisioner.provision("tenant-a", policy)

    assert guardrail_id == "gr-new-1"
    assert version == "DRAFT"
    assert len(client.create_calls) == 1
    assert client.update_calls == []
    assert client.create_calls[0]["name"] == "strict-a1b2c3"


async def test_update_guardrail_called_when_id_already_set() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(bedrock_guardrail_id="gr-existing", bedrock_guardrail_version="DRAFT")

    await provisioner.provision("tenant-a", policy)

    assert client.create_calls == []
    assert len(client.update_calls) == 1
    assert client.update_calls[0]["guardrailIdentifier"] == "gr-existing"


async def test_uses_policy_id_not_display_name_for_bedrock_name() -> None:
    """policy_id is account-wide unique (slug + uuid); policy.name is a
    human display string two tenants could collide on."""
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(name="Default", policy_id="default-9f8e7d")

    await provisioner.provision("tenant-a", policy)

    assert client.create_calls[0]["name"] == "default-9f8e7d"
    assert "Default" in client.create_calls[0]["description"]


async def test_content_filters_map_to_bedrock_shape() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(
        bedrock_content_filters=BedrockContentFilters(
            sexual_content=BedrockFilterConfig(
                input_strength=BedrockStrength.LOW, output_strength=BedrockStrength.MEDIUM
            )
        )
    )

    await provisioner.provision("tenant-a", policy)

    filters = client.create_calls[0]["contentPolicyConfig"]["filtersConfig"]
    sexual = next(f for f in filters if f["type"] == "SEXUAL")
    assert sexual["inputStrength"] == "LOW"
    assert sexual["outputStrength"] == "MEDIUM"
    prompt_attack = next(f for f in filters if f["type"] == "PROMPT_ATTACK")
    assert prompt_attack["outputStrength"] == "NONE"
    assert {f["type"] for f in filters} == {
        "SEXUAL",
        "VIOLENCE",
        "HATE",
        "INSULTS",
        "MISCONDUCT",
        "PROMPT_ATTACK",
    }


async def test_pii_config_only_includes_non_disabled_fields() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(
        pii=PiiConfig(
            email=PiiFieldConfig(action=PiiAction.BLOCK),
            ssn=PiiFieldConfig(action=PiiAction.REDACT),
        )
    )

    await provisioner.provision("tenant-a", policy)

    request = client.create_calls[0]
    entities = {
        e["type"]: e["action"]
        for e in request["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]
    }
    assert entities == {"EMAIL": "BLOCK", "US_SOCIAL_SECURITY_NUMBER": "ANONYMIZE"}


async def test_pii_config_omitted_when_all_disabled() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy()  # all PII fields default to DISABLED

    await provisioner.provision("tenant-a", policy)

    assert "sensitiveInformationPolicyConfig" not in client.create_calls[0]


async def test_banned_topics_map_to_deny_topics() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(topics=TopicConfig(banned_topics=["politics", "religion"]))

    await provisioner.provision("tenant-a", policy)

    topics_config = client.create_calls[0]["topicPolicyConfig"]["topicsConfig"]
    assert {t["name"] for t in topics_config} == {"politics", "religion"}
    assert all(t["type"] == "DENY" for t in topics_config)


async def test_topic_policy_omitted_when_no_banned_topics() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy()

    await provisioner.provision("tenant-a", policy)

    assert "topicPolicyConfig" not in client.create_calls[0]


async def test_literal_keyword_rules_map_to_words_config() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(
        keywords=KeywordPolicy(
            rules=[
                KeywordRule(pattern="jailbreak", pattern_type=KeywordPatternType.LITERAL),
                KeywordRule(pattern=r"\d{16}", pattern_type=KeywordPatternType.REGEX),
            ]
        )
    )

    await provisioner.provision("tenant-a", policy)

    words_config = client.create_calls[0]["wordPolicyConfig"]["wordsConfig"]
    assert words_config == [{"text": "jailbreak"}]  # regex rule dropped, not coerced


async def test_word_policy_omitted_when_only_regex_rules() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(
        keywords=KeywordPolicy(
            rules=[KeywordRule(pattern=r"\d{16}", pattern_type=KeywordPatternType.REGEX)]
        )
    )

    await provisioner.provision("tenant-a", policy)

    assert "wordPolicyConfig" not in client.create_calls[0]


async def test_blocked_messages_map_to_input_and_output_messaging() -> None:
    from app.modules.guardrails.models import BlockedMessages

    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(blocked_messages=BlockedMessages(content_blocked="Nope."))

    await provisioner.provision("tenant-a", policy)

    request = client.create_calls[0]
    assert request["blockedInputMessaging"] == "Nope."
    assert request["blockedOutputsMessaging"] == "Nope."


class _FakeCredentialStore:
    """Minimal stand-in for BedrockCredentialStore.get() — avoids spinning up
    a real DynamoDB table just to test credential *resolution* logic, which
    is what these tests are actually about."""

    def __init__(self, records: dict[str, BedrockCredentialRecord]) -> None:
        self._records = records

    async def get(self, tenant_id: str, credential_id: str) -> BedrockCredentialRecord | None:
        record = self._records.get(credential_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record


def _credential(credential_id: str = "cred-a1b2c3", role_arn: str = "arn:aws:iam::999:role/x"):
    now = datetime.now(UTC).isoformat()
    return BedrockCredentialRecord(
        credential_id=credential_id,
        tenant_id="tenant-a",
        name="Cross-account",
        role_arn=role_arn,
        created_by="admin@example.com",
        created_at=now,
        updated_at=now,
    )


async def test_default_client_used_when_no_credential_id_set() -> None:
    """No bedrock_credential_id -> ambient role, no STS call at all."""
    client = FakeBedrockControlPlaneClient()
    sts = FakeSTSClient()
    provisioner = BedrockGuardrailProvisioner(client, sts_client=sts)
    policy = _policy()

    await provisioner.provision("tenant-a", policy)

    assert sts.assume_role_calls == []
    assert len(client.create_calls) == 1


async def test_credential_id_resolves_via_sts_assume_role_and_client_factory() -> None:
    credential = _credential(role_arn="arn:aws:iam::999999999999:role/cross-account-bedrock")
    credential_store = _FakeCredentialStore({credential.credential_id: credential})
    sts = FakeSTSClient(
        access_key_id="ASIATESTKEY", secret_access_key="test-secret", session_token="test-token"
    )
    scoped_client = FakeBedrockControlPlaneClient(guardrail_id="gr-scoped")
    received_credentials: list[dict[str, str]] = []

    def client_factory(creds: dict[str, str]):
        received_credentials.append(creds)
        return scoped_client

    provisioner = BedrockGuardrailProvisioner(
        FakeBedrockControlPlaneClient(guardrail_id="gr-default"),
        sts_client=sts,
        credential_store=credential_store,
        client_factory=client_factory,
    )
    policy = _policy(bedrock_credential_id=credential.credential_id)

    guardrail_id, _version = await provisioner.provision("tenant-a", policy)

    assert guardrail_id == "gr-scoped"  # went through the scoped client, not the default one
    assert len(sts.assume_role_calls) == 1
    assert sts.assume_role_calls[0]["RoleArn"] == credential.role_arn
    assert sts.assume_role_calls[0]["RoleSessionName"] == f"panasa-guardrail-{policy.policy_id}"
    assert received_credentials == [
        {
            "aws_access_key_id": "ASIATESTKEY",
            "aws_secret_access_key": "test-secret",
            "aws_session_token": "test-token",
        }
    ]


async def test_unknown_credential_id_raises_not_found() -> None:
    credential_store = _FakeCredentialStore({})
    provisioner = BedrockGuardrailProvisioner(
        FakeBedrockControlPlaneClient(),
        sts_client=FakeSTSClient(),
        credential_store=credential_store,
        client_factory=lambda _creds: FakeBedrockControlPlaneClient(),
    )
    policy = _policy(bedrock_credential_id="does-not-exist")

    with pytest.raises(BedrockCredentialNotFoundError):
        await provisioner.provision("tenant-a", policy)


async def test_credential_id_without_store_or_sts_configured_raises_not_found() -> None:
    """Provisioner constructed without sts_client/credential_store (the
    no-arg default from before Section 37.15) — a policy that references a
    credential_id can't be resolved at all, not silently ignored."""
    provisioner = BedrockGuardrailProvisioner(FakeBedrockControlPlaneClient())
    policy = _policy(bedrock_credential_id="cred-a1b2c3")

    with pytest.raises(BedrockCredentialNotFoundError):
        await provisioner.provision("tenant-a", policy)


async def test_deprovision_deletes_guardrail_when_id_set() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(bedrock_guardrail_id="gr-existing", bedrock_guardrail_version="DRAFT")

    await provisioner.deprovision("tenant-a", policy)

    assert len(client.delete_calls) == 1
    assert client.delete_calls[0]["guardrailIdentifier"] == "gr-existing"


async def test_deprovision_is_noop_when_no_guardrail_id() -> None:
    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy()  # bedrock_guardrail_id defaults to None

    await provisioner.deprovision("tenant-a", policy)

    assert client.delete_calls == []


async def test_deprovision_swallows_aws_failures_instead_of_raising() -> None:
    """Best-effort delete: an AWS-side failure must never block the caller
    (the guardrail policy DELETE route) from completing."""
    provisioner = BedrockGuardrailProvisioner(FailingBedrockControlPlaneClient())
    policy = _policy(bedrock_guardrail_id="gr-existing")

    await provisioner.deprovision("tenant-a", policy)  # must not raise


async def test_deprovision_resolves_credential_id_before_deleting() -> None:
    credential = _credential()
    credential_store = _FakeCredentialStore({credential.credential_id: credential})
    sts = FakeSTSClient()
    scoped_client = FakeBedrockControlPlaneClient(guardrail_id="gr-scoped")
    provisioner = BedrockGuardrailProvisioner(
        FakeBedrockControlPlaneClient(guardrail_id="gr-default"),
        sts_client=sts,
        credential_store=credential_store,
        client_factory=lambda _creds: scoped_client,
    )
    policy = _policy(
        bedrock_credential_id=credential.credential_id, bedrock_guardrail_id="gr-existing"
    )

    await provisioner.deprovision("tenant-a", policy)

    assert len(sts.assume_role_calls) == 1
    assert len(scoped_client.delete_calls) == 1


async def test_compliance_config_never_sent_to_bedrock() -> None:
    from app.modules.guardrails.models import ComplianceConfig, ComplianceFramework

    client = FakeBedrockControlPlaneClient()
    provisioner = BedrockGuardrailProvisioner(client)
    policy = _policy(
        compliance=ComplianceConfig(
            frameworks=[ComplianceFramework.GDPR, ComplianceFramework.HIPAA]
        )
    )

    await provisioner.provision("tenant-a", policy)

    request = client.create_calls[0]
    assert not any("compliance" in key.lower() for key in request)
