import copy
import re
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import flow


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method != "GET":
            return {"id": "created"}
        if path not in self.responses:
            raise AssertionError(f"Unexpected GET {path}")
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    args = flow.CliArgs(False, False, False, False, (), None, False)
    return flow.build_config(tmp_path, args), args, flow.Logger(False), flow.Summary()


def artefact(config, kind, data, name="sample"):
    path = config.repo_root / "workflows/src/demo/artefacts" / kind / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def ai_function(**overrides):
    return {
        "name": "assistant",
        "context": "example.demo",
        "provider": "openai",
        "systemPrompt": "Be concise",
        "toolFunctions": [],
        **overrides,
    }


def subscription(**overrides):
    return {
        "name": "events",
        "context": "example.demo",
        "type": "CUSTOM",
        "websocketUrl": "wss://example.invalid/events",
        "query": "subscription { event }",
        "enabled": False,
        "destination": {"functionContext": "example.demo", "functionName": "handler"},
        **overrides,
    }


def subscription_detail(**overrides):
    return {
        "id": "old",
        "name": "events",
        "context": "example.demo",
        "type": "CUSTOM",
        "transportProtocol": "WS",
        "websocketUrl": "wss://example.invalid/events",
        "query": "subscription { event }",
        "functionId": "handler",
        "functionParams": None,
        "enabled": False,
        "description": None,
        "visibility": "ENVIRONMENT",
        "ohipMaintainOffset": False,
        "eventInactivityThresholdMs": None,
        "paramsObject": None,
        **overrides,
    }


def subscription_client(records=None):
    return FakeClient(
        {
            "/subscriptions/graphql": records or [],
            "/functions/server": [
                {"id": "handler", "name": "handler", "context": "example.demo"}
            ],
            "/functions/server/tenant": [],
            "/functions/server/public": [],
            "/variables": [
                {"id": "secret", "name": "token", "context": "example.demo"}
            ],
        }
    )


@pytest.mark.parametrize(
    "kind", ["ai-functions", "mcp-servers", "canopy", "subscriptions"]
)
def test_extra_types_are_exposed_in_push(kind):
    assert flow.parse_args(["push", "--types", kind]).enabled_types == (kind,)


@pytest.mark.parametrize(
    "namespace", ["", "example..nested", "example-example", "class"]
)
def test_namespace_is_required_and_valid(monkeypatch, namespace):
    monkeypatch.setenv("POLYAPI_NAMESPACE", namespace)
    with pytest.raises(flow.DeployError):
        flow.required_namespace()


def test_ai_resolves_external_tools_and_owned_variable(settings):
    config, *_ = settings
    data = ai_function(
        toolFunctions=[
            {
                "functionName": "lookup",
                "functionContext": "public.weather",
                "argumentConfig": [
                    {
                        "key": "key",
                        "source": "variable",
                        "variableName": "token",
                        "variableContext": "example.demo",
                    }
                ],
            }
        ]
    )
    result = flow.resolve_ai_payload(
        data,
        [{"name": "lookup", "context": "public.weather", "id": "tool"}],
        [{"name": "token", "context": "example.demo", "id": "secret"}],
        config,
    )
    assert result["toolFunctions"] == [
        {
            "id": "tool",
            "argumentConfig": [
                {"key": "key", "source": "variable", "variableId": "secret"}
            ],
        }
    ]
    assert "functionName" in data["toolFunctions"][0]


def test_duplicate_tool_identity_is_rejected(settings):
    config, *_ = settings
    with pytest.raises(flow.DeployError, match="2 matches"):
        flow.resolve_ai_payload(
            ai_function(
                toolFunctions=[
                    {"functionName": "tool", "functionContext": "public.shared"}
                ]
            ),
            [
                {"name": "tool", "context": "public.shared", "id": identifier}
                for identifier in ("one", "two")
            ],
            [],
            config,
        )


@pytest.mark.parametrize("dry_run", [False, True])
def test_ai_create_and_dry_run(settings, dry_run):
    config, args, logger, summary = settings
    data = ai_function()
    artefact(config, "ai-functions", data)
    client = FakeClient({"/functions/ai": [], "/variables": []})
    assert flow.sync_ai_functions(
        client, config, replace(args, dry_run=dry_run), logger, summary
    )
    assert client.writes == (
        [] if dry_run else [("POST", "/functions/ai", {"payload": data})]
    )
    assert summary.ai_functions.created == 1


def test_ai_patch_removes_tool_and_changes_only_mutable_fields(settings):
    config, args, logger, summary = settings
    artefact(config, "ai-functions", ai_function())
    client = FakeClient(
        {
            "/functions/ai": [
                {"id": "one", "name": "assistant", "context": "example.demo"}
            ],
            "/functions/ai/one": ai_function(toolFunctions=[{"id": "obsolete"}]),
            "/variables": [],
        }
    )
    flow.sync_ai_functions(client, config, args, logger, summary)
    assert client.writes == [
        ("PATCH", "/functions/ai/one", {"payload": {"toolFunctions": []}})
    ]


def test_hardcoded_values_are_compared_whole_but_platform_fields_are_ignored():
    remote = {
        "id": "search-id",
        "platformAdded": True,
        "argumentConfig": [
            {
                "key": "options",
                "source": "hardcoded",
                "value": {"units": "metric", "legacy": {"flag": True}},
            }
        ],
    }
    removed = {
        "id": "search-id",
        "argumentConfig": [
            {"key": "options", "source": "hardcoded", "value": {"units": "metric"}}
        ],
    }
    assert flow.project_remote(removed, remote) != removed
    nested = copy.deepcopy(removed)
    nested["argumentConfig"][0]["value"] = {"units": "metric", "legacy": {}}
    assert flow.project_remote(nested, remote) != nested
    same = copy.deepcopy(remote)
    del same["platformAdded"]
    assert flow.project_remote(same, remote) == same


def test_ai_patch_sends_a_hardcoded_value_with_a_removed_property(settings):
    config, args, logger, summary = settings
    tool = {
        "functionName": "searchLocations",
        "functionContext": "public.weather",
        "argumentConfig": [
            {"key": "options", "source": "hardcoded", "value": {"units": "metric"}}
        ],
    }
    artefact(config, "ai-functions", ai_function(toolFunctions=[tool]))
    deployed = {
        "id": "search-id",
        "argumentConfig": [
            {
                "key": "options",
                "source": "hardcoded",
                "value": {"units": "metric", "legacy": True},
            }
        ],
    }
    responses = {
        f"/functions/{kind}{suffix}": []
        for kind in ("api", "server", "client")
        for suffix in ("", "/tenant", "/public")
    }
    responses.update(
        {
            "/functions/ai": [
                {"id": "one", "name": "assistant", "context": "example.demo"}
            ],
            "/functions/ai/one": ai_function(toolFunctions=[deployed]),
            "/functions/api": MCP_FUNCTIONS,
            "/variables": [],
        }
    )
    client = FakeClient(responses)
    flow.sync_ai_functions(client, config, args, logger, summary)
    sent = {**deployed, "argumentConfig": tool["argumentConfig"]}
    assert client.writes == [
        ("PATCH", "/functions/ai/one", {"payload": {"toolFunctions": [sent]}})
    ]


def test_ai_immutable_change_does_not_write(settings):
    config, args, logger, summary = settings
    artefact(config, "ai-functions", ai_function())
    client = FakeClient(
        {
            "/functions/ai": [
                {"id": "one", "name": "assistant", "context": "example.demo"}
            ],
            "/functions/ai/one": ai_function(provider="other"),
            "/variables": [],
        }
    )
    with pytest.raises(flow.DeployError, match="recreation"):
        flow.sync_ai_functions(client, config, args, logger, summary)
    assert not client.writes


def test_malformed_remote_list_never_becomes_empty_create(settings):
    config, args, logger, summary = settings
    artefact(config, "ai-functions", ai_function())
    client = FakeClient({"/functions/ai": {"unexpected": []}})
    with pytest.raises(flow.DeployError, match="invalid resource list"):
        flow.sync_ai_functions(client, config, args, logger, summary)
    assert not client.writes


def test_duplicate_local_ai_functions_fail_before_read(settings):
    config, args, logger, summary = settings
    artefact(config, "ai-functions", ai_function(), "first")
    artefact(config, "ai-functions", ai_function(), "second")
    client = FakeClient()
    with pytest.raises(flow.DeployError, match="Duplicate"):
        flow.sync_ai_functions(client, config, args, logger, summary)
    assert not client.calls


@pytest.mark.parametrize("context", ["example_other", "public.weather"])
def test_namespace_preflight_rejects_foreign_local_resource(settings, context):
    config, args, logger, _ = settings
    artefact(config, "ai-functions", ai_function(context=context))
    with pytest.raises(flow.DeployError, match="POLYAPI_NAMESPACE"):
        flow.validate_local_namespace(config, args, logger)


@pytest.mark.parametrize("scoped", [False, True])
def test_extra_orphan_deletion_is_namespace_and_scope_bounded(settings, scoped):
    config, args, logger, summary = settings
    path = artefact(config, "ai-functions", ai_function())
    args = replace(
        args, delete_orphans=True, path_filters=(path.parent,) if scoped else ()
    )
    records = [
        {"name": "obsolete", "context": context, "id": context}
        for context in ("example.demo", "other.demo", "example_other.demo")
    ]
    client = FakeClient({"/functions/ai": records, "/variables": []})
    flow.sync_ai_functions(client, config, args, logger, summary)
    deletes = [call[1] for call in client.writes if call[0] == "DELETE"]
    assert deletes == ([] if scoped else ["/functions/ai/example.demo"])


def test_core_orphans_never_touch_external_context(settings):
    config, args, logger, summary = settings
    config = replace(config, enabled_types=("functions",))
    client = FakeClient(
        {
            "/functions/server": [
                {"id": "external", "context": "public.shared", "name": "helper"}
            ],
            "/functions/client": [],
        }
    )
    result, _ = flow.collect_orphans(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        function_registry=flow.FunctionRegistry(),
        webhook_index={},
    )
    assert result == []


@pytest.mark.parametrize("kind", ["jobs", "triggers"])
def test_orphan_ownership_requires_every_reference_to_be_known(settings, kind):
    config, args, logger, _ = settings
    detail = (
        {"functions": [{"id": "owned"}, {"id": "unknown"}]}
        if kind == "jobs"
        else {"serverFunctionId": "owned", "webhookHandleId": "unknown"}
    )
    client = FakeClient(
        {
            "/functions/server": [
                {"id": "owned", "context": "example.demo", "name": "handler"}
            ],
            "/functions/client": [],
            f"/{kind}": [{"id": "remote", "name": "remote"}],
            f"/{kind}/remote": {"id": "remote", "name": "remote", **detail},
        }
    )
    orphans, _ = flow.collect_orphans(
        client=client,
        config=replace(config, enabled_types=(kind,)),
        cli_args=args,
        logger=logger,
        function_registry=flow.FunctionRegistry(),
        webhook_index={},
    )
    assert not orphans


def test_job_cannot_hide_an_unowned_raw_id_among_portable_references(settings):
    config, args, logger, _ = settings
    artefact(
        config,
        "jobs",
        {
            "name": "mixed",
            "functions": [
                {"functionName": "handler", "functionContext": "example.demo"},
                {"id": "environment-specific-id"},
            ],
        },
    )
    with pytest.raises(flow.DeployError, match="namespace-owned"):
        flow.validate_local_namespace(config, args, logger)


@pytest.mark.parametrize("dry_run", [False, True])
def test_canopy_create_separates_metadata_and_writes_id_only_after_creation(
    settings, dry_run
):
    config, args, logger, summary = settings
    data = {
        "name": "Demo",
        "subpath": "example-demo",
        "description": "Description",
        "visibility": "ENVIRONMENT",
        "collections": [],
    }
    path = artefact(config, "canopy", data)
    original = path.read_bytes()
    client = FakeClient({"/applications": []})
    flow.sync_canopy(client, config, replace(args, dry_run=dry_run), logger, summary)
    if dry_run:
        assert path.read_bytes() == original
        assert not client.writes
    else:
        assert json.loads(path.read_text()) == {**data, "id": "created"}
        payload = client.writes[0][2]["payload"]
        assert payload["description"] == "Description"
        assert "description" not in payload["config"]
        assert "visibility" not in payload["config"]


@pytest.mark.parametrize("missing", [False, True])
def test_canopy_description_uses_metadata_patch(settings, missing):
    config, args, logger, summary = settings
    data = {
        "name": "Demo",
        "subpath": "example-demo",
        "description": "new",
        "collections": [],
    }
    artefact(config, "canopy", data)
    client = FakeClient(
        {
            "/applications": [
                {
                    "id": "app",
                    "name": "Demo",
                    "subpath": "example-demo",
                    "description": "old",
                }
            ],
            "/applications/app/config": (
                flow.ApiError("GET", "/applications/app/config", 404)
                if missing
                else {key: value for key, value in data.items() if key != "description"}
            ),
        }
    )
    flow.sync_canopy(client, config, args, logger, summary)
    assert client.writes[0][:2] == ("PATCH", "/applications/app")
    assert client.writes[0][2]["payload"]["description"] == "new"


def test_canopy_omitted_description_preserves_it_and_array_removal_updates(settings):
    config, args, logger, summary = settings
    data = {"name": "Demo", "subpath": "example-demo", "collections": []}
    artefact(config, "canopy", data)
    client = FakeClient(
        {
            "/applications": [
                {"id": "app", "subpath": "example-demo", "description": "keep"}
            ],
            "/applications/app/config": {**data, "collections": [{"name": "removed"}]},
        }
    )
    flow.sync_canopy(client, config, args, logger, summary)
    assert client.writes == [("PUT", "/applications/app/config", {"payload": data})]


def test_canopy_failed_read_does_not_write(settings):
    config, args, logger, summary = settings
    artefact(config, "canopy", {"name": "Demo", "subpath": "example-demo"})
    client = FakeClient(
        {
            "/applications": [{"id": "app", "subpath": "example-demo"}],
            "/applications/app/config": flow.ApiError(
                "GET", "/applications/app/config", 503
            ),
        }
    )
    with pytest.raises(flow.ApiError):
        flow.sync_canopy(client, config, args, logger, summary)
    assert not client.writes


@pytest.mark.parametrize("dry_run", [False, True])
def test_subscription_creation_resolves_nested_vari(settings, dry_run):
    config, args, logger, summary = settings
    data = subscription(
        paramsObject={
            "auth": [
                {
                    "type": "PolyVariable",
                    "variableName": "token",
                    "variableContext": "example.demo",
                    "path": "value",
                }
            ]
        }
    )
    artefact(config, "subscriptions", data)
    client = subscription_client()
    flow.sync_subscriptions(
        client, config, replace(args, dry_run=dry_run), logger, summary
    )
    if dry_run:
        assert not client.writes
    else:
        payload = client.writes[0][2]["payload"]
        assert payload["functionId"] == "handler"
        assert payload["paramsObject"]["auth"] == [
            {"type": "PolyVariable", "id": "secret", "path": "value"}
        ]
        assert "destination" not in payload


@pytest.mark.parametrize(
    "force,dry_run,confirmed",
    [
        (False, False, False),
        (True, True, False),
        (True, False, False),
        (True, False, True),
    ],
)
def test_subscription_recreation_requires_force_and_confirmation(
    settings, monkeypatch, force, dry_run, confirmed
):
    config, args, logger, summary = settings
    artefact(config, "subscriptions", subscription())
    client = subscription_client(
        [{"id": "old", "name": "events", "context": "example.demo"}]
    )
    # A structural change (the query) cannot be patched in place.
    client.responses["/subscriptions/graphql/old"] = {
        **subscription_detail(),
        "query": "subscription { other }",
    }
    confirm = Mock(return_value=confirmed)
    sleep = Mock()
    monkeypatch.setattr(flow, "confirm_subscription_recreate", confirm)
    monkeypatch.setattr(flow.time, "sleep", sleep)
    flow.sync_subscriptions(
        client, config, replace(args, force=force, dry_run=dry_run), logger, summary
    )
    if force and not dry_run and confirmed:
        assert [call[:2] for call in client.writes] == [
            ("DELETE", "/subscriptions/graphql/old"),
            ("POST", "/subscriptions/graphql"),
        ]
        sleep.assert_called_once_with(30)
    else:
        assert not client.writes
    if dry_run or not force:
        confirm.assert_not_called()


def test_invalid_replacement_never_deletes_stream(settings):
    config, args, logger, summary = settings
    artefact(config, "subscriptions", subscription(type="invalid"))
    client = subscription_client(
        [{"id": "old", "name": "events", "context": "example.demo"}]
    )
    with pytest.raises(flow.DeployError, match="validation"):
        flow.sync_subscriptions(
            client, config, replace(args, force=True), logger, summary
        )
    assert not client.writes


def test_write_otp_is_added_once_and_reads_are_unprompted(monkeypatch):
    manager = flow.OtpManager(True)
    prompt = Mock(return_value={"x-otp": "synthetic-otp"})
    monkeypatch.setattr(manager, "delete_headers", prompt)
    client = flow.PolyApiClient(
        "https://example.invalid", "synthetic-key", otp_manager=manager
    )
    response = Mock(status_code=204, content=b"")
    request = Mock(return_value=response)
    monkeypatch.setattr(client.session, "request", request)
    client.request("GET", "/applications")
    prompt.assert_not_called()
    client.request("POST", "/applications", payload={})
    assert request.call_args.kwargs["headers"] == {"x-otp": "synthetic-otp"}


def test_response_error_never_exposes_provider_body(monkeypatch):
    client = flow.PolyApiClient("https://example.invalid", "synthetic-key")
    monkeypatch.setattr(
        client.session,
        "request",
        Mock(return_value=Mock(status_code=500, text="synthetic-secret")),
    )
    with pytest.raises(flow.ApiError) as exc:
        client.request("POST", "/applications", payload={})
    assert "synthetic-secret" not in str(exc.value)


def test_every_sample_function_builds_with_real_sdk_parser(
    monkeypatch, sample_namespace
):
    monkeypatch.setenv("POLYAPI_NAMESPACE", sample_namespace)
    root = Path(__file__).resolve().parents[2]
    args = flow.CliArgs(True, False, False, False, (), None, False)
    config = flow.build_config(root, args)
    specs = flow.load_function_specs(config, args, flow.Logger(False))
    assert len(specs) == 7
    flow.validate_local_namespace(config, args, flow.Logger(False))
    for spec in specs:
        payload = flow.build_function_payload(spec)
        assert payload["name"] == spec.name
        assert payload["language"] == "python"
        assert payload["returnType"]


@pytest.mark.parametrize(
    "kind", ["ai-functions", "mcp-servers", "canopy", "subscriptions"]
)
def test_extra_resources_participate_in_env_placeholder_preflight(settings, kind):
    config, args, *_ = settings
    artefact(config, kind, {"description": "{{TEMPLATE_SYNTHETIC_MISSING_VALUE}}"})
    refs = flow.collect_missing_env_token_refs(config, args)
    assert [ref.env_name for ref in refs] == ["TEMPLATE_SYNTHETIC_MISSING_VALUE"]


def test_secrecy_recovery_signal_does_not_expose_provider_body(monkeypatch):
    client = flow.PolyApiClient("https://example.invalid", "synthetic-key")
    monkeypatch.setattr(
        client.session,
        "request",
        Mock(
            return_value=Mock(
                status_code=400,
                text="Cannot change secrecy level from SECRET to NONE; synthetic-private-value",
            )
        ),
    )
    with pytest.raises(flow.ApiError) as caught:
        client.request("PATCH", "/variables/config", payload={})
    assert caught.value.secrecy_change_rejected
    assert "synthetic-private-value" not in str(caught.value)


@pytest.mark.parametrize("kind", ["jobs", "triggers"])
def test_same_name_foreign_resource_is_never_updated(settings, kind):
    config, args, logger, summary = settings
    registry = flow.FunctionRegistry()
    registry.load(
        "server",
        [
            {"id": "owned", "context": "example.demo", "name": "handler"},
            {"id": "foreign", "context": "other.demo", "name": "handler"},
        ],
    )
    reference = {"functionName": "handler", "functionContext": "example.demo"}
    data = (
        {"functions": [reference]}
        if kind == "jobs"
        else {"destination": reference, "errorHandler": {"path": "/example"}}
    )
    artefact(config, kind, {"name": "same-name", "enabled": False, **data})
    detail = (
        {"functions": [{"id": "foreign"}]}
        if kind == "jobs"
        else {"serverFunctionId": "foreign", "errorHandler": {"path": "/example"}}
    )
    client = FakeClient(
        {
            f"/{kind}": [{"id": "existing", "name": "same-name"}],
            f"/{kind}/existing": {"id": "existing", "name": "same-name", **detail},
        }
    )
    options = dict(
        client=client,
        config=config,
        cli_args=replace(args, force=True),
        logger=logger,
        summary=summary,
        function_registry=registry,
        resume_state=flow.ResumeState(config.repo_root, load=False),
    )
    if kind == "jobs":
        flow.sync_jobs(**options)
    else:
        flow.sync_triggers(
            **options, webhook_index={}, otp_manager=flow.OtpManager(False)
        )
    assert not client.writes
    assert summary.get(kind).failed == 1


def test_same_route_foreign_webhook_is_never_updated(settings):
    config, args, logger, summary = settings
    data = {
        "name": "local",
        "context": "example.demo",
        "method": "POST",
        "slug": "shared-route",
        "subpath": "hook",
    }
    artefact(config, "webhooks", data)
    remote = {**data, "id": "foreign", "name": "remote", "context": "other.demo"}
    client = FakeClient({"/webhooks": [remote], "/webhooks/foreign": remote})
    flow.sync_webhooks(
        client=client,
        config=config,
        cli_args=replace(args, force=True),
        logger=logger,
        summary=summary,
        function_registry=flow.FunctionRegistry(),
        resume_state=flow.ResumeState(config.repo_root, load=False),
    )
    assert not client.writes
    assert summary.webhooks.failed == 1


def test_duplicate_named_trigger_is_not_selected_arbitrarily():
    with pytest.raises(flow.DeployError, match="Duplicate"):
        flow.find_existing_trigger(
            [{"id": "one", "name": "same"}, {"id": "two", "name": "same"}],
            {"name": "same"},
        )


def test_sample_artefacts_resolve_using_the_sample_functions(
    settings, sample_namespace
):
    config, args, logger, _ = settings
    config = replace(
        config,
        repo_root=Path(__file__).resolve().parents[2],
        namespace=sample_namespace,
        namespaces=(sample_namespace,),
    )
    registry = flow.FunctionRegistry()
    for spec in flow.load_function_specs(config, args, logger):
        registry.upsert(
            spec.function_type,
            {"id": spec.name, "name": spec.name, "context": spec.context},
        )
    for item in flow.load_artifact_files(config, args, config.variable_globs):
        payload = flow.build_variable_payload(item.path, item.data)
        assert payload["secrecy"] in {"NONE", "SECRET", "OBSCURED"}
        if payload["secrecy"] == "SECRET":
            assert payload["value"] == {}
    webhooks = {}
    for item in flow.load_artifact_files(config, args, config.webhook_globs):
        payload = flow.build_webhook_payload(item.path, item.data, registry)
        assert payload["securityFunctions"][0]["id"] == "validate_motd_payload"
        assert payload["requirePolyApiKey"]
        webhooks[(payload["context"], payload["name"])] = {**payload, "id": "webhook"}
    for item in flow.load_artifact_files(config, args, config.trigger_globs):
        spec = flow.build_trigger_spec(item.path, item.data, registry, webhooks)
        assert spec["create_payload"]["destination"]["serverFunctionId"]
    for item in flow.load_artifact_files(config, args, config.job_globs):
        payload = flow.build_job_payload(item.path, item.data, registry)
        assert not payload["enabled"]
        assert payload["functions"][0]["eventPayload"]["mood"] == "wise"
    for item in flow.extra_resource_files(config, args, "ai-functions"):
        tools = [
            {
                "id": name,
                "name": name,
                "context": f"{sample_namespace}.sample_workflow",
            }
            for name in ("searchLocations", "getCurrentWeather")
        ]
        payload = flow.resolve_ai_payload(item.data, tools, [], config)
        assert payload["enabled"]
        assert {tool["id"] for tool in payload["toolFunctions"]} == {
            "searchLocations",
            "getCurrentWeather",
        }
        assert all(
            argument["source"] in {"ai", "hardcoded"}
            for tool in payload["toolFunctions"]
            for argument in tool["argumentConfig"]
        )


@pytest.mark.parametrize("override", ['{"demo":"chosen"}', "demo=chosen"])
def test_canopy_override_precedence_and_unresolved_source_writeback(
    settings, monkeypatch, override
):
    config, args, logger, summary = settings
    monkeypatch.setenv("CANOPY_APP_IDS", override)
    data = {"id": "inline", "subpath": "demo", "name": "Demo", "collections": []}
    artefact(config, "canopy", data)
    client = FakeClient(
        {
            "/applications": [
                {"id": "chosen", "name": "Remote"},
                {"id": "inline", "name": "Demo"},
            ],
            "/applications/chosen/config": {},
        }
    )
    flow.sync_canopy(client, config, args, logger, summary)
    assert client.writes[0][:2] == ("PUT", "/applications/chosen/config")
    assert "id" not in client.writes[0][2]["payload"]


def test_canopy_name_fallback_and_ambiguous_identity(settings):
    config, args, logger, summary = settings
    artefact(
        config,
        "canopy",
        {"id": "old-environment", "subpath": "new-path", "name": "Demo"},
    )
    client = FakeClient(
        {
            "/applications": [{"id": "actual", "name": "Demo"}],
            "/applications/actual/config": {},
        }
    )
    flow.sync_canopy(client, config, args, logger, summary)
    assert client.writes[0][1] == "/applications/actual/config"
    client = FakeClient(
        {
            "/applications": [
                {"id": "one", "name": "Demo"},
                {"id": "two", "name": "Demo"},
            ]
        }
    )
    with pytest.raises(flow.DeployError, match="Ambiguous"):
        flow.sync_canopy(client, config, args, logger, summary)
    assert not client.writes


def test_canopy_id_writeback_does_not_materialize_env_values(settings, monkeypatch):
    config, args, logger, summary = settings
    monkeypatch.setenv("TEST_TITLE", "resolved-title")
    path = artefact(
        config,
        "canopy",
        {"name": "Demo", "subpath": "example-demo", "title": "{{TEST_TITLE}}"},
    )
    client = FakeClient({"/applications": []})
    flow.sync_canopy(client, config, args, logger, summary)
    assert client.writes[0][2]["payload"]["config"]["title"] == "resolved-title"
    assert json.loads(path.read_text())["title"] == "{{TEST_TITLE}}"


def test_legacy_artifacts_spelling_has_identical_discovery(settings):
    config, args, _, _ = settings
    path = artefact(config, "ai-functions", ai_function())
    legacy = path.parent.parent.with_name("artifacts") / "ai-functions" / path.name
    legacy.parent.mkdir(parents=True)
    path.rename(legacy)
    assert (
        flow.extra_resource_files(config, args, "ai-functions")[0].path
        == legacy.resolve()
    )


def test_namespace_list_bounds_writes_and_orphans(settings, monkeypatch):
    config, args, *_ = settings
    monkeypatch.setenv("POLYAPI_NAMESPACES", "example, second")
    config = flow.build_config(config.repo_root, args)
    flow.require_owned_context("second.tools", config)
    assert not flow.context_is_orphan_skipped("second.tools", config)
    assert flow.context_is_orphan_skipped("secondhand.tools", config)
    with pytest.raises(flow.DeployError):
        flow.require_owned_context("third.tools", config)


def test_shared_references_do_not_enter_the_function_sync_registry(settings):
    config, *_ = settings
    owned = flow.FunctionRegistry()
    owned.load(
        "server", [{"id": "owned", "name": "handler", "context": "example.demo"}]
    )
    client = FakeClient(
        {
            "/functions/server/tenant": [
                {"id": "shared", "name": "handler", "context": "shared.tools"}
            ],
            "/functions/server/public": [],
        }
    )
    refs = flow.reference_registry(client, owned)
    assert refs.require_kind_id("server", "shared.tools", "handler") == "shared"
    assert owned.get("server", "shared.tools", "handler") is None
    assert flow.context_is_orphan_skipped("shared.tools", config)


def test_ambiguous_shared_reference_fails_closed(settings):
    owned = flow.FunctionRegistry()
    owned.load(
        "server", [{"id": "owned", "name": "handler", "context": "example.demo"}]
    )
    client = FakeClient(
        {
            "/functions/server/tenant": [
                {"id": "other", "name": "handler", "context": "example.demo"}
            ]
        }
    )
    with pytest.raises(flow.DeployError, match="Ambiguous"):
        flow.reference_registry(client, owned)


def test_namespaced_job_may_reference_shared_server_function(settings):
    config, args, logger, _ = settings
    data = {
        "name": "example_shared_job",
        "functions": [{"functionContext": "shared.tools", "functionName": "handler"}],
        "enabled": False,
        "executionType": "sequential",
    }
    path = artefact(config, "jobs", data)
    flow.validate_local_namespace(config, args, logger)
    registry = flow.FunctionRegistry()
    registry.load(
        "server", [{"id": "shared", "context": "shared.tools", "name": "handler"}]
    )
    assert (
        flow.build_job_payload(path, data, registry)["functions"][0]["id"] == "shared"
    )
    flow.require_owned_function_ids(
        ["shared"], registry, config, resource_name=data["name"]
    )
    with pytest.raises(flow.DeployError, match="unmanaged"):
        flow.require_owned_function_ids(
            ["unknown"], registry, config, resource_name=data["name"]
        )


def test_sample_webhook_keeps_its_declared_schema(settings, sample_namespace):
    config, args, logger, _ = settings
    config = replace(
        config,
        repo_root=Path(__file__).resolve().parents[2],
        namespace=sample_namespace,
        namespaces=(sample_namespace,),
    )
    registry = flow.FunctionRegistry()
    for spec in flow.load_function_specs(config, args, logger):
        registry.upsert(
            spec.function_type,
            {"id": spec.name, "name": spec.name, "context": spec.context},
        )
    item = flow.load_artifact_files(config, args, config.webhook_globs)[0]
    payload = flow.build_webhook_payload(item.path, item.data, registry)
    assert payload["eventPayloadTypeSchema"] == item.data["eventPayloadTypeSchema"]
    flow.validate_endpoint_payload("CreateWebhookHandleDto", payload)
    flow.validate_endpoint_payload("UpdateWebhookHandleDto", payload)


def test_all_sample_functions_build_with_the_pinned_python_parser(
    settings, sample_namespace
):
    config, args, logger, _ = settings
    config = replace(
        config,
        repo_root=Path(__file__).resolve().parents[2],
        namespace=sample_namespace,
        namespaces=(sample_namespace,),
    )
    specs = flow.load_function_specs(config, args, logger)
    assert len(specs) == 7
    for spec in specs:
        payload = flow.build_function_payload(spec)
        assert payload["name"] == spec.name and payload["language"] == "python"
        assert payload["context"] == spec.context


@pytest.mark.parametrize("status", [403, 404, 500])
def test_unavailable_shared_scope_is_not_confused_with_provider_failure(status):
    path = "/functions/server/tenant"
    client = FakeClient({path: flow.ApiError("GET", path, status)})
    if status == 500:
        with pytest.raises(flow.ApiError):
            flow.list_optional_scope_resources(client, path)
    else:
        assert flow.list_optional_scope_resources(client, path) == []


def test_first_deployment_dry_run_resolves_local_functions_webhook_and_job(
    settings, sample_namespace
):
    config, args, logger, summary = settings
    config = replace(
        config,
        repo_root=Path(__file__).resolve().parents[2],
        namespace=sample_namespace,
        namespaces=(sample_namespace,),
    )
    args = replace(
        args,
        dry_run=True,
        path_filters=(config.repo_root / "workflows/src/sample_workflow",),
    )
    registry = flow.FunctionRegistry()
    client = FakeClient(
        {
            "/functions/server": [],
            "/functions/client": [],
            "/specs": [],
            "/webhooks": [],
            "/triggers": [],
            "/jobs": [],
        }
    )
    registry, changed = flow.sync_functions(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        resume_state=flow.ResumeState(config.repo_root, load=False),
    )
    webhooks = flow.sync_webhooks(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        function_registry=registry,
        resume_state=flow.ResumeState(config.repo_root, load=False),
    )
    flow.sync_triggers(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        function_registry=registry,
        webhook_index=webhooks,
        otp_manager=flow.OtpManager(False),
        resume_state=flow.ResumeState(config.repo_root, load=False),
    )
    flow.sync_jobs(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        function_registry=registry,
        resume_state=flow.ResumeState(config.repo_root, load=False),
    )
    assert changed
    assert summary.functions.created == 4
    assert (
        summary.webhooks.created == 1
        # The webhook binds motd_server directly; only the error trigger remains.
        and summary.triggers.created == 1
        and summary.jobs.created == 1
    )
    assert not flow.summary_has_incomplete_work(summary)
    assert not client.writes


def test_weather_dry_run_resolves_owned_tools_without_a_secret(
    settings, sample_namespace
):
    config, args, logger, summary = settings
    config = replace(
        config,
        repo_root=Path(__file__).resolve().parents[2],
        namespace=sample_namespace,
        namespaces=(sample_namespace,),
    )
    args = replace(
        args,
        dry_run=True,
        path_filters=(config.repo_root / "workflows/src/sample_workflow",),
    )
    responses = {
        f"/functions/{kind}{suffix}": []
        for kind in ("api", "server", "client")
        for suffix in ("", "/tenant", "/public")
    }
    responses.update({"/functions/ai": [], "/variables": []})
    responses["/functions/api"] = [
        {
            "id": name,
            "context": f"{sample_namespace}.sample_workflow",
            "name": name,
        }
        for name in ("searchLocations", "getCurrentWeather")
    ]
    client = FakeClient(responses)
    assert flow.sync_ai_functions(client, config, args, logger, summary)
    assert summary.ai_functions.created == 1
    assert not client.writes


def test_ai_dry_run_resolves_new_secret_metadata_without_creating_it(settings):
    config, args, logger, summary = settings
    args = replace(args, dry_run=True)
    artefact(
        config,
        "vari",
        {"name": "token", "context": "example.demo", "secrecy": "SECRET", "value": {}},
    )
    artefact(
        config,
        "ai-functions",
        ai_function(
            toolFunctions=[
                {
                    "functionName": "lookup",
                    "functionContext": "public.tools",
                    "argumentConfig": [
                        {
                            "key": "key",
                            "source": "variable",
                            "variableName": "token",
                            "variableContext": "example.demo",
                        }
                    ],
                }
            ]
        ),
    )
    responses = {
        f"/functions/{kind}{suffix}": []
        for kind in ("api", "server", "client")
        for suffix in ("", "/tenant", "/public")
    }
    responses.update({"/functions/ai": [], "/variables": []})
    responses["/functions/api/public"] = [
        {"id": "tool", "context": "public.tools", "name": "lookup"}
    ]
    client = FakeClient(responses)
    assert flow.sync_ai_functions(client, config, args, logger, summary)
    assert summary.ai_functions.created == 1
    assert not client.writes


@pytest.mark.parametrize("selected", ["server", "client"])
def test_function_orphans_respect_selected_function_kind(settings, selected):
    config, args, logger, summary = settings
    config = replace(config, enabled_types=("functions",))
    args = replace(
        args,
        enabled_types=("functions",),
        function_types=(selected,),
        delete_orphans=True,
    )
    responses = {}
    for kind, suffix in (("server", "sfx"), ("client", "cfx")):
        path = config.repo_root / f"workflows/src/demo/{kind}/worker_{suffix}.py"
        path.parent.mkdir(parents=True)
        path.write_text(
            'polyConfig = {"name": "worker", "context": "example.demo"}\n'
            "def worker() -> dict:\n    return {}\n"
        )
        responses[f"/functions/{kind}"] = [
            {"id": f"{kind}-kept", "name": "worker", "context": "example.demo"},
            {"id": f"{kind}-orphan", "name": "old", "context": "example.demo"},
        ]
    client = FakeClient(responses)
    assert flow.process_orphans(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        function_registry=flow.FunctionRegistry(),
        webhook_index={},
        otp_manager=flow.OtpManager(False),
    )
    assert [(method, path) for method, path, _ in client.writes] == [
        ("DELETE", f"/functions/{selected}/{selected}-orphan")
    ]


def test_trigger_same_basename_and_changed_resume_payload(settings):
    config, args, logger, summary = settings
    registry = flow.FunctionRegistry()
    registry.upsert(
        "server", {"id": "handler", "name": "handler", "context": "example.demo"}
    )
    client = FakeClient({"/triggers": []})
    target = ("https://example.invalid", "dev")
    state = flow.ResumeState(config.repo_root, False, target=target)
    paths = []
    for name in ("first", "second"):
        path = (
            config.repo_root / f"workflows/src/{name}/artefacts/triggers/on_event.json"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "name": f"example.{name}",
                    "errorHandler": {"path": name, "statusCodes": [500]},
                    "destination": {
                        "functionContext": "example.demo",
                        "functionName": "handler",
                    },
                }
            )
        )
        paths.append(path)
    options = dict(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        function_registry=registry,
        webhook_index={},
        otp_manager=flow.OtpManager(False),
    )
    flow.sync_triggers(**options, summary=summary, resume_state=state)
    assert summary.triggers.created == 2 and summary.triggers.failed == 0
    assert len(client.writes) == 2
    resumed = flow.Summary()
    flow.sync_triggers(
        **options,
        summary=resumed,
        resume_state=flow.ResumeState(config.repo_root, True, target=target),
    )
    assert resumed.triggers.skipped == 2
    data = json.loads(paths[0].read_text())
    data["enabled"] = False
    paths[0].write_text(json.dumps(data))
    changed = flow.Summary()
    flow.sync_triggers(
        **options,
        summary=changed,
        resume_state=flow.ResumeState(config.repo_root, True, target=target),
    )
    assert changed.triggers.created == 1 and changed.triggers.skipped == 1
    assert client.writes[-1][2]["payload"]["enabled"] is False


@pytest.mark.parametrize("kind", ["variables", "tables", "schemas", "webhooks", "jobs"])
@pytest.mark.parametrize("mode", ["changed", "unchanged", "force", "legacy"])
def test_core_resource_resume_checks_current_payload(settings, kind, mode):
    config, args, logger, summary = settings
    config = replace(config, use_mtime_guard=False)
    registry = flow.FunctionRegistry()
    registry.upsert(
        "server", {"id": "handler", "name": "handler", "context": "example.demo"}
    )
    common = {"name": "settings", "context": "example.demo", "description": "before"}
    inputs = {
        "variables": (
            "vari",
            {**common, "secrecy": "NONE", "value": {"threshold": 10}},
        ),
        "tables": (
            "tabi",
            {**common, "columns": [{"name": "record_key", "type": "string"}]},
        ),
        "schemas": ("schemas", {**common, "definition": {"type": "object"}}),
        "webhooks": (
            "webhooks",
            {
                **common,
                "method": "POST",
                "slug": "example-settings",
                "webhookType": "SYNC",
            },
        ),
        "jobs": (
            "jobs",
            {
                "name": "example.settings",
                "description": "before",
                "enabled": False,
                "executionType": "sequential",
                "schedule": {"type": "interval", "value": 60},
                "functions": [
                    {
                        "functionName": "handler",
                        "functionContext": "example.demo",
                        "eventPayload": {},
                        "headersPayload": {},
                        "paramsPayload": {},
                    }
                ],
            },
        ),
    }
    category, data = inputs[kind]
    path = artefact(config, category, data)
    client = FakeClient({f"/{kind}": []})
    if kind == "tables":
        client.responses["/tables"] = {"results": [], "pagination": {"pages": 1}}
    target = ("https://example.invalid", "test-environment")
    state = flow.ResumeState(config.repo_root, False, target=target)
    options = dict(client=client, config=config, logger=logger)
    if kind in {"webhooks", "jobs"}:
        options["function_registry"] = registry
    elif kind == "variables":
        options["otp_manager"] = flow.OtpManager(False)
    sync = getattr(flow, f"sync_{kind}")
    sync(**options, cli_args=args, summary=summary, resume_state=state)
    assert summary.get(kind).created == 1 and summary.get(kind).failed == 0
    payload = copy.deepcopy(client.writes[0][2]["payload"])
    client.responses[f"/{kind}"] = [{"id": "created", **payload}]
    if kind == "tables":
        client.responses["/tables"] = {
            "results": [{"id": "created", **payload}],
            "pagination": {"pages": 1},
        }
    client.responses[f"/{kind}/created"] = {"id": "created", **payload}
    client.calls.clear()
    if mode in {"changed", "legacy"}:
        data["description"] = "after"
        if kind == "variables":
            data["value"] = {"threshold": 20}
        path.write_text(json.dumps(data))
    if mode == "legacy":
        resume_path = config.repo_root / flow.ResumeState.FILENAME
        legacy = json.loads(resume_path.read_text())
        legacy.pop("fingerprints")
        resume_path.write_text(json.dumps(legacy))
    resumed = flow.Summary()
    sync(
        **options,
        cli_args=replace(args, resume=True, force=mode == "force"),
        summary=resumed,
        resume_state=flow.ResumeState(config.repo_root, True, target=target),
    )
    assert resumed.get(kind).failed == 0
    if mode == "unchanged":
        assert resumed.get(kind).skipped == 1 and not client.writes
    else:
        assert resumed.get(kind).updated == 1
        assert len(client.writes) == 1
        method, route, kwargs = client.writes[0]
        assert (method, route) == ("PATCH", f"/{kind}/created")
        assert kwargs["payload"]["description"] == (
            "before" if mode == "force" else "after"
        )
        if kind == "variables" and mode != "force":
            assert kwargs["payload"]["value"] == {"threshold": 20}


@pytest.mark.parametrize("secrecy", ["OBSCURED", "SECRET", "PARTIAL"])
def test_sensitive_variable_resume_never_persists_content_digest(settings, secrecy):
    config, args, logger, summary = settings
    data = {
        "name": "settings",
        "context": "example.demo",
        "secrecy": secrecy,
        "value": {"token": "synthetic-sensitive-value"},
    }
    artefact(config, "vari", data)
    target = ("https://example.invalid", "test-environment")
    state = flow.ResumeState(config.repo_root, False, target=target)
    key = "var:example.demo:settings"
    state.mark_done(key, "previous-non-secret-fingerprint")
    client = FakeClient({"/variables": []})
    flow.sync_variables(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        otp_manager=flow.OtpManager(False),
        resume_state=state,
    )
    assert summary.variables.created == 1 and summary.variables.failed == 0
    saved = json.loads((config.repo_root / flow.ResumeState.FILENAME).read_text())
    assert saved == {
        "target": {"baseUrl": target[0], "environmentId": target[1]},
        "done": [key],
        "fingerprints": {},
    }
    assert not flow.ResumeState(config.repo_root, True, target=target).is_done(key)


@pytest.mark.parametrize("source_kind", ["webhook", "error"])
@pytest.mark.parametrize("operation", ["create", "update", "unchanged", "recreate"])
def test_trigger_owner_is_preserved_through_sync(settings, source_kind, operation):
    config, args, logger, summary = settings
    registry = flow.FunctionRegistry()
    registry.upsert(
        "server", {"id": "handler", "name": "handler", "context": "example.demo"}
    )
    data = {
        "name": "example.trigger",
        "destination": {"functionContext": "example.demo", "functionName": "handler"},
    }
    remote = {
        "id": "trigger",
        "name": data["name"],
        "serverFunctionId": "handler",
        "enabled": True,
        "ownerUserId": "owner-old",
    }
    webhooks = {("example.demo", "hook"): {"id": "hook"}}
    if source_kind == "webhook":
        data["source"] = {"webhookContext": "example.demo", "webhookName": "hook"}
        remote.update(webhookHandleId="hook", waitForResponse=True)
    else:
        data["errorHandler"] = {"path": "demo", "statusCodes": [500]}
        remote["errorHandler"] = copy.deepcopy(data["errorHandler"])
    if operation in {"create", "update"}:
        data["ownerUserId"] = "owner-new"
    if operation == "recreate":
        if source_kind == "webhook":
            remote["webhookHandleId"] = "old-hook"
        else:
            remote["errorHandler"]["path"] = "old-path"
    artefact(config, "triggers", data)
    client = FakeClient(
        {
            "/triggers": [] if operation == "create" else [remote],
            "/triggers/trigger": remote,
        }
    )
    flow.sync_triggers(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        function_registry=registry,
        webhook_index=webhooks,
        otp_manager=flow.OtpManager(False),
        resume_state=flow.ResumeState(config.repo_root, False),
    )
    assert summary.triggers.failed == 0
    if operation == "unchanged":
        assert client.writes == [] and summary.triggers.skipped == 1
    else:
        method, _, options = client.writes[-1]
        assert method == ("PATCH" if operation == "update" else "POST")
        assert options["payload"]["ownerUserId"] == (
            "owner-old" if operation == "recreate" else "owner-new"
        )


def test_webhook_template_body_changes_reach_update(settings):
    config, args, logger, summary = settings
    remote = {
        "id": "hook",
        "name": "hook",
        "context": "example.demo",
        "eventPayload": {"value": "example"},
        "method": "POST",
        "slug": "example",
        "subpath": "hook",
        "templateBody": '{"eventPayload":{"value":"example"}}',
    }
    local = {key: value for key, value in remote.items() if key != "id"}
    local["templateBody"] = '{"eventPayload":{"value":42}}'
    artefact(config, "webhooks", local)
    client = FakeClient({"/webhooks": [remote], "/webhooks/hook": remote})
    flow.sync_webhooks(
        client=client,
        config=config,
        cli_args=args,
        logger=logger,
        summary=summary,
        function_registry=flow.FunctionRegistry(),
        resume_state=flow.ResumeState(config.repo_root, False),
    )
    assert summary.webhooks.updated == 1 and summary.webhooks.failed == 0
    assert len(client.writes) == 1
    assert client.writes[0][0:2] == ("PATCH", "/webhooks/hook")
    assert client.writes[0][2]["payload"]["templateBody"] == local["templateBody"]


def mcp_server(**overrides):
    return {
        "name": "example-weather-tools",
        "description": "Weather lookup tools for MCP clients.",
        "enabled": True,
        "isPublic": False,
        "toolFunctions": [
            {
                "functionName": "searchLocations",
                "functionContext": "public.weather",
                "argumentConfig": [
                    {"key": "name", "source": "ai"},
                    {"key": "count", "source": "hardcoded", "value": 3},
                    {"key": "format", "source": "hardcoded", "value": "json"},
                    {
                        "key": "apiKey",
                        "source": "variable",
                        "variableName": "token",
                        "variableContext": "example.demo",
                    },
                ],
            }
        ],
        "oauthClientId": "weather-client",
        "oauthClientSecretVariable": {
            "variableName": "oauthSecret",
            "variableContext": "example.demo",
        },
        **overrides,
    }


MCP_RESOLVED = {
    "name": "example-weather-tools",
    "description": "Weather lookup tools for MCP clients.",
    "enabled": True,
    "isPublic": False,
    "oauthClientId": "weather-client",
    "oauthClientSecretVariableId": "secret-id",
    "toolFunctions": [
        {
            "id": "search-id",
            "argumentConfig": [
                {"key": "name", "source": "ai"},
                {"key": "count", "source": "hardcoded", "value": 3},
                {"key": "format", "source": "hardcoded", "value": "json"},
                {"key": "apiKey", "source": "variable", "variableId": "token-id"},
            ],
        }
    ],
}
MCP_FUNCTIONS = [
    {"id": "search-id", "name": "searchLocations", "context": "public.weather"}
]
MCP_VARIABLES = [
    {"id": "token-id", "name": "token", "context": "example.demo"},
    {"id": "secret-id", "name": "oauthSecret", "context": "example.demo"},
]


def mcp_client(servers=None):
    responses = {
        f"/functions/{kind}{suffix}": []
        for kind in ("api", "server", "client")
        for suffix in ("", "/tenant", "/public")
    }
    responses.update(
        {
            "/mcp-servers": servers or [],
            "/functions/api": MCP_FUNCTIONS,
            "/variables": MCP_VARIABLES,
        }
    )
    # Like PolyAPI, only the single-server endpoint returns mcpUrl.
    for identifier in ("created", "mcp-id"):
        responses[f"/mcp-servers/{identifier}"] = {
            "id": identifier,
            "mcpUrl": f"https://example.invalid/mcp-servers/{identifier}/mcp",
        }
    return FakeClient(responses)


def test_mcp_resolves_tools_variables_and_oauth_secret(settings):
    config, *_ = settings
    data = mcp_server()
    assert (
        flow.resolve_mcp_payload(data, MCP_FUNCTIONS, MCP_VARIABLES, config)
        == MCP_RESOLVED
    )
    assert "functionName" in data["toolFunctions"][0]
    cleared = flow.resolve_mcp_payload(
        {"name": "cleared", "oauthClientSecretVariable": None}, [], [], config
    )
    assert cleared == {"name": "cleared", "oauthClientSecretVariableId": None}


@pytest.mark.parametrize(
    "overrides",
    [
        {"toolFunctionIds": ["remote"]},
        {"oauthClientSecretVariableId": "remote"},
        {"environmentId": "remote"},
        {"mcpUrl": "https://example.invalid/mcp"},
        {"context": "example.demo"},
        {"toolFunctions": [{"id": "remote"}]},
        {
            "toolFunctions": [
                {
                    "functionName": "searchLocations",
                    "functionContext": "public.weather",
                    "argumentConfig": [
                        {"key": "apiKey", "source": "variable", "variableId": "x"}
                    ],
                }
            ]
        },
        {"name": " "},
    ],
)
def test_mcp_rejects_environment_specific_or_invalid_fields(settings, overrides):
    config, *_ = settings
    with pytest.raises(flow.DeployError):
        flow.resolve_mcp_payload(
            mcp_server(**overrides), MCP_FUNCTIONS, MCP_VARIABLES, config
        )


def test_mcp_variable_references_must_be_namespace_owned(settings):
    config, args, logger, _ = settings
    data = mcp_server(
        oauthClientSecretVariable={
            "variableName": "oauthSecret",
            "variableContext": "other.demo",
        }
    )
    with pytest.raises(flow.DeployError, match="POLYAPI_NAMESPACE"):
        flow.resolve_mcp_payload(data, MCP_FUNCTIONS, MCP_VARIABLES, config)
    artefact(config, "mcp-servers", data)
    with pytest.raises(flow.DeployError, match="POLYAPI_NAMESPACE"):
        flow.validate_local_namespace(config, args, logger)


def test_mcp_local_preflight_needs_no_remote_records(settings):
    config, args, logger, _ = settings
    artefact(config, "mcp-servers", mcp_server())
    config = replace(config, enabled_types=("mcp-servers",))
    assert not flow.validate_local_artifacts(config, args, logger).invalid_paths


@pytest.mark.parametrize("dry_run", [False, True])
def test_mcp_create_and_dry_run(settings, dry_run, capsys):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server())
    client = mcp_client()
    assert flow.sync_mcp_servers(
        client, config, replace(args, dry_run=dry_run), logger, summary, "env-1"
    )
    assert client.writes == (
        [] if dry_run else [("POST", "/mcp-servers", {"payload": MCP_RESOLVED})]
    )
    assert summary.mcp_servers.created == 1
    # The create response has no URL; it comes from the single-server endpoint.
    lookups = [call[1] for call in client.calls if call[1] == "/mcp-servers/created"]
    assert lookups == ([] if dry_run else ["/mcp-servers/created"])
    printed = (
        "https://example.invalid/mcp-servers/created/mcp" in capsys.readouterr().out
    )
    assert printed is not dry_run


def test_mcp_url_lookup_failure_does_not_fail_the_sync(settings, capsys):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server())
    client = mcp_client()
    client.responses["/mcp-servers/created"] = flow.DeployError("unavailable")
    assert flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert summary.mcp_servers.created == 1
    assert "Could not read the MCP URL for" in capsys.readouterr().out


def test_mcp_matches_by_name_in_target_environment_and_patches_changes(
    settings, capsys
):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server(enabled=False))
    # Like PolyAPI's list, the remote record carries no mcpUrl, and a server
    # created from toolFunctions has no legacy toolFunctionIds.
    remote = {
        **MCP_RESOLVED,
        "id": "mcp-id",
        "environmentId": "env-1",
        "createdAt": "2026-11-01T00:00:00Z",
        "toolFunctionIds": None,
    }
    url = "https://example.invalid/mcp-servers/mcp-id/mcp"
    other_environment = {**remote, "id": "elsewhere", "environmentId": "env-2"}
    client = mcp_client([other_environment, remote])
    assert flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert client.writes == [
        ("PATCH", "/mcp-servers/mcp-id", {"payload": {"enabled": False}})
    ]
    assert summary.mcp_servers.updated == 1
    assert f"MCP URL for example-weather-tools: {url}" in capsys.readouterr().out

    unchanged = flow.Summary()
    artefact(config, "mcp-servers", mcp_server())
    client = mcp_client([remote])
    assert not flow.sync_mcp_servers(client, config, args, logger, unchanged, "env-1")
    assert not client.writes
    assert unchanged.mcp_servers.skipped == 1
    assert f"MCP URL for example-weather-tools: {url}" in capsys.readouterr().out


def test_mcp_syncing_the_tool_list_clears_legacy_tool_function_ids(settings):
    config, args, logger, summary = settings
    legacy = {
        **MCP_RESOLVED,
        "id": "mcp-id",
        "environmentId": "env-1",
        "toolFunctionIds": ["old-tool-id"],
    }
    artefact(config, "mcp-servers", mcp_server())
    client = mcp_client([legacy])
    assert flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert client.writes == [
        ("PATCH", "/mcp-servers/mcp-id", {"payload": {"toolFunctionIds": []}})
    ]

    # Clearing the tool list removes the current and the legacy tools.
    artefact(config, "mcp-servers", mcp_server(toolFunctions=[]))
    client = mcp_client([legacy])
    flow.sync_mcp_servers(client, config, args, logger, flow.Summary(), "env-1")
    assert client.writes == [
        (
            "PATCH",
            "/mcp-servers/mcp-id",
            {"payload": {"toolFunctions": [], "toolFunctionIds": []}},
        )
    ]
    flow.validate_endpoint_payload("UpdateMCPServerDto", client.writes[0][2]["payload"])

    # An artefact that does not manage tools leaves them alone.
    unmanaged = mcp_server()
    del unmanaged["toolFunctions"]
    artefact(config, "mcp-servers", unmanaged)
    client = mcp_client([legacy])
    assert not flow.sync_mcp_servers(
        client, config, args, logger, flow.Summary(), "env-1"
    )
    assert not client.writes


@pytest.mark.parametrize(
    "elsewhere, reason",
    [
        ("/functions/client", "is a client function, which MCP servers cannot call"),
        ("/functions/api/tenant", "is shared from another environment or tenant"),
        ("/functions/server/public", "is shared from another environment or tenant"),
        (None, "was not found among API or server functions in this environment"),
    ],
)
def test_mcp_tools_must_be_api_or_server_functions_in_the_environment(
    settings, elsewhere, reason
):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server())
    client = mcp_client()
    client.responses["/functions/api"] = []
    if elsewhere:
        client.responses[elsewhere] = MCP_FUNCTIONS
    with pytest.raises(
        flow.DeployError, match=re.escape(f"public.weather.searchLocations {reason}")
    ):
        flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert not client.writes


def test_mcp_same_named_function_in_this_environment_wins(settings):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server())
    client = mcp_client()
    client.responses["/functions/client"] = [{**MCP_FUNCTIONS[0], "id": "client-id"}]
    client.responses["/functions/api/public"] = [
        {**MCP_FUNCTIONS[0], "id": "shared-id"}
    ]
    assert flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert client.writes == [("POST", "/mcp-servers", {"payload": MCP_RESOLVED})]


def test_mcp_patch_sends_a_hardcoded_value_with_a_removed_property(settings):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server())
    deployed = copy.deepcopy(MCP_RESOLVED)
    deployed["toolFunctions"][0]["argumentConfig"][1]["value"] = 3
    deployed["toolFunctions"][0]["argumentConfig"].append(
        {"key": "extra", "source": "ai"}
    )
    remote = {**deployed, "id": "mcp-id", "environmentId": "env-1"}
    client = mcp_client([remote])
    flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert client.writes == [
        (
            "PATCH",
            "/mcp-servers/mcp-id",
            {"payload": {"toolFunctions": MCP_RESOLVED["toolFunctions"]}},
        )
    ]

    # A JSON value that lost a property is sent whole.
    local = mcp_server()
    local["toolFunctions"][0]["argumentConfig"][1]["value"] = {"limit": 3}
    artefact(config, "mcp-servers", local)
    resolved = copy.deepcopy(MCP_RESOLVED["toolFunctions"])
    resolved[0]["argumentConfig"][1]["value"] = {"limit": 3}
    stale = copy.deepcopy(resolved)
    stale[0]["argumentConfig"][1]["value"] = {"limit": 3, "legacy": True}
    client = mcp_client([{**remote, "toolFunctions": stale}])
    flow.sync_mcp_servers(client, config, args, logger, flow.Summary(), "env-1")
    assert client.writes == [
        ("PATCH", "/mcp-servers/mcp-id", {"payload": {"toolFunctions": resolved}})
    ]


def test_mcp_duplicate_names_fail_before_any_write(settings):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server())
    duplicates = [
        {**MCP_RESOLVED, "id": identifier, "environmentId": "env-1"}
        for identifier in ("one", "two")
    ]
    client = mcp_client(duplicates)
    with pytest.raises(flow.DeployError, match="2 MCP servers named"):
        flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert not client.writes

    artefact(config, "mcp-servers", mcp_server(), "second")
    client = FakeClient()
    with pytest.raises(flow.DeployError, match="Duplicate local MCP server name"):
        flow.sync_mcp_servers(client, config, args, logger, summary, "env-1")
    assert not client.calls


def test_mcp_servers_are_never_orphans(settings):
    config, args, logger, summary = settings
    artefact(config, "mcp-servers", mcp_server())
    stranger = {"id": "stranger", "name": "someone-else", "environmentId": "env-1"}
    client = mcp_client([stranger])
    flow.sync_mcp_servers(
        client, config, replace(args, delete_orphans=True), logger, summary, "env-1"
    )
    assert [call[0] for call in client.writes] == ["POST"]
    assert not summary.orphans


# --- PolyAPI Release 35 -------------------------------------------------------


@pytest.mark.parametrize(
    "value, seconds",
    [
        (5, 300),
        ("5", 300),
        ("10s", 10),
        ("90 s", 90),
        ("30m", 1800),
        ("2H", 7200),
        (" 60s ", 60),
    ],
)
def test_job_interval_values_match_the_platform_parser(value, seconds):
    assert flow.job_interval_seconds(value) == seconds


@pytest.mark.parametrize("value", ["abc", "5d", "1.5", 1.5, True, None, "-5", 0, ""])
def test_invalid_job_interval_values_are_rejected(tmp_path, value):
    assert flow.job_interval_seconds(value) is None
    payload = {"name": "job", "schedule": {"type": "interval", "value": value}}
    with pytest.raises(flow.DeployError, match="invalid interval"):
        flow.validate_job_schedule(payload, tmp_path / "job.json")


def test_job_interval_under_ten_seconds_fails_locally(tmp_path):
    registry = flow.FunctionRegistry()
    registry.upsert("server", {"id": "fn", "context": "example.demo", "name": "run"})
    data = {
        "name": "job",
        "schedule": {"type": "interval", "value": "5s"},
        "functions": [{"functionContext": "example.demo", "functionName": "run"}],
    }
    with pytest.raises(flow.DeployError, match="minimum is 10 seconds"):
        flow.build_job_payload(tmp_path / "job.json", data, registry)
    data["schedule"]["value"] = "10s"
    payload = flow.build_job_payload(tmp_path / "job.json", data, registry)
    assert payload["schedule"]["value"] == "10s"
    flow.validate_endpoint_payload(
        "CreateJobDto", {**payload, "enabled": False, "executionType": "sequential"}
    )


@pytest.mark.parametrize(
    "local, remote, same",
    [
        ("60s", 1, True),
        ("1m", 1, True),
        ("1", 1, True),
        ("90s", "90s", True),
        ("2m", 1, False),
        ("10s", "15s", False),
    ],
)
def test_job_interval_drift_compares_seconds(local, remote, same):
    local_payload = {"schedule": {"type": "interval", "value": local}}
    remote_payload = {"schedule": {"type": "interval", "value": remote}}
    left, right = flow.normalize_job_subset(local_payload, remote_payload)
    assert (left == right) is same


def test_periodical_job_schedule_is_not_reinterpreted():
    local = {"schedule": {"type": "periodical", "value": "*/5 * * * *"}}
    left, right = flow.normalize_job_subset(local, copy.deepcopy(local))
    assert left == right == {"schedule": {"type": "periodical", "value": "*/5 * * * *"}}


def webhook_registry():
    registry = flow.FunctionRegistry()
    registry.upsert(
        "server", {"id": "handler-id", "context": "example.demo", "name": "handler"}
    )
    return registry


def webhook_data(**overrides):
    return {
        "name": "hook",
        "context": "example.demo",
        "description": "Hook",
        "subpath": "hook",
        "method": "POST",
        **overrides,
    }


def test_webhook_server_function_resolves_and_null_disconnects(tmp_path):
    path = tmp_path / "hook.json"
    reference = {"functionContext": "example.demo", "functionName": "handler"}
    payload = flow.build_webhook_payload(
        path, webhook_data(serverFunction=reference), webhook_registry()
    )
    assert payload["serverFunctionId"] == "handler-id"
    assert "serverFunction" not in payload
    flow.validate_endpoint_payload("CreateWebhookHandleDto", payload)
    payload = flow.build_webhook_payload(
        path, webhook_data(serverFunction=None), webhook_registry()
    )
    assert payload["serverFunctionId"] is None
    flow.validate_endpoint_payload("UpdateWebhookHandleDto", payload)
    assert "serverFunctionId" not in flow.build_webhook_payload(
        path, webhook_data(), webhook_registry()
    )


@pytest.mark.parametrize(
    "data",
    [
        webhook_data(serverFunctionId="raw-id"),
        webhook_data(serverFunction="{{example.demo.handler}}"),
        webhook_data(serverFunction={"functionName": "handler"}),
        webhook_data(serverFunction={"functionContext": "example.demo"}),
    ],
)
def test_webhook_server_function_requires_portable_reference(tmp_path, data):
    with pytest.raises(flow.DeployError, match="serverFunction"):
        flow.build_webhook_payload(tmp_path / "hook.json", data, webhook_registry())


def test_webhook_server_function_preflight_and_contexts(settings):
    config, *_ = settings
    data = webhook_data(
        serverFunction={"functionContext": "shared.tools", "functionName": "run"}
    )
    path = artefact(config, "webhooks", data)
    flow.validate_local_artifact("webhooks", path, data, config)
    assert flow.webhook_contexts_from_raw(data) == {"example.demo", "shared.tools"}


def sync_webhook(settings, data, remote=None, *, triggers=(), dry_run=False):
    config, args, logger, summary = settings
    artefact(config, "webhooks", data)
    for index, trigger in enumerate(triggers):
        artefact(config, "triggers", trigger, name=f"trigger{index}")
    responses = {"/webhooks": [remote] if remote else []}
    if remote:
        responses[f"/webhooks/{remote['id']}"] = remote
    client = FakeClient(responses)
    warnings = Mock()
    logger.warn = warnings
    flow.sync_webhooks(
        client=client,
        config=replace(config, use_mtime_guard=False),
        cli_args=replace(args, dry_run=dry_run),
        logger=logger,
        summary=summary,
        function_registry=webhook_registry(),
        resume_state=flow.ResumeState(config.repo_root, False),
    )
    return client, warnings, summary


def test_webhook_server_function_change_reaches_update(settings):
    reference = {"functionContext": "example.demo", "functionName": "handler"}
    remote = {
        **webhook_data(),
        "id": "hook-id",
        "slug": None,
        "serverFunctionId": None,
    }
    client, _, summary = sync_webhook(
        settings, webhook_data(serverFunction=reference), remote
    )
    assert client.writes[0][:2] == ("PATCH", "/webhooks/hook-id")
    assert client.writes[0][2]["payload"]["serverFunctionId"] == "handler-id"
    remote["serverFunctionId"] = "handler-id"
    client, _, summary = sync_webhook(
        settings, webhook_data(serverFunction=reference), remote
    )
    assert not client.writes
    client, _, _ = sync_webhook(settings, webhook_data(serverFunction=None), remote)
    assert client.writes[0][2]["payload"]["serverFunctionId"] is None


HANDLER = {"functionContext": "example.demo", "functionName": "handler"}


def hook_trigger(destination=HANDLER, **overrides):
    return {
        "name": "example_hook",
        "source": {"webhookContext": "example.demo", "webhookName": "hook"},
        "destination": destination,
        **overrides,
    }


@pytest.mark.parametrize(
    "trigger",
    [
        hook_trigger(),
        hook_trigger(
            {"functionContext": "example.demo", "functionName": "other"},
            waitForResponse=False,
        ),
    ],
)
@pytest.mark.parametrize("types", [None, ("webhooks",), ("triggers",)])
def test_bound_webhook_with_local_trigger_fails_preflight(settings, trigger, types):
    config, args, logger, _ = settings
    artefact(config, "webhooks", webhook_data(serverFunction=HANDLER), name="hook")
    artefact(config, "triggers", trigger, name="hook_trigger")
    if types is not None:
        config = replace(config, enabled_types=types)
    with pytest.raises(flow.DeployError) as error:
        flow.validate_local_artifacts(config, args, logger)
    message = str(error.value)
    assert "Webhook example.demo.hook" in message
    assert "workflows/src/demo/artefacts/webhooks/hook.json" in message
    assert "workflows/src/demo/artefacts/triggers/hook_trigger.json" in message
    assert "replaces its triggers" in message
    assert "Remove the trigger" in message and "serverFunction binding" in message


def test_bound_webhook_conflict_is_not_skipped_by_skip_invalid(settings):
    config, args, _, _ = settings
    artefact(config, "webhooks", webhook_data(serverFunction=HANDLER), name="hook")
    artefact(config, "triggers", hook_trigger(), name="hook_trigger")
    with pytest.raises(flow.DeployError, match="replaces its triggers"):
        flow.validate_local_artifacts(config, replace(args, skip_invalid=True), Mock())


@pytest.mark.parametrize(
    "webhook, trigger_source",
    [
        (webhook_data(serverFunction=None), "hook"),
        (webhook_data(), "hook"),
        (webhook_data(serverFunction=HANDLER), "another_hook"),
    ],
)
def test_unbound_or_unrelated_webhooks_keep_their_triggers(
    settings, webhook, trigger_source
):
    config, args, logger, _ = settings
    artefact(config, "webhooks", webhook, name="hook")
    trigger = hook_trigger()
    trigger["source"]["webhookName"] = trigger_source
    artefact(config, "triggers", trigger, name="hook_trigger")
    assert flow.find_webhook_binding_trigger_conflicts(config, args) == []


def test_binding_conflict_ignores_unrelated_selection(settings):
    config, args, _, _ = settings
    artefact(config, "webhooks", webhook_data(serverFunction=HANDLER), name="hook")
    artefact(config, "triggers", hook_trigger(), name="hook_trigger")
    assert not flow.find_webhook_binding_trigger_conflicts(
        replace(config, enabled_types=("functions", "variables")), args
    )
    other = config.repo_root / "workflows/src/other"
    other.mkdir(parents=True)
    assert not flow.find_webhook_binding_trigger_conflicts(
        config, replace(args, path_filters=(other,))
    )
    trigger_dir = config.repo_root / "workflows/src/demo/artefacts/triggers"
    assert flow.find_webhook_binding_trigger_conflicts(
        config, replace(args, path_filters=(trigger_dir,))
    )


def test_sync_webhooks_no_longer_warns_of_double_execution(settings):
    _, warnings, _ = sync_webhook(
        settings,
        webhook_data(serverFunction=HANDLER),
        triggers=[hook_trigger()],
        dry_run=True,
    )
    assert not any("twice" in str(call) for call in warnings.call_args_list)


def test_adopted_webhook_keeps_its_server_function_portable():
    document = flow.build_local_webhook_document(
        {**webhook_data(), "serverFunctionId": "handler-id"}, webhook_registry()
    )
    assert document["serverFunction"] == {
        "functionContext": "example.demo",
        "functionName": "handler",
    }


def existing_subscription_client(detail):
    client = subscription_client(
        [{"id": "old", "name": "events", "context": "example.demo"}]
    )
    client.responses["/subscriptions/graphql/old"] = detail
    return client


def test_unchanged_subscription_is_skipped(settings):
    config, args, logger, summary = settings
    artefact(config, "subscriptions", subscription())
    client = existing_subscription_client(subscription_detail())
    flow.sync_subscriptions(client, config, args, logger, summary)
    assert not client.writes
    assert summary.subscriptions.skipped == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_non_structural_subscription_changes_patch_in_place(
    settings, monkeypatch, dry_run
):
    config, args, logger, summary = settings
    artefact(
        config,
        "subscriptions",
        subscription(
            enabled=True,
            description="Events",
            functionParams={"mode": "fast"},
            eventInactivityThresholdMs=120000,
            ohipMaintainOffset=False,
        ),
    )
    client = existing_subscription_client(subscription_detail())
    confirm = Mock()
    monkeypatch.setattr(flow, "confirm_subscription_recreate", confirm)
    flow.sync_subscriptions(
        client, config, replace(args, dry_run=dry_run), logger, summary
    )
    confirm.assert_not_called()
    assert summary.subscriptions.updated == 1
    if dry_run:
        assert not client.writes
    else:
        assert client.writes == [
            (
                "PATCH",
                "/subscriptions/graphql/old",
                {
                    "payload": {
                        "description": "Events",
                        "enabled": True,
                        "eventInactivityThresholdMs": 120000,
                        "functionParams": {"mode": "fast"},
                    }
                },
            )
        ]


def test_destination_change_still_requires_confirmed_recreation(settings):
    config, args, logger, summary = settings
    artefact(config, "subscriptions", subscription(enabled=True))
    client = existing_subscription_client(subscription_detail(functionId="previous"))
    flow.sync_subscriptions(client, config, args, logger, summary)
    assert not client.writes
    assert summary.subscriptions.blocked == 1


def test_forced_subscription_without_structural_change_patches_params(settings):
    config, args, logger, summary = settings
    data = subscription(
        paramsObject={
            "token": {
                "type": "PolyVariable",
                "variableName": "token",
                "variableContext": "example.demo",
            }
        }
    )
    artefact(config, "subscriptions", data)
    client = existing_subscription_client(subscription_detail())
    flow.sync_subscriptions(client, config, replace(args, force=True), logger, summary)
    method, path, options = client.writes[0]
    assert (method, path) == ("PATCH", "/subscriptions/graphql/old")
    assert options["payload"]["paramsObject"] == {
        "token": {"type": "PolyVariable", "id": "secret"}
    }
    assert options["payload"]["enabled"] is False
    assert len(client.writes) == 1


def test_subscription_offset_selection_is_never_patched():
    payload = {"type": "OHIP", "ohipOffsetSelection": "SPECIFIC", "ohipOffset": "1"}
    structural, changes = flow.plan_subscription_update(
        payload, subscription_detail(type="OHIP"), force=True
    )
    assert structural == []
    assert changes == {}


def test_ai_twilio_token_variable_resolves_secret_and_null(settings):
    config, *_ = settings
    variables = [
        {
            "id": "twilio",
            "name": "twilio",
            "context": "example.demo",
            "secrecy": "SECRET",
        }
    ]
    reference = {"variableName": "twilio", "variableContext": "example.demo"}
    payload = flow.resolve_ai_payload(
        ai_function(
            type="VOICE", twilioAuthTokenVariable=reference, voiceInitialMessage=None
        ),
        [],
        variables,
        config,
    )
    assert payload["twilioAuthTokenVariableId"] == "twilio"
    assert payload["voiceInitialMessage"] is None
    assert "twilioAuthTokenVariable" not in payload
    payload = flow.resolve_ai_payload(
        ai_function(twilioAuthTokenVariable=None), [], variables, config
    )
    assert payload["twilioAuthTokenVariableId"] is None
    flow.validate_endpoint_payload(
        "UpdateAiFunctionV2Dto",
        {"twilioAuthTokenVariableId": None, "voiceInitialMessage": None},
    )


@pytest.mark.parametrize(
    "token, variables, message",
    [
        (
            {"variableName": "twilio", "variableContext": "example.demo"},
            [
                {
                    "id": "v",
                    "name": "twilio",
                    "context": "example.demo",
                    "secrecy": "NONE",
                }
            ],
            "SECRET",
        ),
        ({"name": "twilio", "context": "example.demo"}, [], "variableName"),
        ("twilio-id", [], "variableName"),
    ],
)
def test_ai_twilio_token_variable_rejects_invalid_references(
    settings, token, variables, message
):
    config, *_ = settings
    with pytest.raises(flow.DeployError, match=message):
        flow.resolve_ai_payload(
            ai_function(type="VOICE", twilioAuthTokenVariable=token),
            [],
            variables,
            config,
        )


def test_ai_twilio_token_variable_requires_a_voice_function(settings):
    config, *_ = settings
    reference = {"variableName": "twilio", "variableContext": "example.demo"}
    with pytest.raises(flow.DeployError, match="VOICE"):
        flow.resolve_ai_payload(
            ai_function(type="TEXT", twilioAuthTokenVariable=reference), [], [], config
        )


def test_ai_twilio_token_accepts_legacy_secret_flag(settings):
    config, *_ = settings
    reference = {"variableName": "twilio", "variableContext": "example.demo"}
    for secret, valid in ((True, True), (False, False)):
        variables = [
            {"id": "v", "name": "twilio", "context": "example.demo", "secret": secret}
        ]
        data = ai_function(type="VOICE", twilioAuthTokenVariable=reference)
        if valid:
            flow.resolve_ai_payload(data, [], variables, config)
        else:
            with pytest.raises(flow.DeployError, match="SECRET"):
                flow.resolve_ai_payload(data, [], variables, config)


OUTPUT_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}
ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"const": "failed"},
        "message": {"type": "string"},
    },
    "required": ["status", "message"],
}


def test_ai_error_schema_matching_the_platform_rule_is_accepted(settings):
    config, *_ = settings
    flow.resolve_ai_payload(
        ai_function(outputSchema=OUTPUT_SCHEMA, errorSchema=ERROR_SCHEMA),
        [],
        [],
        config,
    )
    enum_status = copy.deepcopy(ERROR_SCHEMA)
    enum_status["properties"]["status"] = {"enum": ["failed"]}
    flow.resolve_ai_payload(
        ai_function(outputSchema=OUTPUT_SCHEMA, errorSchema=enum_status), [], [], config
    )


@pytest.mark.parametrize(
    "output, change, message",
    [
        (None, {}, "requires outputSchema"),
        (OUTPUT_SCHEMA, {"required": ["message"]}, "status"),
        (OUTPUT_SCHEMA, {"properties": {"message": {"type": "string"}}}, "status"),
        (OUTPUT_SCHEMA, {"required": ["status"]}, "message"),
        (
            OUTPUT_SCHEMA,
            {
                "properties": {
                    "status": {"const": "failed"},
                    "message": {"type": "number"},
                }
            },
            "message",
        ),
    ],
)
def test_ai_error_schema_rule_fails_locally(settings, output, change, message):
    config, *_ = settings
    schema = {**copy.deepcopy(ERROR_SCHEMA), **change}
    with pytest.raises(flow.DeployError, match=message):
        flow.resolve_ai_payload(
            ai_function(outputSchema=output, errorSchema=schema), [], [], config
        )


def test_ai_raw_twilio_token_id_is_not_portable(settings):
    config, *_ = settings
    with pytest.raises(flow.DeployError, match="portable"):
        flow.resolve_ai_payload(
            ai_function(twilioAuthTokenVariableId="raw"), [], [], config
        )


def test_ai_twilio_token_preflight_uses_placeholder_reference(settings):
    config, *_ = settings
    data = ai_function(
        type="VOICE",
        twilioAuthTokenVariable={
            "variableName": "twilio",
            "variableContext": "example.demo",
        },
    )
    path = artefact(config, "ai-functions", data)
    flow.validate_local_artifact("ai-functions", path, data, config)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"answer": {"type": "string"}}},
        {"properties": {"answer": {"type": "string"}}},
        {
            "$ref": "#/definitions/Answer",
            "definitions": {"Answer": {"type": "object", "properties": {}}},
        },
        None,
    ],
)
def test_ai_output_schema_with_object_root_is_accepted(settings, schema):
    config, *_ = settings
    flow.resolve_ai_payload(ai_function(outputSchema=schema), [], [], config)


@pytest.mark.parametrize("key", ["outputSchema", "errorSchema"])
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string"},
        {"anyOf": [{"type": "object"}, {"type": "string"}]},
        {"enum": ["a", "b"]},
        {"$ref": "#/$defs/Items", "$defs": {"Items": {"type": "array"}}},
        ["not", "a", "schema"],
    ],
)
def test_ai_schema_without_object_root_fails_locally(settings, key, schema):
    config, *_ = settings
    data = ai_function(**{key: schema})
    if key == "errorSchema":
        data["outputSchema"] = OUTPUT_SCHEMA
    with pytest.raises(flow.DeployError, match=f"{key} must describe an object"):
        flow.resolve_ai_payload(data, [], [], config)


def mcp_widget_server(widget):
    return {
        "name": "tools",
        "toolFunctions": [
            {
                "functionName": "lookup",
                "functionContext": "example.demo",
                "widget": widget,
            }
        ],
    }


def test_mcp_widget_settings_resolve_to_variable_ids(settings):
    config, *_ = settings
    functions = [{"id": "lookup", "name": "lookup", "context": "example.demo"}]
    variables = [{"id": "theme-id", "name": "theme", "context": "example.demo"}]
    data = mcp_widget_server(
        {
            "enabled": True,
            "settings": [
                {
                    "key": "theme",
                    "variable": {
                        "variableName": "theme",
                        "variableContext": "example.demo",
                    },
                }
            ],
        }
    )
    payload = flow.resolve_mcp_payload(data, functions, variables, config)
    assert payload["toolFunctions"][0]["widget"] == {
        "enabled": True,
        "settings": [{"key": "theme", "variableId": "theme-id"}],
    }
    disabled = flow.resolve_mcp_payload(
        mcp_widget_server({"enabled": False}), functions, variables, config
    )
    assert disabled["toolFunctions"][0]["widget"] == {"enabled": False}
    assert flow.mcp_variable_references(data) == [
        {"variableName": "theme", "variableContext": "example.demo"}
    ]


@pytest.mark.parametrize(
    "widget",
    [
        {"settings": []},
        {"enabled": "yes"},
        {"enabled": True, "settings": {}},
        {"enabled": True, "settings": [{"key": "theme", "variableId": "raw"}]},
        {"enabled": True, "settings": [{"key": "theme", "variable": {"name": "x"}}]},
        {"enabled": True, "extra": 1},
        {
            "enabled": True,
            "settings": [
                {
                    "key": "theme",
                    "variable": {
                        "variableName": "theme",
                        "variableContext": "example.demo",
                    },
                },
                {
                    "key": "theme",
                    "variable": {
                        "variableName": "theme",
                        "variableContext": "example.demo",
                    },
                },
            ],
        },
    ],
)
def test_mcp_widget_rejects_invalid_shapes(settings, widget):
    config, *_ = settings
    functions = [{"id": "lookup", "name": "lookup", "context": "example.demo"}]
    variables = [{"id": "theme-id", "name": "theme", "context": "example.demo"}]
    with pytest.raises(flow.DeployError, match="widget"):
        flow.resolve_mcp_payload(
            mcp_widget_server(widget), functions, variables, config
        )


def test_mcp_widget_variables_must_be_namespace_owned(settings):
    config, args, logger, _ = settings
    artefact(
        config,
        "mcp-servers",
        mcp_widget_server(
            {
                "enabled": True,
                "settings": [
                    {
                        "key": "theme",
                        "variable": {
                            "variableName": "theme",
                            "variableContext": "other.demo",
                        },
                    }
                ],
            }
        ),
    )
    with pytest.raises(flow.DeployError):
        flow.validate_local_namespace(config, args, logger)


def canopy_public(**overrides):
    return {
        "name": "Demo",
        "subpath": "example-demo",
        "visibility": "PUBLIC",
        "publicAccess": True,
        "collections": [],
        **overrides,
    }


@pytest.mark.parametrize(
    "data",
    [
        canopy_public(visibility="ENVIRONMENT"),
        {key: value for key, value in canopy_public().items() if key != "visibility"},
        canopy_public(publicAccess="true"),
    ],
)
def test_canopy_public_access_requires_public_visibility(settings, data):
    config, *_ = settings
    with pytest.raises(flow.DeployError, match="publicAccess"):
        flow.validate_canopy(data, config)


def canopy_client(remote_config, visibility="PUBLIC"):
    return FakeClient(
        {
            "/applications": [
                {
                    "id": "app",
                    "name": "Demo",
                    "subpath": "example-demo",
                    "visibility": visibility,
                }
            ],
            "/applications/app/config": remote_config,
        }
    )


def test_canopy_newly_enabling_public_access_is_skipped_without_a_terminal(
    settings, monkeypatch
):
    config, args, logger, summary = settings
    artefact(config, "canopy", canopy_public())
    client = canopy_client(
        {"name": "Demo", "subpath": "example-demo", "collections": []}
    )
    monkeypatch.setattr(flow.sys.stdin, "isatty", lambda: False)
    prompt = Mock()
    monkeypatch.setattr("builtins.input", prompt)
    flow.sync_canopy(client, config, args, logger, summary)
    prompt.assert_not_called()
    assert not client.writes
    assert summary.canopy.blocked == 1


@pytest.mark.parametrize("answer, writes", [("y", 1), ("", 0), ("no", 0)])
def test_canopy_newly_enabling_public_access_asks_first(
    settings, monkeypatch, answer, writes
):
    config, args, logger, summary = settings
    artefact(config, "canopy", canopy_public())
    client = canopy_client(
        {"name": "Demo", "subpath": "example-demo", "collections": []}
    )
    monkeypatch.setattr(flow.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(flow.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", Mock(return_value=answer))
    flow.sync_canopy(client, config, args, logger, summary)
    assert len(client.writes) == writes
    if writes:
        assert client.writes[0][:2] == ("PUT", "/applications/app/config")
        assert client.writes[0][2]["payload"]["publicAccess"] is True


def test_canopy_public_access_already_enabled_needs_no_prompt(settings, monkeypatch):
    config, args, logger, summary = settings
    artefact(config, "canopy", canopy_public(icon="new"))
    client = canopy_client(
        {
            "name": "Demo",
            "subpath": "example-demo",
            "collections": [],
            "publicAccess": True,
        }
    )
    prompt = Mock()
    monkeypatch.setattr("builtins.input", prompt)
    flow.sync_canopy(client, config, args, logger, summary)
    prompt.assert_not_called()
    assert client.writes[0][:2] == ("PUT", "/applications/app/config")


def test_canopy_public_access_on_a_non_public_application_fails(settings):
    config, args, logger, summary = settings
    artefact(config, "canopy", canopy_public())
    client = canopy_client({"collections": []}, visibility="ENVIRONMENT")
    with pytest.raises(flow.DeployError, match="PUBLIC application"):
        flow.sync_canopy(client, config, args, logger, summary)
    assert not client.writes


def test_canopy_public_access_dry_run_warns_without_prompting(settings, monkeypatch):
    config, args, logger, summary = settings
    artefact(config, "canopy", canopy_public())
    client = FakeClient({"/applications": []})
    prompt = Mock()
    monkeypatch.setattr("builtins.input", prompt)
    flow.sync_canopy(client, config, replace(args, dry_run=True), logger, summary)
    prompt.assert_not_called()
    assert not client.writes
    assert summary.canopy.created == 1


@pytest.mark.parametrize("pattern", ["example.*", "example.**"])
def test_generate_context_patterns_pass_through(settings, pattern):
    config, args, logger, _ = settings
    config = replace(config, generate_contexts=(pattern,))
    assert flow.resolve_poly_generate_contexts(config, args, logger) == (pattern,)


def test_generate_context_patterns_cannot_bypass_excluded_contexts(settings):
    config, args, logger, _ = settings
    config = replace(
        config,
        generate_contexts=("example.**",),
        orphan_excluded_contexts=("example.private",),
    )
    with pytest.raises(flow.DeployError, match="patterns"):
        flow.resolve_poly_generate_contexts(config, args, logger)
