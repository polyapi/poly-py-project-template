from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import flow


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    monkeypatch.delenv("POLYAPI_NAMESPACES", raising=False)
    args = flow.CliArgs(True, False, False, False, (), ("variables",), False)
    return flow.build_config(tmp_path, args), args, flow.Logger(False)


def variable(config, name, data):
    path = config.repo_root / f"workflows/src/demo/artefacts/vari/{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_invalid_artifact_stops_before_transport(settings):
    config, args, logger = settings
    variable(config, "bad", {"name": 42, "context": "example.demo"})
    with pytest.raises(flow.DeployError, match="Local validation failed"):
        flow.validate_local_artifacts(config, args, logger)


@pytest.mark.parametrize(
    "invalid", ["{broken", {"name": 42, "context": "example.demo"}]
)
def test_skip_invalid_excludes_only_failed_paths_and_disables_orphans(
    settings, invalid
):
    config, args, logger = settings
    bad = variable(config, "bad", invalid)
    good = variable(
        config,
        "good",
        {
            "name": "config",
            "context": "example.demo",
            "value": {"functionName": "ordinary data"},
            "secrecy": "NONE",
        },
    )
    args = flow.validate_local_artifacts(
        config, replace(args, skip_invalid=True, delete_orphans=True), logger
    )
    assert args.invalid_paths == (bad,)
    assert [
        item.path
        for item in flow.load_artifact_files(config, args, config.variable_globs)
    ] == [good]
    client = Mock()
    assert (
        flow.process_orphans(
            client=client,
            config=config,
            cli_args=args,
            logger=logger,
            summary=flow.Summary(),
            function_registry=flow.FunctionRegistry(),
            webhook_index={},
            otp_manager=flow.OtpManager(False),
        )
        is False
    )
    assert (
        flow.extra_orphans(
            client,
            config,
            args,
            logger,
            flow.Summary(),
            "ai-functions",
            "/functions/ai",
            [],
            [],
        )
        is False
    )
    client.request.assert_not_called()


def test_all_template_inputs_pass_preflight(monkeypatch, sample_namespace):
    monkeypatch.setenv("POLYAPI_NAMESPACE", sample_namespace)
    monkeypatch.delenv("POLYAPI_NAMESPACES", raising=False)
    args = flow.CliArgs(True, False, False, False, (), None, False)
    config = flow.build_config(Path(__file__).resolve().parents[2], args)
    result = flow.validate_local_artifacts(config, args, flow.Logger(False))
    assert result.invalid_paths == ()
    flow.validate_local_namespace(config, result, flow.Logger(False))


def test_ts_flags_and_exact_confirmation_acknowledgement():
    args = flow.parse_args(
        [
            "push",
            "--build-schema",
            "--skip-invalid",
            "--skip-validation",
            "I know what am doing",
        ]
    )
    assert args.build_schema and args.skip_invalid and args.skip_validation_acknowledged
    with pytest.raises(flow.DeployError, match="exactly"):
        flow.parse_args(["push", "--skip-validation", "yes"])


def test_confirmation_bypass_never_bypasses_production_key(settings, monkeypatch):
    _, args, _ = settings
    args = replace(args, dry_run=False, skip_validation_acknowledged=True)
    preflight = Mock(production_sensitive=True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unexpected prompt"))
    with pytest.raises(flow.DeployError, match="POLY_EXEC_API_KEY"):
        flow.enforce_production_safeguards(preflight, args, None)
    flow.enforce_production_safeguards(preflight, args, "test-only-placeholder")


def test_acknowledgement_rejects_missing_placeholders_even_in_terminal(
    settings, monkeypatch
):
    config, args, logger = settings
    variable(
        config,
        "config",
        {
            "name": "config",
            "context": "example.demo",
            "value": "{{MISSING_PARITY_TEST_VALUE}}",
        },
    )
    monkeypatch.delenv("MISSING_PARITY_TEST_VALUE", raising=False)
    monkeypatch.setattr(flow.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(flow.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unexpected prompt"))
    with pytest.raises(flow.DeployError, match="Missing env-file values"):
        flow.maybe_prompt_for_missing_env_tokens(
            config, replace(args, skip_validation_acknowledged=True), logger
        )


def test_push_refreshes_then_validates_before_auth_or_sync(settings, monkeypatch):
    from cli import schema_refresh

    config, args, _ = settings
    args = replace(args, build_schema=True, skip_validation_acknowledged=True)
    monkeypatch.setattr(flow, "parse_args", lambda: args)
    monkeypatch.setattr(flow, "find_repo_root", lambda: config.repo_root)
    monkeypatch.setattr(flow, "load_env", lambda _: Mock(exists=True))
    monkeypatch.setattr(flow, "get_target_env_id", lambda _: "expected")
    monkeypatch.setattr(flow, "build_config", lambda *_: config)
    monkeypatch.setattr(
        flow,
        "build_deployment_preflight",
        lambda *_: Mock(production_sensitive=False, expected_env_branch=None),
    )
    monkeypatch.setattr(flow, "print_startup_banner", lambda **_: None)
    monkeypatch.setattr(flow, "print_deployment_visibility", lambda *_: None)
    events = []
    monkeypatch.setattr(schema_refresh, "refresh", lambda _: events.append("refresh"))

    def validate(*_):
        events.append("validate")
        raise flow.DeployError("Invalid local input")

    monkeypatch.setattr(flow, "validate_local_artifacts", validate)
    monkeypatch.setattr(
        flow,
        "get_api_key_and_url",
        lambda: pytest.fail("Auth was accessed before validation"),
    )
    with pytest.raises(flow.DeployError, match="Invalid local input"):
        flow.main()
    assert events == ["refresh", "validate"]


def test_python_sdk_argument_schemas_are_validated_without_mutating_payload():
    payload = {
        "name": "test",
        "context": "example.demo",
        "language": "python",
        "code": "def test(): pass",
        "arguments": [
            {
                "name": "arg",
                "key": "arg",
                "type": "object",
                "typeSchema": '{"type":"object"}',
            },
            {"name": "name", "key": "name", "type": "string", "typeSchema": None},
        ],
    }
    before = json.dumps(payload)
    flow.validate_endpoint_payload("CreateClientCustomFunctionDto", payload)
    assert json.dumps(payload) == before
    payload["arguments"][0]["typeSchema"] = "invalid JSON"
    with pytest.raises(flow.DeployError, match="valid JSON"):
        flow.validate_endpoint_payload("CreateClientCustomFunctionDto", payload)


@pytest.mark.parametrize(
    "source",
    [
        'polyConfig = {"name": "worker", "context": "example.demo"}\ndef worker(:\n',
        'polyConfig = {"context": "example.demo"}\n',
        'polyConfig = {"name": "worker"}\n',
        "polyConfig = {}\n",
        "polyConfig = []\n",
        "polyConfig = make_config()\n",
        "polyConfig: PolyServerFunction\n",
    ],
)
def test_invalid_function_declaration_cannot_become_an_orphan(settings, source):
    config, args, logger = settings
    config = replace(config, enabled_types=("functions",))
    args = replace(args, enabled_types=("functions",), delete_orphans=True)
    path = config.repo_root / "workflows/src/demo/server/worker_sfx.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)
    with pytest.raises(flow.DeployError, match="Local validation failed"):
        flow.validate_local_artifacts(config, args, logger)
    checked = flow.validate_local_artifacts(
        config, replace(args, skip_invalid=True), logger
    )
    assert checked.invalid_paths == (path,)
    assert flow.load_function_specs(config, checked, logger) == []
    client = Mock()
    assert not flow.process_orphans(
        client=client,
        config=config,
        cli_args=checked,
        logger=logger,
        summary=flow.Summary(),
        function_registry=flow.FunctionRegistry(),
        webhook_index={},
        otp_manager=flow.OtpManager(False),
    )
    client.request.assert_not_called()


@pytest.mark.parametrize(
    "source",
    [
        "def helper():\n    return {}\n",
        "# Helpers do not declare polyConfig.\ndef helper():\n    return {}\n",
    ],
)
def test_helpers_without_deployable_metadata_remain_ignored(tmp_path, source):
    path = tmp_path / "helper.py"
    path.write_text(source)
    assert flow.load_function_specs_from_python(path) == []


def test_maintained_functions_emit_native_sdk_boundary_contracts(
    monkeypatch, sample_namespace
):
    monkeypatch.setenv("POLYAPI_NAMESPACE", sample_namespace)
    monkeypatch.delenv("POLYAPI_NAMESPACES", raising=False)
    monkeypatch.setattr(flow, "get_execution_api_key", lambda: None)
    args = flow.parse_args(["push", "--types", "functions", "--dry-run"])
    config = flow.build_config(Path(__file__).resolve().parents[2], args)
    specs = flow.load_function_specs(config, args, flow.Logger(False))
    contexts = flow.build_local_func_context(specs)
    expected = {
        "motd_client": (["string"], "string", None),
        "motd_server": (
            ["object", "object", "object"],
            None,
            {"statusCode": "integer", "message": "string"},
        ),
        "motd_error_handler": (["object"], None, {"message": "string"}),
        "validate_motd_payload": (["object", "object", "object"], "boolean", None),
        "log": (["string", "string", "string", "object"], "boolean", None),
        "send_alert": (None, "boolean", None),
        "error_alerts": (["object"], None, {"notified": "boolean"}),
    }
    assert {spec.name for spec in specs} == set(expected)
    for spec in specs:
        payload = flow.build_function_payload(spec, contexts)
        argument_types, return_type, fields = expected[spec.name]
        assert payload["returnType"] != "any", spec.name
        assert all(arg["type"] != "any" for arg in payload["arguments"]), spec.name
        if argument_types:
            assert [
                arg["type"] for arg in payload["arguments"]
            ] == argument_types, spec.name
        if return_type:
            assert payload["returnType"] == return_type, spec.name
        if fields:
            schema = payload["returnTypeSchema"]
            assert {
                key: value["type"] for key, value in schema["properties"].items()
            } == fields
            assert set(schema["required"]) == set(fields)
        if spec.name == "send_alert":
            schema = json.loads(payload["arguments"][0]["typeSchema"])
            assert schema["properties"]["message"]["type"] == "string"
            assert schema["required"] == ["message"]
