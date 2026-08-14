"""Type coercion helpers shared by anything reading raw DynamoDB items.

boto3's DynamoDB resource returns numeric attributes as `Decimal`; Pydantic
models expect plain `int`/`float`. Centralised here since both the registry
store and the versioner need it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def decimal_to_native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [decimal_to_native(v) for v in value]
    if isinstance(value, dict):
        return {k: decimal_to_native(v) for k, v in value.items()}
    return value
