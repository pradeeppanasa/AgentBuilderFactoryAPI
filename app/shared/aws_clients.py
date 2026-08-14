"""Small boto3 client factories not big enough to earn their own module."""

from __future__ import annotations

from typing import Any

import boto3

from app.config import Settings


def create_eventbridge_client(settings: Settings) -> Any:
    return boto3.client("events", region_name=settings.aws_region)


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
