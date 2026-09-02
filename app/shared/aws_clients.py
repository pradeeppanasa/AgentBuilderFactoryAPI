"""Small boto3 client factories not big enough to earn their own module."""

from __future__ import annotations

from typing import Any

import boto3

from app.config import Settings


def create_eventbridge_client(settings: Settings) -> Any:
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.eventbridge_endpoint:
        kwargs["endpoint_url"] = settings.eventbridge_endpoint
    return boto3.client("events", **kwargs)


def create_codecommit_client(settings: Settings) -> Any:
    return boto3.client("codecommit", region_name=settings.aws_region)


def create_cloudwatch_client(settings: Settings) -> Any:
    return boto3.client("cloudwatch", region_name=settings.aws_region)


def create_ecr_client(settings: Settings) -> Any:
    return boto3.client("ecr", region_name=settings.aws_region)


def create_stepfunctions_client(settings: Settings) -> Any:
    return boto3.client("stepfunctions", region_name=settings.aws_region)


def create_ecs_client(settings: Settings) -> Any:
    return boto3.client("ecs", region_name=settings.aws_region)


def create_bedrock_runtime_client(settings: Settings) -> Any:
    """Guardrail Layer 2 (app.modules.guardrails.engine) only — R27's "every
    LLM inference call goes through model_router" doesn't cover this:
    bedrock-runtime:ApplyGuardrail evaluates content against a guardrail
    policy, it doesn't generate a completion, so it isn't an "inference
    call" in the sense R27 prohibits direct boto3 access to. LiteLLM also
    has no equivalent wrapper for this API."""
    return boto3.client("bedrock-runtime", region_name=settings.aws_region)


def create_bedrock_client(settings: Settings) -> Any:
    """Control-plane client — CreateGuardrail/UpdateGuardrail/DeleteGuardrail
    (app.modules.guardrails.provisioner), distinct from
    create_bedrock_runtime_client's data-plane ApplyGuardrail. Uses the
    Runtime's own ambient IAM role — the *default* client a
    BedrockGuardrailProvisioner falls back to when a GuardrailPolicy has no
    `bedrock_credential_id` set. See create_bedrock_client_with_credentials
    for the cross-account (STS AssumeRole) case."""
    return boto3.client("bedrock", region_name=settings.aws_region)


def create_bedrock_agent_client(settings: Settings) -> Any:
    """Control-plane client for Bedrock Knowledge Bases —
    CreateKnowledgeBase/CreateDataSource/StartIngestionJob/GetIngestionJob
    (app.modules.knowledge_base.provisioner). A different boto3 service
    ("bedrock-agent") from create_bedrock_client's plain "bedrock" above —
    guardrails and knowledge bases are control-planed by different Bedrock
    sub-services."""
    return boto3.client("bedrock-agent", region_name=settings.aws_region)


def create_sts_client(settings: Settings) -> Any:
    """Section 37.15 (2026-08-16) — resolves a GuardrailPolicy's
    bedrock_credential_id into temporary credentials via sts:AssumeRole.
    See app.modules.bedrock_credentials.models.BedrockCredentialRecord's
    docstring for why role assumption was chosen over static access keys."""
    return boto3.client("sts", region_name=settings.aws_region)


def create_bedrock_client_with_credentials(settings: Settings, credentials: dict[str, str]) -> Any:
    """Builds a `bedrock` control-plane client from temporary STS
    credentials (aws_access_key_id/aws_secret_access_key/aws_session_token)
    instead of the ambient IAM role — used by
    app.modules.guardrails.provisioner.BedrockGuardrailProvisioner's
    client_factory when a policy's bedrock_credential_id resolves via
    AssumeRole to a different AWS account/role."""
    return boto3.client("bedrock", region_name=settings.aws_region, **credentials)
