from __future__ import annotations

from dataclasses import replace
import copy
import ast
import json

import pytest

from cli import flow


class FakePlatform:
    def __init__(self):
        self.functions = {}
        self.writes = []
        self.fail = None
        self.shared_specs = []

    def request(self, method, path, **kwargs):
        if method == "GET" and path == "/specs":
            return copy.deepcopy(self.shared_specs) + [
                dict(
                    payload,
                    id=key,
                    type="customFunction" if kind == "client" else "serverFunction",
                )
                for key, (kind, payload) in self.functions.items()
            ]
        kind = path.split("/")[2]
        if method == "GET" and path.count("/") == 2:
            return [
                dict(payload, id=key)
                for key, (item_kind, payload) in self.functions.items()
                if item_kind == kind
            ]
        if method == "GET":
            key = path.split("/")[-1]
            return dict(self.functions[key][1], id=key)
        assert method == "POST"
        payload = copy.deepcopy(kwargs["payload"])
        key = f"{payload['context']}.{payload['name']}"
        if key == self.fail:
            raise flow.DeployError("synthetic deployment failure")
        self.writes.append(key)
        self.functions[key] = (kind, payload)
        return {"id": key}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    monkeypatch.setattr(flow, "get_execution_api_key", lambda: None)
    args = flow.parse_args(["push-dynamic", "--types", "functions"])
    config = replace(flow.build_config(tmp_path, args), use_mtime_guard=False)
    logger = flow.Logger(False)
    files = {}

    def add(name, kind, calls=()):
        path = tmp_path / "workflows" / "src" / name / kind / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        declaration = "PolyClientFunction" if kind == "client" else "PolyServerFunction"
        source = (
            f"from polyapi import poly\nfrom polyapi.typedefs import {declaration}\n"
        )
        source += (
            f'polyConfig: {declaration} = {{"name": "{name}", "context": "example"}}\n'
        )
        source += f"async def {name}() -> bool:\n"
        source += "".join(f"    await poly.example.{call}()\n" for call in calls)
        source += "    return True\n"
        path.write_text(source)
        files[name] = path

    add("helper", "client")
    add("middle", "client", ["helper"])
    add("worker", "server", ["middle"])
    add("other_worker", "server", ["helper", "middle"])
    add("http_caller", "server", ["worker_async"])
    add("unrelated", "client")
    specs = flow.load_function_specs(config, args, logger)
    contexts = flow.build_local_func_context(specs)
    client = FakePlatform()
    for spec in specs:
        payload = flow.build_function_payload(spec, contexts)
        client.functions[f"example.{spec.name}"] = (spec.function_type, payload)
    client.functions["example.helper"][1]["code"] = client.functions["example.helper"][
        1
    ]["code"].replace("return True", "return False")
    state = flow.ResumeState(tmp_path, False, target=("https://unit.invalid", "unit"))
    return config, args, logger, client, state, files


def plan(project, **options):
    config, args, logger, client, state, _ = project
    return flow.plan_dynamic_functions(
        client, config, replace(args, **options), logger, state
    )


def execute(project, prepared, **options):
    config, args, logger, client, state, _ = project
    summary = flow.Summary()
    flow.execute_dynamic_functions(
        prepared, client, config, replace(args, **options), logger, summary, state
    )
    return summary


def test_native_sdk_nested_payloads_and_deployment_order(project):
    prepared = plan(project)
    assert set(prepared.order) == {
        "example.helper",
        "example.middle",
        "example.worker",
        "example.other_worker",
    }
    assert (
        prepared.order.index("example.helper")
        < prepared.order.index("example.middle")
        < prepared.order.index("example.worker")
    )
    assert prepared.payloads["example.worker"]["generateContexts"] == ["example"]
    assert prepared.payloads["example.worker"]["returnType"] == "boolean"
    summary = execute(project, prepared)
    assert project[3].writes == prepared.order
    assert summary.functions.updated == 4
    assert not plan(project).order


def test_scoped_client_selection_still_rebuilds_servers(project):
    prepared = plan(
        project, path_filters=(project[5]["helper"],), function_types=("client",)
    )
    assert set(prepared.order) == {
        "example.helper",
        "example.middle",
        "example.worker",
        "example.other_worker",
    }


def test_changed_intermediate_promoted_and_metadata_only_seed(project):
    project[3].functions["example.middle"][1]["visibility"] = "TENANT"
    prepared = plan(project, path_filters=(project[5]["helper"],))
    assert prepared.seeds == {"example.helper", "example.middle"}


def test_excluded_consumer_aborts_before_writes(project):
    config, args, logger, client, state, files = project
    with pytest.raises(flow.DeployError, match="excluded function"):
        flow.plan_dynamic_functions(
            client, replace(config, skip_paths=(files["worker"],)), args, logger, state
        )
    assert client.writes == []


def test_out_of_scope_invalid_consumer_aborts_before_writes(project):
    files = project[5]
    files["worker"].write_text(
        "from missing_runtime_dependency import missing\n" + files["worker"].read_text()
    )
    with pytest.raises(flow.DeployError, match="Invalid dependency function"):
        plan(project, path_filters=(files["helper"],))
    assert project[3].writes == []


def test_newer_changed_dependency_blocks_entire_plan(project):
    config, args, logger, client, state, _ = project
    client.functions["example.helper"][1]["updatedAt"] = "2099-01-01T00:00:00Z"
    with pytest.raises(flow.DeployError, match="newer remote"):
        flow.plan_dynamic_functions(
            client, replace(config, use_mtime_guard=True), args, logger, state
        )
    assert client.writes == []


def test_unchanged_consumers_ignore_mtime_guard(project):
    config, args, logger, client, state, _ = project
    client.functions["example.worker"][1]["updatedAt"] = "2099-01-01T00:00:00Z"
    prepared = flow.plan_dynamic_functions(
        client, replace(config, use_mtime_guard=True), args, logger, state
    )
    assert "example.worker" in prepared.order


def test_failure_stops_and_resume_retains_unchanged_consumers(project):
    config, args, logger, client, state, _ = project
    prepared = plan(project)
    client.fail = "example.middle"
    summary = execute(project, prepared)
    assert summary.functions.failed == 1
    assert client.writes == ["example.helper"]
    reloaded = flow.ResumeState(
        config.repo_root, True, target=("https://unit.invalid", "unit")
    )
    client.fail = None
    resumed = flow.plan_dynamic_functions(
        client, config, replace(args, resume=True), logger, reloaded
    )
    assert set(resumed.order) == {
        "example.middle",
        "example.worker",
        "example.other_worker",
    }
    flow.execute_dynamic_functions(
        resumed, client, config, args, logger, flow.Summary(), reloaded
    )
    assert not reloaded.pending_functions
    assert len(client.writes) == 4


def test_dry_run_has_no_writes_or_resume_state_changes(project):
    state = project[4]
    state.mark_done("existing", "fingerprint")
    before = state._path.read_bytes()
    prepared = plan(project)
    execute(project, prepared, dry_run=True)
    assert state._path.read_bytes() == before
    assert project[3].writes == []


def test_explicit_seed_forces_unchanged_tree_and_skip_intermediate(project):
    prepared = plan(project, seeds=("example.helper",), skip_unchanged_cfx=True)
    assert set(prepared.order) == {
        "example.helper",
        "example.worker",
        "example.other_worker",
    }


@pytest.mark.parametrize("direct", [True, False])
@pytest.mark.parametrize("excluded_contexts", [(), ("OOB.blocked",)])
def test_client_name_ending_async_keeps_transitive_sdk_contexts(
    project, direct, excluded_contexts
):
    config, args, logger, client, state, files = project
    files["helper"].write_text(
        "from polyapi import vari\n"
        "from polyapi.typedefs import PolyClientFunction\n"
        'polyConfig: PolyClientFunction = {"name": "helper_async", "context": "example"}\n'
        "async def helper_async() -> bool:\n"
        "    await vari.OOB.provider.settings.get_async()\n"
        "    return True\n"
    )
    consumer = "worker" if direct else "middle"
    old_call = "example.middle()" if direct else "example.helper()"
    files[consumer].write_text(
        files[consumer].read_text().replace(old_call, "example.helper_async()")
    )
    prepared = flow.plan_dynamic_functions(
        client,
        replace(config, orphan_excluded_contexts=excluded_contexts),
        replace(args, seeds=("example.helper_async",)),
        logger,
        state,
    )
    assert "example.worker" in prepared.order
    assert set(prepared.payloads["example.worker"]["generateContexts"]) == {
        "example",
        "OOB.provider",
    }
    assert client.writes == []


@pytest.mark.parametrize(
    "flags",
    [
        ["--skip-invalid"],
        ["--adopt-orphans"],
        ["--types", "variables"],
        ["example.helper", "--delete-orphans"],
    ],
)
def test_dynamic_rejects_partial_or_destructive_options(flags):
    with pytest.raises(flow.DeployError):
        flow.parse_args(["push-dynamic", *flags])


def test_named_seeds_default_to_functions_only():
    args = flow.parse_args(
        ["push-dynamic", "example.helper", "--seeds", "example.middle,example.worker"]
    )
    assert args.dynamic
    assert args.enabled_types == ("functions",)
    assert args.seeds == ("example.helper", "example.middle", "example.worker")


def test_pending_tree_cannot_be_discarded_by_normal_or_other_target_run(project):
    config, args, logger, client, state, _ = project
    state.plan_functions({"fn:server:example:worker"})
    with pytest.raises(flow.DeployError, match="--resume"):
        flow.ResumeState(
            config.repo_root, False, target=("https://unit.invalid", "unit")
        )
    with pytest.raises(flow.DeployError, match="another target"):
        flow.ResumeState(
            config.repo_root, True, target=("https://other.invalid", "unit")
        )
    with pytest.raises(flow.DeployError, match="push-dynamic"):
        flow.sync_functions(
            client=client,
            config=config,
            cli_args=replace(args, dynamic=False),
            logger=logger,
            summary=flow.Summary(),
            resume_state=state,
        )
    assert client.writes == []


def test_changed_local_source_on_resume_rebuilds_already_completed_consumer(project):
    config, args, logger, client, state, files = project
    client.fail = "example.worker"
    execute(project, plan(project))
    assert "example.other_worker" in client.writes
    files["helper"].write_text(
        files["helper"].read_text().replace("return True", "return bool(1)")
    )
    resumed = flow.ResumeState(
        config.repo_root, True, target=("https://unit.invalid", "unit")
    )
    prepared = flow.plan_dynamic_functions(client, config, args, logger, resumed)
    assert {"example.helper", "example.other_worker", "example.worker"} <= set(
        prepared.order
    )


def test_dynamic_plan_confirmation_and_large_server_count(project, monkeypatch):
    config, args, _, _, _, _ = project
    prepared = plan(project)
    monkeypatch.setattr(flow.sys.stdin, "isatty", lambda: False)
    with pytest.raises(flow.DeployError, match="--yes"):
        flow.confirm_dynamic_plan(prepared, config, args)
    flow.confirm_dynamic_plan(prepared, config, replace(args, dry_run=True))
    flow.confirm_dynamic_plan(prepared, config, replace(args, yes=True))
    monkeypatch.setattr(flow.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(flow.sys.stdout, "isatty", lambda: True)
    responses = iter(["yes", "wrong"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    with pytest.raises(flow.DeployError, match="cancelled"):
        flow.confirm_dynamic_plan(prepared, config, replace(args, max_leaves=1))
    responses = iter(["yes", "2"])
    flow.confirm_dynamic_plan(prepared, config, replace(args, max_leaves=1))


@pytest.mark.parametrize("failure", ["invalid", "deploy", "resource", "none"])
@pytest.mark.parametrize(
    "selection, json_output",
    [
        ("scoped", False),
        ("scoped", True),
        ("seed_remote", False),
        ("seed_remote", True),
        ("seed_specs", False),
    ],
)
def test_main_plans_before_any_write_and_stops_pipeline_on_failure(
    project, monkeypatch, capsys, failure, json_output, selection
):
    from unittest.mock import Mock

    config, args, logger, client, state, files = project
    options = (
        ["--types", "functions,variables", "--paths", str(files["helper"])]
        if selection == "scoped"
        else [
            "example.helper",
            "--seed-from",
            "specs" if selection == "seed_specs" else "remote",
        ]
    )
    if selection == "seed_specs":
        install_specs(monkeypatch, synthetic_specs(project))
    monkeypatch.setenv("POLY_DEPLOY_ENABLED_TYPES", "functions,variables")
    args = flow.parse_args(
        ["push-dynamic", "--yes", *options] + (["--json"] if json_output else [])
    )
    config = replace(
        config, enabled_types=flow.build_config(config.repo_root, args).enabled_types
    )
    if json_output:
        state.mark_done("fn:client:example:unrelated", "existing-fingerprint")
        resume_before = state._path.read_bytes()
    if failure == "invalid":
        files["worker"].write_text(
            "from missing_runtime_dependency import missing\n"
            + files["worker"].read_text()
        )
    if failure == "deploy":
        client.fail = "example.middle"
    monkeypatch.setattr(flow, "parse_args", lambda: args)
    monkeypatch.setattr(flow, "find_repo_root", lambda: config.repo_root)
    monkeypatch.setattr(flow, "load_env", lambda _: Mock(exists=True))
    monkeypatch.setattr(flow, "get_target_env_id", lambda _: "unit")
    monkeypatch.setattr(flow, "build_config", lambda *_: config)
    monkeypatch.setattr(
        flow, "build_deployment_preflight", lambda *_: Mock(production_sensitive=False)
    )
    for name in (
        "print_startup_banner",
        "print_deployment_visibility",
        "maybe_confirm_branch_env_file_match",
        "enforce_production_safeguards",
        "maybe_prompt_for_missing_env_tokens",
    ):
        monkeypatch.setattr(flow, name, Mock())
    monkeypatch.setattr(flow, "maybe_prompt_for_initial_scope", lambda args, *_: args)
    monkeypatch.setattr(
        flow, "get_api_key_and_url", lambda: ("test-key", "https://unit.invalid")
    )
    monkeypatch.setattr(flow, "PolyApiClient", lambda **_: client)
    monkeypatch.setattr(flow, "enforce_target_environment", Mock())
    variables = Mock()
    if failure == "resource":

        def fail_resource(**kwargs):
            kwargs["summary"].variables.failed += 1

        variables.side_effect = fail_resource
    monkeypatch.setattr(flow, "sync_variables", variables)
    webhook_index = Mock(return_value={})
    monkeypatch.setattr(flow, "build_remote_webhook_index", webhook_index)
    orphan_step = Mock(return_value=False)
    monkeypatch.setattr(flow, "process_orphans", orphan_step)
    generation = Mock()
    monkeypatch.setattr(flow, "run_poly_generate", generation)
    if failure == "invalid":
        message = (
            "Invalid dependency function"
            if selection == "scoped"
            else "Local validation failed"
        )
        with pytest.raises(flow.DeployError, match=message):
            flow.main()
        assert not variables.called
        assert not client.writes
    elif failure == "resource":
        assert flow.main() == 1
        assert variables.called
        assert client.writes == []
        assert not webhook_index.called
        assert not orphan_step.called
        assert not generation.called
    elif failure == "deploy" and not json_output:
        assert flow.main() == 1
        assert variables.called
        assert client.writes == ["example.helper"]
        assert not webhook_index.called
        assert not orphan_step.called
        assert not generation.called
    else:
        assert flow.main() == 0
        assert variables.called
        if json_output:
            assert not client.writes
            assert not generation.called
            assert variables.call_args.kwargs["cli_args"].dry_run
            assert state._path.read_bytes() == resume_before
        else:
            assert len(client.writes) == 4
            assert generation.called
    if json_output:
        import json

        report = json.loads(capsys.readouterr().out)
        assert report["status"] == (
            "failed" if failure in {"invalid", "resource"} else "preview"
        )
        assert state._path.read_bytes() == resume_before
        assert "payloads" not in report
        assert "code" not in report
        assert "executionApiKey" not in report


def test_read_only_resume_state_protects_the_entire_dry_run_pipeline(project):
    config, _, _, _, state, _ = project
    state.plan_functions({"fn:server:example:worker"})
    before = state._path.read_bytes()
    preview = flow.ResumeState(
        config.repo_root, True, target=("https://unit.invalid", "unit"), read_only=True
    )
    preview.mark_done("var:example:config", "synthetic-fingerprint")
    preview.plan_functions({"fn:client:example:helper"})
    preview.clear()
    assert state._path.read_bytes() == before


def test_class_namespace_client_rebuilds_nested_server_consumers(project):
    config, args, logger, client, _, files = project
    files["middle"].write_text(
        "from polyapi import poly\n"
        "from polyapi.typedefs import PolyClientFunction\n"
        'polyConfig: PolyClientFunction = {"name": "middle", "context": "example"}\n'
        "class Calls:\n"
        "    api = poly.example\n"
        "    async def run(self):\n"
        "        return await self.api.helper()\n"
        "async def middle() -> bool:\n"
        "    return await Calls().run()\n"
    )
    specs = flow.load_function_specs(config, args, logger)
    contexts = flow.build_local_func_context(specs)
    middle = next(spec for spec in specs if spec.name == "middle")
    client.functions["example.middle"] = (
        "client",
        flow.build_function_payload(middle, contexts),
    )
    prepared = plan(project)
    assert prepared.seeds == {"example.helper"}
    assert set(prepared.order) == {
        "example.helper",
        "example.middle",
        "example.worker",
        "example.other_worker",
    }
    assert prepared.order.index("example.helper") < prepared.order.index(
        "example.middle"
    )
    assert prepared.order.index("example.middle") < prepared.order.index(
        "example.worker"
    )
    execute(project, prepared)
    assert client.writes == prepared.order


@pytest.mark.parametrize("signature", ["**kwargs", "api=None, /, **kwargs"])
def test_kwargs_namespace_client_rebuilds_nested_server_consumers(project, signature):
    config, args, logger, client, _, files = project
    files["middle"].write_text(
        "from polyapi import poly\n"
        "from polyapi.typedefs import PolyClientFunction\n"
        'polyConfig: PolyClientFunction = {"name": "middle", "context": "example"}\n'
        f"async def invoke({signature}):\n"
        '    return await kwargs["api"].helper()\n'
        "async def middle() -> bool:\n"
        "    return await invoke(api=poly.example)\n"
    )
    specs = flow.load_function_specs(config, args, logger)
    contexts = flow.build_local_func_context(specs)
    middle = next(spec for spec in specs if spec.name == "middle")
    client.functions["example.middle"] = (
        "client",
        flow.build_function_payload(middle, contexts),
    )
    prepared = plan(project)
    assert prepared.seeds == {"example.helper"}
    assert set(prepared.order) == {
        "example.helper",
        "example.middle",
        "example.worker",
        "example.other_worker",
    }
    assert prepared.order.index("example.helper") < prepared.order.index(
        "example.middle"
    )
    assert prepared.order.index("example.middle") < prepared.order.index(
        "example.worker"
    )
    assert client.writes == []
    execute(project, prepared)
    assert client.writes == prepared.order


@pytest.mark.parametrize(
    "forward",
    [
        "async def invoke(api):\n    return await api.helper()\nasync def worker() -> bool:\n    return await invoke(poly.example)\n",
        "async def invoke(**kwargs):\n    return await kwargs['api'].helper()\nasync def worker() -> bool:\n    return await invoke(api=poly.example)\n",
        "class Runner:\n    def __init__(self, api):\n        self.api = api\n    async def call(self):\n        return await self.api.helper()\nasync def worker() -> bool:\n    return await Runner(poly.example).call()\n",
    ],
)
def test_forwarded_client_contexts_and_packages_reach_server(project, forward):
    files = project[5]
    files["helper"].write_text(
        "from dotenv import dotenv_values\nfrom io import StringIO\nfrom polyapi import vari\n"
        "from polyapi.typedefs import PolyClientFunction\n"
        'polyConfig: PolyClientFunction = {"name": "helper", "context": "example"}\n'
        "async def helper() -> bool:\n    await vari.OOB.provider.settings.get_async()\n"
        "    return dotenv_values(stream=StringIO('enabled=true'))['enabled'] == 'true'\n"
    )
    files["worker"].write_text(
        "from polyapi import poly\nfrom polyapi.typedefs import PolyServerFunction\n"
        'polyConfig: PolyServerFunction = {"name": "worker", "context": "example"}\n'
        + forward
    )
    prepared = plan(project, seeds=("example.helper",))
    worker = prepared.payloads["example.worker"]
    assert worker["generateContexts"] == ["example", "OOB.provider"]
    assert worker["requirements"] == ["python-dotenv"]
    assert prepared.payloads["example.other_worker"]["requirements"] == [
        "python-dotenv"
    ]
    # The caller embeds a server proxy, not that server's client dependencies.
    caller = plan(project, seeds=("example.http_caller",)).payloads[
        "example.http_caller"
    ]
    assert caller["generateContexts"] == ["example"]
    assert "requirements" not in caller


@pytest.mark.parametrize(
    "call", ["api.remote_helper()", "api.remote_helper.__call__()"]
)
def test_remote_forwarded_client_packages_use_native_parser_and_dedupe(project, call):
    config, args, logger, client, state, files = project
    files["worker"].write_text(
        "from polyapi import poly\nfrom dotenv import dotenv_values\n"
        "from polyapi.typedefs import PolyServerFunction\n"
        'polyConfig: PolyServerFunction = {"name": "worker", "context": "example"}\n'
        f"async def invoke(api):\n    return await {call}\n"
        "async def worker() -> bool:\n    return await invoke(poly.external)\n"
    )
    client.functions["external.remote_helper"] = (
        "client",
        {
            "context": "external",
            "name": "remote_helper",
            "language": "python",
            "code": "from dotenv import dotenv_values\nfrom polyapi import vari\nasync def remote_helper() -> bool:\n    await vari.OOB.provider.settings.get_async()\n    return True\n",
        },
    )
    payload = plan(project, seeds=("example.worker",)).payloads["example.worker"]
    assert payload["requirements"] == ["python-dotenv"]
    assert set(payload["generateContexts"]) == {"external", "OOB.provider"}


def synthetic_specs(project):
    return [
        {
            "type": "customFunction" if kind == "client" else "serverFunction",
            "context": payload["context"],
            "name": payload["name"],
            "language": "python",
            "code": payload["code"],
        }
        for kind, payload in project[3].functions.values()
    ]


def install_specs(monkeypatch, values):
    from polyapi import generate

    monkeypatch.setattr(generate, "read_cached_specs", lambda: values)


def test_specs_comparison_uses_python_sdk_cache_shape_and_promotes_clients(
    project, monkeypatch
):
    values = synthetic_specs(project)
    next(item for item in values if item["name"] == "middle")["code"] = "old code"
    install_specs(monkeypatch, values)
    prepared = plan(project, seed_from="specs", path_filters=(project[5]["helper"],))
    assert prepared.seeds == {"example.helper", "example.middle"}
    assert "example.http_caller" not in prepared.order
    assert prepared.payloads["example.worker"]["language"] == "python"


def test_explicit_remote_detects_changes_with_named_seeds_and_none_does_not(project):
    prepared = plan(project, seeds=("example.unrelated",), seed_from="remote")
    assert prepared.seeds == {"example.unrelated", "example.helper"}
    assert plan(project, seeds=("example.unrelated",), seed_from="none").order == [
        "example.unrelated"
    ]
    assert plan(project, seed_from="none").order == []


def test_missing_cached_server_code_is_conservative_seed(project, monkeypatch):
    values = synthetic_specs(project)
    next(item for item in values if item["name"] == "http_caller").pop("code")
    install_specs(monkeypatch, values)
    assert "example.http_caller" in plan(project, seed_from="specs").seeds


def test_specs_live_run_still_enforces_remote_timestamp_guard(project, monkeypatch):
    install_specs(monkeypatch, synthetic_specs(project))
    monkeypatch.setattr(flow, "can_update", lambda **_: (False, "newer remote"))
    with pytest.raises(flow.DeployError, match="newer remote"):
        plan(project, seed_from="specs")
    assert not project[3].writes


@pytest.mark.parametrize(
    "options",
    [
        ["--hubs"],
        ["--seed-from", "specs", "--dry-run"],
        ["--seed-from", "specs"],
        ["--seed-from", "none", "example.helper", "--through-sfx"],
        ["--seed-from", "none", "example.helper", "--why", "example.worker"],
        ["--seed-from", "none", "example.helper", "--dry-run", "--through-sfx"],
    ],
)
def test_offline_reports_never_load_credentials_parse_payloads_or_access_network(
    project, monkeypatch, capsys, options
):
    import json

    install_specs(monkeypatch, synthetic_specs(project))
    args = flow.parse_args(["push-dynamic", "--json", *options])
    monkeypatch.setattr(flow, "parse_args", lambda: args)
    monkeypatch.setattr(flow, "find_repo_root", lambda: project[0].repo_root)

    def forbidden(*args, **kwargs):
        pytest.fail("Offline report crossed an effect boundary")

    for name in (
        "load_env",
        "get_api_key_and_url",
        "get_execution_api_key",
        "PolyApiClient",
        "build_function_payload",
        "confirm_dynamic_plan",
        "execute_dynamic_functions",
    ):
        monkeypatch.setattr(flow, name, forbidden)
    assert flow.main() == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["offline"] is True
    assert "Offline report only" in captured.err
    assert not (project[0].repo_root / flow.ResumeState.FILENAME).exists()
    if args.hubs:
        assert report["hubs"][0] == {
            "key": "example.helper",
            "clientCallers": 1,
            "serverLeaves": 2,
        }
    elif args.why:
        assert report["chains"] == [
            ["example.helper", "example.middle", "example.worker"]
        ]
    elif args.through_sfx:
        caller = next(
            node for node in report["nodes"] if node["key"] == "example.http_caller"
        )
        assert caller["deploy"] is False
        assert caller["reason"] == "report-only"
        assert "example.http_caller" not in report["order"]
    else:
        assert report["cacheEnvironment"] == "unknown"
        assert report["seeds"] == ["example.helper"]


@pytest.mark.parametrize("bad", ["--build-schema", "--delete-orphans", "--resume"])
@pytest.mark.parametrize(
    "report",
    [["--hubs"], ["--why", "example.worker"], ["--seed-from", "specs", "--dry-run"]],
)
def test_report_modes_reject_mutating_flags(report, bad):
    with pytest.raises(flow.DeployError, match="cannot"):
        flow.parse_args(["push-dynamic", *report, bad])


def test_cache_errors_fail_closed_and_json_is_not_polluted(
    project, monkeypatch, capsys
):
    import json
    from polyapi import generate

    def missing():
        raise FileNotFoundError()

    monkeypatch.setattr(generate, "read_cached_specs", missing)
    args = flow.parse_args(
        ["push-dynamic", "--seed-from", "specs", "--dry-run", "--json"]
    )
    monkeypatch.setattr(flow, "parse_args", lambda: args)
    monkeypatch.setattr(flow, "find_repo_root", lambda: project[0].repo_root)
    with pytest.raises(flow.DeployError, match="No readable"):
        flow.main()
    assert json.loads(capsys.readouterr().out) == {"status": "failed"}
    install_specs(monkeypatch, [dict(synthetic_specs(project)[0], context=None)])
    with pytest.raises(flow.DeployError, match="Invalid function identity"):
        flow.read_cached_function_specs()


@pytest.mark.parametrize("offline", [False, True])
def test_specs_cache_uses_native_precedence_language_filter_and_root_entries(
    project, monkeypatch, offline
):
    values = synthetic_specs(project)
    helper = next(item for item in values if item["name"] == "helper")
    catalog = [
        dict(helper, code="superseded shared source"),
        *values,
        dict(helper, language="typescript", code="export function helper() {}"),
        dict(helper, context="", name="root_helper"),
    ]
    install_specs(monkeypatch, catalog)
    cached = flow.read_cached_function_specs()
    assert cached[("client", "example.helper")]["code"] == helper["code"]
    assert cached[("client", "root_helper")]["context"] == ""
    config, args, logger, client, state, _ = project
    prepared = flow.plan_dynamic_functions(
        None if offline else client,
        config,
        replace(args, seed_from="specs", dry_run=offline),
        logger,
        state,
    )
    assert prepared.seeds == {"example.helper"}
    assert client.writes == []


@pytest.mark.parametrize(
    "winner_type", ["customFunction", "serverFunction", "apiFunction"]
)
def test_specs_cache_keeps_only_effective_function_kind(monkeypatch, winner_type):
    client = {
        "type": "customFunction",
        "language": "python",
        "context": "shared",
        "name": "helper",
        "code": "old client source",
    }
    winner = dict(client, type=winner_type, code="effective source")
    install_specs(monkeypatch, [client, winner])
    cached = flow.read_cached_function_specs()
    kind = {"customFunction": "client", "serverFunction": "server"}.get(winner_type)
    assert cached == ({(kind, "shared.helper"): winner} if kind else {})


@pytest.mark.parametrize(
    "http_type,http_name",
    [
        ("serverFunction", "helper"),
        ("apiFunction", "helper"),
        ("aiFunction", "helper"),
        ("authFunction", "getToken"),
        ("authFunction", "introspectToken"),
        ("authFunction", "refreshToken"),
        ("authFunction", "revokeToken"),
        ("webhookHandle", "events"),
    ],
)
@pytest.mark.parametrize("context", ["shared", ""])
@pytest.mark.parametrize("suffix", ["", "_async"])
@pytest.mark.parametrize("http_last", [False, True])
def test_sdk_callable_precedence_matches_native_exports_and_cache(
    tmp_path, monkeypatch, http_type, http_name, context, suffix, http_last
):
    from polyapi import generate

    client_name = f"{http_name}{suffix}"
    client = {
        "id": "client-export",
        "type": "customFunction",
        "context": context,
        "name": client_name,
        "language": "python",
        "code": (
            f"async def {client_name}() -> bool:\n"
            '    """client-export"""\n'
            "    return True\n"
        ),
    }
    http = {
        "id": "http-export",
        "type": http_type,
        "context": context,
        "name": http_name,
        "function": {
            "arguments": [],
            "returnType": {"kind": "plain", "value": "boolean"},
        },
    }
    if http_type == "webhookHandle":
        http["function"]["arguments"] = [
            {
                "name": "callback",
                "required": True,
                "type": {
                    "kind": "function",
                    "spec": {"arguments": [], "returnType": {"kind": "void"}},
                },
            },
            {
                "name": "options",
                "required": False,
                "type": {"kind": "plain", "value": "object"},
            },
        ]
    catalog = [client, http] if http_last else [http, client]
    # Generate real SDK modules in isolation, without cache or credential access.
    monkeypatch.setattr(generate, "__file__", str(tmp_path / "generate.py"))
    generate.generate_functions(generate.parse_function_specs(catalog))
    module = ast.parse((tmp_path / "poly" / context / "__init__.py").read_text())
    exported = {}
    for statement in module.body:
        # Native clients are emitted inside an ImportError guard.
        for node in statement.body if isinstance(statement, ast.Try) else [statement]:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                identity = (
                    "client-export"
                    if ast.get_docstring(node) == "client-export"
                    else "http-export"
                )
                exported[f"{context}.{node.name}" if context else node.name] = identity
    single_export = http_name == "getToken" or http_type == "webhookHandle"
    assert len(exported) == (1 if single_export and not suffix else 2)
    indexed = flow.index_sdk_function_specs(catalog, source="synthetic catalog")
    assert {key: item["id"] for key, item in indexed.items()} == exported

    install_specs(monkeypatch, catalog)
    cached = flow.read_cached_function_specs()
    expected = {}
    for item in catalog:
        key = f"{context}.{item['name']}" if context else item["name"]
        kind = {"customFunction": "client", "serverFunction": "server"}.get(
            item["type"]
        )
        if kind and exported[key] == item["id"]:
            expected[(kind, key)] = item
    # Generated wrappers must never masquerade as separately declared functions.
    assert cached == expected


@pytest.mark.parametrize(
    "catalog",
    [
        {},
        [None],
        [{}],
        [{"type": "customFunction", "context": None, "name": "helper"}],
        [{"type": "customFunction", "context": "shared", "name": ""}],
        [
            {
                "type": "customFunction",
                "context": "shared",
                "name": "helper",
                "function": {"arguments": [None]},
            }
        ],
    ],
)
def test_specs_cache_still_rejects_malformed_catalogs(monkeypatch, catalog):
    install_specs(monkeypatch, catalog)
    with pytest.raises(flow.DeployError, match="Invalid"):
        flow.read_cached_function_specs()


@pytest.mark.parametrize("shared_source", [False, True])
def test_live_why_reads_target_without_prompts_mutations_or_resume_changes(
    project, monkeypatch, capsys, shared_source
):
    import json
    from unittest.mock import Mock

    config, _, _, client, state, files = project
    marker = config.repo_root / "shared_source_executed.txt"
    if shared_source:
        files["worker"].write_text(
            files["worker"]
            .read_text()
            .replace("poly.example.middle()", "poly.shared.helper_async()")
        )
        client.shared_specs = [
            {
                "type": "customFunction",
                "language": "python",
                "context": "shared",
                "name": "helper_async",
                "code": (
                    "from polyapi import poly\n"
                    "async def helper_async() -> bool:\n"
                    "    return await poly.shared.leaf()\n"
                ),
            },
            {
                "type": "customFunction",
                "language": "python",
                "context": "shared",
                "name": "leaf",
                "code": (
                    "from pathlib import Path\nimport yaml\n"
                    f"Path({str(marker)!r}).write_text('executed')\n"
                    "async def leaf() -> bool:\n    return True\n"
                ),
            },
        ]
    state.plan_functions({"fn:server:example:worker"})
    resume_before = (config.repo_root / flow.ResumeState.FILENAME).read_bytes()
    args = flow.parse_args(["push-dynamic", "--why", "example.worker", "--json"])
    monkeypatch.setattr(flow, "parse_args", lambda: args)
    monkeypatch.setattr(flow, "find_repo_root", lambda: config.repo_root)
    monkeypatch.setattr(flow, "load_env", lambda _: Mock(exists=True))
    monkeypatch.setattr(flow, "get_target_env_id", lambda _: "unit")
    monkeypatch.setattr(flow, "build_config", lambda *_: config)
    monkeypatch.setattr(
        flow, "build_deployment_preflight", lambda *_: Mock(production_sensitive=False)
    )
    monkeypatch.setattr(
        flow, "get_api_key_and_url", lambda: ("synthetic-key", "https://unit.invalid")
    )
    monkeypatch.setattr(flow, "PolyApiClient", lambda **_: client)
    verified = Mock()
    monkeypatch.setattr(flow, "enforce_target_environment", verified)
    for name in ("print_startup_banner", "print_deployment_visibility"):
        monkeypatch.setattr(flow, name, Mock())

    def forbidden(*args, **kwargs):
        pytest.fail("Explanation attempted a deployment step")

    for name in (
        "maybe_confirm_branch_env_file_match",
        "maybe_prompt_for_initial_scope",
        "confirm_dynamic_plan",
        "execute_dynamic_functions",
        "process_orphans",
        "run_poly_generate",
    ):
        monkeypatch.setattr(flow, name, forbidden)
    assert flow.main() == 0
    assert verified.called
    assert not marker.exists()
    assert not client.writes
    assert (config.repo_root / flow.ResumeState.FILENAME).read_bytes() == resume_before
    report = json.loads(capsys.readouterr().out)
    if not shared_source:
        assert report["chains"] == [
            ["example.helper", "example.middle", "example.worker"]
        ]
    assert report["status"] == "report"
    assert report["offline"] is False


def test_offline_plan_cannot_enter_deployer(project):
    config, args, logger, client, state, _ = project
    args = replace(args, seed_from="none", seeds=("example.helper",))
    prepared = flow.plan_dynamic_functions(None, config, args, logger, state)
    with pytest.raises(flow.DeployError, match="Offline reports cannot be deployed"):
        execute(project, prepared)
    assert not client.writes


def test_json_sdk_generation_routes_subprocess_output_to_stderr(project, monkeypatch):
    from unittest.mock import Mock

    config, args, logger, _, _, _ = project
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(flow.subprocess, "run", run)
    monkeypatch.setattr(flow, "resolve_poly_generate_contexts", lambda *_: ())
    flow.run_poly_generate(config, logger, "test", replace(args, json_output=True))
    assert run.call_args.kwargs["stdout"] is flow.sys.stderr


@pytest.mark.parametrize("offline", [False, True])
def test_cached_multiline_string_change_rebuilds_consumers(
    project, monkeypatch, offline
):
    config, args, logger, client, state, files = project
    old = (
        files["helper"]
        .read_text()
        .replace("-> bool:", "-> str:")
        .replace("return True", 'return """ABC \nDEF"""')
    )
    files["helper"].write_text(old)
    spec = flow.FunctionSpec(
        "helper", "example", "client", files["helper"], files["helper"]
    )
    client.functions["example.helper"] = ("client", flow.build_function_payload(spec))
    install_specs(monkeypatch, synthetic_specs(project))
    files["helper"].write_text(old.replace("ABC \n", "ABC\n"))
    prepared = flow.plan_dynamic_functions(
        None if offline else client,
        config,
        replace(args, seed_from="specs", dry_run=offline),
        logger,
        state,
    )
    assert prepared.seeds == {"example.helper"}
    assert set(prepared.order) == {
        "example.helper",
        "example.middle",
        "example.worker",
        "example.other_worker",
    }


def test_cache_ignores_only_receipt_line_endings_and_file_end_whitespace(project):
    path = project[5]["helper"]
    spec = flow.FunctionSpec("helper", "example", "client", path, path)
    source = path.read_text()
    cached = {("client", "example.helper"): {"code": source}}
    receipt = "# Poly deployed @ 2026-10-21T00:00:00Z - example.helper - https://unit.invalid - abc1234\n"
    assert not flow.cached_function_changed(
        spec, (receipt + source).replace("\n", "\r\n") + " \t\r\n", cached
    )


@pytest.mark.parametrize("seed_from", ["none", "specs"])
@pytest.mark.parametrize("middle_changed", [False, True])
def test_offline_skip_requires_known_comparison(
    project, monkeypatch, capsys, seed_from, middle_changed
):
    config, args, logger, client, state, _ = project
    if middle_changed:
        client.functions["example.middle"][1]["code"] = "old code"
    install_specs(monkeypatch, synthetic_specs(project))
    args = replace(
        args,
        seed_from=seed_from,
        dry_run=True,
        seeds=("example.helper",),
        skip_unchanged_cfx=True,
    )
    offline = flow.plan_dynamic_functions(None, config, args, logger, state)
    captured = capsys.readouterr()
    assert ("example.middle" in offline.order) == (
        seed_from == "none" or middle_changed
    )
    if seed_from == "none":
        assert "not compared" in captured.out
    live = flow.plan_dynamic_functions(client, config, args, logger, state)
    assert ("example.middle" in live.order) == middle_changed
    assert not client.writes


@pytest.mark.parametrize("declaration", ["global", "nonlocal"])
def test_declared_sdk_aliases_rebuild_server_with_native_payload(project, declaration):
    import textwrap

    path = project[5]["worker"]
    header = path.read_text().split("async def worker")[0]
    configure = f"def configure():\n    {declaration} api\n    api = poly.example\n"
    call = "configure()\nreturn await api.middle()\n"
    source = (
        configure + "async def worker() -> bool:\n" + textwrap.indent(call, "    ")
        if declaration == "global"
        else "async def worker() -> bool:\n"
        + textwrap.indent("api = None\n" + configure + call, "    ")
    )
    path.write_text(header + source)
    prepared = plan(project, seeds=("example.helper",))
    assert "example.worker" in prepared.order
    assert prepared.graph.dependencies["example.worker"] == {"example.middle"}
    assert prepared.payloads["example.worker"]["returnType"] == "boolean"
    assert prepared.payloads["example.worker"]["generateContexts"] == ["example"]


@pytest.mark.parametrize("json_output", [False, True])
def test_script_cancellation_keeps_json_stdout_valid(monkeypatch, capsys, json_output):
    import ast
    import json
    from pathlib import Path

    args = flow.parse_args(
        ["push-dynamic", "--hubs"] + (["--json"] if json_output else [])
    )
    monkeypatch.setattr(flow, "parse_args", lambda: args)

    def interrupt(*_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(flow, "run_flow", interrupt)
    # Exercise the actual script handler as well as main's report finalization.
    tree = ast.parse(Path(flow.__file__).read_text())
    entrypoint = ast.Module(body=[tree.body[-1]], type_ignores=[])
    with pytest.raises(SystemExit) as error:
        exec(
            compile(entrypoint, flow.__file__, "exec"),
            {**vars(flow), "__name__": "__main__"},
        )
    assert error.value.code == 130
    captured = capsys.readouterr()
    assert "Cancelled." in captured.err
    if json_output:
        assert json.loads(captured.out) == {"status": "failed"}
    else:
        assert captured.out == ""


@pytest.mark.parametrize(
    "expression",
    [
        "[str(api) for api in (1, 2)]",
        "{str(api) for api in (1, 2)}",
        "{api: str(api) for api in (1, 2)}",
        "all(api > 0 for api in (1, 2))",
    ],
)
def test_comprehension_shadow_keeps_native_server_in_deployment_plan(
    project, expression
):
    path = project[5]["worker"]
    header = path.read_text().split("async def worker")[0]
    path.write_text(
        header + "api = poly.example\nasync def worker() -> bool:\n"
        f"    return bool({expression}) and await api.helper()\n"
    )
    prepared = plan(project, seeds=("example.helper",))
    assert "example.worker" in prepared.order
    assert prepared.graph.dependencies["example.worker"] == {"example.helper"}
    assert prepared.payloads["example.worker"]["returnType"] == "boolean"
    assert prepared.payloads["example.worker"]["generateContexts"] == ["example"]


def test_resume_save_replaces_file_without_truncating_previous_snapshot(tmp_path):
    import json

    target = ("https://unit.invalid", "unit")
    state = flow.ResumeState(tmp_path, False, target=target)
    state.plan_functions({"fn:client:example:helper", "fn:server:example:worker"})
    before = state._path.read_text()
    with state._path.open() as previous_snapshot:
        state.mark_done("fn:client:example:helper", "fingerprint")
        assert previous_snapshot.read() == before
    assert json.loads(state._path.read_text())["pending_functions"] == [
        "fn:server:example:worker"
    ]
    assert not list(tmp_path.glob(".flow_resume.*.tmp"))


@pytest.mark.parametrize("operation", ["fsync", "replace"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_resume_save_failure_keeps_previous_snapshot(
    tmp_path, monkeypatch, operation, failure
):
    target = ("https://unit.invalid", "unit")
    state = flow.ResumeState(tmp_path, False, target=target)
    pending = {"fn:server:example:worker"}
    state.plan_functions(pending)
    before = state._path.read_bytes()

    def fail(*_):
        raise failure("synthetic persistence failure")

    monkeypatch.setattr(flow.os, operation, fail)
    expected = flow.DeployError if failure is OSError else KeyboardInterrupt
    with pytest.raises(expected):
        state.mark_done("fn:server:example:worker", "fingerprint")
    assert state._path.read_bytes() == before
    assert flow.ResumeState(tmp_path, True, target=target).pending_functions == pending
    assert not list(tmp_path.glob(".flow_resume.*.tmp"))


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"[]",
        b"null",
        b"\xff",
        b'{"done": [], "pending_functions": "fn:server:example:worker"}',
        b'{"done": [], "pending_functions": [1]}',
        b'{"done": "invalid"}',
        b'{"done": [], "fingerprints": []}',
        b'{"done": [], "fingerprints": {"key": 1}}',
        b'{"done": [], "target": "invalid"}',
    ],
)
def test_invalid_resume_state_blocks_run_without_overwriting(tmp_path, resume, content):
    path = tmp_path / flow.ResumeState.FILENAME
    path.write_bytes(content)
    with pytest.raises(flow.DeployError, match="Cannot read valid"):
        flow.ResumeState(tmp_path, resume, target=("https://unit.invalid", "unit"))
    assert path.read_bytes() == content


def test_unreadable_resume_state_blocks_run(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / flow.ResumeState.FILENAME
    path.write_text('{"done": []}')
    original = Path.read_text

    def fail_read(current, *args, **kwargs):
        if current == path:
            raise PermissionError("synthetic details must not be exposed")
        return original(current, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(flow.DeployError, match="Cannot read valid") as error:
        flow.ResumeState(tmp_path, False, target=("https://unit.invalid", "unit"))
    assert "synthetic details" not in str(error.value)


def test_failed_resume_save_after_client_deploy_preserves_consumer_rebuilds(
    project, monkeypatch
):
    config, args, logger, client, state, _ = project
    prepared = plan(project)
    original_replace = flow.os.replace

    def fail_after_deploy(*paths):
        if client.writes:
            raise OSError("synthetic interrupted save")
        return original_replace(*paths)

    monkeypatch.setattr(flow.os, "replace", fail_after_deploy)
    summary = execute(project, prepared)
    assert summary.functions.failed == 1
    assert client.writes == ["example.helper"]
    reloaded = flow.ResumeState(
        config.repo_root, True, target=("https://unit.invalid", "unit")
    )
    resumed = flow.plan_dynamic_functions(
        client, config, replace(args, resume=True), logger, reloaded
    )
    assert set(resumed.order) == set(prepared.order)
    monkeypatch.setattr(flow.os, "replace", original_replace)
    flow.execute_dynamic_functions(
        resumed, client, config, args, logger, flow.Summary(), reloaded
    )
    assert not reloaded.pending_functions
    assert client.writes[1:] == resumed.order


def test_failed_initial_resume_save_prevents_deployment(project, monkeypatch):
    prepared = plan(project)

    def fail(*_):
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr(flow.os, "fsync", fail)
    with pytest.raises(flow.DeployError):
        execute(project, prepared)
    assert not project[3].writes
    assert not project[4]._path.exists()


@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_partial_resume_write_keeps_previous_snapshot(
    tmp_path, monkeypatch, operation, failure
):
    target = ("https://unit.invalid", "unit")
    state = flow.ResumeState(tmp_path, False, target=target)
    pending = {"fn:server:example:worker"}
    state.plan_functions(pending)
    before = state._path.read_bytes()
    create_temporary = flow.tempfile.NamedTemporaryFile

    def interrupted_temporary(*args, **kwargs):
        output = create_temporary(*args, **kwargs)
        write = output.write

        def fail(*values):
            if operation == "write":
                write(values[0][:10])
            raise failure("synthetic partial save")

        monkeypatch.setattr(output, operation, fail)
        return output

    monkeypatch.setattr(flow.tempfile, "NamedTemporaryFile", interrupted_temporary)
    expected = flow.DeployError if failure is OSError else KeyboardInterrupt
    with pytest.raises(expected):
        state.mark_done("fn:server:example:worker", "fingerprint")
    assert state._path.read_bytes() == before
    assert flow.ResumeState(tmp_path, True, target=target).pending_functions == pending
    assert not list(tmp_path.glob(".flow_resume.*.tmp"))


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--yes", "--force"],
        ["--skip-validation", flow.SKIP_VALIDATION_ACKNOWLEDGEMENT],
        ["--types", "functions,variables"],
        ["--resume"],
        ["--delete-orphans"],
    ],
)
def test_json_always_implies_dry_run(options):
    args = flow.parse_args(["push-dynamic", "--json", *options])
    assert args.json_output
    assert args.dry_run


def test_json_rejects_schema_refresh_before_running(monkeypatch):
    monkeypatch.setattr(
        flow.sys, "argv", ["flow.py", "push-dynamic", "--json", "--build-schema"]
    )

    def forbidden(*_):
        pytest.fail("JSON schema-refresh rejection must precede the pipeline")

    monkeypatch.setattr(flow, "run_flow", forbidden)
    with pytest.raises(flow.DeployError, match="--json.*--build-schema"):
        flow.main()


@pytest.mark.parametrize(
    "helper",
    [
        "def invoke(transform=lambda api: api):\n    return api.helper()\n",
        "async def invoke(transform=lambda api: api):\n    return await api.helper()\n",
        "invoke = lambda transform=(lambda api: api): api.helper()\n",
    ],
)
def test_default_lambda_scope_preserves_native_consumer_plan(project, helper):
    path = project[5]["worker"]
    header = path.read_text().split("async def worker")[0]
    path.write_text(
        header
        + "api = poly.example\n"
        + helper
        + "async def worker() -> bool:\n    return await invoke()\n"
    )
    prepared = plan(project, seeds=("example.helper",))
    assert "example.worker" in prepared.order
    assert prepared.graph.dependencies["example.worker"] == {"example.helper"}
    payload = prepared.payloads["example.worker"]
    assert payload["returnType"] == "boolean"
    assert payload["generateContexts"] == ["example"]
    assert execute(project, prepared).functions.failed == 0
    assert "example.worker" in project[3].writes


@pytest.mark.parametrize(
    "options, expected_types",
    [
        ([], ("functions",)),
        (["--seed-from", "none"], ("functions",)),
        (["--seed-from", "remote"], None),
        (["--seed-from", "specs"], None),
        (["--seed-from", "remote", "--json"], None),
        (["--seed-from", "specs", "--json"], ("functions",)),
        (["--seed-from", "specs", "--dry-run"], ("functions",)),
        (
            ["--seed-from", "remote", "--types", "functions,variables"],
            ("functions", "variables"),
        ),
    ],
)
def test_named_seed_resource_selection(options, expected_types):
    args = flow.parse_args(["push-dynamic", "example.helper", *options])
    assert args.enabled_types == expected_types


@pytest.mark.parametrize(
    "options, prompted",
    [
        ([], False),
        (["--seed-from", "none"], False),
        (["--seed-from", "remote"], True),
        (["--seed-from", "specs"], True),
        (["--seed-from", "remote", "--types", "functions"], False),
    ],
)
def test_named_seed_scope_prompt_matches_selected_resources(
    project, monkeypatch, options, prompted
):
    from unittest.mock import Mock

    args = flow.parse_args(["push-dynamic", "example.helper", *options])
    monkeypatch.setenv("POLY_DEPLOY_ENABLED_TYPES", "functions,variables")
    config = replace(
        flow.build_config(project[0].repo_root, args), initial_scope_prompt_enabled=True
    )
    scope = project[5]["helper"].parent
    prompt = Mock(return_value=scope)
    monkeypatch.setattr(flow, "prompt_for_initial_scope", prompt)
    monkeypatch.setattr(flow.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(flow.sys.stdout, "isatty", lambda: True)
    result = flow.maybe_prompt_for_initial_scope(args, config, project[2])
    assert prompt.called is prompted
    assert result.path_filters == ((scope,) if prompted else ())
    assert result.seeds == ("example.helper",)


@pytest.mark.parametrize(
    "assignment, expression",
    [
        ("*unused, api = (None, 1, poly.example)", "api.helper()"),
        ("_, *apis = (None, poly.example)", "apis[0].helper()"),
        ("first, *apis, last = (0, poly.example, 1)", "apis[0].helper()"),
        ("_, (*unused, api) = (None, (0, poly.example))", "api.helper()"),
        ("api, = (*[poly.example],)", "api.helper()"),
    ],
)
async def test_starred_unpacking_rebuilds_native_server_consumer(
    project, assignment, expression
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    path = project[5]["worker"]
    header = path.read_text().split("async def worker")[0]
    body = f"{assignment}\nasync def worker() -> bool:\n    return await {expression}\n"
    path.write_text(header + body)
    helper = AsyncMock(return_value=True)
    namespace = {"poly": SimpleNamespace(example=SimpleNamespace(helper=helper))}
    exec(body, namespace)
    assert await namespace["worker"]() is True
    helper.assert_awaited_once()
    prepared = plan(project, seeds=("example.helper",), seed_from="none")
    assert "example.worker" in prepared.order
    assert "example.helper" in prepared.graph.dependencies["example.worker"]
    assert prepared.payloads["example.worker"]["returnType"] == "boolean"
    assert prepared.payloads["example.worker"]["generateContexts"] == ["example"]
    assert execute(project, prepared).functions.failed == 0
    assert "example.worker" in project[3].writes


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        [],
        ["example.helper", "--seed-from", "remote"],
        ["example.helper", "--seed-from", "specs"],
    ],
)
def test_dynamic_env_without_functions_stops_before_effects(
    project, monkeypatch, options, resume
):
    from unittest.mock import Mock

    config, _, _, client, state, _ = project
    state.plan_functions({"fn:server:example:worker"})
    before = state._path.read_bytes()
    args = flow.parse_args(
        ["push-dynamic", "--yes", *options] + (["--resume"] if resume else [])
    )

    def load_env(_):
        # Simulate the selected target file overriding the process environment.
        monkeypatch.setenv("POLY_DEPLOY_ENABLED_TYPES", "variables")
        return Mock(exists=True)

    monkeypatch.setenv("POLY_DEPLOY_ENABLED_TYPES", "functions,variables")
    monkeypatch.setattr(flow, "find_repo_root", lambda: config.repo_root)
    monkeypatch.setattr(flow, "load_env", load_env)
    monkeypatch.setattr(flow, "get_target_env_id", lambda _: "unit")
    preflight = Mock(side_effect=AssertionError("Invalid selection reached preflight"))
    monkeypatch.setattr(flow, "build_deployment_preflight", preflight)
    with pytest.raises(flow.DeployError, match="POLY_DEPLOY_ENABLED_TYPES"):
        flow.run_flow(args, {})
    preflight.assert_not_called()
    assert client.writes == []
    assert state._path.read_bytes() == before


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["push"], ("variables",)),
        (["push-dynamic", "example.helper"], ("functions",)),
        (["push-dynamic", "example.helper", "--seed-from", "none"], ("functions",)),
        (
            ["push-dynamic", "--seed-from", "remote", "--types", "functions,variables"],
            ("functions", "variables"),
        ),
        (["push-dynamic", "--hubs"], ("functions",)),
        (["push-dynamic", "--seed-from", "specs", "--json"], ("functions",)),
        (["push-dynamic", "--why", "example.worker"], ("functions",)),
    ],
)
def test_explicit_resource_selection_overrides_environment(
    tmp_path, monkeypatch, argv, expected
):
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    monkeypatch.setenv("POLY_DEPLOY_ENABLED_TYPES", "variables")
    args = flow.parse_args(argv)
    assert flow.build_config(tmp_path, args).enabled_types == expected


@pytest.mark.parametrize(
    "arguments",
    [
        "None, None, poly.example",
        "*[None, None], poly.example",
        "None, *[None], poly.example",
        "*(None,), *(None,), poly.example",
        "*[None, None], namespace",
        "*(None, None, poly.example)",
        "*[None, None], api=poly.example",
    ],
)
async def test_starred_helper_call_rebuilds_native_server_consumer(project, arguments):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    config, args, logger, client, _, files = project
    path = files["worker"]
    header = path.read_text().split("async def worker")[0]
    body = (
        "namespace = poly.example\n"
        "async def invoke(first, second, api):\n    return await api.helper()\n"
        f"async def worker() -> bool:\n    return await invoke({arguments})\n"
    )
    path.write_text(header + body)
    helper = AsyncMock(return_value=True)
    namespace = {"poly": SimpleNamespace(example=SimpleNamespace(helper=helper))}
    exec(body, namespace)
    assert await namespace["worker"]() is True
    helper.assert_awaited_once()
    # Keep the server source unchanged remotely: only its dependency triggers a rebuild.
    specs = flow.load_function_specs(config, args, logger)
    spec = next(item for item in specs if item.name == "worker")
    payload = flow.build_function_payload(spec, flow.build_local_func_context(specs))
    client.functions["example.worker"] = ("server", payload)
    prepared = plan(project, seeds=("example.helper",), seed_from="none")
    assert "example.worker" in prepared.order
    assert "example.helper" in prepared.graph.dependencies["example.worker"]
    assert prepared.payloads["example.worker"]["returnType"] == "boolean"
    assert prepared.payloads["example.worker"]["generateContexts"] == ["example"]
    assert execute(project, prepared).functions.failed == 0
    assert "example.worker" in client.writes


@pytest.mark.parametrize(
    "call",
    ["poly.example.helper.__call__()", "poly.example.helper.__call__.__call__()"],
)
async def test_callable_attribute_rebuilds_native_server_with_client_requirements(
    project, call
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    config, args, logger, client, _, files = project
    helper_path = files["helper"]
    helper_path.write_text(
        "import yaml\n"
        + helper_path.read_text().replace(
            "return True", "return bool(yaml.safe_load('true'))"
        )
    )
    worker_path = files["worker"]
    header = worker_path.read_text().split("async def worker")[0]
    body = f"async def worker() -> bool:\n    return await {call}\n"
    worker_path.write_text(header + body)
    helper = AsyncMock(return_value=True)
    namespace = {"poly": SimpleNamespace(example=SimpleNamespace(helper=helper))}
    exec(body, namespace)
    assert await namespace["worker"]() is True
    helper.assert_awaited_once()
    # An unchanged consumer must rebuild when only its referenced client changes.
    specs = flow.load_function_specs(config, args, logger)
    spec = next(item for item in specs if item.name == "worker")
    payload = flow.build_function_payload(spec, flow.build_local_func_context(specs))
    client.functions["example.worker"] = ("server", payload)
    assert payload["requirements"] == ["PyYAML"]
    assert payload["returnType"] == "boolean"
    assert payload["generateContexts"] == ["example"]
    prepared = plan(project, seeds=("example.helper",), seed_from="none")
    assert "example.worker" in prepared.order
    assert prepared.graph.dependencies["example.worker"] == {"example.helper"}
    assert prepared.payloads["example.worker"]["requirements"] == ["PyYAML"]
    assert execute(project, prepared).functions.failed == 0
    assert "example.worker" in client.writes


@pytest.mark.parametrize(
    "visibility, context",
    [("TENANT", "shared"), ("PUBLIC", "vendor.shared"), ("TENANT", ""), ("PUBLIC", "")],
)
@pytest.mark.parametrize("dynamic", [False, True])
def test_shared_sdk_clients_supply_nested_packages_and_contexts(
    project, visibility, context, dynamic
):
    config, args, logger, client, state, files = project
    worker = files["worker"]
    header = worker.read_text().split("async def worker")[0]
    callable_path = f"{context}.helper_async" if context else "helper_async"
    worker.write_text(
        header
        + f"async def worker() -> bool:\n    return await poly.{callable_path}.__call__()\n"
    )
    client.shared_specs = [
        {
            "id": "shared-client",
            "type": "customFunction",
            "language": "python",
            "context": context,
            "name": "helper_async",
            "visibilityMetadata": {"visibility": visibility},
            "code": "from polyapi import poly\nasync def helper_async() -> bool:\n    return await poly.vendor.nested.finish()\n",
        },
        {
            "id": "nested-client",
            "type": "customFunction",
            "language": "python",
            "context": "vendor.nested",
            "name": "finish",
            "visibilityMetadata": {"visibility": "PUBLIC"},
            "code": "import yaml\nfrom polyapi import vari\nasync def finish() -> bool:\n    await vari.OOB.provider.settings.get_async()\n    return bool(yaml.safe_load('true'))\n",
        },
    ]
    if dynamic:
        prepared = plan(project, seeds=("example.worker",), seed_from="none")
        assert prepared.order == ["example.worker"]
        assert execute(project, prepared).functions.failed == 0
    else:
        specs = flow.load_function_specs(config, args, logger)
        spec = next(item for item in specs if item.name == "worker")
        stats = flow.ArtifactStats()
        flow.sync_function_group(
            specs=[spec],
            client=client,
            registry=flow.fetch_function_registry(client),
            detail_cache={},
            config=config,
            cli_args=args,
            logger=logger,
            stats=stats,
            resume_state=state,
        )
        assert stats.failed == 0
    payload = client.functions["example.worker"][1]
    assert payload["requirements"] == ["PyYAML"]
    if context:
        assert set(payload["generateContexts"]) == {
            context.split(".")[0],
            "vendor",
            "OOB.provider",
        }
    else:
        assert payload["generateContexts"] == []
    assert payload["returnType"] == "boolean"
    assert client.writes == ["example.worker"]
    assert state.pending_functions == set()


@pytest.mark.parametrize("scope", [None, "explicit", "excluded", "excluded-name"])
def test_nested_root_client_generation_preserves_scope_guards(
    project, monkeypatch, scope
):
    from types import SimpleNamespace
    from polyapi import generate

    config, args, logger, client, state, files = project
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", "poly.shared.outer()")
    )
    if scope == "explicit":
        files["worker"].write_text(
            files["worker"]
            .read_text()
            .replace(
                '"context": "example"',
                '"context": "example", "generate_contexts": ["shared"]',
            )
        )
    if scope in {"excluded", "excluded-name"}:
        excluded = "root_helper" if scope == "excluded-name" else "unrelated.private"
        config = replace(config, orphan_excluded_contexts=(excluded,))
    client.shared_specs = [
        {
            "type": "customFunction",
            "language": "python",
            "context": "shared",
            "name": "outer",
            "code": "from polyapi import poly\nasync def outer() -> bool:\n    return await poly.root_helper()\n",
        },
        {
            "type": "customFunction",
            "language": "python",
            "context": "",
            "name": "root_helper",
            "code": "import yaml\nfrom polyapi import vari\nasync def root_helper() -> bool:\n    await vari.OOB.provider.settings.get_async()\n    return bool(yaml.safe_load('true'))\n",
        },
    ]
    specs = flow.load_function_specs(config, args, logger)
    contexts = flow.build_remote_func_context(
        client,
        local_func_context=flow.build_local_func_context(specs),
        excluded_contexts=config.orphan_excluded_contexts,
    )
    assert "root_helper" in contexts
    assert "OOB.provider" in flow.get_transitive_contexts("example.worker", contexts)
    worker = next(item for item in specs if item.name == "worker")
    if scope:
        with pytest.raises(flow.DeployError, match="require unfiltered generation"):
            flow.build_function_payload(
                worker, contexts, config.orphan_excluded_contexts
            )
        with pytest.raises(flow.DeployError, match="Invalid dependency function"):
            flow.plan_dynamic_functions(
                client,
                config,
                replace(args, seeds=("example.worker",), seed_from="none"),
                logger,
                state,
            )
        assert client.writes == []
        assert not (config.repo_root / flow.ResumeState.FILENAME).exists()
        return
    payload = flow.build_function_payload(worker, contexts)
    assert payload["requirements"] == ["PyYAML"]
    assert payload["generateContexts"] == []
    # Use the pinned SDK's real request builder without credentials or network.
    monkeypatch.setattr(
        generate,
        "get_api_key_and_url",
        lambda: ("synthetic-key", "https://unit.invalid"),
    )
    monkeypatch.setattr(generate, "get_direct_execute_config", lambda: False)

    def get_catalog(url, *, headers, params):
        assert "contexts" not in params
        return SimpleNamespace(status_code=200, json=lambda: client.shared_specs)

    monkeypatch.setattr(generate.http_client, "get", get_catalog)
    assert (
        generate.get_specs(contexts=payload["generateContexts"], no_types=True)
        == client.shared_specs
    )


def test_shared_catalog_uses_sdk_precedence_and_keeps_local_source(project):
    config, args, logger, client, _, files = project
    client.shared_specs = [
        {
            "type": "customFunction",
            "context": "example",
            "name": "helper",
            "language": "python",
            "code": "invalid shared source",
        },
        {
            "type": "customFunction",
            "context": "shared",
            "name": "run",
            "language": "python",
            "code": "invalid superseded source",
        },
        {
            "type": "customFunction",
            "context": "shared",
            "name": "run",
            "language": "typescript",
            "code": "export function run() {}",
        },
        {
            "type": "customFunction",
            "context": "shared",
            "name": "run",
            "language": "python",
            "code": "from polyapi import vari\nasync def run() -> bool:\n    await vari.OOB.provider.settings.get_async()\n    return True\n",
        },
    ]
    worker = files["worker"]
    worker.write_text(
        worker.read_text().replace("poly.example.middle()", "poly.shared.run()")
    )
    specs = flow.load_function_specs(config, args, logger)
    local = flow.build_local_func_context(specs)
    contexts = flow.build_remote_func_context(client, local_func_context=local)
    assert contexts["example.helper"]["code"] == files["helper"].read_text()
    assert contexts["shared.run"]["contexts"] == ["OOB.provider"]
    assert client.writes == []


def test_shared_catalog_preserves_exclusions_and_server_http_boundary(project):
    config, args, logger, client, _, files = project
    client.shared_specs = [
        {
            "type": "customFunction",
            "context": "",
            "name": "root_helper",
            "language": "python",
            "code": "invalid unreferenced root source",
        },
        {
            "type": "customFunction",
            "context": "excluded",
            "name": "helper",
            "language": "python",
            "code": "invalid excluded source",
        },
        {
            "type": "customFunction",
            "context": "other",
            "name": "unused",
            "language": "python",
            "code": "invalid unreferenced source",
        },
        {
            "type": "serverFunction",
            "context": "shared",
            "name": "worker",
            "language": "python",
            "code": "invalid remote server source",
        },
        {
            "type": "customFunction",
            "context": "shared",
            "name": "foreign",
            "language": "typescript",
            "code": "export function foreign() {}",
        },
    ]
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace(
            "poly.example.middle()",
            "poly.excluded.helper()\n    await poly.shared.worker_async()\n    await poly.shared.foreign()",
        )
    )
    local = flow.build_local_func_context(
        flow.load_function_specs(config, args, logger), ("excluded",)
    )
    contexts = flow.build_remote_func_context(
        client, ("excluded",), local_func_context=local
    )
    assert contexts == local
    assert client.writes == []


@pytest.mark.parametrize("code", [None, "", "   "])
def test_referenced_shared_client_without_source_blocks_before_writes(project, code):
    client, files = project[3], project[5]
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", "poly.shared.helper()")
    )
    client.shared_specs = [
        {
            "type": "customFunction",
            "context": "shared",
            "name": "helper",
            "language": "python",
            "code": code,
        }
    ]
    with pytest.raises(flow.DeployError, match="Missing Python source"):
        plan(project, seeds=("example.worker",), seed_from="none")
    assert client.writes == []


@pytest.mark.parametrize("status", [403, 500])
def test_shared_catalog_failure_stops_before_writes(project, monkeypatch, status):
    client = project[3]
    original = client.request

    def request(method, path, **kwargs):
        if path == "/specs":
            raise flow.ApiError(method, path, status)
        return original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(flow.ApiError):
        plan(project, seeds=("example.worker",), seed_from="none")
    assert client.writes == []


@pytest.mark.parametrize("winner_type", ["serverFunction", "apiFunction"])
@pytest.mark.parametrize("call", ["helper", "helper_async"])
def test_shared_client_shadowed_by_sdk_http_function_is_not_scanned(
    project, winner_type, call
):
    config, args, logger, client, _, files = project
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", f"poly.shared.{call}()")
    )
    client.shared_specs = [
        {
            "type": "customFunction",
            "context": "shared",
            "name": "helper",
            "language": "python",
            "code": "invalid shadowed client source",
        },
        {
            "type": winner_type,
            "context": "shared",
            "name": "helper",
            "language": "python",
        },
    ]
    prepared = plan(project, seeds=("example.worker",), seed_from="none")
    payload = prepared.payloads["example.worker"]
    assert payload["generateContexts"] == ["shared"]
    assert "requirements" not in payload
    assert execute(project, prepared).functions.failed == 0
    assert client.writes == ["example.worker"]


@pytest.mark.parametrize(
    "http_type,http_name",
    [
        ("serverFunction", "helper"),
        ("apiFunction", "helper"),
        ("aiFunction", "helper"),
        ("authFunction", "introspectToken"),
        ("authFunction", "refreshToken"),
        ("authFunction", "revokeToken"),
    ],
)
@pytest.mark.parametrize("http_last", [False, True])
@pytest.mark.parametrize("call_suffix", ["()", ".__call__()"])
@pytest.mark.parametrize("context", ["shared", ""])
def test_shared_async_alias_precedence_controls_dependency_payload(
    project, http_type, http_name, http_last, call_suffix, context
):
    _, _, _, client, _, files = project
    namespace = f"poly.{context}" if context else "poly"
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", f"{namespace}.{http_name}_async{call_suffix}")
    )
    package = "uninstalled_review_only_package" if http_last else "yaml"
    shared_client = {
        "type": "customFunction",
        "context": context,
        "name": f"{http_name}_async",
        "language": "python",
        "code": (
            f"import {package}\n"
            f"async def {http_name}_async() -> bool:\n"
            "    return True\n"
        ),
    }
    http = {"type": http_type, "context": context, "name": http_name}
    client.shared_specs = [shared_client, http] if http_last else [http, shared_client]
    prepared = plan(project, seeds=("example.worker",), seed_from="none")
    payload = prepared.payloads["example.worker"]
    assert prepared.order == ["example.worker"]
    assert payload["generateContexts"] == ([context] if context else [])
    assert payload.get("requirements", []) == ([] if http_last else ["PyYAML"])
    assert client.writes == []
    assert execute(project, prepared).functions.failed == 0
    assert client.writes == ["example.worker"]


@pytest.mark.parametrize(
    "http_type,http_name",
    [
        ("serverFunction", "remote_helper"),
        ("apiFunction", "remote_helper"),
        ("aiFunction", "remote_helper"),
        ("authFunction", "introspectToken"),
        ("authFunction", "refreshToken"),
        ("authFunction", "revokeToken"),
    ],
)
@pytest.mark.parametrize("catalog_order", ["http_last", "client_last", "new_client"])
@pytest.mark.parametrize("call_suffix", ["()", ".__call__()"])
def test_local_client_source_cannot_override_another_sdk_declaration(
    project, http_type, http_name, catalog_order, call_suffix
):
    config, _, _, client, _, files = project
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace(
            "poly.example.middle()", f"poly.example.{http_name}_async{call_suffix}"
        )
    )
    local_client = files["unrelated"].with_name(f"{http_name}_async.py")
    package = (
        "yaml" if catalog_order == "client_last" else "uninstalled_review_only_package"
    )
    local_client.write_text(
        f"import {package}\n"
        + files["unrelated"].read_text().replace("unrelated", f"{http_name}_async")
    )
    remote_client = {
        "type": "customFunction",
        "context": "example",
        "name": f"{http_name}_async",
        "language": "python",
        # Matching declarations must still use the local source, not stale code.
        "code": "invalid remote source",
    }
    http = {"type": http_type, "context": "example", "name": http_name}
    client.shared_specs = {
        "http_last": [remote_client, http],
        "client_last": [http, remote_client],
        "new_client": [http],
    }[catalog_order]
    if catalog_order == "client_last":
        prepared = plan(project, seeds=("example.worker",), seed_from="none")
        assert prepared.payloads["example.worker"]["requirements"] == ["PyYAML"]
        assert prepared.order == ["example.worker"]
        assert client.writes == []
        assert execute(project, prepared).functions.failed == 0
        assert client.writes == prepared.order
        return
    with pytest.raises(flow.DeployError, match="Ambiguous generated SDK name") as error:
        plan(project, seeds=("example.worker",), seed_from="none")
    assert f"example.{http_name}_async" in str(error.value)
    assert f"{http_type} example.{http_name}" in str(error.value)
    assert "ModuleNotFoundError" not in str(error.value)
    assert client.writes == []
    assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


@pytest.mark.parametrize("server_last", [False, True])
def test_local_server_alias_cannot_override_another_sdk_declaration(
    project, server_last
):
    config, _, _, client, _, _ = project
    remote_server = dict(
        client.functions.pop("example.worker")[1], type="serverFunction"
    )
    shared_client = {
        "type": "customFunction",
        "context": "example",
        "name": "worker_async",
        "language": "python",
        "code": "async def worker_async() -> bool:\n    return True\n",
    }
    client.shared_specs = (
        [shared_client, remote_server]
        if server_last
        else [remote_server, shared_client]
    )
    if server_last:
        prepared = plan(project, seeds=("example.http_caller",), seed_from="none")
        assert prepared.order == ["example.http_caller"]
        assert "requirements" not in prepared.payloads["example.http_caller"]
    else:
        with pytest.raises(
            flow.DeployError, match="Ambiguous generated SDK name"
        ) as error:
            plan(project, seeds=("example.http_caller",), seed_from="none")
        assert "example.worker_async" in str(error.value)
        assert "local server example.worker" in str(error.value)
    assert client.writes == []
    assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


@pytest.mark.parametrize("context", ["", "shared"])
@pytest.mark.parametrize("via_client", [False, True])
@pytest.mark.parametrize("dynamic", [False, True])
def test_webhook_listener_generation_scope(project, context, via_client, dynamic):
    config, args, logger, client, state, files = project
    listener = f"{context}.events" if context else "events"
    call = f"poly.{listener}.__call__(lambda *args: None)"
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", "poly.shared.outer()" if via_client else call)
    )
    client.shared_specs = [
        {"type": "webhookHandle", "context": context, "name": "events"},
        {"type": "webhookHandle", "context": "", "name": "unused_events"},
        {
            "type": "customFunction",
            "language": "python",
            "context": "shared",
            "name": "outer",
            "code": (
                "from polyapi import poly\n"
                "async def outer() -> bool:\n"
                f"    await {call}\n    return True\n"
            ),
        },
    ]
    if dynamic:
        prepared = plan(project, seeds=("example.worker",), seed_from="none")
        assert prepared.order == ["example.worker"]
        assert execute(project, prepared).functions.failed == 0
    else:
        specs = flow.load_function_specs(config, args, logger)
        stats = flow.ArtifactStats()
        flow.sync_function_group(
            specs=[spec for spec in specs if spec.name == "worker"],
            client=client,
            registry=flow.fetch_function_registry(client),
            detail_cache={},
            config=config,
            cli_args=args,
            logger=logger,
            stats=stats,
            resume_state=state,
        )
        assert stats.failed == 0
    payload = client.functions["example.worker"][1]
    assert payload["generateContexts"] == (["shared"] if context else [])
    assert "requirements" not in payload
    assert client.writes == ["example.worker"]
    assert state.pending_functions == set()


@pytest.mark.parametrize("scope", ["excluded", "excluded-name", "explicit"])
@pytest.mark.parametrize("via_client", [False, True])
@pytest.mark.parametrize(
    "http_type,http_name,call",
    [
        ("serverFunction", "helper", "helper_async"),
        ("authFunction", "refreshToken", "refreshToken_async"),
        ("authFunction", "getToken", "getToken"),
        ("webhookHandle", "events", "events"),
    ],
)
def test_root_external_callable_preserves_generation_scope_guards(
    project, scope, via_client, http_type, http_name, call
):
    config, args, logger, client, state, files = project
    worker = files["worker"]
    target = f"poly.{call}()"
    if http_type == "webhookHandle":
        target = f"poly.{call}(lambda *args: None)"
    code = worker.read_text().replace(
        "poly.example.middle()", "poly.shared.outer()" if via_client else target
    )
    if scope == "explicit":
        code = code.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example"]',
        )
    else:
        excluded = call if scope == "excluded-name" else "private"
        config = replace(config, orphan_excluded_contexts=(excluded,))
    worker.write_text(code)
    client.shared_specs = [
        {
            "type": "customFunction",
            "context": "",
            "name": call,
            "code": "invalid shadowed client source",
        },
        {"type": http_type, "context": "", "name": http_name},
    ]
    if via_client:
        client.shared_specs.append(
            {
                "type": "customFunction",
                "language": "python",
                "context": "shared",
                "name": "outer",
                "code": (
                    "from polyapi import poly\n"
                    "async def outer() -> bool:\n"
                    f"    await {target}\n    return True\n"
                ),
            }
        )
    specs = flow.load_function_specs(config, args, logger)
    contexts = flow.build_remote_func_context(
        client, local_func_context=flow.build_local_func_context(specs)
    )
    worker_spec = next(spec for spec in specs if spec.name == "worker")
    with pytest.raises(flow.DeployError, match="require unfiltered generation"):
        flow.build_function_payload(
            worker_spec, contexts, config.orphan_excluded_contexts
        )
    with pytest.raises(flow.DeployError, match="require unfiltered generation"):
        flow.sync_function_group(
            specs=[worker_spec],
            client=client,
            registry=flow.fetch_function_registry(client),
            detail_cache={},
            config=config,
            cli_args=args,
            logger=logger,
            stats=flow.ArtifactStats(),
            resume_state=state,
        )
    with pytest.raises(flow.DeployError, match="Invalid dependency function"):
        flow.plan_dynamic_functions(
            client,
            config,
            replace(args, seeds=("example.worker",), seed_from="none"),
            logger,
            state,
        )
    assert client.writes == []
    assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


def test_client_requirements_use_sdk_metadata_without_executing_or_importing(
    monkeypatch,
):
    monkeypatch.setattr(
        flow,
        "packages_distributions",
        lambda: {"review_dependency": ["review-dependency", "ignored-distribution"]},
    )
    source = (
        "import json, requests, polyapi\n"
        "import review_dependency.one, review_dependency.two\n"
        "from review_dependency.three import value\n"
        "raise RuntimeError('must not execute')\n"
        "@missing_decorator()\n"
        "def helper(value=missing_default()) -> bool:\n"
        "    return True\n"
    )
    assert flow.collect_client_requirements(source) == {"review-dependency"}


def test_shared_client_requirements_include_function_local_imports(project):
    _, _, _, client, _, files = project
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", "poly.shared.helper()")
    )
    client.shared_specs = [
        {
            "type": "customFunction",
            "language": "python",
            "context": "shared",
            "name": "helper",
            "code": (
                "async def helper() -> bool:\n"
                "    from yaml import safe_load\n"
                "    return bool(safe_load('true'))\n"
            ),
        }
    ]
    prepared = plan(project, seeds=("example.worker",), seed_from="none")
    assert prepared.payloads["example.worker"]["requirements"] == ["PyYAML"]
    assert client.writes == []


@pytest.mark.parametrize(
    "source",
    [
        "import uninstalled_review_only_package\nasync def helper():\n    return True\n",
        "async def helper(:\n    return True\n",
    ],
)
def test_shared_client_invalid_imports_or_syntax_stop_before_writes(project, source):
    config, _, _, client, _, files = project
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", "poly.shared.helper()")
    )
    client.shared_specs = [
        {
            "type": "customFunction",
            "language": "python",
            "context": "shared",
            "name": "helper",
            "code": source,
        }
    ]
    with pytest.raises(flow.DeployError):
        plan(project, seeds=("example.worker",), seed_from="none")
    assert client.writes == []
    assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


@pytest.mark.parametrize("dependency", ["direct", "local-client", "shared-client"])
@pytest.mark.parametrize("scope", [None, "explicit", "excluded", "excluded-name"])
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize(
    "resource, reader",
    [
        ("vari", "direct"),
        ("vari", "helper"),
        ("vari", "tuple"),
        ("vari", "lexical"),
        ("vari", "returned"),
        ("tabi", "direct"),
        ("tabi", "helper"),
        ("tabi", "tuple"),
        ("tabi", "lexical"),
        ("tabi", "returned"),
        ("tabi", "query-type"),
    ],
)
def test_root_resource_generation_and_scope_guards(
    project, monkeypatch, tmp_path, dependency, scope, dynamic, resource, reader
):
    from types import SimpleNamespace
    from polyapi import generate, poly_tables, variables

    root_name = "root_setting" if resource == "vari" else "root_table"
    reference = (
        "await vari.root_setting.get_async()"
        if resource == "vari"
        else "tabi.root_table.select_many({})"
    )
    helpers = {
        "direct": "",
        "helper": (
            "def query(table):\n    return table.select_many({})\n"
            "def read_root():\n    return query(tabi.root_table)\n"
        ),
        "tuple": (
            "def read_root():\n    table, = (tabi.root_table,)\n"
            "    return table.select_many({})\n"
        ),
        "lexical": (
            "def read_root():\n    table = tabi.root_table\n"
            "    return table.select_many({})\n"
            "def read_scoped():\n    table = tabi.catalog.records\n"
            "    return table.select_many({})\n"
        ),
        "returned": (
            "def get_table():\n    return tabi.root_table\n"
            "def read_root():\n    return get_table().select_many({})\n"
        ),
        "query-type": (
            "def read_root():\n    query_type = tabi.root_tableSelectOneQuery\n"
            "    return query_type(where={})\n"
        ),
    }[reader]
    if resource == "vari":
        helpers = (
            helpers.replace("tabi.root_table", "vari.root_setting")
            .replace("tabi.catalog.records", "vari.catalog.setting")
            .replace(".select_many({})", ".get()")
        )
    if helpers:
        reference = "read_root()"

    config, args, logger, client, state, files = project
    client.functions["example.helper"][1]["code"] = client.functions["example.helper"][
        1
    ]["code"].replace("return False", "return True")
    worker = files["worker"]
    if dependency == "direct":
        worker.write_text(
            f"from polyapi import {resource}\n"
            + helpers
            + worker.read_text().replace(
                "await poly.example.middle()",
                f"await poly.example.middle()\n    {reference}",
            )
        )
    elif dependency == "local-client":
        helper = files["helper"]
        helper.write_text(
            f"import yaml\nfrom polyapi import {resource}\n"
            + helpers
            + helper.read_text().replace(
                "return True",
                f"{reference}\n    return bool(yaml.safe_load('true'))",
            )
        )
    else:
        worker.write_text(
            worker.read_text().replace("poly.example.middle()", "poly.shared.outer()")
        )
        client.shared_specs = [
            {
                "type": "customFunction",
                "language": "python",
                "context": "shared",
                "name": "outer",
                "visibilityMetadata": {"visibility": "TENANT"},
                "code": "from polyapi import poly\nasync def outer() -> bool:\n    return await poly.vendor.nested.finish()\n",
            },
            {
                "type": "customFunction",
                "language": "python",
                "context": "vendor.nested",
                "name": "finish",
                "visibilityMetadata": {"visibility": "PUBLIC"},
                "code": f"import yaml\nfrom polyapi import {resource}\n{helpers}async def finish() -> bool:\n    {reference}\n    return bool(yaml.safe_load('true'))\n",
            },
        ]
    root_variable = {
        "id": "synthetic-variable",
        "type": "serverVariable",
        "context": "",
        "name": "root_setting",
        "variable": {
            "secrecy": "NONE",
            "valueType": {"kind": "plain", "value": "string"},
        },
    }
    root_table = {
        "id": "synthetic-table",
        "type": "table",
        "context": None,
        "name": "root_table",
        "schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    }
    root_spec = root_variable if resource == "vari" else root_table
    client.shared_specs.append(root_spec)
    if scope == "explicit":
        worker.write_text(
            worker.read_text().replace(
                '"context": "example"',
                '"context": "example", "generate_contexts": ["example", "shared"]',
            )
        )
    elif scope:
        excluded = root_name if scope == "excluded-name" else "private"
        config = replace(config, orphan_excluded_contexts=(excluded,))
    specs = flow.load_function_specs(config, args, logger)
    local_contexts = flow.build_local_func_context(
        specs, config.orphan_excluded_contexts
    )
    worker_spec = next(spec for spec in specs if spec.name == "worker")

    def deploy():
        if dynamic:
            prepared = flow.plan_dynamic_functions(
                client,
                config,
                replace(
                    args,
                    seeds=(
                        (
                            "example.helper"
                            if dependency == "local-client"
                            else "example.worker"
                        ),
                    ),
                    seed_from="none",
                ),
                logger,
                state,
            )
            summary = flow.Summary()
            flow.execute_dynamic_functions(
                prepared, client, config, args, logger, summary, state
            )
            assert summary.functions.failed == 0
        else:
            stats = flow.ArtifactStats()
            flow.sync_function_group(
                specs=[worker_spec],
                client=client,
                registry=flow.fetch_function_registry(client),
                detail_cache={},
                config=config,
                cli_args=args,
                logger=logger,
                stats=stats,
                resume_state=state,
                local_func_context=local_contexts,
            )
            assert stats.failed == 0

    if scope:
        expected_error = (
            "Invalid dependency function"
            if dynamic
            else "require unfiltered generation"
        )
        with pytest.raises(flow.DeployError, match=expected_error):
            deploy()
        assert client.writes == []
        assert not (config.repo_root / flow.ResumeState.FILENAME).exists()
        return

    deploy()
    payload = client.functions["example.worker"][1]
    assert payload["generateContexts"] == []
    if dependency != "direct":
        assert payload["requirements"] == ["PyYAML"]
    if dynamic and dependency == "local-client":
        assert set(client.writes) == {
            "example.helper",
            "example.middle",
            "example.worker",
            "example.other_worker",
        }
        assert client.functions["example.other_worker"][1]["generateContexts"] == []
    else:
        assert client.writes == ["example.worker"]
    assert state.pending_functions == set()

    # Prove the native request selects the root spec and generates its export.
    monkeypatch.setattr(
        generate,
        "get_api_key_and_url",
        lambda: ("synthetic-key", "https://unit.invalid"),
    )
    monkeypatch.setattr(generate, "get_direct_execute_config", lambda: False)

    def get_catalog(url, *, headers, params):
        assert "contexts" not in params
        return SimpleNamespace(status_code=200, json=lambda: [root_spec])

    monkeypatch.setattr(generate.http_client, "get", get_catalog)
    selected = generate.get_specs(contexts=payload["generateContexts"], no_types=True)
    assert selected == [root_spec]
    generated = tmp_path / "generated"
    generated.mkdir()
    if resource == "vari":
        monkeypatch.setattr(variables, "__file__", str(generated / "variables.py"))
        variables.create_variable(selected[0])
    else:
        monkeypatch.setattr(poly_tables, "__file__", str(generated / "poly_tables.py"))
        poly_tables.generate_tables(selected)
    module = ast.parse((generated / resource / "__init__.py").read_text())
    expected_export = (
        f"{root_name}SelectOneQuery" if reader == "query-type" else root_name
    )
    assert any(
        isinstance(node, ast.ClassDef) and node.name == expected_export
        for node in module.body
    )


@pytest.mark.parametrize(
    "imports, reference",
    [
        ("from polyapi import vari", "vari.root_setting"),
        ("from polyapi import vari as settings", "settings.root_setting"),
        ("import polyapi as sdk", "sdk.vari.root_setting"),
        ("from polyapi.vari import root_setting as setting", "setting"),
        ("from polyapi import vari\nsetting = vari.root_setting", "setting"),
    ],
)
@pytest.mark.parametrize(
    "method", ["get", "get_async", "inject", "update", "update_async", "onUpdate"]
)
def test_root_vari_methods_and_aliases_preserve_empty_context(
    tmp_path, monkeypatch, imports, reference, method
):
    import sys
    import types

    import polyapi

    # This synthetic root Vari belongs to the fixture, not the developer's SDK.
    variables = types.ModuleType("polyapi.vari")
    variables.__dict__["root_setting"] = object()
    monkeypatch.setitem(sys.modules, "polyapi.vari", variables)
    monkeypatch.setitem(polyapi.__dict__, "vari", variables)
    path = tmp_path / "worker_sfx.py"
    path.write_text(
        f"{imports}\nfrom polyapi import poly\n"
        "async def worker() -> bool:\n"
        "    await poly.scoped.http_async()\n"
        f"    {reference}.{method}()\n    return True\n"
    )
    spec = flow.FunctionSpec("worker", "example", "server", path, path)
    assert flow.build_function_payload(spec)["generateContexts"] == []
    with pytest.raises(flow.DeployError, match="require unfiltered generation"):
        flow.build_function_payload(spec, excluded_contexts=("root_setting",))


@pytest.mark.parametrize("resource", ["vari", "tabi"])
@pytest.mark.parametrize("excluded_contexts", [(), ("private",)])
def test_root_resource_does_not_cross_server_http_boundary(
    project, excluded_contexts, resource
):
    config, args, logger, _, _, files = project
    worker = files["worker"]
    worker.write_text(
        f"from polyapi import {resource}\n"
        + worker.read_text().replace(
            "return True",
            (
                "return bool(await vari.root_setting.get_async())"
                if resource == "vari"
                else "return bool(tabi.root_table.select_many({}))"
            ),
        )
    )
    specs = flow.load_function_specs(config, args, logger)
    contexts = flow.build_local_func_context(specs, excluded_contexts)
    caller = next(spec for spec in specs if spec.name == "http_caller")
    payload = flow.build_function_payload(caller, contexts, excluded_contexts)
    assert payload["generateContexts"] == ["example"]


@pytest.mark.parametrize("resource", ["vari", "tabi"])
@pytest.mark.parametrize("scope", ["inferred", "explicit", "excluded"])
def test_root_resource_example_text_does_not_widen_generation(
    tmp_path, scope, resource
):
    reference = (
        "vari.root_setting.get_async()"
        if resource == "vari"
        else "tabi.root_table.select_many({})"
    )
    path = tmp_path / "worker_sfx.py"
    path.write_text(
        f"from polyapi import poly, {resource}\n"
        "async def worker() -> bool:\n"
        f"    example = '{reference}'\n"
        "    await poly.scoped.http_async()\n    return bool(example)\n"
    )
    spec = flow.FunctionSpec("worker", "example", "server", path, path)
    if scope == "explicit":
        spec = replace(spec, generate_contexts=("scoped",))
    excluded = ("private",) if scope == "excluded" else ()
    assert flow.build_function_payload(spec, excluded_contexts=excluded)[
        "generateContexts"
    ] == ["scoped"]


@pytest.mark.parametrize(
    "imports, reference",
    [
        ("from polyapi import tabi", "tabi.root_table"),
        ("from polyapi import tabi as tables", "tables.root_table"),
        ("import polyapi as sdk", "sdk.tabi.root_table"),
        ("import polyapi.tabi as tables", "tables.root_table"),
        ("from polyapi.tabi import root_table as table", "table"),
        ("from polyapi import tabi\ntable = tabi.root_table", "table"),
        ("from polyapi import tabi as tables\ntable = tables.root_table", "table"),
    ],
)
@pytest.mark.parametrize(
    "member",
    [
        "count",
        "select_many",
        "select_one",
        "insert_many",
        "insert_one",
        "upsert_many",
        "upsert_one",
        "update_many",
        "update_one",
        "delete_many",
        "delete_one",
        "table_id",
    ],
)
def test_root_tabi_members_and_aliases_preserve_empty_context(
    tmp_path, imports, reference, member
):
    path = tmp_path / "worker_sfx.py"
    call = "" if member == "table_id" else "({})"
    path.write_text(
        f"{imports}\nfrom polyapi import poly\n"
        "async def worker() -> bool:\n"
        "    await poly.scoped.http_async()\n"
        f"    {reference}.{member}{call}\n    return True\n"
    )
    spec = flow.FunctionSpec("worker", "example", "server", path, path)
    assert flow.build_function_payload(spec)["generateContexts"] == []
    with pytest.raises(flow.DeployError, match="require unfiltered generation"):
        flow.build_function_payload(spec, excluded_contexts=("root_table",))


@pytest.mark.parametrize("scope", ["inferred", "explicit", "excluded"])
@pytest.mark.parametrize("table_name", ["records", "count"])
@pytest.mark.parametrize(
    "imports, reference",
    [
        ("from polyapi import tabi", "tabi.catalog.records"),
        ("from polyapi import tabi as tables", "tables.catalog.records"),
        ("from polyapi import tabi\ntable = tabi.catalog.records", "table"),
        ("from polyapi import tabi\ntables = tabi.catalog", "tables.records"),
    ],
)
def test_scoped_tabi_keeps_filtered_generation(
    tmp_path, scope, imports, reference, table_name
):
    imports = imports.replace("records", table_name)
    reference = reference.replace("records", table_name)
    path = tmp_path / "worker_sfx.py"
    path.write_text(
        f"{imports}\nasync def worker() -> bool:\n"
        f"    return bool({reference}.select_many({{}}))\n"
    )
    spec = flow.FunctionSpec("worker", "example", "server", path, path)
    if scope == "explicit":
        spec = replace(spec, generate_contexts=("catalog",))
    excluded = ("private",) if scope == "excluded" else ()
    assert flow.build_function_payload(spec, excluded_contexts=excluded)[
        "generateContexts"
    ] == ["catalog"]


@pytest.mark.parametrize("scope", ["inferred", "explicit", "excluded"])
def test_quoted_tabi_alias_is_not_a_dependency(tmp_path, scope):
    path = tmp_path / "worker_sfx.py"
    path.write_text(
        '"""Example only:\ntable = tabi.root_table\n"""\n'
        "from typing import Any\nfrom polyapi import poly, tabi\n"
        "async def worker(table: Any) -> bool:\n"
        "    await poly.scoped.http_async()\n"
        "    return bool(table.select_many({}))\n"
    )
    spec = flow.FunctionSpec("worker", "example", "server", path, path)
    if scope == "explicit":
        spec = replace(spec, generate_contexts=("scoped",))
    excluded = ("private",) if scope == "excluded" else ()
    assert flow.build_function_payload(spec, excluded_contexts=excluded)[
        "generateContexts"
    ] == ["scoped"]


@pytest.mark.parametrize("scope", ["inferred", "explicit", "excluded"])
@pytest.mark.parametrize("table_name", ["records", "count"])
@pytest.mark.parametrize("reader", ["helper", "tuple", "lexical", "shadow"])
def test_tabi_value_tracking_keeps_scoped_tables_scoped(
    tmp_path, scope, table_name, reader
):
    sources = {
        "helper": (
            "def query(table):\n    return table.select_many({})\n"
            f"def read():\n    return query(tabi.catalog.{table_name})\n"
        ),
        "tuple": (
            f"def read():\n    table, = (tabi.catalog.{table_name},)\n"
            "    return table.select_many({})\n"
        ),
        "lexical": (
            f"def read():\n    table = tabi.catalog.{table_name}\n"
            "    return table.select_many({})\n"
            f"def other():\n    table = tabi.catalog.other.{table_name}\n"
            "    return table.count({})\n"
        ),
        "shadow": (
            "from types import SimpleNamespace\n"
            "table = tabi.root_table\n"
            "def unrelated(table):\n    return table.select_many({})\n"
            f"def read():\n    table = tabi.catalog.{table_name}\n"
            "    unrelated(SimpleNamespace(select_many=lambda query: []))\n"
            "    return table.select_many({})\n"
        ),
    }
    path = tmp_path / "worker_sfx.py"
    path.write_text(
        "from polyapi import tabi\n"
        + sources[reader]
        + "async def worker() -> bool:\n    return bool(read())\n"
    )
    spec = flow.FunctionSpec("worker", "example", "server", path, path)
    if scope == "explicit":
        spec = replace(spec, generate_contexts=("catalog",))
    excluded = ("private",) if scope == "excluded" else ()
    assert flow.build_function_payload(spec, excluded_contexts=excluded)[
        "generateContexts"
    ] == ["catalog"]


@pytest.mark.parametrize("context", [None, "catalog.nested"])
@pytest.mark.parametrize("usage", ["value", "import", "returned", "unused_helper"])
def test_native_tabi_exports_retain_their_owner_and_scope_guards(
    tmp_path, monkeypatch, context, usage
):
    from polyapi import poly_tables

    table = {
        "type": "table",
        "context": context,
        "name": "records",
        "id": "synthetic",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "details": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                },
            },
        },
    }
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setattr(poly_tables, "__file__", str(generated / "poly_tables.py"))
    poly_tables.generate_tables([table])
    directory = generated / "tabi"
    if context:
        directory = directory.joinpath(*context.split("."))
    definitions = ast.parse((directory / "__init__.py").read_text())
    exports = {
        node.name for node in definitions.body if isinstance(node, ast.ClassDef)
    } | {"recordsColumns"}
    # Includes a schema-dependent nested row type, not just a suffix checklist.
    assert any(name.startswith("_") for name in exports)
    assert {"records", "recordsRow", "recordsSelectOneQuery"} <= exports
    prefix = f"{context}." if context else ""
    owner = f"{prefix}records"
    table_exports, failures = flow.index_sdk_table_exports([table])
    assert failures == {}
    assert table_exports == {f"{prefix}{name}": owner for name in exports}
    path = tmp_path / "worker_sfx.py"
    for export in sorted(exports):
        reference = f"tabi.{prefix}{export}"
        body = {
            "value": f"def read():\n    return {reference}\n",
            "returned": f"def obtain():\n    return {reference}\ndef read():\n    return obtain()\n",
            "import": f"from polyapi.tabi{'.' + context if context else ''} import {export} as value\ndef read():\n    return True\n",
            "unused_helper": f"def ignore(value):\n    return True\ndef read():\n    return ignore({reference})\n",
        }[usage]
        code = (
            "from polyapi import poly, tabi\n"
            + body
            + "async def worker() -> bool:\n    await poly.scoped.http_async()\n    return bool(read())\n"
        )
        path.write_text(code)
        spec = flow.FunctionSpec("worker", "example", "server", path, path)
        contexts = {
            "example.worker": {
                "kind": "server",
                "code": "stale source",
                "table_exports": table_exports,
                "calls": [],
                "contexts": [],
                "root_tables": ["stale_root"],
            }
        }
        payload = flow.build_function_payload(spec, contexts)
        assert payload["generateContexts"] == (["scoped", "catalog"] if context else [])
        if context:
            assert flow.build_function_payload(spec, contexts, ("private",))[
                "generateContexts"
            ] == ["scoped", context]
        else:
            with pytest.raises(flow.DeployError, match="require unfiltered generation"):
                flow.build_function_payload(spec, contexts, ("private",))
            with pytest.raises(flow.DeployError, match="require unfiltered generation"):
                flow.build_function_payload(
                    replace(spec, generate_contexts=("scoped",)), contexts
                )


def deploy_table_scope_case(project, dynamic):
    config, args, logger, client, state, files = project
    args = replace(args, seeds=("example.worker",), seed_from="none", force=True)
    if dynamic:
        prepared = flow.plan_dynamic_functions(client, config, args, logger, state)
        summary = flow.Summary()
        flow.execute_dynamic_functions(
            prepared, client, config, args, logger, summary, state
        )
        assert summary.functions.failed == 0
    else:
        specs = flow.load_function_specs(config, args, logger)
        stats = flow.ArtifactStats()
        flow.sync_function_group(
            specs=[spec for spec in specs if spec.name == "worker"],
            client=client,
            registry=flow.fetch_function_registry(client),
            detail_cache={},
            config=config,
            cli_args=replace(args, dynamic=False),
            logger=logger,
            stats=stats,
            resume_state=state,
            local_func_context=flow.build_local_func_context(
                specs, config.orphan_excluded_contexts
            ),
        )
        assert stats.failed == 0


def unsupported_table(context):
    return {
        "type": "table",
        "context": context,
        "name": "events",
        "id": "synthetic-table",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "external-id": {"type": "string"},
            },
        },
    }


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("context", ["unrelated", ""])
@pytest.mark.parametrize("excluded", [False, True])
def test_unrelated_table_render_failure_does_not_block_deployment(
    project, dynamic, context, excluded
):
    config, args, logger, client, state, files = project
    if excluded:
        config = replace(config, orphan_excluded_contexts=("unrelated",))
    client.shared_specs.append(unsupported_table(context))
    flow.validate_endpoint_payload(
        "CreateTableDto",
        {
            "name": "events",
            "context": context,
            "columns": [{"name": "external-id", "type": "text"}],
        },
    )
    # An unselected local function may use the table; that must not invalidate
    # the selected server's independent generation scope.
    reference = f"tabi.{context + '.' if context else ''}eventsSelectOneQuery"
    files["unrelated"].write_text(
        "from polyapi import tabi\n"
        + files["unrelated"]
        .read_text()
        .replace("return True", f"return bool({reference})")
    )
    deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == ["example.worker"]
    assert client.functions["example.worker"][1]["generateContexts"] == ["example"]


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        "selected-context",
        "explicit-context",
        "direct",
        "query-type",
        "shared-client",
        "excluded",
        "root-type",
        "unfiltered",
        "empty-inferred",
    ],
)
def test_required_table_render_failure_still_blocks_before_writes(
    project, dynamic, usage
):
    config, args, logger, client, state, files = project
    context = "example" if usage == "selected-context" else "catalog"
    if usage == "root-type":
        context = ""
    client.shared_specs.append(unsupported_table(context))
    worker = files["worker"]
    source = worker.read_text()
    if usage == "explicit-context":
        source = source.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["catalog"]',
        )
    elif usage == "unfiltered":
        client.shared_specs.append(
            {
                "type": "customFunction",
                "language": "python",
                "context": "",
                "name": "root_reader",
                "code": "async def root_reader() -> bool:\n    return True\n",
            }
        )
        source = source.replace("poly.example.middle()", "poly.root_reader()")
    elif usage == "empty-inferred":
        source = source.replace("    await poly.example.middle()\n", "")
    elif usage not in {"selected-context"}:
        reference = f"tabi.{context + '.' if context else ''}events"
        reference += (
            "SelectOneQuery" if usage in {"query-type", "root-type"} else ".count({})"
        )
        if usage == "shared-client":
            client.shared_specs.append(
                {
                    "type": "customFunction",
                    "language": "python",
                    "context": "shared",
                    "name": "read_table",
                    "code": f"from polyapi import tabi\nasync def read_table() -> bool:\n    return bool({reference})\n",
                }
            )
            reference = "await poly.shared.read_table()"
        source = "from polyapi import tabi\n" + source.replace(
            "return True", f"return bool({reference})"
        )
        # Explicit filters must not hide a broken dependency, including unknown
        # flattened exports when native table rendering could not complete.
        source = source.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example", "shared"]',
        )
    if usage == "excluded":
        config = replace(config, orphan_excluded_contexts=("catalog",))
    worker.write_text(source)
    with pytest.raises(
        flow.DeployError,
        match="Invalid dependency function|Cannot generate SDK table exports",
    ):
        deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == []
    assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


def test_native_table_row_fallback_is_deferred_to_generation_scope(monkeypatch):
    from polyapi import schema

    # The native wrapper can return no definitions for unsupported schemas.
    # _render_table_row_classes then raises IndexError while splitting output.
    monkeypatch.setattr(
        schema, "wrapped_generate_schema_types", lambda *args: ("Dict", "")
    )
    exports, failures = flow.index_sdk_table_exports([unsupported_table("unrelated")])
    node = {"table_exports": exports, "table_failures": failures, "code": ""}
    flow.validate_table_generation([node], ["example"])
    with pytest.raises(flow.DeployError, match="unrelated.events"):
        flow.validate_table_generation([node], ["unrelated"])


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("explicit_scope", [False, True])
@pytest.mark.parametrize(
    "form",
    [
        "module_import",
        "qualified_import",
        "namespace_alias",
        "unused_import",
        "unused_qualified_import",
        "unused_alias",
    ],
)
def test_tabi_namespace_does_not_require_excluded_sibling_tables(
    project, dynamic, explicit_scope, form
):
    config, args, logger, client, state, files = project
    config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    source = files["worker"].read_text()
    if explicit_scope:
        source = source.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example", "catalog.good"]',
        )
    prefix = (
        "from polyapi.tabi import catalog\n"
        if form in {"module_import", "unused_import"}
        else (
            "import polyapi.tabi.catalog as catalog\n"
            if form in {"qualified_import", "unused_qualified_import"}
            else "from polyapi import tabi\ncatalog = tabi.catalog\n"
        )
    )
    if not form.startswith("unused"):
        source = source.replace(
            "return True", "return bool(catalog.good.events.count({}))"
        )
    files["worker"].write_text(prefix + source)
    good = unsupported_table("catalog.good")
    good["schema"]["properties"].pop("external-id")
    client.shared_specs.extend([good, unsupported_table("catalog.bad")])
    deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == ["example.worker"]
    assert client.functions["example.worker"][1]["generateContexts"] == [
        "example",
        "catalog.good",
    ]


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("form", ["module_import", "namespace_alias"])
def test_shared_client_tabi_namespace_narrows_to_required_child(project, dynamic, form):
    config, args, logger, client, state, files = project
    config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    prefix = (
        "from polyapi.tabi import catalog\n"
        if form == "module_import"
        else "from polyapi import tabi\ncatalog = tabi.catalog\n"
    )
    client.shared_specs.append(
        {
            "type": "customFunction",
            "language": "python",
            "context": "shared",
            "name": "reader",
            "code": prefix
            + "async def reader() -> bool:\n    return bool(catalog.good.events.count({}))\n",
        }
    )
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", "poly.shared.reader()")
    )
    good = unsupported_table("catalog.good")
    good["schema"]["properties"].pop("external-id")
    client.shared_specs.extend([good, unsupported_table("catalog.bad")])
    deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == ["example.worker"]
    assert client.functions["example.worker"][1]["generateContexts"] == [
        "shared",
        "catalog.good",
    ]


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        "external(catalog)",
        "getattr(catalog, branch).events.count({})",
        "catalog.bad.events",
        "catalog.bad.eventsSelectOneQuery",
        "from polyapi.tabi.catalog.bad import events",
        "from polyapi.tabi.catalog.bad import eventsSelectOneQuery",
    ],
)
def test_tabi_namespace_narrowing_keeps_required_table_guards(project, dynamic, usage):
    config, args, logger, client, state, files = project
    config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    source = (
        files["worker"]
        .read_text()
        .replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example", "catalog.good"]',
        )
    )
    source = "from polyapi.tabi import catalog\n" + source.replace(
        "return True", f"catalog.good.events.count({{}})\n    {usage}\n    return True"
    )
    files["worker"].write_text(source)
    good = unsupported_table("catalog.good")
    good["schema"]["properties"].pop("external-id")
    client.shared_specs.extend([good, unsupported_table("catalog.bad")])
    with pytest.raises(
        flow.DeployError,
        match="Invalid dependency function|Cannot generate SDK table exports",
    ):
        deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == []
    assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("explicit_scope", [False, True])
@pytest.mark.parametrize(
    "form", ["module_import", "qualified_import", "namespace_alias"]
)
def test_tabi_module_root_table_collision_keeps_narrow_generation(
    project, dynamic, explicit_scope, form
):
    config, args, logger, client, state, files = project
    if not explicit_scope:
        config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    source = files["worker"].read_text()
    if explicit_scope:
        source = source.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example", "catalog.good"]',
        )
    prefix = {
        "module_import": "from polyapi.tabi import catalog\n",
        "qualified_import": "import polyapi.tabi.catalog as catalog\n",
        "namespace_alias": "from polyapi import tabi\ncatalog = tabi.catalog\n",
    }[form]
    files["worker"].write_text(
        prefix
        + source.replace("return True", "return bool(catalog.good.events.count({}))")
    )
    good = unsupported_table("catalog.good")
    good["schema"]["properties"].pop("external-id")
    client.shared_specs.extend([good, unsupported_table("catalog.bad")])
    # First prove the selected module is valid, then add the unrelated collision.
    deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    client.writes.clear()
    root = unsupported_table("")
    root["name"] = "catalog"
    root["schema"]["properties"].pop("external-id")
    client.shared_specs.append(root)
    deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == ["example.worker"]
    assert client.functions["example.worker"][1]["generateContexts"] == [
        "example",
        "catalog.good",
    ]


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("form", ["module_import", "namespace_alias"])
@pytest.mark.parametrize("used", [False, True])
def test_shared_client_tabi_module_root_table_collision(project, dynamic, form, used):
    config, args, logger, client, state, files = project
    config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    prefix = (
        "from polyapi.tabi import catalog\n"
        if form == "module_import"
        else "from polyapi import tabi\ncatalog = tabi.catalog\n"
    )
    client.shared_specs.append(
        {
            "type": "customFunction",
            "language": "python",
            "context": "shared",
            "name": "reader",
            "code": prefix
            + "async def reader() -> bool:\n    return "
            + ("bool(catalog.good.events.count({}))" if used else "True")
            + "\n",
        }
    )
    files["worker"].write_text(
        files["worker"]
        .read_text()
        .replace("poly.example.middle()", "poly.shared.reader()")
    )
    if not used:
        files["worker"].write_text(
            files["worker"]
            .read_text()
            .replace(
                '"context": "example"',
                '"context": "example", "generate_contexts": ["shared", "catalog.good"]',
            )
        )
    for context, name in [("catalog.good", "events"), ("", "catalog")]:
        table = unsupported_table(context)
        table["name"] = name
        table["schema"]["properties"].pop("external-id")
        client.shared_specs.append(table)
    deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == ["example.worker"]
    assert client.functions["example.worker"][1]["generateContexts"] == [
        "shared",
        "catalog.good",
    ]


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("explicit_scope", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        "catalog.count({})",
        "external(catalog)",
        "getattr(catalog, operation)({})",
        "class Reader:\n        table = catalog",
        "from polyapi.tabi import catalogSelectOneQuery",
    ],
)
def test_tabi_module_collision_preserves_root_table_scope_guards(
    project, dynamic, explicit_scope, usage
):
    config, args, logger, client, state, files = project
    if not explicit_scope:
        config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    source = files["worker"].read_text()
    if explicit_scope:
        source = source.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example", "catalog.good"]',
        )
    files["worker"].write_text(
        "from polyapi.tabi import catalog\n"
        + source.replace(
            "return True",
            f"catalog.good.events.count({{}})\n    {usage}\n    return True",
        )
    )
    for context, name in [("catalog.good", "events"), ("", "catalog")]:
        table = unsupported_table(context)
        table["name"] = name
        table["schema"]["properties"].pop("external-id")
        client.shared_specs.append(table)
    with pytest.raises(
        flow.DeployError,
        match="Invalid dependency function|require unfiltered generation",
    ):
        deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == []
    assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


def test_native_sdk_filtered_tabi_module_with_root_name_collision(
    tmp_path, monkeypatch
):
    import importlib
    from polyapi import poly_tables

    package = tmp_path / "tabi_collision_probe"
    package.mkdir()
    (package / "__init__.py").write_text("")
    monkeypatch.setattr(poly_tables, "__file__", str(package / "poly_tables.py"))
    good = unsupported_table("catalog.good")
    good["schema"]["properties"].pop("external-id")
    # The platform's catalog.good context filter excludes the root catalog table.
    poly_tables.generate_tables([good])
    monkeypatch.syspath_prepend(str(tmp_path))
    catalog = importlib.import_module("tabi_collision_probe.tabi.catalog")
    assert catalog.good.events.table_id == good["id"]
    assert catalog.good.eventsSelectOneQuery


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("scope", ["selected", "missing", "excluded", "excluded-only"])
@pytest.mark.parametrize(
    "prefix",
    [
        "from polyapi.tabi import catalog\n",
        "import polyapi.tabi.catalog as catalog\n",
        "from polyapi import tabi\ncatalog = tabi.catalog\n",
        "from polyapi import tabi\ndef accept(resource):\n    return True\naccept(tabi.catalog)\n",
    ],
)
def test_unused_tabi_collision_requires_a_generated_module(
    project, dynamic, scope, prefix
):
    config, args, logger, client, state, files = project
    selection = {
        "selected": '["example", "catalog.good"]',
        "missing": '["example"]',
        "excluded": '["example", "catalog.good"]',
        "excluded-only": '["catalog.good"]',
    }[scope]
    if scope.startswith("excluded"):
        config = replace(config, orphan_excluded_contexts=("catalog.good",))
    files["worker"].write_text(
        prefix
        + files["worker"]
        .read_text()
        .replace(
            '"context": "example"',
            f'"context": "example", "generate_contexts": {selection}',
        )
    )
    for context, name in [("catalog.good", "events"), ("", "catalog")]:
        table = unsupported_table(context)
        table["name"] = name
        table["schema"]["properties"].pop("external-id")
        client.shared_specs.append(table)
    if scope == "selected":
        deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
        assert client.writes == ["example.worker"]
        assert client.functions["example.worker"][1]["generateContexts"] == [
            "example",
            "catalog.good",
        ]
    else:
        with pytest.raises(
            flow.DeployError,
            match="Invalid dependency function|require unfiltered generation",
        ):
            deploy_table_scope_case(
                (config, args, logger, client, state, files), dynamic
            )
        assert client.writes == []
        assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("namespace", ["tabi", "vari"])
@pytest.mark.parametrize("scope", ["inferred", "explicit", "excluded"])
@pytest.mark.parametrize("argument", ["positional", "keyword", "default"])
def test_unused_root_resource_helper_arguments_preserve_scope_guards(
    project, dynamic, namespace, scope, argument
):
    config, args, logger, client, state, files = project
    source = files["worker"].read_text()
    if scope == "explicit":
        source = source.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example"]',
        )
    elif scope == "excluded":
        config = replace(config, orphan_excluded_contexts=("private",))
    signature, call = {
        "positional": ("resource", f"accept({namespace}.root_resource)"),
        "keyword": ("*, resource", f"accept(resource={namespace}.root_resource)"),
        # Defaults are evaluated even when the caller supplies a different value.
        "default": (f"resource={namespace}.root_resource", "accept(None)"),
    }[argument]
    files["worker"].write_text(
        f"from polyapi import {namespace}\ndef accept({signature}):\n    return True\n"
        + source.replace("return True", f"return {call}")
    )
    if namespace == "tabi":
        resource = unsupported_table("")
        resource["name"] = "root_resource"
        resource["schema"]["properties"].pop("external-id")
    else:
        resource = {
            "id": "synthetic-variable",
            "type": "serverVariable",
            "context": "",
            "name": "root_resource",
            "variable": {
                "secrecy": "NONE",
                "valueType": {"kind": "plain", "value": "string"},
            },
        }
    client.shared_specs.append(resource)
    if scope == "inferred":
        deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
        assert client.writes == ["example.worker"]
        assert client.functions["example.worker"][1]["generateContexts"] == []
    else:
        with pytest.raises(
            flow.DeployError,
            match="Invalid dependency function|require unfiltered generation",
        ):
            deploy_table_scope_case(
                (config, args, logger, client, state, files), dynamic
            )
        assert client.writes == []
        assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("namespace", ["tabi", "vari"])
@pytest.mark.parametrize("shared", [False, True])
def test_resource_helper_namespace_arguments_do_not_include_excluded_siblings(
    project, dynamic, namespace, shared
):
    config, args, logger, client, state, files = project
    config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    member = "count({})" if namespace == "tabi" else "get()"
    prefix = (
        f"from polyapi import {namespace}\n"
        f"def read(resources):\n    return resources.good.events.{member}\n"
    )
    call = f"read({namespace}.catalog)"
    if shared:
        client.shared_specs.append(
            {
                "type": "customFunction",
                "language": "python",
                "context": "shared",
                "name": "reader",
                "code": prefix
                + f"async def reader() -> bool:\n    return bool({call})\n",
            }
        )
        files["worker"].write_text(
            files["worker"]
            .read_text()
            .replace("poly.example.middle()", "poly.shared.reader()")
        )
    else:
        files["worker"].write_text(
            prefix
            + files["worker"].read_text().replace("return True", f"return bool({call})")
        )
    for context, name in [
        ("catalog.good", "events"),
        ("catalog.bad", "events"),
        ("", "catalog"),
    ]:
        if namespace == "tabi":
            resource = unsupported_table(context)
            resource["name"] = name
            if context != "catalog.bad":
                resource["schema"]["properties"].pop("external-id")
        else:
            resource = {
                "id": f"synthetic-variable-{context}",
                "type": "serverVariable",
                "context": context,
                "name": name,
                "variable": {
                    "secrecy": "NONE",
                    "valueType": {"kind": "plain", "value": "string"},
                },
            }
        client.shared_specs.append(resource)
    deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
    assert client.writes == ["example.worker"]
    assert client.functions["example.worker"][1]["generateContexts"] == [
        "shared" if shared else "example",
        "catalog.good",
    ]


@pytest.mark.parametrize("form", ["positional", "keyword", "default"])
def test_unused_callable_helper_argument_rebuilds_native_consumers(project, form):
    config, args, logger, client, state, files = project
    signature, call = {
        "positional": ("callback", "ignore(sdk.example.helper)"),
        "keyword": ("*, callback", "ignore(callback=sdk.example.helper)"),
        "default": ("callback=sdk.example.helper", "ignore(None)"),
    }[form]
    files["middle"].write_text(
        "from polyapi import poly as sdk\n"
        f"def ignore({signature}):\n    return True\n"
        + files["middle"].read_text().replace("await poly.example.helper()", call)
    )
    prepared = plan(project, seeds=("example.helper",), seed_from="none")
    assert set(prepared.order) == {
        "example.helper",
        "example.middle",
        "example.worker",
        "example.other_worker",
    }
    assert (
        prepared.order.index("example.helper")
        < prepared.order.index("example.middle")
        < prepared.order.index("example.worker")
    )
    summary = execute(project, prepared)
    assert summary.functions.failed == 0
    assert client.writes == prepared.order
    assert "example.http_caller" not in client.writes


@pytest.mark.parametrize(
    "prefix",
    [
        "from polyapi.poly.example import helper as unused\n",
        "from polyapi import poly\nunused = poly.example.helper\n",
    ],
)
def test_unused_callable_import_or_alias_rebuilds_native_consumers(project, prefix):
    config, args, logger, client, state, files = project
    files["middle"].write_text(
        prefix
        + files["middle"].read_text().replace("    await poly.example.helper()\n", "")
    )
    prepared = plan(project, seeds=("example.helper",), seed_from="none")
    assert set(prepared.order) == {
        "example.helper",
        "example.middle",
        "example.worker",
        "example.other_worker",
    }
    summary = execute(project, prepared)
    assert summary.functions.failed == 0
    assert client.writes == prepared.order


@pytest.mark.parametrize("namespace", ["tabi", "vari"])
@pytest.mark.parametrize("context", ["settings", "settings.nested"])
@pytest.mark.parametrize("excluded", [False, True])
@pytest.mark.parametrize("name", ["preferences", "get", "count"])
def test_new_resource_values_are_included_before_catalog_creation(
    project, namespace, context, excluded, name
):
    config, args, logger, client, state, files = project
    kind = "tables" if namespace == "tabi" else "variables"
    config = replace(
        config,
        namespaces=("example", "settings"),
        enabled_types=(kind, "functions"),
        orphan_excluded_contexts=("settings.private",) if excluded else (),
    )
    artifact = (
        config.repo_root / f"workflows/src/settings/artefacts/{namespace}/{name}.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            {
                "name": name,
                "context": context,
                **(
                    {"columns": [{"name": "id", "type": "text"}]}
                    if namespace == "tabi"
                    else {"value": "placeholder", "secrecy": "NONE"}
                ),
            }
        )
    )
    selected = flow.load_artifact_files(
        config,
        args,
        config.table_globs if namespace == "tabi" else config.variable_globs,
    )
    assert [item.data["context"] for item in selected] == [context]
    files["worker"].write_text(
        f"from polyapi import {namespace}\ndef ignore(resource):\n    return True\n"
        + files["worker"]
        .read_text()
        .replace("return True", f"return ignore({namespace}.{context}.{name})")
    )
    if excluded and context == "settings":
        # The SDK cannot select the parent while excluding its child.
        with pytest.raises(flow.DeployError, match="Invalid dependency function"):
            flow.plan_dynamic_functions(
                client,
                config,
                replace(args, seeds=("example.worker",), seed_from="none"),
                logger,
                state,
            )
        assert client.writes == []
        return
    prepared = flow.plan_dynamic_functions(
        client,
        config,
        replace(args, seeds=("example.worker",), seed_from="none"),
        logger,
        state,
    )
    assert prepared.payloads["example.worker"]["generateContexts"] == [
        "example",
        context if excluded else "settings",
    ]
    # Resource synchronization runs after planning. Execute the prepared payload
    # unchanged, as the real pipeline does, once the new resource exists.
    resource = unsupported_table(context)
    resource["schema"]["properties"].pop("external-id")
    resource["name"] = name
    if namespace == "vari":
        resource = {
            "id": "synthetic-variable",
            "type": "serverVariable",
            "context": context,
            "name": name,
            "variable": {
                "secrecy": "NONE",
                "valueType": {"kind": "plain", "value": "string"},
            },
        }
    client.shared_specs.append(resource)
    summary = flow.Summary()
    flow.execute_dynamic_functions(
        prepared, client, config, args, logger, summary, state
    )
    assert summary.functions.failed == 0
    assert client.writes == ["example.worker"]
    assert (
        client.functions["example.worker"][1]["generateContexts"]
        == prepared.payloads["example.worker"]["generateContexts"]
    )


@pytest.mark.parametrize("namespace,name", [("vari", "get"), ("tabi", "count")])
@pytest.mark.parametrize("selection", ["disabled", "path_filter", "skip_path"])
def test_unselected_local_resource_does_not_override_root_scope_guard(
    project, namespace, name, selection
):
    config, args, logger, client, state, files = project
    kind = "variables" if namespace == "vari" else "tables"
    config = replace(
        config,
        namespaces=("example", "settings"),
        enabled_types=(kind, "functions"),
        orphan_excluded_contexts=("private",),
    )
    artifact = (
        config.repo_root / f"workflows/src/settings/artefacts/{namespace}/{name}.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"context": "settings", "name": name}))
    if selection == "disabled":
        config = replace(config, enabled_types=("functions",))
    elif selection == "skip_path":
        config = replace(config, skip_paths=(artifact,))
    else:
        args = replace(args, path_filters=(files["worker"],))
    files["worker"].write_text(
        f"from polyapi import {namespace}\n"
        + files["worker"]
        .read_text()
        .replace("return True", f"return {namespace}.settings.{name}")
    )
    with pytest.raises(flow.DeployError, match="Invalid dependency function"):
        flow.plan_dynamic_functions(
            client,
            config,
            replace(args, seeds=("example.worker",), seed_from="none"),
            logger,
            state,
        )
    assert client.writes == []


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize(
    "form", ["module_import", "qualified_import", "namespace_alias"]
)
@pytest.mark.parametrize(
    "use", ["child", "unused", "missing", "root_call", "root_value"]
)
def test_vari_module_collision_preserves_narrowing_and_root_guards(
    project, monkeypatch, dynamic, form, use
):
    import sys
    import types

    import polyapi

    # The fake platform's catalog must not come from the developer's SDK.
    variables = types.ModuleType("polyapi.vari")
    catalog = types.ModuleType("polyapi.vari.catalog")
    variables.__dict__["catalog"] = catalog
    monkeypatch.setitem(sys.modules, "polyapi.vari", variables)
    monkeypatch.setitem(sys.modules, "polyapi.vari.catalog", catalog)
    monkeypatch.setitem(polyapi.__dict__, "vari", variables)
    config, args, logger, client, state, files = project
    config = replace(config, orphan_excluded_contexts=("catalog.bad",))
    selection = '["example"]' if use == "missing" else '["example", "catalog.good"]'
    prefix = {
        "module_import": "from polyapi.vari import catalog\n",
        "qualified_import": "import polyapi.vari.catalog as catalog\n",
        "namespace_alias": "from polyapi import vari\ncatalog = vari.catalog\n",
    }[form]
    body = {
        "child": "return bool(catalog.good.setting.get())",
        "unused": "return True",
        "missing": "return True",
        "root_call": "catalog.good.setting.get()\n    return bool(catalog.get())",
        "root_value": "catalog.good.setting.get()\n    return bool(catalog)",
    }[use]
    files["worker"].write_text(
        prefix
        + files["worker"]
        .read_text()
        .replace(
            '"context": "example"',
            f'"context": "example", "generate_contexts": {selection}',
        )
        .replace("return True", body)
    )
    for context, name in [
        ("", "catalog"),
        ("catalog.good", "setting"),
        ("catalog.bad", "setting"),
    ]:
        client.shared_specs.append(
            {
                "id": f"synthetic-{context}",
                "type": "serverVariable",
                "context": context,
                "name": name,
                "variable": {
                    "secrecy": "NONE",
                    "valueType": {"kind": "plain", "value": "string"},
                },
            }
        )
    if use in {"child", "unused"}:
        deploy_table_scope_case((config, args, logger, client, state, files), dynamic)
        assert client.writes == ["example.worker"]
        assert client.functions["example.worker"][1]["generateContexts"] == [
            "example",
            "catalog.good",
        ]
    else:
        with pytest.raises(
            flow.DeployError,
            match="Invalid dependency function|require unfiltered generation",
        ):
            deploy_table_scope_case(
                (config, args, logger, client, state, files), dynamic
            )
        assert client.writes == []
        assert not (config.repo_root / flow.ResumeState.FILENAME).exists()


def test_native_sdk_filtered_vari_module_with_root_name_collision(
    tmp_path, monkeypatch
):
    import importlib
    import sys
    from types import ModuleType
    from polyapi import variables

    # Full SDK generation supplies this sibling module; this focused test only
    # generates Vari and never connects a variable update listener.
    client_id = ModuleType("polyapi.poly.client_id")
    client_id.__dict__["client_id"] = "synthetic-client"
    monkeypatch.setitem(sys.modules, "polyapi.poly.client_id", client_id)
    package = tmp_path / "vari_collision_probe"
    package.mkdir()
    (package / "__init__.py").write_text("")
    monkeypatch.setattr(variables, "__file__", str(package / "variables.py"))
    variables.create_variable(
        {
            "id": "synthetic-variable",
            "type": "serverVariable",
            "context": "catalog.good",
            "name": "setting",
            "variable": {
                "secrecy": "NONE",
                "valueType": {"kind": "plain", "value": "string"},
            },
        }
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    catalog = importlib.import_module("vari_collision_probe.vari.catalog")
    assert catalog.good.setting.variable_id == "synthetic-variable"


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_dynamic_confirmation_aliases_preserve_other_guards(flag):
    args = flow.parse_args(["push-dynamic", flag])
    assert args.yes
    assert not args.force
    assert not args.skip_validation_acknowledged


@pytest.mark.parametrize("existing_table", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        "selected_context",
        "direct",
        "query_type",
        "shared_client",
        "unrelated",
        "excluded",
    ],
)
def test_table_sync_revalidates_prepared_server_scopes_before_function_writes(
    project, existing_table, usage
):
    config, args, logger, client, state, files = project
    config = replace(config, enabled_types=("tables", "functions"))
    context = "example" if usage == "selected_context" else "catalog"
    valid = unsupported_table(context)
    valid["schema"]["properties"].pop("external-id")
    if existing_table:
        client.shared_specs.append(valid)
    source = files["worker"].read_text()
    if usage in {"direct", "query_type", "shared_client"}:
        reference = "tabi.catalog.events"
        reference += "SelectOneQuery" if usage == "query_type" else ".table_id"
        if usage == "shared_client":
            client.shared_specs.append(
                {
                    "type": "customFunction",
                    "language": "python",
                    "context": "shared",
                    "name": "read_table",
                    "code": f"from polyapi import tabi\nasync def read_table() -> bool:\n    return bool({reference})\n",
                }
            )
            reference = "await poly.shared.read_table()"
        source = "from polyapi import tabi\n" + source.replace(
            "return True", f"return bool({reference})"
        )
        source = source.replace(
            '"context": "example"',
            '"context": "example", "generate_contexts": ["example", "shared"]',
        )
    if usage == "excluded":
        config = replace(config, orphan_excluded_contexts=("catalog",))
    files["worker"].write_text(source)
    args = replace(args, seeds=("example.worker",), seed_from="none")
    prepared = flow.plan_dynamic_functions(client, config, args, logger, state)
    # The prerequisite resource step changed the catalog after the plan was built.
    client.shared_specs = [
        item for item in client.shared_specs if item["type"] != "table"
    ] + [unsupported_table(context)]
    summary = flow.Summary()
    if usage in {"unrelated", "excluded"}:
        flow.execute_dynamic_functions(
            prepared, client, config, args, logger, summary, state
        )
        assert summary.functions.failed == 0
        assert client.writes == ["example.worker"]
    else:
        with pytest.raises(flow.DeployError, match="Cannot generate SDK table exports"):
            flow.execute_dynamic_functions(
                prepared, client, config, args, logger, summary, state
            )
        assert client.writes == []
        assert not state._path.exists()
