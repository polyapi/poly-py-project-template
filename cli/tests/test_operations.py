import copy
import json
from unittest.mock import Mock

import pytest

from cli import flow, operations


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    monkeypatch.delenv("POLYAPI_NAMESPACES", raising=False)
    return flow.build_config(
        tmp_path, flow.CliArgs(False, False, False, False, (), None, False)
    )


def api_model():
    return {
        "functions": [
            {
                "name": "getRandomJoke",
                "context": "example.demo",
                "description": "Get a joke",
                "arguments": [{"name": "hostUrl", "type": "string", "required": True}],
                "source": {
                    "auth": {"type": "noauth"},
                    "body": {"mode": "empty"},
                    "url": "{{hostUrl}}/random_joke",
                    "method": "GET",
                },
            }
        ],
        "schemas": [],
        "webhooks": [],
    }


@pytest.mark.parametrize("disable_ai", [True, False])
@pytest.mark.parametrize("literal", [True, False])
@pytest.mark.parametrize("generate_only", [True, False])
def test_training_restores_required_api_key_before_saving_or_upserting(
    config, disable_ai, literal, generate_only
):
    from cli.tests.test_openapi_security import model, specification

    translated = model()
    function = translated["functions"][0]
    if literal:
        function["source"]["auth"] = {"type": "noauth"}
        function["source"]["headers"] = [
            {"key": "Authorization", "value": "JWT {{apiKey}}"}
        ]
    writes = []

    def request(method, endpoint, **kwargs):
        if endpoint == "/specification-input/oas":
            return copy.deepcopy(translated)
        if endpoint == "/functions/api/description-generation":
            return {
                key: copy.deepcopy(function[key])
                for key in ("name", "description", "arguments")
            }
        assert method == "PUT" and endpoint == "/functions/api"
        writes.append(kwargs["payload"])

    client = Mock()
    client.request.side_effect = request
    path = config.repo_root / "secured.json"
    operations.train_openapi(
        client,
        [(path, "example.demo", json.dumps(specification()))],
        config,
        dry_run=False,
        generate_only=generate_only,
        overwrite_model=False,
        disable_ai=disable_ai,
    )
    saved = json.loads(path.with_suffix(".poly.json").read_text())
    assert writes == ([] if generate_only else saved["functions"])
    restored = saved["functions"][0]
    assert {header["key"] for header in restored["source"]["headers"]} >= {"x-api-key"}
    assert (
        next(arg for arg in restored["arguments"] if arg["name"] == "xApiKey")[
            "required"
        ]
        is True
    )
    assert restored["source"]["auth"] == function["source"]["auth"]


def test_ambiguous_security_stops_before_model_save_and_training(config):
    from cli.tests.test_openapi_security import model, specification

    spec = specification()
    spec["paths"]["/members/{other}/"] = {"get": {"security": []}}
    client = Mock()
    client.request.return_value = model()
    path = config.repo_root / "ambiguous.json"
    with pytest.raises(flow.DeployError, match="Ambiguous"):
        operations.train_openapi(
            client,
            [(path, "example.demo", json.dumps(spec))],
            config,
            dry_run=False,
            generate_only=False,
            overwrite_model=False,
            disable_ai=True,
        )
    assert client.request.call_count == 1
    assert not path.with_suffix(".poly.json").exists()


def test_default_training_enriches_names_descriptions_and_arguments(config):
    path = config.repo_root / "api.json"
    original = api_model()
    enriched = {
        "name": "randomJoke",
        "context": "outside.ignored",
        "description": "Retrieve one random joke",
        "arguments": [
            {
                "name": "hostUrl",
                "type": "string",
                "required": True,
                "description": "Service URL",
            }
        ],
    }
    client = Mock()
    client.request.side_effect = [original, enriched, {"id": "created"}]
    operations.train_openapi(
        client,
        [(path, "example.demo", "spec")],
        config,
        dry_run=False,
        generate_only=False,
        overwrite_model=False,
    )
    calls = client.request.call_args_list
    assert calls[1].args == ("POST", "/functions/api/description-generation")
    assert calls[1].kwargs["payload"]["source"] == original["functions"][0]["source"]
    saved = json.loads(path.with_suffix(".poly.json").read_text())
    assert saved["functions"][0]["name"] == "randomJoke"
    assert saved["functions"][0]["context"] == "example.demo"
    assert saved["functions"][0]["arguments"] == enriched["arguments"]
    assert calls[2].kwargs["payload"] == saved["functions"][0]


def test_enrichment_failure_retains_metadata_and_never_prints_provider_body(
    config, capsys
):
    client = Mock()
    client.request.side_effect = flow.DeployError("sensitive provider response")
    result = operations.enrich_model(client, api_model(), config, "example.demo")
    assert result == api_model()
    output = capsys.readouterr().err
    assert "retaining translated metadata" in output
    assert "sensitive" not in output


def test_webhook_enrichment_preserves_payload_contract(config):
    model = {
        "webhooks": [
            {
                "name": "event",
                "context": "example.demo",
                "description": "Event",
                "eventPayloadTypeSchema": {"type": "object"},
            }
        ]
    }
    client = Mock()
    client.request.return_value = {
        "name": "onEvent",
        "description": "Receive an event",
        "context": "foreign",
    }
    result = operations.enrich_model(client, model, config, "example.demo")
    assert client.request.call_args.args == ("POST", "/webhooks/description-generation")
    assert client.request.call_args.kwargs["payload"]["eventPayload"] == {
        "type": "object"
    }
    assert result["webhooks"][0]["name"] == "onEvent"
    assert result["webhooks"][0]["eventPayloadTypeSchema"] == {"type": "object"}


def test_enrichment_collision_fails_before_training(config):
    model = api_model()
    model["functions"].append({**model["functions"][0], "name": "second"})
    client = Mock()
    client.request.side_effect = [
        model,
        {"name": "same", "description": "one", "arguments": []},
        {"name": "same", "description": "two", "arguments": []},
    ]
    with pytest.raises(flow.DeployError, match="Duplicate"):
        operations.train_openapi(
            client,
            [(config.repo_root / "api.json", "example.demo", "spec")],
            config,
            dry_run=False,
            generate_only=False,
            overwrite_model=False,
        )
    assert all(call.args[0] == "POST" for call in client.request.call_args_list)
    assert not list(config.repo_root.glob("*.poly.json"))


def test_explicit_rename_preserves_longer_names_and_updates_references():
    assert operations.rename_model(
        {"name": "foo", "url": "{{foo}}/foobar"}, [["foo", "bar"]]
    ) == {"name": "bar", "url": "{{bar}}/foobar"}


def test_missing_operation_names_can_be_generated(config):
    model = api_model()
    model["functions"][0].pop("name")
    model["functions"].append(copy.deepcopy(model["functions"][0]))
    client = Mock()
    client.request.side_effect = [
        {"name": "first", "description": "one", "arguments": []},
        {"name": "second", "description": "two", "arguments": []},
    ]
    result = operations.enrich_model(client, model, config, "example.demo")
    assert [item["name"] for item in result["functions"]] == ["first", "second"]


def test_log_formatting_matches_ts_envelopes_and_supports_raw():
    response = {
        "response": {
            "logs": [
                {
                    "timestamp": "2026-10-20",
                    "level": "info",
                    "message": "ready",
                    "requestId": "test",
                },
                "plain line",
            ],
            "page": 2,
        }
    }
    formatted = operations.format_logs(response)
    assert "Logs (2)" in formatted
    assert "2026-10-20 | INFO | ready" in formatted
    assert '"requestId": "test"' in formatted
    assert "plain line" in formatted
    assert json.loads(operations.format_logs(response, raw=True)) == response
    assert operations.format_logs({"logs": []}) == "No logs found."
    args = operations.create_parser().parse_args(
        ["logs", "id", "--logs-page", "2", "--logs-page-size", "25", "--raw"]
    )
    assert args.page == 2 and args.page_size == 25 and args.raw


def test_optional_prompts_require_a_terminal(monkeypatch):
    monkeypatch.setattr(operations.sys.stdin, "isatty", lambda: False)
    with pytest.raises(flow.DeployError, match="required in non-interactive"):
        operations.prompt_value(None, "Context")
    assert operations.prompt_value("explicit", "Context") == "explicit"


def test_interactive_training_prompts_for_source_and_context(config, monkeypatch):
    source = config.repo_root / "spec.json"
    source.write_text('{"openapi":"3.0.3","paths":{"/ping":{}}}')
    monkeypatch.setattr(operations.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(operations.sys.stdout, "isatty", lambda: True)
    answers = iter([str(source), "example.demo"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(flow, "find_repo_root", lambda: config.repo_root)
    monkeypatch.setattr(flow, "load_env", lambda _: object())
    monkeypatch.setattr(flow, "build_config", lambda *_: config)
    assert operations.main(["train-openapi", "--dry-run"]) == 0
    assert not list(config.repo_root.glob("*.poly.json"))


def test_training_translates_with_host_argument_then_uses_exact_upsert(config):
    path = config.repo_root / "joke.json"
    text = '{"openapi":"3.0.3","paths":{"/random_joke":{"get":{"operationId":"getRandomJoke"}}}}'
    path.write_text(text)
    client = Mock()
    client.request.return_value = api_model()
    specs = operations.load_specs(path, "example.demo", "example")
    operations.train_openapi(
        client,
        specs,
        config,
        dry_run=False,
        generate_only=False,
        overwrite_model=False,
        disable_ai=True,
    )
    calls = client.request.call_args_list
    assert calls[0].args == ("POST", "/specification-input/oas")
    assert calls[0].kwargs == {
        "params": {"context": "example.demo", "hostUrlAsArgument": "hostUrl"},
        "raw_body": json.dumps(json.loads(text)),
        "headers": {"Content-Type": "text/plain"},
    }
    assert calls[1].args == ("PUT", "/functions/api")
    assert calls[1].kwargs["payload"] == api_model()["functions"][0]
    assert json.loads(path.with_suffix(".poly.json").read_text()) == api_model()


def test_training_dry_run_has_no_remote_or_local_effects(config):
    path = config.repo_root / "api.yaml"
    path.write_text("openapi: 3.0.3\npaths:\n  /ping: {}\n")
    specs = operations.load_specs(path, "example.{file}", "example")
    assert specs[0][1] == "example.api"
    client = Mock()
    operations.train_openapi(
        client,
        specs,
        config,
        dry_run=True,
        generate_only=False,
        overwrite_model=False,
        disable_ai=True,
    )
    client.request.assert_not_called()
    assert not path.with_suffix(".poly.json").exists()


def test_generation_only_saves_reviewable_model_without_resource_writes(config):
    client = Mock()
    client.request.return_value = api_model()
    path = config.repo_root / "joke.json"
    operations.train_openapi(
        client,
        [(path, "example.demo", "source")],
        config,
        dry_run=False,
        generate_only=True,
        overwrite_model=False,
        disable_ai=True,
    )
    client.request.assert_called_once()
    assert path.with_suffix(".poly.json").is_file()
    with pytest.raises(flow.DeployError, match="already exists"):
        operations.train_openapi(
            client,
            [(path, "example.demo", "source")],
            config,
            dry_run=False,
            generate_only=True,
            overwrite_model=False,
            disable_ai=True,
        )
    assert client.request.call_count == 1


def test_invalid_translation_stops_before_any_training(config):
    model = api_model()
    model["functions"][0]["context"] = "foreign.demo"
    client = Mock()
    client.request.return_value = model
    with pytest.raises(flow.DeployError, match="namespace|NAMESPACE"):
        operations.train_openapi(
            client,
            [(config.repo_root / "api.json", "example.demo", "source")],
            config,
            dry_run=False,
            generate_only=False,
            overwrite_model=False,
            disable_ai=True,
        )
    assert client.request.call_count == 1
    assert not list(config.repo_root.glob("*.poly.json"))


def test_schema_dependencies_precede_functions_and_recursive_contracts_are_preserved(
    config,
):
    model = api_model()
    model["schemas"] = [
        {
            "name": "Parent",
            "context": "example.demo",
            "definition": {
                "type": "object",
                "properties": {"child": {"x-poly-ref": {"path": "example.demo.Child"}}},
            },
        },
        {"name": "Child", "context": "example.demo", "definition": {"type": "string"}},
    ]
    validated = operations.validate_model(model, config)
    client = Mock()
    operations.train_model(client, validated, dry_run=False)
    assert [
        (call.args[1], call.kwargs["payload"]["name"])
        for call in client.request.call_args_list
    ] == [
        ("/schemas", "Child"),
        ("/schemas", "Parent"),
        ("/functions/api", "getRandomJoke"),
    ]
    model["schemas"][1]["definition"] = {"x-poly-ref": {"path": "example.demo.Parent"}}
    recursive = operations.validate_model(model, config)
    assert {item["name"] for item in recursive["schemas"]} == {"Parent", "Child"}
    assert recursive["schemas"][0]["definition"] == model["schemas"][1]["definition"]


def test_cross_file_duplicate_model_identities_fail_before_upserts(config):
    client = Mock()
    client.request.return_value = api_model()
    specs = [
        (config.repo_root / f"{index}.json", "example.demo", "source")
        for index in range(2)
    ]
    with pytest.raises(flow.DeployError, match="Duplicate"):
        operations.train_openapi(
            client,
            specs,
            config,
            dry_run=False,
            generate_only=False,
            overwrite_model=False,
            disable_ai=True,
        )
    assert all(call.args[0] == "POST" for call in client.request.call_args_list)


def test_bad_openapi_input_and_foreign_context_are_rejected(config):
    path = config.repo_root / "api.json"
    path.write_text('{"openapi":"3.0.3","paths":{}}')
    with pytest.raises(flow.DeployError, match="no paths"):
        operations.load_specs(path, "example.demo", "example")
    path.write_text('{"swagger":"2.0","paths":{"/ping":{}}}')
    with pytest.raises(flow.DeployError, match="context"):
        operations.load_specs(path, "foreign.demo", "example")


def test_delete_plan_is_exact_context_and_requires_all_references_owned(config):
    data = {
        "/functions/server": [
            {"id": "owned", "context": "example.demo"},
            {"id": "foreign", "context": "other.demo"},
        ],
        "/variables": [
            {"id": "one", "context": "example.demo"},
            {"id": "child", "context": "example.demo.child"},
            {"id": "foreign", "context": "example.demonstration"},
        ],
        "/jobs": [{"id": "good"}, {"id": "mixed"}, {"id": "unknown"}],
        "/jobs/good": {"functions": [{"id": "owned"}]},
        "/jobs/mixed": {"functions": [{"id": "owned"}, {"id": "foreign"}]},
        "/jobs/unknown": {"functions": [{"id": "owned"}, {"id": "unresolved"}]},
    }
    client = Mock()
    client.request.side_effect = lambda method, path, **kwargs: copy.deepcopy(
        data[path]
    )
    plan = operations.deletion_plan(
        client, ["variables", "jobs"], "example.demo", include_children=False
    )
    assert [endpoint for endpoint, _ in plan] == ["/jobs/good", "/variables/one"]
    assert all(call.args[0] == "GET" for call in client.request.call_args_list)
    plan = operations.deletion_plan(
        client, ["variables"], "example.demo", include_children=True
    )
    assert {path for path, _ in plan} == {"/variables/one", "/variables/child"}


@pytest.mark.parametrize(
    "apply,answer", [(False, "DELETE"), (True, "delete"), (True, "no")]
)
def test_delete_preview_and_rejected_confirmation_never_write(
    monkeypatch, apply, answer
):
    monkeypatch.setattr(operations.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(operations.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    client = Mock()
    plan = [("/variables/id", {"name": "value", "context": "example.demo"})]
    if apply:
        with pytest.raises(flow.DeployError, match="cancelled"):
            operations.delete_content(client, plan, apply=apply)
    else:
        operations.delete_content(client, plan, apply=apply)
    client.request.assert_not_called()


def test_delete_requires_readback_and_stops_if_resource_remains(monkeypatch):
    monkeypatch.setattr(operations.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(operations.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "DELETE")
    plan = [("/variables/one", {"name": "one"}), ("/variables/two", {"name": "two"})]
    client = Mock()
    client.request.side_effect = [None, {"id": "one"}]
    with pytest.raises(flow.DeployError, match="could not be verified"):
        operations.delete_content(client, plan, apply=True)
    assert len(client.request.call_args_list) == 2
    client.reset_mock(side_effect=True)
    client.request.side_effect = [
        None,
        flow.ApiError("GET", "/variables/one", 404),
        None,
        flow.ApiError("GET", "/variables/two", 404),
    ]
    operations.delete_content(client, plan, apply=True)
    assert [call.args[0] for call in client.request.call_args_list] == [
        "DELETE",
        "GET",
        "DELETE",
        "GET",
    ]


def test_logs_encode_id_and_preserve_query_parameters():
    client = Mock()
    operations.function_logs(
        client, "id/segment", page=2, page_size=30, keyword="failure", limit=20
    )
    client.request.assert_called_once_with(
        "GET",
        "/functions/server/id%2Fsegment/logs",
        params={"logsPage": 2, "logsPageSize": 30, "keyword": "failure", "limit": 20},
    )


def test_raw_transport_applies_otp_and_refuses_redirects():
    client = flow.PolyApiClient(
        "https://example.invalid", "synthetic-key", otp_manager=Mock()
    )
    client.otp_manager.delete_headers.return_value = {"x-otp": "synthetic-otp"}
    client.session = Mock()
    client.session.request.return_value = Mock(status_code=204, content=b"")
    client.request(
        "POST",
        "/specification-input/oas",
        raw_body="specification",
        headers={"Content-Type": "text/plain"},
    )
    options = client.session.request.call_args.kwargs
    assert options["data"] == "specification" and "json" not in options
    assert options["headers"] == {
        "x-otp": "synthetic-otp",
        "Content-Type": "text/plain",
    }
    assert options["allow_redirects"] is False


@pytest.mark.parametrize(
    "argv",
    [
        ["train-openapi", "input.json", "--context", "example.demo"],
        [
            "delete-content",
            "--context",
            "example.demo",
            "--types",
            "variables",
            "--apply",
        ],
        ["logs", "function-id"],
    ],
)
def test_every_remote_command_verifies_target_before_resource_requests(
    config, monkeypatch, argv
):
    monkeypatch.setattr(flow, "find_repo_root", lambda: config.repo_root)
    monkeypatch.setattr(flow, "load_env", lambda *_: object())
    monkeypatch.setattr(flow, "build_config", lambda *_: config)
    monkeypatch.setattr(flow, "get_target_env_id", lambda *_: "selected-target")
    monkeypatch.setattr(flow, "build_deployment_preflight", lambda *_: object())
    monkeypatch.setattr(flow, "print_deployment_visibility", lambda *_: None)
    monkeypatch.setattr(flow, "maybe_confirm_branch_env_file_match", lambda *_: None)
    safeguards = Mock()
    monkeypatch.setattr(flow, "enforce_production_safeguards", safeguards)
    monkeypatch.setattr(flow, "get_execution_api_key", lambda: None)
    monkeypatch.setattr(
        flow,
        "get_api_key_and_url",
        lambda: ("synthetic-key", "https://example.invalid"),
    )
    monkeypatch.setattr(operations, "load_specs", lambda *_: [])
    client = Mock()
    monkeypatch.setattr(flow, "PolyApiClient", lambda *args, **kwargs: client)
    verify = Mock(side_effect=flow.DeployError("Target mismatch"))
    monkeypatch.setattr(flow, "enforce_target_environment", verify)
    with pytest.raises(flow.DeployError, match="Target mismatch"):
        operations.main(argv)
    safeguards.assert_called_once()
    verify.assert_called_once()
    client.request.assert_not_called()


def test_same_stem_specs_cannot_overwrite_each_others_models(config):
    specs = [
        (config.repo_root / f"api.{extension}", "example.demo", "source")
        for extension in ("json", "yaml")
    ]
    client = Mock()
    with pytest.raises(flow.DeployError, match="same model file"):
        operations.train_openapi(
            client,
            specs,
            config,
            dry_run=False,
            generate_only=False,
            overwrite_model=False,
            disable_ai=True,
        )
    client.request.assert_not_called()


def schema_training_model(graph):
    model = api_model()
    target = "Node" if graph == "self" else "Child"
    model["schemas"] = [
        {
            "name": "Node",
            "context": "example.demo",
            "definition": {
                "type": "object",
                "properties": {
                    "child": {"x-poly-ref": {"path": "example.demo." + target}}
                },
            },
        }
    ]
    if graph != "self":
        definition = (
            {"x-poly-ref": {"path": "example.demo.Node"}}
            if graph == "mutual"
            else {"type": "string"}
        )
        model["schemas"].append(
            {"name": "Child", "context": "example.demo", "definition": definition}
        )
    return model


@pytest.fixture
def training_cli(config, monkeypatch):
    monkeypatch.setattr(flow, "find_repo_root", lambda: config.repo_root)
    monkeypatch.setattr(flow, "load_env", lambda *_: object())
    monkeypatch.setattr(flow, "build_config", lambda *_: config)
    monkeypatch.setattr(flow, "get_target_env_id", lambda *_: "synthetic-target")
    monkeypatch.setattr(flow, "build_deployment_preflight", lambda *_: object())
    for name in (
        "print_deployment_visibility",
        "maybe_confirm_branch_env_file_match",
        "enforce_production_safeguards",
        "enforce_target_environment",
    ):
        monkeypatch.setattr(flow, name, Mock())
    monkeypatch.setattr(flow, "get_execution_api_key", lambda: None)
    monkeypatch.setattr(
        flow,
        "get_api_key_and_url",
        lambda: ("synthetic-key", "https://example.invalid"),
    )
    client, generate = Mock(), Mock()
    monkeypatch.setattr(flow, "PolyApiClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(flow, "run_poly_generate", generate)
    return client, generate


@pytest.mark.parametrize("command", ["train-openapi", "train-model"])
@pytest.mark.parametrize("graph", ["self", "mutual", "acyclic"])
@pytest.mark.parametrize("skip_generate", [False, True])
def test_training_cli_blocks_cycles_before_upserts_unless_generation_skipped(
    config, training_cli, command, graph, skip_generate
):
    client, generate = training_cli
    model = schema_training_model(graph)
    source = config.repo_root / "api.json"
    if command == "train-model":
        source.write_text(json.dumps(model))
    else:
        source.write_text(json.dumps({"openapi": "3.0.3", "paths": {"/node": {}}}))
    client.request.side_effect = lambda method, *args, **kwargs: (
        copy.deepcopy(model) if method == "POST" else {"id": "synthetic"}
    )
    argv = [command, str(source)]
    if command == "train-openapi":
        argv += ["--context", "example.demo", "--disable-ai"]
    if skip_generate:
        argv.append("--skip-generate")
    if graph != "acyclic" and not skip_generate:
        with pytest.raises(flow.DeployError, match="--skip-generate"):
            operations.main(argv)
        assert all(call.args[0] == "POST" for call in client.request.call_args_list)
        generate.assert_not_called()
    else:
        assert operations.main(argv) == 0
        upserts = [
            call for call in client.request.call_args_list if call.args[0] == "PUT"
        ]
        assert len(upserts) == len(model["schemas"]) + len(model["functions"])
        definitions = {item["name"]: item["definition"] for item in model["schemas"]}
        assert {
            call.kwargs["payload"]["name"]: call.kwargs["payload"]["definition"]
            for call in upserts
            if call.args[1] == "/schemas"
        } == definitions
        assert generate.call_count == (0 if skip_generate else 1)


@pytest.mark.parametrize("skip_generate", [False, True])
def test_saved_recursive_model_preview_is_offline_and_requires_explicit_opt_in(
    config, training_cli, monkeypatch, skip_generate
):
    client, generate = training_cli
    source = config.repo_root / "recursive.poly.json"
    source.write_text(json.dumps(schema_training_model("self")))
    monkeypatch.setattr(
        flow,
        "get_api_key_and_url",
        Mock(side_effect=AssertionError("offline preview must not load credentials")),
    )
    argv = ["train-model", str(source), "--dry-run"]
    if skip_generate:
        assert operations.main([*argv, "--skip-generate"]) == 0
    else:
        with pytest.raises(flow.DeployError, match="--skip-generate"):
            operations.main(argv)
    client.request.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize("skip_generate", [False, True])
def test_cross_file_schema_cycle_is_checked_before_any_upsert(config, skip_generate):
    model = schema_training_model("mutual")
    models = [{"schemas": [item]} for item in model["schemas"]]
    client = Mock()
    remaining = iter(models)
    client.request.side_effect = lambda method, *args, **kwargs: (
        copy.deepcopy(next(remaining)) if method == "POST" else {"id": "synthetic"}
    )
    specs = [
        (config.repo_root / f"api{n}.json", "example.demo", "spec") for n in range(2)
    ]
    options = {"skip_generate": True} if skip_generate else {}
    if skip_generate:
        operations.train_openapi(
            client,
            specs,
            config,
            dry_run=False,
            generate_only=False,
            overwrite_model=False,
            disable_ai=True,
            **options,
        )
        assert [call.args[0] for call in client.request.call_args_list] == [
            "POST",
            "POST",
            "PUT",
            "PUT",
        ]
    else:
        with pytest.raises(flow.DeployError, match="--skip-generate"):
            operations.train_openapi(
                client,
                specs,
                config,
                dry_run=False,
                generate_only=False,
                overwrite_model=False,
                disable_ai=True,
                **options,
            )
        assert [call.args[0] for call in client.request.call_args_list] == [
            "POST",
            "POST",
        ]


def test_generate_only_can_export_recursive_contract_without_training(
    config, training_cli
):
    client, generate = training_cli
    model = schema_training_model("self")
    source = config.repo_root / "api.json"
    source.write_text(json.dumps({"openapi": "3.0.3", "paths": {"/node": {}}}))
    client.request.return_value = model
    assert (
        operations.main(
            [
                "train-openapi",
                str(source),
                "--context",
                "example.demo",
                "--disable-ai",
                "--generate-only",
            ]
        )
        == 0
    )
    assert [call.args[0] for call in client.request.call_args_list] == ["POST"]
    assert (
        json.loads(source.with_suffix(".poly.json").read_text())["schemas"]
        == model["schemas"]
    )
    generate.assert_not_called()
