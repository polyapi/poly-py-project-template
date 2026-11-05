import copy
from contextlib import closing
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest
import requests
from jsonschema import Draft7Validator

from cli import schema_refresh as refresh


@pytest.mark.parametrize("entry_key", ["key", "value"])
def test_schema_refresh_preserves_both_native_api_key_entry_kinds(entry_key):
    schema = {
        "type": "object",
        "components": {
            "schemas": {
                "KeyValueApiKeyEntrySource": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": ["key"]},
                        "value": {"type": "string"},
                    },
                },
            }
        },
    }
    refresh.patch_platform_quirks("CreateApiFunctionDto", schema)
    definition = schema["components"]["schemas"]["KeyValueApiKeyEntrySource"]
    Draft7Validator(definition).validate({"key": entry_key, "value": "{{apiKey}}"})
    assert not Draft7Validator(definition).is_valid({"key": "unknown", "value": "x"})


@pytest.mark.parametrize("invalid", ["location", "entry", "length", "value_type"])
def test_native_api_key_schema_still_rejects_invalid_auth(invalid):
    from cli import flow
    from cli.tests.test_openapi_security import model

    function = model()["functions"][0]
    entries = function["source"]["auth"]["apikey"]
    if invalid == "location":
        entries[0]["value"] = "cookie"
    elif invalid == "entry":
        entries[2]["key"] = "unknown"
    elif invalid == "length":
        entries.pop()
    else:
        entries[2]["value"] = 42
    with pytest.raises(flow.DeployError):
        flow.validate_endpoint_payload("CreateApiFunctionDto", function)


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
    from cli import environment

    snapshot = dict(os.environ)
    monkeypatch.setattr(environment, "get_saved_sdk_connection", lambda: (None, None))
    for name in (
        "BASE_URL",
        "POLY_API_BASE_URL",
        "POLYAPI_BASE_URL",
        "API_KEY",
        "POLY_API_KEY",
        "POLYAPI_API_KEY",
        "POLYAPI_OPENAPI_SPEC_URL",
        "POLYAPI_DESIGNS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


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


def test_refresh_uses_saved_setup_url_without_env_binding(
    tmp_path, monkeypatch, schema_source_env
):
    from cli import environment

    monkeypatch.setattr(
        environment,
        "get_saved_sdk_connection",
        lambda: ("placeholder-saved-key", "https://saved.invalid"),
    )
    read_spec = Mock(return_value=platform_spec())
    monkeypatch.setattr(refresh, "read_spec", read_spec)
    assert refresh.refresh(tmp_path) == len(refresh.TARGETS)
    read_spec.assert_called_once_with("https://saved.invalid/swagger-json")


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


def test_mcp_server_schemas_are_refreshed_with_any_hardcoded_value():
    spec = platform_spec()
    schemas = spec["components"]["schemas"]
    schemas["MCPToolFunctionArgumentConfigDto"] = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "source": {"type": "string"},
            "value": {"type": "object"},
        },
    }
    for name in ("CreateMCPServerDto", "UpdateMCPServerDto"):
        schemas[name]["properties"]["argument"] = {
            "$ref": "#/components/schemas/MCPToolFunctionArgumentConfigDto"
        }
    result = refresh.endpoint_schemas(spec)
    for name in ("CreateMCPServerDto", "UpdateMCPServerDto"):
        validator = Draft7Validator(result[f"{name}.schema.json"])
        for value in ("https://api.open-meteo.com", 3, True, {"units": "metric"}):
            argument = {"key": "host", "source": "hardcoded", "value": value}
            assert validator.is_valid({"name": "tools", "argument": argument})
        # AI function corrections do not leak into MCP validators.
        properties = result[f"{name}.schema.json"]["components"]["schemas"][name][
            "properties"
        ]
        assert "apiType" not in properties


def test_missing_mcp_endpoints_retain_existing_schemas(tmp_path):
    spec = copy.deepcopy(platform_spec())
    spec["paths"].pop("/mcp-servers")
    spec["paths"].pop("/mcp-servers/{id}")
    source = tmp_path / "platform.json"
    source.write_text(json.dumps(spec))
    existing = tmp_path / "cli/schemas/CreateMCPServerDto.schema.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing validator")
    refresh.refresh(tmp_path, str(source))
    assert existing.read_text() == "existing validator"


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


def assert_requests_tls_verification():
    class ContextVerified(Exception):
        pass

    def inspect_tls_context(*args, ssl_context, server_hostname, **kwargs):
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_context.check_hostname
        assert server_hostname == "poly.example.invalid"
        raise ContextVerified

    request = requests.Request("GET", "https://poly.example.invalid").prepare()
    with (
        patch("urllib3.connection.HTTPSConnection._new_conn", return_value=Mock()),
        patch("urllib3.connection.ssl_wrap_socket", side_effect=inspect_tls_context),
        closing(requests.adapters.HTTPAdapter()) as adapter,
        pytest.raises(ContextVerified),
    ):
        adapter.send(request, verify=True, proxies={}, timeout=1)


def test_standalone_schema_refresh_preserves_requests_tls_context():
    # Exercise Requests' real TLS setup after SDK initialization in a fresh
    # process, stopping before any network connection or TLS handshake.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cli import schema_refresh; "
            "from cli.tests.test_schema_refresh import assert_requests_tls_verification; "
            "assert_requests_tls_verification()",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_runtime_tests_preserve_requests_tls_context():
    assert_requests_tls_verification()
