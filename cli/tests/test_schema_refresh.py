import copy
import json
from pathlib import Path
import ssl
import subprocess
import sys
from unittest.mock import Mock

import pytest
import requests
from jsonschema import Draft7Validator

from cli import schema_refresh as refresh


def platform_spec():
    spec = {"openapi": "3.0.3", "paths": {}, "components": {"schemas": {}}}
    for name, (path, method) in refresh.TARGETS.items():
        spec["paths"].setdefault(path, {})[method] = {
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{name}"}
                    }
                }
            }
        }
        spec["components"]["schemas"][name] = {
            "type": "object",
            "properties": {"name": {"type": "string", "pattern": "/^[a-z]+$/i"}},
            "required": ["name"],
        }
    return spec


@pytest.fixture
def schema_source_env(monkeypatch):
    for name in (
        "POLY_API_BASE_URL",
        "POLYAPI_OPENAPI_SPEC_URL",
        "POLYAPI_DESIGNS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://eu1.polyapi.io",
        "https://na1.polyapi.io/",
        "https://na2.polyapi.io",
        "https://poly.example.invalid/gateway/",
        "http://localhost:3000/",
    ],
)
def test_refresh_uses_selected_instance(
    tmp_path, monkeypatch, schema_source_env, base_url
):
    monkeypatch.setenv("POLY_API_BASE_URL", base_url)
    read_spec = Mock(return_value=platform_spec())
    monkeypatch.setattr(refresh, "read_spec", read_spec)

    assert refresh.refresh(tmp_path) == len(refresh.TARGETS)

    read_spec.assert_called_once_with(base_url.rstrip("/") + "/swagger-json")


@pytest.mark.parametrize("explicit_source", [None, "designs/platform-openapi.json"])
def test_schema_source_overrides_instance(
    tmp_path, monkeypatch, schema_source_env, explicit_source
):
    monkeypatch.setenv("POLY_API_BASE_URL", "https://na2.polyapi.io")
    configured_source = "https://schemas.example.invalid/platform.json"
    monkeypatch.setenv("POLYAPI_OPENAPI_SPEC_URL", configured_source)
    read_spec = Mock(return_value=platform_spec())
    monkeypatch.setattr(refresh, "read_spec", read_spec)

    refresh.refresh(tmp_path, explicit_source)

    read_spec.assert_called_once_with(explicit_source or configured_source)


@pytest.mark.parametrize("base_url", [None, "", "  "])
def test_refresh_requires_a_source_before_reads_or_writes(
    tmp_path, monkeypatch, schema_source_env, base_url
):
    if base_url is not None:
        monkeypatch.setenv("POLY_API_BASE_URL", base_url)
    read_spec = Mock()
    monkeypatch.setattr(refresh, "read_spec", read_spec)
    existing = tmp_path / "cli/schemas/CreateSchemaDto.schema.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("previous schema")

    with pytest.raises(refresh.SchemaRefreshError, match="Set POLY_API_BASE_URL"):
        refresh.refresh(tmp_path)

    read_spec.assert_not_called()
    assert existing.read_text() == "previous schema"
    assert list(existing.parent.iterdir()) == [existing]


@pytest.mark.parametrize(
    "base_url",
    [
        "na1.polyapi.io",
        "https:///missing-host",
        "https://poly.example.invalid?query=value",
        "https://poly.example.invalid#fragment",
        "https://user:placeholder@poly.example.invalid",
    ],
)
def test_refresh_rejects_invalid_instance_urls(
    tmp_path, monkeypatch, schema_source_env, base_url
):
    monkeypatch.setenv("POLY_API_BASE_URL", base_url)
    read_spec = Mock()
    monkeypatch.setattr(refresh, "read_spec", read_spec)

    with pytest.raises(refresh.SchemaRefreshError, match="HTTP\\(S\\) instance URL"):
        refresh.refresh(tmp_path)

    read_spec.assert_not_called()
    assert not (tmp_path / "cli/schemas").exists()


def test_refresh_generates_validators_and_normalizes_patterns(tmp_path):
    source = tmp_path / "platform.json"
    source.write_text(json.dumps(platform_spec()))
    assert refresh.refresh(tmp_path, str(source)) == len(refresh.TARGETS)
    schema = json.loads((tmp_path / "cli/schemas/CreateJobDto.schema.json").read_text())
    validator = Draft7Validator(schema)
    assert validator.is_valid({"name": "valid"})
    assert not validator.is_valid({"name": 123})
    assert not validator.is_valid({})
    assert len(schema["components"]["schemas"]) == 1


def test_failed_refresh_leaves_previous_validators_unchanged(tmp_path):
    source = tmp_path / "platform.json"
    spec = platform_spec()
    spec["paths"].pop("/jobs")
    source.write_text(json.dumps(spec))
    existing = tmp_path / "cli/schemas/CreateSchemaDto.schema.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("previous schema")
    with pytest.raises(refresh.SchemaRefreshError, match="Missing request schema"):
        refresh.refresh(tmp_path, str(source))
    assert existing.read_text() == "previous schema"
    assert list(existing.parent.iterdir()) == [existing]


def test_recursive_refs_nullable_and_missing_refs():
    spec = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "nullable": True},
                        "child": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        }
    }
    schema = refresh.schema_document({"$ref": "#/components/schemas/Node"}, spec)
    assert Draft7Validator(schema).is_valid({"name": None, "child": {"name": "leaf"}})
    with pytest.raises(refresh.SchemaRefreshError, match="Unresolved"):
        refresh.schema_document({"$ref": "#/components/schemas/Missing"}, spec)
    with pytest.raises(refresh.SchemaRefreshError, match="internal"):
        refresh.schema_document({"$ref": "https://example.invalid/schema"}, spec)


def test_schema_quirks_match_ts_contract():
    spec = platform_spec()
    schemas = spec["components"]["schemas"]
    for name in ("CreateWebhookHandleDto", "UpdateWebhookHandleDto"):
        schemas[name]["required"].append("templateBody")
    schemas["CreateVariableDto"]["properties"]["value"] = {"type": "object"}
    schemas["CreateTableDto"]["properties"]["column"] = {
        "$ref": "#/components/schemas/TableColumnDto"
    }
    schemas["TableColumnDto"] = {
        "type": "object",
        "properties": {"default": {"type": "object"}},
    }
    for name in ("CreateClientCustomFunctionDto", "CreateServerCustomFunctionDto"):
        schemas[name]["required"].extend(
            ["sourceCode", "internalDependencies", "externalDependencies"]
        )
    result = refresh.endpoint_schemas(spec)
    assert Draft7Validator(result["UpdateWebhookHandleDto.schema.json"]).is_valid(
        {"name": "hook"}
    )
    assert Draft7Validator(result["CreateVariableDto.schema.json"]).is_valid(
        {"name": "setting", "value": "scalar"}
    )
    assert Draft7Validator(result["CreateTableDto.schema.json"]).is_valid(
        {"name": "table", "column": {"default": "now()"}}
    )
    assert Draft7Validator(result["CreateAiFunctionV2Dto.schema.json"]).is_valid(
        {"name": "agent", "type": "TEXT", "outputSchema": None}
    )
    assert Draft7Validator(
        result["CreateServerCustomFunctionDto.schema.json"]
    ).is_valid({"name": "handler"})
    assert not Draft7Validator(
        result["CreateServerCustomFunctionDto.schema.json"]
    ).is_valid({})


def test_local_design_schemas_and_ambiguous_source(tmp_path):
    source = tmp_path / "platform.json"
    source.write_text(json.dumps(platform_spec()))
    designs = tmp_path / "designs"
    designs.mkdir()
    design = {
        "openapi": "3.0.3",
        "components": {"schemas": {"Input": {"type": "integer"}}},
    }
    (designs / "api.json").write_text(json.dumps(design))
    refresh.refresh(tmp_path, str(source), designs)
    generated = json.loads(
        (tmp_path / "cli/generated/openapi-schemas.json").read_text()
    )
    assert Draft7Validator(generated["Input"]).is_valid(4)
    assert not Draft7Validator(generated["Input"]).is_valid("4")
    (designs / "other.json").write_text(json.dumps(design))
    with pytest.raises(refresh.SchemaRefreshError, match="Multiple"):
        refresh.refresh(tmp_path, str(source), designs)


def test_schema_download_has_no_ambient_credentials_or_redirects(monkeypatch):
    session = Mock()
    session.get.return_value = Mock(status_code=200, text=json.dumps(platform_spec()))
    context = Mock()
    context.__enter__ = Mock(return_value=session)
    context.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(refresh.requests, "Session", lambda: context)
    assert (
        refresh.read_spec("https://example.invalid/swagger-json")["openapi"] == "3.0.3"
    )
    assert session.trust_env is False
    session.get.assert_called_once_with(
        "https://example.invalid/swagger-json", timeout=30, allow_redirects=False
    )


def test_missing_optional_ai_endpoint_retains_existing_schema(tmp_path):
    spec = copy.deepcopy(platform_spec())
    spec["paths"].pop("/functions/ai")
    spec["paths"].pop("/functions/ai/{id}")
    source = tmp_path / "platform.json"
    source.write_text(json.dumps(spec))
    existing = tmp_path / "cli/schemas/CreateAiFunctionV2Dto.schema.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing validator")
    refresh.refresh(tmp_path, str(source))
    assert existing.read_text() == "existing validator"


def test_invalid_schema_never_replaces_previous_files(tmp_path):
    spec = platform_spec()
    spec["components"]["schemas"]["CreateJobDto"]["type"] = "not-a-json-type"
    source = tmp_path / "platform.json"
    source.write_text(json.dumps(spec))
    with pytest.raises(refresh.SchemaRefreshError, match="invalid JSON schema"):
        refresh.refresh(tmp_path, str(source))
    assert not (tmp_path / "cli/schemas").exists()


def test_standalone_schema_refresh_preserves_requests_tls_context():
    # A fresh process catches the SDK truststore hook running after requests
    # has cached its SSL context. No network or target credentials are needed.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cli import schema_refresh; from cli import flow; "
            "import requests, ssl; "
            "context = requests.adapters._preloaded_ssl_context; "
            "context.verify_mode = ssl.CERT_REQUIRED; "
            "assert context.verify_mode == ssl.CERT_REQUIRED; "
            "assert context.check_hostname",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_runtime_tests_preserve_requests_tls_context():
    context = requests.adapters._preloaded_ssl_context
    context.verify_mode = ssl.CERT_REQUIRED
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
