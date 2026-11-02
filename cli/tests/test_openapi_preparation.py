import copy
import json
from unittest.mock import Mock

import pytest

from cli import flow, operations
from cli.openapi_preparation import (
    clean_descriptions,
    description_text,
    normalize_schema_names,
    select_operations,
)


def specification():
    return {
        "swagger": "2.0",
        "paths": {
            "/phone": {
                "parameters": [{"$ref": "#/parameters/id"}],
                "get": {
                    "operationId": "validatePhone",
                    "responses": {"200": {"schema": {"$ref": "#/definitions/Parent"}}},
                },
                "post": {"operationId": "createPhone"},
            }
        },
        "parameters": {"id": {"name": "id", "in": "query", "type": "string"}},
        "definitions": {
            "Parent": {"properties": {"child": {"$ref": "#/definitions/Child"}}},
            "Child": {"type": "string"},
            "Unused": {"type": "boolean"},
        },
    }


def test_selection_retains_dependencies_and_path_parameters_without_changing_source():
    source = specification()
    original = copy.deepcopy(source)
    selected = select_operations(source, ["validatePhone"])
    assert set(selected["paths"]["/phone"]) == {"get", "parameters"}
    assert set(selected["definitions"]) == {"Parent", "Child"}
    assert selected["parameters"] == source["parameters"]
    assert source == original


def test_selection_refuses_missing_ambiguous_and_external_references():
    source = specification()
    with pytest.raises(ValueError, match="found 0"):
        select_operations(source, ["missing"])
    source["paths"]["/duplicate"] = copy.deepcopy(source["paths"]["/phone"])
    with pytest.raises(ValueError, match="found 2"):
        select_operations(source, ["validatePhone"])
    del source["paths"]["/duplicate"]
    source["definitions"]["Child"] = {"$ref": "other.json#/Thing"}
    with pytest.raises(ValueError, match="bundle external"):
        select_operations(source, ["validatePhone"])


def test_descriptions_become_readable_text_without_rewriting_literals():
    assert (
        description_text(
            "Use this API. <p><strong>OperationId:</strong>validatePhone</p>"
        )
        == "Use this API.\nOperationId:validatePhone"
    )
    assert (
        description_text(
            "<p>A &amp; B &#x1F600;</p><ul><li>First</li><li>Second</li></ul><script>secret()</script>"
        )
        == "A & B 😀\n\n- First\n- Second"
    )
    assert (
        description_text("List<T> and <hotelId> are required")
        == "List<T> and <hotelId> are required"
    )
    source = {
        "description": "<b>API</b>",
        "source": {"description": "<b>literal</b>"},
        "definition": {
            "properties": {"example": {"description": "<b>field</b>"}},
            "example": {"description": "<b>payload</b>"},
        },
    }
    cleaned = clean_descriptions(source)
    assert cleaned["description"] == "API"
    assert cleaned["definition"]["properties"]["example"]["description"] == "field"
    assert cleaned["source"] == source["source"]
    assert cleaned["definition"]["example"] == source["definition"]["example"]


def test_numeric_schema_normalization_preserves_response_status_and_examples():
    source = {
        "openapi": "3.0.3",
        "components": {"schemas": {"200": {"type": "number", "example": 200}}},
        "paths": {
            "/test": {
                "get": {
                    "responses": {
                        "200": {"schema": {"$ref": "#/components/schemas/200"}}
                    }
                }
            }
        },
    }
    result = normalize_schema_names(source)
    assert result["components"]["schemas"] == {
        "Schema200": {"type": "number", "example": 200}
    }
    assert (
        result["paths"]["/test"]["get"]["responses"]["200"]["schema"]["$ref"]
        == "#/components/schemas/Schema200"
    )
    assert "200" in source["components"]["schemas"]


def test_training_filters_before_translation_and_cleans_final_ai_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    monkeypatch.delenv("POLYAPI_NAMESPACES", raising=False)
    config = flow.build_config(
        tmp_path, flow.CliArgs(False, False, False, False, (), None, False)
    )
    model = {
        "functions": [
            {
                "name": "validatePhone",
                "context": "example.crm",
                "description": "<p>Original</p>",
                "arguments": [],
                "source": {
                    "auth": {"type": "noauth"},
                    "body": {"mode": "empty"},
                    "url": "{{hostUrl}}/phone",
                    "method": "GET",
                },
            }
        ]
    }
    client = Mock()
    client.request.side_effect = [
        model,
        {
            "name": "validatePhone",
            "description": "<b>AI</b> &amp; text",
            "arguments": [],
        },
    ]
    operations.train_openapi(
        client,
        [(tmp_path / "crm.json", "example.crm", json.dumps(specification()))],
        config,
        dry_run=False,
        generate_only=True,
        overwrite_model=False,
        operation_ids=["validatePhone"],
    )
    translated = json.loads(client.request.call_args_list[0].kwargs["raw_body"])
    assert set(translated["paths"]["/phone"]) == {"get"}
    assert translated["paths"]["/phone"]["get"]["parameters"][0]["name"] == "id"
    saved = json.loads((tmp_path / "crm.poly.json").read_text())
    assert saved["functions"][0]["description"] == "AI & text"
    assert client.request.call_count == 2


def test_flow_dispatches_operations_commands(monkeypatch):
    monkeypatch.setattr("sys.argv", ["flow", "train-openapi", "--help"])
    called = Mock(return_value=0)
    monkeypatch.setattr(operations, "main", called)
    assert flow.main() == 0
    called.assert_called_once_with(["train-openapi", "--help"])
