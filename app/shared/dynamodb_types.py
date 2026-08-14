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


def native_to_decimal(value: Any) -> Any:
    """Inverse of decimal_to_native. boto3's DynamoDB Table resource rejects
    bare Python floats outright (`TypeError: Float types are not supported.
    Use Decimal types instead.`) — anything with a float attribute
    (BaseModel.model_dump(mode="json")'s output included) must go through
    this before put_item/update_item. Decimal(str(value)) rather than
    Decimal(value) avoids binary float representation artifacts (e.g.
    Decimal(0.1) has a long non-0.1 tail; Decimal(str(0.1)) does not).
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [native_to_decimal(v) for v in value]
    if isinstance(value, dict):
        return {k: native_to_decimal(v) for k, v in value.items()}
    return value
