"""Parses an OpenAPI 3.0 document into one connector seed per path+method
operation (CLAUDE_Advanced_Config.md Section 3.6 Method B / Section 37.11
`POST /connectors/import-openapi`).

Deliberately conservative: this produces a reasonable starting point per
operation (name, description, input/output JSON Schema, endpoint template),
not a full OpenAPI-to-JSONSchema compiler — `$ref` resolution, oneOf/allOf,
and non-JSON content types are out of scope. A generated connector is a
normal tenant connector the user can still edit afterward (Method C).
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _fallback_name(method: str, path: str) -> str:
    """Used when an operation has no `operationId` — e.g. "/pets/{petId}" +
    "get" -> "get_pets_petId", not the literal "get__pets_{petId}" a plain
    `path.replace("/", "_")` would produce."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
    return f"{method}_{cleaned}"


class ParsedConnectorSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    endpoint_template: str | None = None
    credentials_required: list[str] = Field(default_factory=list)


class OpenAPIParseError(ValueError):
    pass


def parse_openapi_document(raw: dict[str, Any] | str) -> list[ParsedConnectorSeed]:
    document = _load(raw)

    if not isinstance(document.get("paths"), dict):
        raise OpenAPIParseError("OpenAPI document has no 'paths' object")

    base_url = _base_url(document)
    credentials_required = _credentials_required(document)

    seeds: list[ParsedConnectorSeed] = []
    for path, path_item in document["paths"].items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters", [])
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            seeds.append(
                _build_seed(
                    path=path,
                    method=method,
                    operation=operation,
                    shared_params=shared_params,
                    base_url=base_url,
                    credentials_required=credentials_required,
                )
            )

    if not seeds:
        raise OpenAPIParseError("No operations found in OpenAPI document's 'paths'")

    return seeds


def _load(raw: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        loaded = yaml.safe_load(raw)  # YAML is a JSON superset — handles both formats
    except yaml.YAMLError as exc:
        raise OpenAPIParseError(f"Could not parse OpenAPI document: {exc}") from exc
    if not isinstance(loaded, dict):
        raise OpenAPIParseError("OpenAPI document must decode to an object")
    return loaded


def _base_url(document: dict[str, Any]) -> str | None:
    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = servers[0].get("url")
        return url if isinstance(url, str) else None
    return None


def _credentials_required(document: dict[str, Any]) -> list[str]:
    schemes = document.get("components", {}).get("securitySchemes", {})
    return ["api_key"] if schemes else []


def _build_seed(
    *,
    path: str,
    method: str,
    operation: dict[str, Any],
    shared_params: list[Any],
    base_url: str | None,
    credentials_required: list[str],
) -> ParsedConnectorSeed:
    name = operation.get("operationId") or _fallback_name(method, path)
    description = (
        operation.get("summary") or operation.get("description") or f"{method.upper()} {path}"
    )

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in [*shared_params, *operation.get("parameters", [])]:
        if not isinstance(param, dict) or "name" not in param:
            continue
        properties[param["name"]] = param.get("schema", {"type": "string"})
        if param.get("required"):
            required.append(param["name"])

    request_body = operation.get("requestBody")
    body_schema = (
        request_body.get("content", {}).get("application/json", {}).get("schema")
        if isinstance(request_body, dict)
        else None
    )
    if isinstance(body_schema, dict):
        properties["body"] = body_schema

    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required

    return ParsedConnectorSeed(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=_first_json_response_schema(operation.get("responses", {})),
        endpoint_template=f"{base_url}{path}" if base_url else path,
        credentials_required=credentials_required,
    )


def _first_json_response_schema(responses: Any) -> dict[str, Any]:
    if not isinstance(responses, dict):
        return {"type": "object"}
    for status_code in ("200", "201", "default"):
        response = responses.get(status_code)
        if isinstance(response, dict):
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if isinstance(schema, dict):
                return schema
    return {"type": "object"}
