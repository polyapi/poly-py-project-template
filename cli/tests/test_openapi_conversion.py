import copy

import pytest

from cli.openapi_conversion import swagger_to_openapi


def test_swagger_conversion_preserves_transport_contract_and_source():
    source = {
        "swagger": "2.0",
        "info": {"title": "Test", "version": "1"},
        "host": "example.invalid",
        "basePath": "/v1",
        "schemes": ["https"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "securityDefinitions": {"auth": {"type": "basic"}},
        "security": [{"auth": []}],
        "parameters": {
            "id": {"name": "id", "in": "path", "required": True, "type": "string"}
        },
        "definitions": {
            "Item": {
                "type": "object",
                "properties": {"self": {"$ref": "#/definitions/Item"}},
                "example": {"$ref": "literal"},
            }
        },
        "paths": {
            "/items/{id}": {
                "parameters": [{"$ref": "#/parameters/id"}],
                "put": {
                    "operationId": "putItem",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {"$ref": "#/definitions/Item"},
                        },
                        {
                            "name": "tags",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "schema": {"$ref": "#/definitions/Item"},
                            "headers": {"X-Count": {"type": "integer"}},
                            "examples": {
                                "application/json": {"description": "<b>literal</b>"}
                            },
                        }
                    },
                },
            }
        },
    }
    original = copy.deepcopy(source)
    result = swagger_to_openapi(source)
    assert result["openapi"] == "3.0.3"
    assert "swagger" not in result
    assert result["servers"] == [{"url": "https://example.invalid/v1"}]
    operation = result["paths"]["/items/{id}"]["put"]
    assert operation["requestBody"] == {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
        },
    }
    assert operation["parameters"][0]["schema"] == {"type": "string"}
    assert operation["parameters"][1]["style"] == "form"
    assert operation["parameters"][1]["explode"] is True
    assert operation["responses"]["200"]["headers"]["X-Count"]["schema"] == {
        "type": "integer"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["example"] == {
        "description": "<b>literal</b>"
    }
    assert result["components"]["schemas"]["Item"]["example"] == {"$ref": "literal"}
    assert (
        result["components"]["schemas"]["Item"]["properties"]["self"]["$ref"]
        == "#/components/schemas/Item"
    )
    assert result["components"]["securitySchemes"]["auth"] == {
        "type": "http",
        "scheme": "basic",
    }
    assert source == original


def test_form_data_becomes_request_body_with_file_and_required_fields():
    source = {
        "swagger": "2.0",
        "paths": {
            "/upload": {
                "post": {
                    "parameters": [
                        {
                            "name": "file",
                            "in": "formData",
                            "type": "file",
                            "required": True,
                        }
                    ],
                    "responses": {"204": {"description": "Done"}},
                }
            }
        },
    }
    body = swagger_to_openapi(source)["paths"]["/upload"]["post"]["requestBody"]
    assert body["required"] is True
    assert body["content"]["multipart/form-data"]["schema"] == {
        "type": "object",
        "properties": {"file": {"type": "string", "format": "binary"}},
        "required": ["file"],
    }


def test_unsupported_collection_formats_fail_instead_of_changing_encoding():
    source = {
        "swagger": "2.0",
        "paths": {
            "/test": {
                "get": {
                    "parameters": [
                        {
                            "name": "ids",
                            "in": "query",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "tsv",
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    with pytest.raises(ValueError, match="Unsupported Swagger query collectionFormat"):
        swagger_to_openapi(source)
