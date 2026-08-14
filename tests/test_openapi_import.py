"""Unit tests for app.modules.connectors.openapi_import
(CLAUDE_Advanced_Config.md Section 3.6 Method B, Section 37.11 `POST
/connectors/import-openapi`)."""

from __future__ import annotations

import pytest

from app.modules.connectors.openapi_import import OpenAPIParseError, parse_openapi_document

_PETSTORE = {
    "openapi": "3.0.0",
    "info": {"title": "Petstore", "version": "1.0"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "components": {
        "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}
    },
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List all pets",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"type": "object"}}
                            }
                        }
                    }
                },
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    }
                },
                "responses": {
                    "201": {"content": {"application/json": {"schema": {"type": "object"}}}}
                },
            },
        },
        "/pets/{petId}": {
            "parameters": [
                {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "get": {
                "summary": "Get a pet by id",
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
                },
            },
        },
    },
}


def test_parses_one_seed_per_path_method_operation() -> None:
    seeds = parse_openapi_document(_PETSTORE)

    names = {seed.name for seed in seeds}
    assert names == {"listPets", "createPet", "get_pets_petId"}
    assert len(seeds) == 3


def test_uses_operation_id_as_name_when_present() -> None:
    seeds = parse_openapi_document(_PETSTORE)
    list_pets = next(s for s in seeds if s.name == "listPets")

    assert list_pets.description == "List all pets"
    assert list_pets.endpoint_template == "https://api.example.com/v1/pets"


def test_falls_back_to_method_and_path_when_no_operation_id() -> None:
    seeds = parse_openapi_document(_PETSTORE)
    get_by_id = next(s for s in seeds if s.name == "get_pets_petId")

    assert get_by_id.description == "Get a pet by id"


def test_merges_shared_path_parameters_into_input_schema() -> None:
    seeds = parse_openapi_document(_PETSTORE)
    get_by_id = next(s for s in seeds if s.name == "get_pets_petId")

    assert "petId" in get_by_id.input_schema["properties"]
    assert get_by_id.input_schema["required"] == ["petId"]


def test_request_body_schema_included_as_body_property() -> None:
    seeds = parse_openapi_document(_PETSTORE)
    create_pet = next(s for s in seeds if s.name == "createPet")

    assert "body" in create_pet.input_schema["properties"]
    assert create_pet.input_schema["properties"]["body"]["properties"]["name"]["type"] == "string"


def test_output_schema_pulled_from_2xx_response() -> None:
    seeds = parse_openapi_document(_PETSTORE)
    list_pets = next(s for s in seeds if s.name == "listPets")

    assert list_pets.output_schema["type"] == "array"


def test_credentials_required_when_security_schemes_present() -> None:
    seeds = parse_openapi_document(_PETSTORE)

    assert all(seed.credentials_required == ["api_key"] for seed in seeds)


def test_no_security_schemes_means_no_credentials_required() -> None:
    document = {**_PETSTORE, "components": {}}
    seeds = parse_openapi_document(document)

    assert all(seed.credentials_required == [] for seed in seeds)


def test_accepts_yaml_string() -> None:
    yaml_doc = """
openapi: "3.0.0"
info:
  title: Minimal
  version: "1.0"
paths:
  /ping:
    get:
      operationId: ping
      responses:
        "200":
          content:
            application/json:
              schema:
                type: object
"""
    seeds = parse_openapi_document(yaml_doc)

    assert len(seeds) == 1
    assert seeds[0].name == "ping"


def test_missing_paths_raises() -> None:
    with pytest.raises(OpenAPIParseError):
        parse_openapi_document({"openapi": "3.0.0", "info": {}})


def test_empty_paths_raises() -> None:
    with pytest.raises(OpenAPIParseError):
        parse_openapi_document({"openapi": "3.0.0", "paths": {}})


def test_unparseable_string_raises() -> None:
    with pytest.raises(OpenAPIParseError):
        parse_openapi_document("not: valid: yaml: [unbalanced")


def test_no_base_url_falls_back_to_bare_path() -> None:
    document = {
        "openapi": "3.0.0",
        "paths": {"/x": {"get": {"operationId": "getX", "responses": {}}}},
    }
    seeds = parse_openapi_document(document)

    assert seeds[0].endpoint_template == "/x"
