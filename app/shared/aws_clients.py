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
