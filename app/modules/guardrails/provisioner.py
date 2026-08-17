"""Auto-provisions the real AWS Bedrock Guardrail resource behind a
GuardrailPolicy (CLAUDE.md Section 37.7: "Auto-provision Bedrock guardrail
on save"). Called by the API layer on POST/PUT/DELETE of a guardrail
policy — never by GuardrailEngine, which only *applies* an
already-provisioned guardrail via the separate ApplyGuardrail data-plane
API.

Field shapes were verified directly against botocore's bundled Bedrock
service model (`botocore.session.get_session().get_service_model("bedrock")`
-> CreateGuardrail/UpdateGuardrail/DeleteGuardrail operation shapes), not
from memory. Same for STS AssumeRole's request/response shape.

Credential resolution (Section 37.15, closed 2026-08-16): when
`policy.bedrock_credential_id` is set, the guardrail is created/updated/
deleted using a client built from *temporary* credentials obtained via
`sts:AssumeRole` on that credential's stored `role_arn` — never the
Runtime's own ambient IAM role. When unset, falls back to the ambient
role (the `default_bedrock_client` injected at construction), matching
every other AWS client factory in this codebase.

Deliberately partial mappings, each documented at its own site below:
 - PiiConfig.ssn -> US_SOCIAL_SECURITY_NUMBER only (Bedrock has no generic
   "ssn" entity type; CA/UK equivalents exist but aren't in our schema).
 - PiiConfig.api_key_secret -> PASSWORD (closest available entity type;
   Bedrock has no generic "secret/API key" category).
 - PiiConfig.date_time -> no Bedrock equivalent; silently omitted.
 - TopicConfig.allowed_topics -> no Bedrock equivalent (Bedrock's
   GuardrailTopicType enum is DENY-only, no ALLOW/whitelist concept);
   never sent, same disclosed-gap treatment as BertConfig's
   check_nsfw/check_prompt_injection/check_gibberish (see models.py).
 - KeywordRule with pattern_type=REGEX -> Bedrock's wordPolicyConfig only
   supports literal words, not regex; REGEX rules are dropped from the
   Bedrock payload (logged), never silently coerced into a literal match.
 - ComplianceConfig is never sent to Bedrock at all — custom_rules are an
   LLM-judge-at-invocation-time concept (F8's Generated Agent Runtime),
   not a Bedrock guardrail policy concept.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.modules.bedrock_credentials.store import (
    BedrockCredentialNotFoundError,
    BedrockCredentialStore,
)
from app.modules.guardrails.models import GuardrailPolicy, KeywordPatternType, PiiAction
from app.shared.logging import get_logger

log = get_logger()

BedrockClientFactory = Callable[[dict[str, str]], Any]

_PII_ENTITY_MAP: dict[str, str] = {
    "credit_card": "CREDIT_DEBIT_CARD_NUMBER",
    "email": "EMAIL",
    "phone": "PHONE",
    "person_name": "NAME",
    "ssn": "US_SOCIAL_SECURITY_NUMBER",
    "ip_address": "IP_ADDRESS",
    "api_key_secret": "PASSWORD",
}

_PII_ACTION_MAP: dict[PiiAction, str] = {
    PiiAction.BLOCK: "BLOCK",
    PiiAction.REDACT: "ANONYMIZE",
}

_CONTENT_FILTER_TYPES = (
    "SEXUAL",
    "VIOLENCE",
    "HATE",
    "INSULTS",
    "MISCONDUCT",
    "PROMPT_ATTACK",
)
_FILTER_FIELD_BY_TYPE = {
    "SEXUAL": "sexual_content",
    "VIOLENCE": "violence",
    "HATE": "hate_speech",
    "INSULTS": "insults",
    "MISCONDUCT": "misconduct",
    "PROMPT_ATTACK": "prompt_attack",
}


def _default_client_factory_unset(_credentials: dict[str, str]) -> Any:
    raise RuntimeError(
        "BedrockGuardrailProvisioner needs a client_factory to resolve "
        "bedrock_credential_id-bound policies — none was configured."
    )


class BedrockGuardrailProvisioner:
    def __init__(
        self,
        default_bedrock_client: Any,
        sts_client: Any | None = None,
        credential_store: BedrockCredentialStore | None = None,
        client_factory: BedrockClientFactory = _default_client_factory_unset,
    ) -> None:
        self._default_client = default_bedrock_client
        self._sts = sts_client
        self._credential_store = credential_store
        self._client_factory = client_factory

    async def provision(self, tenant_id: str, policy: GuardrailPolicy) -> tuple[str, str]:
        """Creates (or updates, if `policy.bedrock_guardrail_id` is already
        set) the Bedrock guardrail behind `policy` and returns
        (guardrail_id, guardrail_version). The caller decides whether to
        invoke this at all — e.g. skip when `policy.bedrock_enabled` is
        False, since there's nothing useful to provision."""
        client = await self._resolve_client(tenant_id, policy)
        request = self._build_request(policy)

        if policy.bedrock_guardrail_id:
            request["guardrailIdentifier"] = policy.bedrock_guardrail_id
            response = await asyncio.to_thread(client.update_guardrail, **request)
        else:
            response = await asyncio.to_thread(client.create_guardrail, **request)

        return response["guardrailId"], response["version"]

    async def deprovision(self, tenant_id: str, policy: GuardrailPolicy) -> None:
        """Deletes the Bedrock guardrail resource behind `policy`, if any.
        Best-effort: AWS-side failures are logged, never raised — the
        DynamoDB record is this Runtime's source of truth for desired
        state (R02), and a stray orphaned Bedrock resource is a cheaper
        failure mode than blocking the policy delete the admin asked for."""
        if not policy.bedrock_guardrail_id:
            return
        try:
            client = await self._resolve_client(tenant_id, policy)
            await asyncio.to_thread(
                client.delete_guardrail, guardrailIdentifier=policy.bedrock_guardrail_id
            )
        except Exception:
            log.warning(
                "guardrails.provisioner.delete_failed",
                policy_id=policy.policy_id,
                exc_info=True,
            )

    async def _resolve_client(self, tenant_id: str, policy: GuardrailPolicy) -> Any:
        if not policy.bedrock_credential_id:
            return self._default_client
        if self._credential_store is None or self._sts is None:
            raise BedrockCredentialNotFoundError(policy.bedrock_credential_id)

        credential = await self._credential_store.get(tenant_id, policy.bedrock_credential_id)
        if credential is None:
            raise BedrockCredentialNotFoundError(policy.bedrock_credential_id)

        response = await asyncio.to_thread(
            self._sts.assume_role,
            RoleArn=credential.role_arn,
            RoleSessionName=f"panasa-guardrail-{policy.policy_id}"[:64],
        )
        temp_creds = response["Credentials"]
        return self._client_factory(
            {
                "aws_access_key_id": temp_creds["AccessKeyId"],
                "aws_secret_access_key": temp_creds["SecretAccessKey"],
                "aws_session_token": temp_creds["SessionToken"],
            }
        )

    def _build_request(self, policy: GuardrailPolicy) -> dict[str, Any]:
        request: dict[str, Any] = {
            # policy_id, not policy.name: Bedrock guardrail names must be
            # unique per AWS account, but policy.name is a human-editable
            # display string multiple tenants sharing one enterprise
            # account could easily collide on (e.g. two tenants both
            # naming a policy "Default"). policy_id already carries a
            # random suffix (GuardrailPolicyStore._slugify + uuid) that
            # makes it account-wide unique.
            "name": policy.policy_id,
            # Bedrock's GuardrailDescription has no pattern restriction but
            # is capped at 200 chars — truncate defensively so a long
            # name/description never causes CreateGuardrail to reject the
            # request outright.
            "description": f"{policy.name} — {policy.description}"[:200],
            "blockedInputMessaging": policy.blocked_messages.content_blocked,
            "blockedOutputsMessaging": policy.blocked_messages.content_blocked,
            "contentPolicyConfig": self._content_policy_config(policy),
        }

        pii_config = self._pii_config(policy)
        if pii_config is not None:
            request["sensitiveInformationPolicyConfig"] = pii_config

        topic_config = self._topic_policy_config(policy)
        if topic_config is not None:
            request["topicPolicyConfig"] = topic_config

        word_config = self._word_policy_config(policy)
        if word_config is not None:
            request["wordPolicyConfig"] = word_config

        return request

    @staticmethod
    def _content_policy_config(policy: GuardrailPolicy) -> dict[str, Any]:
        filters = policy.bedrock_content_filters
        return {
            "filtersConfig": [
                {
                    "type": filter_type,
                    "inputStrength": getattr(filters, field).input_strength.value,
                    "outputStrength": getattr(filters, field).output_strength.value,
                }
                for filter_type in _CONTENT_FILTER_TYPES
                for field in [_FILTER_FIELD_BY_TYPE[filter_type]]
            ]
        }

    @staticmethod
    def _pii_config(policy: GuardrailPolicy) -> dict[str, Any] | None:
        entities = []
        for field_name, bedrock_type in _PII_ENTITY_MAP.items():
            field_config = getattr(policy.pii, field_name)
            if field_config.action == PiiAction.DISABLED:
                continue
            entities.append({"type": bedrock_type, "action": _PII_ACTION_MAP[field_config.action]})
        return {"piiEntitiesConfig": entities} if entities else None

    @staticmethod
    def _topic_policy_config(policy: GuardrailPolicy) -> dict[str, Any] | None:
        if not policy.topics.banned_topics:
            return None
        return {
            "topicsConfig": [
                {"name": topic, "definition": topic, "type": "DENY"}
                for topic in policy.topics.banned_topics
            ]
        }

    @staticmethod
    def _word_policy_config(policy: GuardrailPolicy) -> dict[str, Any] | None:
        literal_words = [
            rule.pattern
            for rule in policy.keywords.rules
            if rule.pattern_type == KeywordPatternType.LITERAL
        ]
        skipped = len(policy.keywords.rules) - len(literal_words)
        if skipped:
            log.warning(
                "guardrails.provisioner.regex_rules_skipped",
                policy_id=policy.policy_id,
                skipped_count=skipped,
            )
        if not literal_words:
            return None
        return {"wordsConfig": [{"text": word} for word in literal_words]}
