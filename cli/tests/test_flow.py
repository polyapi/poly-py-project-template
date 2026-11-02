from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import pytest
from pytest import CaptureFixture, MonkeyPatch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "flow.py"
SPEC = importlib.util.spec_from_file_location("flow", SCRIPT_PATH)
assert SPEC and SPEC.loader
poly_deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = poly_deploy
SPEC.loader.exec_module(poly_deploy)


@pytest.fixture(autouse=True)
def _restore_os_environ():
    """Undo env writes made by the code under test, not just by monkeypatch.

    ``load_env`` copies the env file's variables straight into ``os.environ``.
    monkeypatch cannot revert
    those, so without this a test's env file leaks into every later test (and
    into other test modules in the same session).
    """
    snapshot = dict(os.environ)
    os.environ["POLYAPI_NAMESPACE"] = "example"
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def test_strip_json_comments_keeps_strings() -> None:
    raw = """
    {
      // keep this comment out
      "name": "value // not a comment",
      "nested": {
        /* block comment */
        "path": "x/*still-string*/y"
      }
    }
    """

    stripped = poly_deploy.strip_json_comments(raw)

    assert "// keep this comment out" not in stripped
    assert "/* block comment */" not in stripped
    assert '"value // not a comment"' in stripped
    assert '"x/*still-string*/y"' in stripped


def test_infer_generate_contexts_follows_cast_poly_alias() -> None:
    source = """
from typing import cast
from typing_extensions import Any
from polyapi import poly, vari

async def exchange_rates_orchestrator():
    sample_workflow_api = cast(Any, poly).example.sample_workflow
    await sample_workflow_api.update_occ_exchange_rates()
    return vari.example.loggers.datadog.credentials.dd_key.inject()
"""

    contexts = poly_deploy.infer_generate_contexts(source)

    assert contexts == ["example"]


def test_infer_generate_contexts_excludes_context_before_namespace_collapse() -> None:
    source = """
from polyapi import poly, vari

async def worker():
    await poly.example.excluded.v1.memberships_create()
    return vari.example.excluded.credentials.api_key.inject()
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == []


def test_infer_generate_contexts_narrows_parent_when_exclusions_configured() -> None:
    source = """
from polyapi import poly

async def worker():
    await poly.example.sample_workflow.update_occ_exchange_rates()
    await poly.OOB.provider.v2.configuration.setGlobalExchangeRates()
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.sample_workflow", "OOB.provider.v2.configuration"]


def test_infer_generate_contexts_scans_all_generated_polyapi_namespaces() -> None:
    source = """
from polyapi import poly, vari, tabi, schemas, webhook

async def worker():
    await poly.example.functions.orders.lookup()
    await vari.example.variables.settings.value.get_async()
    await tabi.example.tables.loyalty.profile_links.select_one({})
    record_type = schemas.example_schemas.loyalty.MemberRecord
    await webhook.example.webhooks.loyalty.member_created({})
    return record_type
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == [
        "example.functions.orders",
        "example.variables.settings",
        "example.tables.loyalty",
        "example_schemas.loyalty",
        "example.webhooks.loyalty",
    ]


@pytest.mark.parametrize("operation", sorted(poly_deploy.VARI_CONTEXT_METHODS))
def test_infer_generate_contexts_scans_vari_operations(operation: str) -> None:
    source = f"""
from polyapi import vari

async def worker():
    return await vari.example.settings.feature_flag.{operation}()
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.settings"]


def test_infer_generate_contexts_scans_bound_vari_method() -> None:
    source = """
from polyapi import vari

reader = vari.example.loggers.datadog.config.platform.get
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.loggers.datadog.config"]


def test_infer_generate_contexts_scans_dynamic_vari_static_base() -> None:
    source = """
from polyapi import vari

db_vari = getattr(vari.example.database, db_name)
service_config_vari = db_vari.service_config
config = service_config_vari.get()
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.database"]


def test_infer_generate_contexts_scans_dynamic_bound_vari_method() -> None:
    source = """
from polyapi import vari

reader = getattr(vari.example.loggers.datadog.config, config_name).get
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.loggers.datadog.config"]


def test_infer_generate_contexts_follows_cast_vari_alias() -> None:
    source = """
from typing import cast
from typing_extensions import Any
from polyapi import vari

service_vari = cast(Any, vari).example.sample_workflow.service
configuration = service_vari.configuration.get()
api_key = service_vari.api_key.inject()
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.sample_workflow.service"]


def test_vari_operation_results_are_not_sdk_aliases() -> None:
    source = """
from polyapi import vari

configuration = vari.example.sample_workflow.service.configuration.get()
configuration.get("hostUrl")
"""
    references = poly_deploy.inspect_polyapi_references(source)["vari"]
    assert references == ["example.sample_workflow.service.configuration.get"]
    assert poly_deploy.infer_generate_contexts(source, ("example.excluded",)) == [
        "example.sample_workflow.service"
    ]


def test_infer_generate_contexts_rejects_unsafe_dynamic_vari_base() -> None:
    source = """
from polyapi import vari

dynamic_context = getattr(vari.example, context_name)
safe_value = vari.example.routing.rules.get()
"""

    with pytest.raises(
        poly_deploy.DeployError,
        match="Cannot safely infer dynamic Vari access from broad context 'example'",
    ):
        poly_deploy.infer_generate_contexts(source, ("example.excluded",))


def test_infer_generate_contexts_skips_excluded_dynamic_vari_base() -> None:
    source = """
from polyapi import vari

dynamic_value = getattr(vari.example.excluded, variable_name)
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == []


def test_infer_generate_contexts_supports_polyapi_namespace_aliases() -> None:
    source = """
import polyapi as sdk
from polyapi import tabi as table_sdk

async def worker():
    await sdk.poly.example.alias.functions.run()
    await sdk.vari.example.alias.settings.value.inject_async()
    await table_sdk.example.alias.database.rows.select_many({})
    record_type = sdk.schemas.example_alias.models.Record
    await sdk.webhook.example.alias.hooks.created({})
    return record_type
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == [
        "example.alias.functions",
        "example.alias.settings",
        "example.alias.database",
        "example_alias.models",
        "example.alias.hooks",
    ]


def test_infer_generate_contexts_supports_generated_submodule_imports() -> None:
    source = """
from polyapi.schemas.example.records import MemberRecord
from polyapi.tabi.example.tables import profile_linksSelectOneQuery

record_type = MemberRecord
query_type = profile_linksSelectOneQuery
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.tables", "example.records"]


def test_infer_generate_contexts_ignores_static_polyapi_imports() -> None:
    source = """
from polyapi import polyCustom, typedefs, config, exceptions
from polyapi.typedefs import PolyClientFunction
from polyapi.config import get_api_key_and_url
from polyapi.exceptions import PolyApiException
from polyapi.execute import execute_async

polyConfig: PolyClientFunction = {}
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == []


@pytest.mark.parametrize(
    "source, expected_import",
    [
        ("from polyapi import jobs\n", "polyapi.jobs"),
        ("import polyapi.jobs\n", "polyapi.jobs"),
        ("from polyapi.jobs import run\n", "polyapi.jobs"),
        ("import polyapi\npolyapi.jobs.run()\n", "polyapi.jobs"),
        ("from polyapi import *\n", "polyapi.*"),
    ],
)
def test_infer_generate_contexts_rejects_unknown_polyapi_imports(
    source: str,
    expected_import: str,
) -> None:
    with pytest.raises(
        poly_deploy.DeployError,
        match=rf"Unsupported PolyAPI import\(s\): {re.escape(expected_import)}",
    ):
        poly_deploy.infer_generate_contexts(source, ("example.excluded",))


def test_infer_generate_contexts_excludes_every_generated_namespace() -> None:
    source = """
from polyapi import poly, vari, tabi, schemas, webhook

async def worker():
    await poly.example.excluded.functions.run()
    await vari.example.excluded.settings.value.get_async()
    await tabi.example.excluded.tables.rows.select_many({})
    record_type = schemas.example.excluded.models.Record
    await webhook.example.excluded.hooks.created({})
    return await tabi.example.excluded2.audit.rows.select_many({"type": record_type})
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.excluded2.audit"]


def test_expand_poly_aliases_does_not_rewrite_function_result_assignments() -> None:
    source = """
from polyapi import poly

async def get_logger():
    logger: Logger = poly.example.utils.logger.datadog.init_logger(init_params)
    logger["info"]("ready")
"""

    expanded = poly_deploy._expand_poly_aliases(source)

    assert "poly.example.utils.logger.datadog.init_logger(init_params)" in expanded
    assert "poly.example.utils.poly" not in expanded


def test_infer_generate_contexts_follows_root_cast_poly_alias() -> None:
    source = """
from typing import cast
from typing_extensions import Any
from polyapi import poly

async def service_token_handler():
    poly_runtime = cast(Any, poly)
    return poly_runtime.example.service.currency.login()
"""

    contexts = poly_deploy.infer_generate_contexts(source, ("example.excluded",))

    assert contexts == ["example.service.currency"]


def test_transitive_contexts_follow_cast_poly_alias_to_client_oob_context() -> None:
    class DummyClient:
        def request(
            self, method: str, path: str, **_: object
        ) -> list[dict[str, str]] | dict[str, str]:
            assert method == "GET"
            if path == "/functions/server":
                return [{"id": "server-1"}]
            if path == "/specs":
                return [
                    {
                        "id": "client-1",
                        "context": "example.sample_workflow",
                        "name": "update_occ_exchange_rates",
                        "type": "customFunction",
                        "code": "\nfrom polyapi import poly\n\nasync def update_occ_exchange_rates():\n    return poly.OOB.provider.v2.configuration.setGlobalExchangeRates()\n",
                    }
                ]
            if path == "/functions/server/server-1":
                return {
                    "contextName": "example.sample_workflow.exchange_rates_orchestrator",
                    "code": """
from typing import cast
from typing_extensions import Any
from polyapi import poly

async def exchange_rates_orchestrator():
    sample_workflow_api = cast(Any, poly).example.sample_workflow
    await sample_workflow_api.update_occ_exchange_rates()
""",
                }
            raise AssertionError(path)

    client = DummyClient()
    root = client.request("GET", "/functions/server/server-1")
    func_context = poly_deploy.build_remote_func_context(
        client,
        local_func_context={
            root["contextName"]: poly_deploy.scan_function_dependencies(
                root["contextName"], root["code"]
            )
        },
    )

    assert poly_deploy.get_transitive_contexts(
        "example.sample_workflow.exchange_rates_orchestrator",
        func_context,
    ) == ["example", "OOB.provider"]


def test_transitive_contexts_exclude_context_before_namespace_collapse() -> None:
    fetched_details: list[str] = []

    class DummyClient:
        def request(
            self, method: str, path: str, **_: object
        ) -> list[dict[str, str]] | dict[str, str]:
            assert method == "GET"
            if path == "/functions/server":
                return [
                    {"id": "server-1", "context": "example.sample_workflow"},
                    {"id": "server-excluded", "context": "example.excluded.v1"},
                ]
            if path == "/specs":
                return []
            fetched_details.append(path)
            if path == "/functions/server/server-1":
                return {
                    "contextName": "example.sample_workflow.exchange_rates_orchestrator",
                    "code": """
from polyapi import poly

async def exchange_rates_orchestrator():
    await poly.example.excluded.v1.memberships_create()
    return await poly.OOB.provider.v2.configuration.setGlobalExchangeRates()
""",
                }
            raise AssertionError(path)

    client = DummyClient()
    root = client.request("GET", "/functions/server/server-1")
    func_context = poly_deploy.build_remote_func_context(
        client,
        ("example.excluded",),
        local_func_context={
            root["contextName"]: poly_deploy.scan_function_dependencies(
                root["contextName"], root["code"], ("example.excluded",)
            )
        },
    )

    assert "/functions/server/server-excluded" not in fetched_details
    assert poly_deploy.get_transitive_contexts(
        "example.sample_workflow.exchange_rates_orchestrator",
        func_context,
        ("example.excluded",),
    ) == ["OOB.provider.v2.configuration"]


def test_transitive_contexts_reach_datadog_send_through_logger_factory() -> None:
    class DummyClient:
        def request(
            self, method: str, path: str, **_: object
        ) -> list[dict[str, str]] | dict[str, str]:
            assert method == "GET"
            if path == "/functions/server":
                return [{"id": "server-1"}]
            if path == "/specs":
                return [
                    {
                        "id": "get-logger",
                        "context": "example.utils.logger.datadog",
                        "name": "get_logger",
                        "type": "customFunction",
                        "code": "\nfrom polyapi import poly\n\nasync def get_logger():\n    logger: Logger = poly.example.utils.logger.datadog.init_logger(init_params)\n    return logger\n",
                    },
                    {
                        "id": "init-logger",
                        "context": "example.utils.logger.datadog",
                        "name": "init_logger",
                        "type": "customFunction",
                        "code": "\nfrom polyapi import poly\n\ndef init_logger():\n    send = poly.example.utils.logger.datadog.send\n    send([])\n",
                    },
                    {
                        "id": "send",
                        "context": "example.utils.logger.datadog",
                        "name": "send",
                        "type": "customFunction",
                        "code": "\nfrom polyapi import poly\n\nasync def send():\n    return await poly.OOB.datadog.logs.send_async()\n",
                    },
                ]
            if path == "/functions/server/server-1":
                return {
                    "contextName": "example.sample_workflow.exchange_rates_orchestrator",
                    "code": """
from polyapi import poly

async def exchange_rates_orchestrator():
    await poly.example.utils.logger.datadog.get_logger({})
""",
                }
            raise AssertionError(path)

    client = DummyClient()
    root = client.request("GET", "/functions/server/server-1")
    func_context = poly_deploy.build_remote_func_context(
        client,
        local_func_context={
            root["contextName"]: poly_deploy.scan_function_dependencies(
                root["contextName"], root["code"]
            )
        },
    )

    assert poly_deploy.get_transitive_contexts(
        "example.sample_workflow.exchange_rates_orchestrator",
        func_context,
    ) == ["example", "OOB.datadog"]


def test_local_transitive_contexts_include_datadog_logger_vari_dependencies(
    tmp_path: Path,
) -> None:
    server_path = tmp_path / "sample_workflow" / "server" / "worker_sfx.py"
    get_logger_path = tmp_path / "utilities" / "common" / "client" / "get_logger_cfx.py"
    init_logger_path = (
        tmp_path / "utilities" / "common" / "client" / "init_logger_cfx.py"
    )
    send_path = tmp_path / "utilities" / "common" / "client" / "send_cfx.py"
    for path in (server_path, get_logger_path, init_logger_path, send_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    server_path.write_text(
        """
from polyapi import poly, vari

async def worker() -> dict:
    vari.example.loggers.datadog.credentials.dd_key.inject()
    return await poly.example.utils.logger.datadog.get_logger({})
""",
        encoding="utf-8",
    )
    get_logger_path.write_text(
        """
from polyapi import poly, vari

allowed_list_raw = vari.example.loggers.datadog.source_systems.get()
platform_config = vari.example.loggers.datadog.config.platform.get()

async def get_logger() -> dict:
    return poly.example.utils.logger.datadog.init_logger({})
""",
        encoding="utf-8",
    )
    init_logger_path.write_text(
        """
from polyapi import poly

def init_logger(params: dict) -> dict:
    return poly.example.utils.logger.datadog.send([])
""",
        encoding="utf-8",
    )
    send_path.write_text(
        """
from polyapi import poly

def send(records: list) -> dict:
    return poly.OOB.datadog.logs.send(records)
""",
        encoding="utf-8",
    )

    server_spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.sample_workflow",
        function_type="server",
        source_path=server_path,
        discovery_path=server_path,
    )
    specs = [
        server_spec,
        poly_deploy.FunctionSpec(
            name="get_logger",
            context="example.utils.logger.datadog",
            function_type="client",
            source_path=get_logger_path,
            discovery_path=get_logger_path,
        ),
        poly_deploy.FunctionSpec(
            name="init_logger",
            context="example.utils.logger.datadog",
            function_type="client",
            source_path=init_logger_path,
            discovery_path=init_logger_path,
        ),
        poly_deploy.FunctionSpec(
            name="send",
            context="example.utils.logger.datadog",
            function_type="client",
            source_path=send_path,
            discovery_path=send_path,
        ),
    ]

    func_context = poly_deploy.build_local_func_context(specs, ("example.excluded",))
    contexts = poly_deploy.resolve_server_generate_contexts(
        server_spec,
        server_path.read_text(encoding="utf-8"),
        func_context,
        ("example.excluded",),
    )

    assert contexts == [
        "example.utils.logger.datadog",
        "example.loggers",
        "OOB.datadog.logs",
    ]


def test_parse_function_spec_from_shell_line_resolves_parent_source(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "playground"
    deployment_dir = workflow_root / "deployment"
    client_dir = workflow_root / "client"
    deployment_dir.mkdir(parents=True)
    client_dir.mkdir(parents=True)

    script_path = deployment_dir / "deployment.sh"
    source_path = client_dir / "get_provider_access_token_cfx.py"
    source_path.write_text("async def get_provider_access_token():\n    return {}\n")

    spec = poly_deploy.parse_function_spec_from_shell_line(
        script_path,
        "python -m polyapi function add get_provider_access_token ./client/get_provider_access_token_cfx.py --context example.test --client",
        1,
    )

    assert spec is not None
    assert spec.source_path == source_path.resolve()
    assert spec.function_type == "client"
    assert spec.context == "example.test"


def test_load_function_specs_from_shell_supports_line_continuations(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "utilities" / "oracledb_adapter"
    deployment_dir = workflow_root / "deployment"
    server_dir = workflow_root / "server"
    deployment_dir.mkdir(parents=True)
    server_dir.mkdir(parents=True)

    script_path = deployment_dir / "deployment.sh"
    source_path = server_dir / "oracle_db_adapter_sfx.py"
    source_path.write_text(
        "async def oracle_db_adapter():\n    return {}\n", encoding="utf-8"
    )
    script_body = (
        "\n".join(
            (
                'DEPLOY_LOG="$(mktemp)"',
                "python -m polyapi function add oracle_db_adapter ../server/oracle_db_adapter_sfx.py \\",
                "  --context example.utils.database \\",
                "  --server \\",
                "  --logs enabled \\",
                '  --generate-contexts OOB.datadog,example,OOB.polyapi 2>&1 | tee "${DEPLOY_LOG}"',
            )
        )
        + "\n"
    )
    script_path.write_text(script_body, encoding="utf-8")

    specs = poly_deploy.load_function_specs_from_shell(script_path)

    assert len(specs) == 1
    assert specs[0].source_path == source_path.resolve()
    assert specs[0].function_type == "server"
    assert specs[0].context == "example.utils.database"
    assert specs[0].logs_enabled is True
    assert specs[0].generate_contexts == ("OOB.datadog", "example", "OOB.polyapi")


def test_parse_function_spec_from_shell_line_requires_context(tmp_path: Path) -> None:
    deployment_dir = tmp_path / "deployment"
    client_dir = tmp_path / "client"
    deployment_dir.mkdir(parents=True)
    client_dir.mkdir(parents=True)
    script_path = deployment_dir / "deployment.sh"
    source_path = client_dir / "worker_cfx.py"
    source_path.write_text("async def worker():\n    return {}\n", encoding="utf-8")

    with pytest.raises(poly_deploy.DeployError, match="Missing --context"):
        poly_deploy.parse_function_spec_from_shell_line(
            script_path,
            "python -m polyapi function add worker ./client/worker_cfx.py --client",
            1,
        )


def test_parse_function_spec_from_shell_line_marks_description_explicit(
    tmp_path: Path,
) -> None:
    deployment_dir = tmp_path / "deployment"
    client_dir = tmp_path / "client"
    deployment_dir.mkdir(parents=True)
    client_dir.mkdir(parents=True)
    script_path = deployment_dir / "deployment.sh"
    source_path = client_dir / "worker_cfx.py"
    source_path.write_text("async def worker():\n    return {}\n", encoding="utf-8")

    spec = poly_deploy.parse_function_spec_from_shell_line(
        script_path,
        (
            "python -m polyapi function add worker ../client/worker_cfx.py "
            "--context example.test --description='Shell description' --client"
        ),
        1,
    )

    assert spec is not None
    assert spec.description_explicit is True
    assert spec.description == "Shell description"


def test_load_function_specs_from_manifest_requires_context(tmp_path: Path) -> None:
    manifest_path = tmp_path / "functions.json"
    source_path = tmp_path / "worker_cfx.py"
    source_path.write_text("async def worker():\n    return {}\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "functions": [
                    {
                        "name": "worker",
                        "file": "worker_cfx.py",
                        "type": "client",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(poly_deploy.DeployError, match="needs a non-empty context"):
        poly_deploy.load_function_specs_from_manifest(manifest_path)


def test_load_function_specs_from_manifest_marks_description_explicit(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "functions.json"
    source_path = tmp_path / "worker_cfx.py"
    source_path.write_text("async def worker():\n    return {}\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "functions": [
                    {
                        "name": "worker",
                        "context": "example.test",
                        "file": "worker_cfx.py",
                        "type": "client",
                        "description": "",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = poly_deploy.load_function_specs_from_manifest(manifest_path)

    assert len(specs) == 1
    assert specs[0].description_explicit is True
    assert specs[0].description == ""


def test_resolve_function_id_with_reference() -> None:
    registry = poly_deploy.FunctionRegistry()
    registry.upsert(
        "server",
        {
            "id": "12345678-1234-1234-1234-123456789abc",
            "context": "example.test",
            "name": "worker",
        },
    )

    resolved = poly_deploy.resolve_function_id(
        "{{example.test.worker}}",
        registry,
        required_type="server",
    )

    assert resolved == "12345678-1234-1234-1234-123456789abc"


def test_substitute_env_tokens_only_replaces_defined_values(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("billing", "Billing##MaY2024")
    monkeypatch.delenv("nameId", raising=False)

    payload = {
        "value": "{{billing}}",
        "route": "GET_/profile/{{nameId}}",
        "nested": ["prefix-{{billing}}", "{{missing_value}}"],
    }

    substituted = poly_deploy.substitute_env_tokens(payload)

    assert substituted["value"] == "Billing##MaY2024"
    assert substituted["route"] == "GET_/profile/{{nameId}}"
    assert substituted["nested"] == ["prefix-Billing##MaY2024", "{{missing_value}}"]


def test_find_unresolved_env_tokens_only_matches_env_style_placeholders() -> None:
    payload = {
        "config": "{{IDENTITY_HOST_URL}}",
        "route": "GET_/profile/{{nameId}}",
        "functionId": "{{example.sample_workflow.job_dispatcher}}",
        "nested": ["{{RNA_OO_HOST_URL}}", "{{pageNo}}"],
    }

    assert poly_deploy.find_unresolved_env_tokens(payload) == {
        "IDENTITY_HOST_URL",
        "RNA_OO_HOST_URL",
    }


def test_collapse_context_prefixes_removes_child_contexts() -> None:
    collapsed = poly_deploy.collapse_context_prefixes(
        {
            "example.sample_workflow",
            "example.sample_workflow.jobs",
            "utils.logger.datadog",
            "utils.logger.datadog.send",
        }
    )

    assert collapsed == ("example.sample_workflow", "utils.logger.datadog")


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ("example.excluded", True),
        ("example.excluded.v1", True),
        ("example.excluded2", False),
        ("example.excludedExtra", False),
        ("OOB.excluded", False),
    ],
)
def test_context_exclusion_uses_segment_boundaries(
    context: str, expected: bool
) -> None:
    assert (
        poly_deploy.context_is_orphan_excluded(context, ("example.excluded",))
        is expected
    )


def test_context_compaction_removes_children_under_safe_parent() -> None:
    compacted = poly_deploy.compact_contexts_against_safe_parents(
        [
            "example.profiles",
            "example.profiles.get_profile_matches",
            "example.profiles.nh_profile_notification",
            "example.loggers.datadog",
            "example.loggers.datadog.credentials",
            "OOB.datadog.logs",
        ],
        ("example.excluded",),
    )

    assert compacted == [
        "example.profiles",
        "example.loggers",
        "OOB.datadog.logs",
    ]


def test_context_compaction_keeps_children_when_parent_has_excluded_descendant() -> (
    None
):
    compacted = poly_deploy.compact_contexts_against_safe_parents(
        ["example", "example.sample_workflow", "example.utils.logger.datadog"],
        ("example.excluded",),
    )

    assert compacted == [
        "example",
        "example.sample_workflow",
        "example.utils.logger.datadog",
    ]


def test_collect_local_context_prefixes_replaces_unsafe_broad_parent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "placeholder.py"
    source_path.write_text(
        "async def placeholder() -> dict:\n    return {}\n",
        encoding="utf-8",
    )
    specs = [
        poly_deploy.FunctionSpec(
            name=f"function_{index}",
            context=context,
            function_type="client",
            source_path=source_path,
            discovery_path=source_path,
        )
        for index, context in enumerate(
            (
                "example",
                "example.profiles",
                "example.utils",
                "example.excluded",
                "example.excluded2",
                "OOB.excluded",
            )
        )
    ]
    monkeypatch.setattr(
        poly_deploy,
        "load_function_specs",
        lambda _config, _cli_args, _logger: specs,
    )
    config = poly_deploy.DeployConfig(
        repo_root=tmp_path,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=("example.excluded",),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=("functions",),
        show_config=False,
    )

    contexts = poly_deploy.collect_local_context_prefixes(
        config,
        cli_args,
        poly_deploy.Logger(verbose=False),
    )

    assert contexts == (
        "OOB.excluded",
        "example.utils",
        "example.profiles",
        "example.excluded2",
    )


def test_collect_local_context_prefixes_includes_function_dependencies(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "active_cfx.py"
    source_path.write_text(
        """from polyapi import poly, vari


async def active() -> dict:
    await poly.example.utils.logger.datadog.get_logger()
    await vari.example.routing.rules.get_async()
    return await vari.example.excluded.credentials.get_async()
""",
        encoding="utf-8",
    )
    spec = poly_deploy.FunctionSpec(
        name="active",
        context="example.active",
        function_type="client",
        source_path=source_path,
        discovery_path=source_path,
    )
    monkeypatch.setattr(
        poly_deploy,
        "load_function_specs",
        lambda _config, _cli_args, _logger: [spec],
    )
    config = poly_deploy.DeployConfig(
        repo_root=tmp_path,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=("example.excluded",),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=("functions",),
        show_config=False,
    )

    contexts = poly_deploy.collect_local_context_prefixes(
        config,
        cli_args,
        poly_deploy.Logger(verbose=False),
    )

    assert contexts == (
        "example.active",
        "example.routing",
        "example.utils.logger.datadog",
    )


def test_resolve_scope_filter_supports_src_relative_shortcuts(tmp_path: Path) -> None:
    repo_root = tmp_path
    target = repo_root / "workflows/src" / "utilities" / "common"
    target.mkdir(parents=True)

    resolved = poly_deploy.resolve_scope_filter(repo_root, "utilities/common")

    assert resolved == target.resolve()


def test_normalize_cli_path_filters_supports_shortcuts(tmp_path: Path) -> None:
    repo_root = tmp_path
    target = repo_root / "workflows/src" / "sample_workflow"
    target.mkdir(parents=True)

    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(Path("sample_workflow"),),
        enabled_types=None,
        show_config=False,
    )

    normalized = poly_deploy.normalize_cli_path_filters(repo_root, cli_args)

    assert normalized.path_filters == (target.resolve(),)


def test_build_config_defaults_http_timeout_to_fifteen_minutes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("POLY_DEPLOY_HTTP_TIMEOUT_SECONDS", raising=False)
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    config = poly_deploy.build_config(tmp_path, cli_args)

    assert config.http_timeout_seconds == 900


def test_build_config_uses_configured_http_timeout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLY_DEPLOY_HTTP_TIMEOUT_SECONDS", "600")
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    config = poly_deploy.build_config(tmp_path, cli_args)

    assert config.http_timeout_seconds == 600


@pytest.mark.parametrize(
    ("primary_value", "legacy_value", "expected"),
    [
        (None, None, ()),
        ("", "", ()),
        (
            "example.xxx,example.excluded,example.yyy",
            None,
            ("example.xxx", "example.excluded", "example.yyy"),
        ),
        (
            None,
            "example.legacy,example.excluded",
            ("example.legacy", "example.excluded"),
        ),
        ("", "example.legacy", ("example.legacy",)),
    ],
)
def test_build_config_uses_only_configured_exclusions(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    primary_value: str | None,
    legacy_value: str | None,
    expected: tuple[str, ...],
) -> None:
    for name, value in (
        ("POLY_DEPLOY_EXCLUDE_ORPHAN_CONTEXTS", primary_value),
        ("EXCLUDE_ORPHANS", legacy_value),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    config = poly_deploy.build_config(tmp_path, cli_args)

    assert config.orphan_excluded_contexts == expected


def test_run_poly_generate_resolves_broad_contexts_with_exclusions(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    deployment_dir = repo_root / "workflows/src" / "active" / "deployment"
    client_dir = repo_root / "workflows/src" / "active" / "client"
    deployment_dir.mkdir(parents=True)
    client_dir.mkdir(parents=True)
    (client_dir / "active_cfx.py").write_text(
        """from polyapi import poly, vari


async def active() -> dict:
    await poly.example.utils.logger.datadog.get_logger()
    rules = await vari.example.routing.rules.get_async()
    credentials = await vari.example.client_headers.client_credentials.get_async()
    return {"rules": rules, "credentials": credentials}
""",
        encoding="utf-8",
    )
    (deployment_dir / "deployment.sh").write_text(
        "python -m polyapi function add active ../client/active_cfx.py --context example.active --client\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(poly_deploy.subprocess, "run", fake_run)

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=("workflows/src/**/deployment/*.sh",),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=("example", "OOB.polyapi"),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=("example.excluded",),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(repo_root / "workflows/src" / "active",),
        enabled_types=("functions",),
        show_config=False,
    )

    poly_deploy.run_poly_generate(
        config, poly_deploy.Logger(verbose=False), "test", cli_args
    )

    assert captured["command"] == [
        sys.executable,
        "-m",
        "polyapi",
        "generate",
        "--contexts",
        "example.active,example.routing,example.client_headers,example.utils.logger.datadog,OOB.polyapi",
    ]


def test_run_poly_generate_fails_before_subprocess_for_unresolved_broad_context(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_subprocess_call(*args: object, **kwargs: object) -> None:
        subprocess_calls.append((args, kwargs))

    monkeypatch.setattr(poly_deploy.subprocess, "run", record_subprocess_call)
    config = poly_deploy.DeployConfig(
        repo_root=tmp_path,
        enabled_types=(),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=("example",),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=("example.excluded",),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    with pytest.raises(
        poly_deploy.DeployError,
        match="Cannot safely resolve broad context 'example'",
    ):
        poly_deploy.run_poly_generate(
            config,
            poly_deploy.Logger(verbose=False),
            "test",
            cli_args,
        )

    assert subprocess_calls == []


def test_poly_api_client_passes_configured_timeout(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class DummyResponse:
        status_code = 204
        content = b""
        headers: dict[str, str] = {}

    client = poly_deploy.PolyApiClient(
        "https://example.polyapi.local",
        "api-key",
        timeout_seconds=123,
    )

    def fake_request(**kwargs: Any) -> DummyResponse:
        captured.update(kwargs)
        return DummyResponse()

    monkeypatch.setattr(client.session, "request", fake_request)

    client.request("GET", "/health")

    assert captured["timeout"] == 123


def test_poly_api_client_wraps_timeout_as_deploy_error(
    monkeypatch: MonkeyPatch,
) -> None:
    client = poly_deploy.PolyApiClient(
        "https://example.polyapi.local",
        "api-key",
        timeout_seconds=456,
    )

    def fake_request(**_: Any) -> object:
        raise poly_deploy.requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(client.session, "request", fake_request)

    with pytest.raises(poly_deploy.DeployError) as exc_info:
        client.request("POST", "/functions/server")

    assert str(exc_info.value) == "POST /functions/server timed out after 456s"


def test_resume_state_requires_matching_fingerprint_for_payload_resume(
    tmp_path: Path,
) -> None:
    resume_path = tmp_path / ".flow_resume.json"
    resume_path.write_text(
        json.dumps({"done": ["fn:server:example.active:worker"]}),
        encoding="utf-8",
    )

    target = ("https://example.invalid", "env-test")
    resume_state = poly_deploy.ResumeState(tmp_path, load=True, target=target)

    assert resume_state.is_done("fn:server:example.active:worker") is False
    assert resume_state.is_done("fn:server:example.active:worker", "new-hash") is False

    resume_state.mark_done("fn:server:example.active:worker", "new-hash")
    reloaded_state = poly_deploy.ResumeState(tmp_path, load=True, target=target)

    assert reloaded_state.is_done("fn:server:example.active:worker", "new-hash") is True
    assert (
        reloaded_state.is_done("fn:server:example.active:worker", "old-hash") is False
    )


def test_resume_state_clear_removes_resume_file(tmp_path: Path) -> None:
    resume_state = poly_deploy.ResumeState(tmp_path, load=False)

    resume_state.mark_done("fn:server:example.active:worker", "new-hash")
    assert (tmp_path / ".flow_resume.json").exists()

    resume_state.clear()

    assert not (tmp_path / ".flow_resume.json").exists()


def test_summary_has_incomplete_work_detects_failed_and_blocked_items() -> None:
    summary = poly_deploy.Summary()

    assert poly_deploy.summary_has_incomplete_work(summary) is False

    summary.functions.failed = 1
    assert poly_deploy.summary_has_incomplete_work(summary) is True

    summary.functions.failed = 0
    summary.variables.blocked = 1
    assert poly_deploy.summary_has_incomplete_work(summary) is True


def test_discover_paths_prioritizes_priority_paths(tmp_path: Path) -> None:
    repo_root = tmp_path
    utility_file = (
        repo_root
        / "workflows/src"
        / "utilities"
        / "common"
        / "artefacts"
        / "vari"
        / "a.json"
    )
    workflow_file = (
        repo_root
        / "workflows/src"
        / "sample_workflow"
        / "artefacts"
        / "vari"
        / "b.json"
    )
    utility_file.parent.mkdir(parents=True)
    workflow_file.parent.mkdir(parents=True)
    utility_file.write_text("{}\n", encoding="utf-8")
    workflow_file.write_text("{}\n", encoding="utf-8")

    discovered = poly_deploy.discover_paths(
        repo_root,
        ("workflows/src/**/artefacts/vari/*.json",),
        (),
        (repo_root / "workflows/src" / "utilities",),
    )

    assert discovered == [utility_file.resolve(), workflow_file.resolve()]


def test_normalize_function_payload_handles_null_lists() -> None:
    normalized = poly_deploy.normalize_function_payload(
        {
            "name": "worker",
            "generateContexts": None,
            "requirements": None,
        }
    )

    assert normalized["generateContexts"] == []
    assert normalized["requirements"] == []


def test_description_compare_ignores_remote_when_local_is_empty() -> None:
    local, remote = poly_deploy.normalize_description_fields_for_compare(
        {"name": "worker", "description": ""},
        {"name": "worker", "description": "server generated"},
    )

    assert local == remote


def test_description_compare_keeps_local_non_empty_description() -> None:
    local, remote = poly_deploy.normalize_description_fields_for_compare(
        {"name": "worker", "description": "local"},
        {"name": "worker", "description": "remote"},
    )

    assert local != remote


def test_summarize_function_payload_diff_omits_source_body() -> None:
    diffs = poly_deploy.summarize_function_payload_diff(
        {
            "code": "def worker() -> dict:\n    return {'ok': True}\n",
            "arguments": [{"name": "body", "type": "object", "key": "body"}],
        },
        {
            "code": "def worker() -> dict:\n    return {'ok': False}\n",
            "arguments": [{"name": "payload", "type": "object", "key": "payload"}],
        },
    )

    assert diffs == [
        "arguments: list entries changed: local=['body'], remote=['payload']",
        "code: string changed (local 46 chars/2 lines, remote 47 chars/2 lines, first differing line 2)",
    ]


def test_summarize_function_payload_diff_shows_argument_field_names() -> None:
    diffs = poly_deploy.summarize_function_payload_diff(
        {
            "arguments": [
                {"name": "payload", "key": "payload", "type": "object"},
            ],
        },
        {
            "arguments": [
                {
                    "name": "payload",
                    "key": "payload",
                    "type": "object",
                    "required": True,
                },
            ],
        },
    )

    assert diffs == [
        "arguments: list item fields changed: payload(required=remote-only)"
    ]


def test_summarize_function_payload_diff_shows_scalar_values() -> None:
    diffs = poly_deploy.summarize_function_payload_diff(
        {"maxExecutionTime": 60},
        {"maxExecutionTime": 3600},
    )

    assert diffs == ["maxExecutionTime: value changed (local 60, remote 3600)"]


def test_should_skip_unchanged_respects_force_flag() -> None:
    normal = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    forced = poly_deploy.CliArgs(
        dry_run=False,
        force=True,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    assert poly_deploy.should_skip_unchanged(normal, {"name": "job"}, {"name": "job"})
    assert not poly_deploy.should_skip_unchanged(
        forced, {"name": "job"}, {"name": "job"}
    )


def test_normalize_function_payload_ignores_empty_argument_metadata() -> None:
    local, remote = poly_deploy.normalize_description_fields_for_compare(
        poly_deploy.normalize_function_payload(
            {
                "name": "worker",
                "arguments": [
                    {
                        "name": "payload",
                        "key": "payload",
                        "type": "object",
                        "description": "",
                        "typeSchema": None,
                    }
                ],
            }
        ),
        poly_deploy.normalize_function_payload(
            {
                "name": "worker",
                "arguments": [
                    {
                        "name": "payload",
                        "key": "payload",
                        "type": "object",
                        "description": "server generated",
                    }
                ],
            }
        ),
    )

    assert local == remote


def test_normalize_function_payload_keeps_non_empty_argument_metadata() -> None:
    local, remote = poly_deploy.normalize_description_fields_for_compare(
        poly_deploy.normalize_function_payload(
            {
                "name": "worker",
                "arguments": [
                    {
                        "name": "payload",
                        "key": "payload",
                        "type": "object",
                        "description": "Payload to send.",
                    }
                ],
            }
        ),
        poly_deploy.normalize_function_payload(
            {
                "name": "worker",
                "arguments": [
                    {
                        "name": "payload",
                        "key": "payload",
                        "type": "object",
                    }
                ],
            }
        ),
    )

    assert local != remote


def test_normalize_function_subset_ignores_unreported_requirements() -> None:
    local, remote = poly_deploy.normalize_function_subset(
        {
            "name": "worker",
            "requirements": ["requests==2.32.0"],
        },
        {
            "name": "worker",
            "requirements": [],
        },
    )

    assert local == remote


def test_normalize_function_subset_keeps_reported_requirements_diff() -> None:
    local, remote = poly_deploy.normalize_function_subset(
        {
            "name": "worker",
            "requirements": ["requests==2.32.0"],
        },
        {
            "name": "worker",
            "requirements": ["requests==2.31.0"],
        },
    )

    assert local != remote


def test_description_compare_does_not_ignore_value_description() -> None:
    local, remote = poly_deploy.normalize_description_fields_for_compare(
        {
            "name": "config",
            "description": "",
            "value": {"description": ""},
        },
        {
            "name": "config",
            "description": "server generated",
            "value": {"description": "business value"},
        },
    )

    assert local != remote


def test_normalize_variable_subset_ignores_empty_optional_metadata() -> None:
    local, remote = poly_deploy.normalize_variable_subset(
        {
            "context": "example.test",
            "name": "configuration",
            "description": "",
            "expiresAt": None,
            "ownerUserId": None,
            "value": {"enabled": True},
        },
        {
            "context": "example.test",
            "name": "configuration",
            "value": {"enabled": True},
        },
    )

    assert local == remote


def test_normalize_variable_subset_keeps_non_empty_description() -> None:
    local, remote = poly_deploy.normalize_variable_subset(
        {
            "context": "example.test",
            "name": "configuration",
            "description": "Config",
            "value": {"enabled": True},
        },
        {
            "context": "example.test",
            "name": "configuration",
            "value": {"enabled": True},
        },
    )

    assert local != remote


def test_normalize_variable_subset_ignores_empty_local_value() -> None:
    local, remote = poly_deploy.normalize_variable_subset(
        {
            "context": "example.test",
            "name": "password",
            "value": "",
        },
        {
            "context": "example.test",
            "name": "password",
            "value": "********",
        },
    )

    assert local == remote


def test_build_variable_update_payload_omits_empty_value() -> None:
    payload = poly_deploy.build_variable_update_payload(
        {
            "context": "example.test",
            "name": "password",
            "value": "",
            "visibility": "ENVIRONMENT",
        }
    )

    assert "value" not in payload
    assert payload["visibility"] == "ENVIRONMENT"


def test_build_variable_update_payload_keeps_non_empty_value() -> None:
    payload = poly_deploy.build_variable_update_payload(
        {
            "context": "example.test",
            "name": "flag",
            "value": "enabled",
        }
    )

    assert payload["value"] == "enabled"


def test_load_function_specs_from_python_marks_poly_config_description_explicit(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "worker_cfx.py"
    source_path.write_text(
        (
            "from polyapi.typedefs import PolyClientFunction\n\n"
            "polyConfig: PolyClientFunction = {\n"
            '    "name": "worker",\n'
            '    "context": "example.test",\n'
            '    "description": "",\n'
            "}\n\n"
            "def worker() -> dict:\n"
            '    """Derived description."""\n'
            "    return {}\n"
        ),
        encoding="utf-8",
    )

    specs = poly_deploy.load_function_specs_from_python(source_path)

    assert len(specs) == 1
    assert specs[0].description_explicit is True
    assert specs[0].description == ""


def test_load_function_specs_from_python_leaves_docstring_description_derived(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "worker_cfx.py"
    source_path.write_text(
        (
            "from polyapi.typedefs import PolyClientFunction\n\n"
            "polyConfig: PolyClientFunction = {\n"
            '    "name": "worker",\n'
            '    "context": "example.test",\n'
            "}\n\n"
            "def worker() -> dict:\n"
            '    """Derived description."""\n'
            "    return {}\n"
        ),
        encoding="utf-8",
    )

    specs = poly_deploy.load_function_specs_from_python(source_path)

    assert len(specs) == 1
    assert specs[0].description_explicit is False
    assert specs[0].description is None


def test_load_function_specs_from_python_reads_generate_contexts(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "router_sfx.py"
    source_path.write_text(
        (
            "from polyapi.typedefs import PolyServerFunction\n\n"
            "polyConfig: PolyServerFunction = {\n"
            '    "name": "router_handler",\n'
            '    "context": "example.utils.generic",\n'
            '    "generate_contexts": ["OOB.datadog", "example", "OOB.provider"],\n'
            "}\n\n"
            "def router_handler() -> dict:\n"
            "    return {}\n"
        ),
        encoding="utf-8",
    )

    specs = poly_deploy.load_function_specs_from_python(source_path)

    assert len(specs) == 1
    assert specs[0].generate_contexts == ("OOB.datadog", "example", "OOB.provider")


def test_load_function_specs_from_python_reads_generate_contexts_camel_case_csv(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "router_sfx.py"
    source_path.write_text(
        (
            "from polyapi.typedefs import PolyServerFunction\n\n"
            "polyConfig: PolyServerFunction = {\n"
            '    "name": "router_handler",\n'
            '    "context": "example.utils.generic",\n'
            '    "generateContexts": "OOB.datadog, example, OOB.provider",\n'
            "}\n\n"
            "def router_handler() -> dict:\n"
            "    return {}\n"
        ),
        encoding="utf-8",
    )

    specs = poly_deploy.load_function_specs_from_python(source_path)

    assert len(specs) == 1
    assert specs[0].generate_contexts == ("OOB.datadog", "example", "OOB.provider")


def test_build_function_payload_omits_missing_return_type_schema(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_sfx.py"
    source_path.write_text("def worker() -> object:\n    return {}\n", encoding="utf-8")

    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.test",
        function_type="server",
        source_path=source_path,
        discovery_path=source_path,
    )

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "Any"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value.lower())

    payload = poly_deploy.build_function_payload(spec)

    assert payload["returnType"] == "any"
    assert "returnTypeSchema" not in payload


def test_function_parse_fallback_preserves_native_dependency_discovery(
    monkeypatch: MonkeyPatch,
) -> None:
    native_parse = poly_deploy.parse_function_code
    calls = 0

    def parse_after_missing_sdk(code, name, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ImportError("cannot import name 'poly' from 'polyapi'")
        return native_parse(code, name, context)

    monkeypatch.setattr(poly_deploy, "parse_function_code", parse_after_missing_sdk)
    code = (
        "from polyapi import poly\n"
        "from packaging.version import Version\n"
        "def worker() -> str:\n"
        "    return str(Version('1.0'))\n"
    )
    parsed = poly_deploy.parse_function_code_with_import_fallback(
        code, "worker", "example.demo"
    )
    assert parsed["dependencies"] == ["packaging"]
    assert calls == 2


def test_function_parse_fallback_stubs_all_generated_polyapi_namespaces(
    monkeypatch: MonkeyPatch,
) -> None:
    calls = 0

    def parse_with_generated_imports(
        _code: str,
        _name: str,
        _context: str,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ImportError(
                "cannot import name 'vari' from 'polyapi' "
                "(/tmp/polyapi/polyapi/__init__.py)",
                name="polyapi",
                path="/tmp/polyapi/polyapi/__init__.py",
            )

        namespace: dict[str, object] = {}
        exec(
            "from polyapi import poly, vari, tabi, schemas, webhook",
            namespace,
        )
        for imported_name in poly_deploy.GENERATED_POLYAPI_NAMESPACES:
            assert isinstance(namespace[imported_name], poly_deploy.ImportStub)
        return {"parsed": True}

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        parse_with_generated_imports,
    )

    parsed = poly_deploy.parse_function_code_with_import_fallback(
        "from polyapi import poly, vari, tabi, schemas, webhook",
        "router_handler",
        "example.utils.generic",
    )

    assert parsed == {"parsed": True}
    assert calls == 2


@pytest.mark.parametrize(
    "namespace",
    poly_deploy.PARSER_STUBBED_POLYAPI_IMPORTS,
)
def test_function_parse_fallback_retries_known_polyapi_import_errors(
    namespace: str,
) -> None:
    error = ImportError(
        f"cannot import name '{namespace}' from 'polyapi' "
        "(/tmp/polyapi/polyapi/__init__.py)",
        name="polyapi",
        path="/tmp/polyapi/polyapi/__init__.py",
    )

    assert poly_deploy.should_retry_function_parse(error)


def test_function_parse_fallback_rejects_unrelated_import_error() -> None:
    error = ImportError(
        "cannot import name 'Client' from 'some_dependency'",
        name="some_dependency",
    )

    assert not poly_deploy.should_retry_function_parse(error)


def test_build_function_payload_preserves_explicit_empty_description(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_cfx.py"
    source_path.write_text("def worker() -> dict:\n    return {}\n", encoding="utf-8")

    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.test",
        function_type="client",
        source_path=source_path,
        discovery_path=source_path,
        description="",
        description_explicit=True,
    )

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "Derived description",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "Derived description",
                "params": [],
                "returns": {"type": "dict"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value)

    payload = poly_deploy.build_function_payload(spec)

    assert payload["description"] == ""


def test_build_function_payload_ignores_legacy_deploy_receipt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_cfx.py"
    source = (
        "# Poly deployed @ 2026-04-20T11:23:45.123456 - "
        "example.test.worker - https://eu1.polyapi.io - a3f9c12 - d4e1b7f\n"
        "def worker() -> dict:\n"
        "    return {}\n"
    )
    source_path.write_text(source, encoding="utf-8")

    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.test",
        function_type="client",
        source_path=source_path,
        discovery_path=source_path,
    )

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "dict"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value)

    payload = poly_deploy.build_function_payload(spec)

    assert not payload["code"].startswith("# Poly deployed @")
    assert source_path.read_text(encoding="utf-8") == source


def test_build_function_payload_includes_server_execution_api_key(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_sfx.py"
    source_path.write_text("def worker() -> object:\n    return {}\n", encoding="utf-8")

    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.test",
        function_type="server",
        source_path=source_path,
        discovery_path=source_path,
        execution_api_key="runtime-key",
    )

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "Any"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value.lower())

    payload = poly_deploy.build_function_payload(spec)

    assert payload["executionApiKey"] == "runtime-key"


def test_build_function_payload_omits_client_execution_api_key(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_cfx.py"
    source_path.write_text("def worker() -> object:\n    return {}\n", encoding="utf-8")

    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.test",
        function_type="client",
        source_path=source_path,
        discovery_path=source_path,
        execution_api_key="runtime-key",
    )

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "Any"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value.lower())

    payload = poly_deploy.build_function_payload(spec)

    assert "executionApiKey" not in payload


def test_build_function_payload_resolves_generate_contexts_with_exclusions(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_sfx.py"
    source_path.write_text(
        (
            "from polyapi import poly\n\n"
            "async def worker() -> dict:\n"
            "    await poly.example.active.helper()\n"
            "    return {}\n"
        ),
        encoding="utf-8",
    )

    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.active",
        function_type="server",
        source_path=source_path,
        discovery_path=source_path,
        generate_contexts=("example", "OOB.polyapi"),
    )

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "dict"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value)

    payload = poly_deploy.build_function_payload(spec, {}, ("example.excluded",))

    assert payload["generateContexts"] == ["example.active", "OOB.polyapi"]


def test_build_function_payload_ignores_stale_inferred_contexts_with_exclusions(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_sfx.py"
    source_path.write_text(
        "async def worker() -> dict:\n    return {}\n",
        encoding="utf-8",
    )
    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.active",
        function_type="server",
        source_path=source_path,
        discovery_path=source_path,
    )
    func_context = {
        "example.active.worker": {
            "calls": [],
            "contexts": ["example"],
        }
    }
    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "dict"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value)

    payload = poly_deploy.build_function_payload(
        spec,
        func_context,
        ("example.excluded",),
    )

    assert "generateContexts" not in payload


def test_build_function_payload_uses_filtered_context_graph_when_exclusions_configured(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_sfx.py"
    source_path.write_text(
        (
            "from polyapi import poly\n\n"
            "async def worker() -> dict:\n"
            "    await poly.example.active.helper()\n"
            "    return {}\n"
        ),
        encoding="utf-8",
    )

    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.active",
        function_type="server",
        source_path=source_path,
        discovery_path=source_path,
    )

    remote_func_context = {
        "example.active.worker": {
            "calls": ["example.active.helper"],
            "contexts": ["example.active", "OOB.provider.v2.configuration"],
        },
        "example.active.helper": {
            "calls": [],
            "contexts": ["OOB.provider.v2.configuration"],
        },
    }

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "dict"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value)

    payload = poly_deploy.build_function_payload(
        spec, remote_func_context, ("example.excluded",)
    )

    assert payload["generateContexts"] == [
        "example.active",
        "OOB.provider.v2.configuration",
    ]


def test_sync_function_group_logs_resolved_generate_contexts(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    source_path = tmp_path / "worker_sfx.py"
    source_path.write_text(
        (
            "from polyapi import poly\n\n"
            "async def worker() -> dict:\n"
            "    await poly.example.active.helper()\n"
            "    return {}\n"
        ),
        encoding="utf-8",
    )

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> list[dict[str, str]]:
            assert method == "GET"
            if path in {"/functions/server", "/specs"}:
                return []
            raise AssertionError(path)

    monkeypatch.setattr(
        poly_deploy,
        "parse_function_code",
        lambda code, name, context: {
            "context": context,
            "description": "",
            "config": {},
            "dependencies": [],
            "types": {
                "description": "",
                "params": [],
                "returns": {"type": "dict"},
            },
        },
    )
    monkeypatch.setattr(poly_deploy, "get_jsonschema_type", lambda value: value)
    monkeypatch.setattr(poly_deploy, "get_execution_api_key", lambda: None)

    poly_deploy.sync_function_group(
        specs=[
            poly_deploy.FunctionSpec(
                name="worker",
                context="example.active",
                function_type="server",
                source_path=source_path,
                discovery_path=source_path,
                generate_contexts=("example", "OOB.polyapi"),
            )
        ],
        client=DummyClient(),
        registry=poly_deploy.FunctionRegistry(),
        detail_cache={},
        config=poly_deploy.DeployConfig(
            repo_root=tmp_path,
            enabled_types=("functions",),
            skip_paths=(),
            priority_paths=(),
            function_discovery_globs=(),
            variable_globs=(),
            webhook_globs=(),
            job_globs=(),
            table_globs=(),
            schema_globs=(),
            trigger_globs=(),
            use_mtime_guard=True,
            remote_timestamp_fields=("updatedAt",),
            update_existing_secret_variables=False,
            recreate_variables_on_secrecy_change=True,
            generate_after_function_sync=True,
            generate_contexts=(),
            generate_names=(),
            generate_ids=(),
            orphan_excluded_contexts=("example.excluded",),
            initial_scope_prompt_enabled=True,
            initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
        ),
        cli_args=poly_deploy.CliArgs(
            dry_run=True,
            force=False,
            verbose=False,
            delete_orphans=False,
            path_filters=(),
            enabled_types=("functions",),
            show_config=False,
        ),
        logger=poly_deploy.Logger(verbose=False),
        stats=poly_deploy.ArtifactStats(),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert (
        "generateContexts: ['example.active', 'OOB.polyapi']" in capsys.readouterr().out
    )


def test_sync_function_group_fails_before_mutation_for_unresolved_broad_context(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_sfx.py"
    source_path.write_text(
        "async def worker() -> dict:\n    return {}\n",
        encoding="utf-8",
    )

    class DummyClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        def request(
            self, method: str, path: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            self.calls.append((method, path, kwargs))
            assert (method, path) == ("GET", "/specs")
            return []

    client = DummyClient()
    monkeypatch.setattr(poly_deploy, "get_execution_api_key", lambda: None)
    config = poly_deploy.DeployConfig(
        repo_root=tmp_path,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=("example.excluded",),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=("functions",),
        show_config=False,
    )

    with pytest.raises(
        poly_deploy.DeployError,
        match="Cannot safely resolve broad context 'example'",
    ):
        poly_deploy.sync_function_group(
            specs=[
                poly_deploy.FunctionSpec(
                    name="worker",
                    context="OOB.worker",
                    function_type="server",
                    source_path=source_path,
                    discovery_path=source_path,
                    generate_contexts=("example",),
                )
            ],
            client=client,
            registry=poly_deploy.FunctionRegistry(),
            detail_cache={},
            config=config,
            cli_args=cli_args,
            logger=poly_deploy.Logger(verbose=False),
            stats=poly_deploy.ArtifactStats(),
            resume_state=poly_deploy.ResumeState(tmp_path, load=False),
            local_func_context={},
        )

    assert client.calls == [("GET", "/specs", {"params": {"noTypes": "true"}})]


def test_sync_function_group_force_updates_unchanged_existing_function(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "worker_cfx.py"
    source_path.write_text("def worker() -> dict:\n    return {}\n", encoding="utf-8")

    payload = {
        "context": "example.test",
        "name": "worker",
        "description": "",
        "code": source_path.read_text(encoding="utf-8"),
        "language": "python",
        "visibility": "ENVIRONMENT",
        "returnType": "object",
        "arguments": [],
    }

    class DummyClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((method, path, kwargs))
            if method == "GET" and path == "/functions/client/fn-1":
                return {"id": "fn-1", "updatedAt": "2099-01-01T00:00:00Z", **payload}
            if method == "POST" and path == "/functions/client":
                return {"id": "fn-1"}
            raise AssertionError((method, path))

    monkeypatch.setattr(
        poly_deploy,
        "build_remote_func_context",
        lambda _client, _excluded_contexts=(): {},
    )
    monkeypatch.setattr(
        poly_deploy,
        "build_function_payload",
        lambda _spec, _remote_func_context, _excluded_contexts=(): payload,
    )
    monkeypatch.setattr(
        poly_deploy,
        "normalize_function_subset",
        lambda _local, _remote: ({"same": True}, {"same": True}),
    )
    monkeypatch.setattr(poly_deploy, "get_execution_api_key", lambda: None)

    registry = poly_deploy.FunctionRegistry()
    registry.load(
        "client",
        [{"id": "fn-1", "context": "example.test", "name": "worker"}],
    )
    client = DummyClient()
    stats = poly_deploy.ArtifactStats()
    changed = poly_deploy.sync_function_group(
        specs=[
            poly_deploy.FunctionSpec(
                name="worker",
                context="example.test",
                function_type="client",
                source_path=source_path,
                discovery_path=source_path,
            )
        ],
        client=client,
        registry=registry,
        detail_cache={},
        config=poly_deploy.DeployConfig(
            repo_root=tmp_path,
            enabled_types=("functions",),
            skip_paths=(),
            priority_paths=(),
            function_discovery_globs=(),
            variable_globs=(),
            webhook_globs=(),
            job_globs=(),
            table_globs=(),
            schema_globs=(),
            trigger_globs=(),
            use_mtime_guard=True,
            remote_timestamp_fields=("updatedAt",),
            update_existing_secret_variables=False,
            recreate_variables_on_secrecy_change=True,
            generate_after_function_sync=True,
            generate_contexts=(),
            generate_names=(),
            generate_ids=(),
            orphan_excluded_contexts=(),
            initial_scope_prompt_enabled=True,
            initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
        ),
        cli_args=poly_deploy.CliArgs(
            dry_run=False,
            force=True,
            verbose=False,
            delete_orphans=False,
            path_filters=(),
            enabled_types=None,
            show_config=False,
        ),
        logger=poly_deploy.Logger(verbose=False),
        stats=stats,
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert changed is True
    assert stats.updated == 1
    assert stats.skipped == 0
    assert any(
        method == "POST" and path == "/functions/client"
        for method, path, _kwargs in client.calls
    )


@pytest.mark.parametrize("generated_fallback", [False, True])
@pytest.mark.parametrize(
    "dependency",
    [
        "missing_runtime_dependency",
        "openpyxl",
        "unmapped_dependency",
        "missing_sdk_symbol",
    ],
)
def test_preflight_rejects_unresolved_runtime_dependencies(
    tmp_path: Path, monkeypatch: MonkeyPatch, dependency: str, generated_fallback: bool
) -> None:
    import builtins
    import types

    original_import = builtins.__import__
    original_json_loads = json.loads
    native_parse = poly_deploy.parse_function_code
    monkeypatch.setitem(
        sys.modules,
        dependency,
        types.ModuleType(dependency) if dependency == "unmapped_dependency" else None,
    )

    def parse_after_missing_sdk(code, name, context):
        if generated_fallback and builtins.__import__ is original_import:
            raise ImportError("cannot import name 'poly' from 'polyapi'")
        return native_parse(code, name, context)

    monkeypatch.setattr(poly_deploy, "parse_function_code", parse_after_missing_sdk)
    source_path = tmp_path / "workflows/src/demo/server/worker_sfx.py"
    source_path.parent.mkdir(parents=True)
    dependency_import = (
        "from polyapi import missing_sdk_symbol\n"
        if dependency == "missing_sdk_symbol"
        else f"import {dependency}\n"
    )
    source_path.write_text(
        "from polyapi.typedefs import PolyServerFunction\n"
        + dependency_import
        + 'polyConfig: PolyServerFunction = {"name": "worker", "context": "example.demo"}\n'
        "def worker() -> str:\n"
        f"    return {dependency}.run()\n"
    )
    args = poly_deploy.CliArgs(True, False, False, False, (), ("functions",), False)
    config = poly_deploy.build_config(tmp_path, args)
    spec = poly_deploy.load_function_specs_from_python(source_path)[0]
    with pytest.raises(
        KeyError if dependency == "unmapped_dependency" else ImportError
    ):
        poly_deploy.build_function_payload(spec)
    with pytest.raises(poly_deploy.DeployError):
        poly_deploy.validate_local_artifacts(config, args, poly_deploy.Logger(False))
    skipped = poly_deploy.validate_local_artifacts(
        config, poly_deploy.replace(args, skip_invalid=True), poly_deploy.Logger(False)
    )
    assert skipped.invalid_paths == (source_path,)
    assert builtins.__import__ is original_import
    assert json.loads is original_json_loads
    assert not isinstance(sys.modules.get(dependency), poly_deploy.PlaceholderModule)


def test_multicolor_rule_matches_requested_length(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(poly_deploy, "style_text", lambda text, **_: text)

    assert len(poly_deploy.multicolor_rule(87, enabled=True)) == 87


def test_logger_ok_uses_yellow_for_skipped_status(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(poly_deploy, "terminal_supports_color", lambda: True)
    logger = poly_deploy.Logger(verbose=False)

    logger.ok("Skipped")

    output = capsys.readouterr().out
    assert "\033[38;2;251;188;5m[OK]\033[0m" in output
    assert "\033[38;2;52;168;83m[OK]\033[0m" not in output


def test_logger_ok_keeps_green_for_non_skipped_status(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(poly_deploy, "terminal_supports_color", lambda: True)
    logger = poly_deploy.Logger(verbose=False)

    logger.ok("Created")

    output = capsys.readouterr().out
    assert "\033[38;2;52;168;83m[OK]\033[0m" in output


def test_build_flow_rule_color_indices_match_logo_boundaries() -> None:
    assert poly_deploy.build_flow_rule_color_indices("top") == [
        *([0] * 19),
        *([1] * 22),
        *([2] * 15),
        *([3] * 24),
    ]
    assert poly_deploy.build_flow_rule_color_indices("bottom") == [
        *([0] * 20),
        *([1] * 22),
        *([2] * 16),
        *([3] * 29),
    ]


def test_render_flow_rule_matches_logo_width_and_color_runs(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[tuple[str, tuple[int, int, int] | None]] = []

    def fake_style_text(
        text: str,
        *,
        rgb: tuple[int, int, int] | None = None,
        **_: object,
    ) -> str:
        captured.append((text, rgb))
        return text

    monkeypatch.setattr(poly_deploy, "style_text", fake_style_text)

    rendered = poly_deploy.render_flow_rule("top", enabled=True)

    assert len(rendered) == 80
    assert [len(text) for text, _ in captured] == [19, 22, 15, 24]
    assert [rgb for _, rgb in captured] == list(poly_deploy.POLYAPI_BRAND_COLORS)


def test_render_bottom_flow_rule_matches_logo_width_and_color_runs(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[tuple[str, tuple[int, int, int] | None]] = []

    def fake_style_text(
        text: str,
        *,
        rgb: tuple[int, int, int] | None = None,
        **_: object,
    ) -> str:
        captured.append((text, rgb))
        return text

    monkeypatch.setattr(poly_deploy, "style_text", fake_style_text)

    rendered = poly_deploy.render_flow_rule("bottom", enabled=True)

    assert len(rendered) == 87
    assert [len(text) for text, _ in captured] == [20, 22, 16, 29]
    assert [rgb for _, rgb in captured] == list(poly_deploy.POLYAPI_BRAND_COLORS)


def test_render_flow_logo_keeps_one_color_per_letter_cluster(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[tuple[str, tuple[int, int, int] | None]] = []

    def fake_style_text(
        text: str,
        *,
        rgb: tuple[int, int, int] | None = None,
        **_: object,
    ) -> str:
        captured.extend((character, rgb) for character in text)
        return text

    monkeypatch.setattr(poly_deploy, "style_text", fake_style_text)

    rendered = poly_deploy.render_flow_logo(enabled=True)

    assert rendered == poly_deploy.FLOW_LOGO

    captured_iter = iter(captured)
    expected_colors = (
        poly_deploy.BLUE,
        poly_deploy.RED,
        poly_deploy.YELLOW,
        poly_deploy.GREEN,
    )

    for line_index, line in enumerate(poly_deploy.FLOW_LOGO.splitlines()):
        cluster_colors: list[set[tuple[int, int, int] | None]] = []
        current_cluster_colors: set[tuple[int, int, int] | None] = set()
        in_cluster = False

        for character in line:
            if character == " ":
                if in_cluster:
                    cluster_colors.append(current_cluster_colors)
                    current_cluster_colors = set()
                    in_cluster = False
                continue

            rendered_character, rgb = next(captured_iter)
            assert rendered_character == character

            if character == "_":
                assert rgb == poly_deploy.WHITE
                if in_cluster:
                    cluster_colors.append(current_cluster_colors)
                    current_cluster_colors = set()
                    in_cluster = False
                continue

            current_cluster_colors.add(rgb)
            in_cluster = True

        if in_cluster:
            cluster_colors.append(current_cluster_colors)

        assert all(len(color_set) == 1 for color_set in cluster_colors)
        assert [next(iter(color_set)) for color_set in cluster_colors] == [
            expected_colors[index]
            for index in poly_deploy.FLOW_LOGO_CLUSTER_COLOR_SEQUENCE[line_index]
        ]


def test_build_variable_payload_ignores_secret_values() -> None:
    payload = poly_deploy.build_variable_payload(
        Path("secret.json"),
        {
            "context": "example.test",
            "name": "password",
            "secret": True,
            "secrecy": "SECRET",
            "value": "",
            "visibility": "ENVIRONMENT",
            "state": "ALPHA",
        },
    )

    assert payload["value"] == {}


def test_build_variable_payload_keeps_non_empty_obscured_values() -> None:
    payload = poly_deploy.build_variable_payload(
        Path("obscured.json"),
        {
            "context": "example.test",
            "name": "token",
            "secrecy": "OBSCURED",
            "value": "do-not-deploy",
            "visibility": "ENVIRONMENT",
            "state": "ALPHA",
        },
    )

    assert payload["value"] == "do-not-deploy"


def test_build_variable_payload_omits_empty_obscured_values() -> None:
    for empty_value in ("", {}, [], None):
        payload = poly_deploy.build_variable_payload(
            Path("obscured.json"),
            {
                "context": "example.test",
                "name": "token",
                "secrecy": "OBSCURED",
                "value": empty_value,
                "visibility": "ENVIRONMENT",
                "state": "ALPHA",
            },
        )

        assert "value" not in payload


def test_build_variable_payload_omits_missing_obscured_values() -> None:
    payload = poly_deploy.build_variable_payload(
        Path("obscured.json"),
        {
            "context": "example.test",
            "name": "token",
            "secrecy": "OBSCURED",
            "visibility": "ENVIRONMENT",
            "state": "ALPHA",
        },
    )

    assert "value" not in payload


def test_collect_missing_env_token_refs_respects_scope_and_defined_values(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    target_file = (
        repo_root
        / "workflows/src"
        / "sample_workflow"
        / "artefacts"
        / "vari"
        / "service_config.json"
    )
    ignored_file = (
        repo_root / "workflows/src" / "publish" / "artefacts" / "webhooks" / "hook.json"
    )
    target_file.parent.mkdir(parents=True)
    ignored_file.parent.mkdir(parents=True)
    target_file.write_text(
        (
            "{\n"
            '  "name": "service_config",\n'
            '  "context": "example.sample_workflow",\n'
            '  "value": {"host": "{{IDENTITY_HOST_URL}}", "tenant": "{{OKTA_TENANT_ID}}"}\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    ignored_file.write_text(
        '{\n  "subpath": "profile/{{nameId}}",\n  "upstream": "{{DEFINED_ENV}}"\n}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("DEFINED_ENV", "set")
    monkeypatch.setenv("IDENTITY_HOST_URL", "https://example.com")
    monkeypatch.delenv("OKTA_TENANT_ID", raising=False)

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables", "webhooks"),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=("workflows/src/**/artefacts/webhooks/*.json",),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(repo_root / "workflows/src" / "sample_workflow",),
        enabled_types=None,
        show_config=False,
    )

    refs = poly_deploy.collect_missing_env_token_refs(config, cli_args)

    assert refs == [
        poly_deploy.MissingEnvTokenRef(
            env_name="OKTA_TENANT_ID",
            path=target_file.resolve(),
            line_number=4,
        )
    ]


def test_maybe_prompt_for_initial_scope_still_prompts_with_existing_state_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    state_file = repo_root / ".poly_deploy.state.json"
    state_file.write_text("{}\n", encoding="utf-8")

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=state_file,
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    logger = poly_deploy.Logger(verbose=False)
    called = {"value": False}

    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)

    def fake_prompt(_: Path) -> None:
        called["value"] = True
        return None

    monkeypatch.setattr(poly_deploy, "prompt_for_initial_scope", fake_prompt)

    result = poly_deploy.maybe_prompt_for_initial_scope(cli_args, config, logger)

    assert result == cli_args
    assert called["value"] is True


def test_create_parser_help_mentions_shortcuts_and_examples() -> None:
    help_text = poly_deploy.create_parser().format_help()

    assert "push" in help_text
    assert "--adopt-orphans" in help_text
    assert "--delete-orphans" in help_text
    assert "sample_workflow" in help_text
    assert "utilities/common" in help_text
    assert (
        "Functions are discovered from inline polyConfig in Python source" in help_text
    )


def test_parse_args_without_command_returns_none(capsys: CaptureFixture[str]) -> None:
    parsed = poly_deploy.parse_args([])

    captured = capsys.readouterr()

    assert parsed is None
    assert "push" in captured.out
    assert "Use the `push` command" in captured.out


def test_parse_args_push_command_builds_cli_args() -> None:
    parsed = poly_deploy.parse_args(
        ["push", "--dry-run", "--paths", "sample_workflow", "--types", "functions,jobs"]
    )

    assert parsed == poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(Path("sample_workflow"),),
        enabled_types=("functions", "jobs"),
        show_config=False,
    )


def test_parse_args_push_command_supports_server_function_type_alias() -> None:
    parsed = poly_deploy.parse_args(["push", "--types", "variables,server-functions"])

    assert parsed == poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=("variables", "functions"),
        show_config=False,
        function_types=("server",),
    )


def test_parse_args_push_command_supports_adopt_orphans() -> None:
    parsed = poly_deploy.parse_args(
        ["push", "--adopt-orphans", "--paths", "sample_workflow"]
    )

    assert parsed == poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(Path("sample_workflow"),),
        enabled_types=None,
        show_config=False,
        adopt_orphans=True,
    )


def test_load_env_prefers_repo_dotenv_over_existing_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    # This exercises the default-.env path, so ENVPATH must be unset -- an
    # ENVPATH exported in the shell would otherwise pick a different file.
    monkeypatch.delenv("ENVPATH", raising=False)
    (repo_root / ".env").write_text(
        "STORK_CONTEXT_PATH_MAP=example.test.json=workflows/src/utilities/common/artefacts/vari\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "STORK_CONTEXT_PATH_MAP", "example.test=workflows/src/old/location"
    )

    poly_deploy.load_env(repo_root)

    assert poly_deploy.os.getenv("STORK_CONTEXT_PATH_MAP") == (
        "example.test.json=workflows/src/utilities/common/artefacts/vari"
    )


def test_load_env_uses_envpath_when_set(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    (repo_root / ".env").write_text(
        "STORK_CONTEXT_PATH_MAP=example.default\n", encoding="utf-8"
    )
    (repo_root / "dev.env").write_text(
        "STORK_CONTEXT_PATH_MAP=example.override\n", encoding="utf-8"
    )
    monkeypatch.setenv("ENVPATH", "./dev.env")

    result = poly_deploy.load_env(repo_root)

    assert result == poly_deploy.EnvLoadResult(
        display_path="./dev.env",
        resolved_path=(repo_root / "dev.env").resolve(),
        exists=True,
        from_envpath=True,
    )
    assert poly_deploy.os.getenv("STORK_CONTEXT_PATH_MAP") == "example.override"


def test_load_env_raises_when_envpath_target_is_missing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVPATH", "./missing.env")

    with pytest.raises(
        poly_deploy.DeployError, match="ENVPATH points to `./missing\\.env`"
    ):
        poly_deploy.load_env(tmp_path)


def test_configured_production_branch_config_supports_env_mapping(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROD_BRANCH", "main:prod.env")

    assert (
        poly_deploy.configured_production_branch_config()
        == poly_deploy.BranchEnvBinding(
            branch="main",
            env_file="prod.env",
        )
    )


def test_configured_production_branch_config_rejects_missing_env_file(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROD_BRANCH", "main:")

    with pytest.raises(
        poly_deploy.DeployError,
        match=r"GIT_PROD_BRANCH entry `main:` must include an env file after `:`\.",
    ):
        poly_deploy.configured_production_branch_config()


def test_resolve_branch_env_file_uses_env_dir_for_bare_filename(tmp_path: Path) -> None:
    assert (
        poly_deploy.resolve_branch_env_file(tmp_path, "dev.env")
        == (tmp_path / "env" / "dev.env").resolve()
    )


def test_resolve_branch_env_file_respects_explicit_relative_path(
    tmp_path: Path,
) -> None:
    assert (
        poly_deploy.resolve_branch_env_file(tmp_path, "./dev.env")
        == (tmp_path / "dev.env").resolve()
    )


def test_format_env_file_visibility_marks_missing_default_env_file() -> None:
    assert (
        poly_deploy.format_env_file_visibility(
            poly_deploy.EnvLoadResult(
                display_path=".env",
                resolved_path=Path(".env"),
                exists=False,
                from_envpath=False,
            )
        )
        == ".env (missing, not loaded)"
    )


@pytest.mark.parametrize("value", [None, "  execution-key-for-test  "])
def test_get_execution_api_key_reads_optional_environment_value(
    monkeypatch: MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("POLY_EXEC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("POLY_EXEC_API_KEY", value)
    assert poly_deploy.get_execution_api_key() == (
        value.strip() if value is not None else None
    )


def test_get_execution_api_key_rejects_empty_value(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("POLY_EXEC_API_KEY", "   ")

    with pytest.raises(
        poly_deploy.DeployError, match="POLY_EXEC_API_KEY is set but empty"
    ):
        poly_deploy.get_execution_api_key()


def test_build_deployment_preflight_marks_production_branch_as_sensitive(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    env_file = poly_deploy.EnvLoadResult(
        display_path=".env",
        resolved_path=(tmp_path / ".env").resolve(),
        exists=True,
        from_envpath=False,
    )
    monkeypatch.setattr(poly_deploy, "configured_production_branch", lambda: "main")
    monkeypatch.setattr(poly_deploy, "current_git_branch", lambda _repo_root: "main")
    monkeypatch.setattr(
        poly_deploy, "resolve_git_production_ref", lambda _repo_root, _branch: "main"
    )
    monkeypatch.setattr(
        poly_deploy,
        "is_branch_directly_derived_from_production",
        lambda _repo_root, **_kwargs: False,
    )
    monkeypatch.setattr(
        poly_deploy,
        "branch_ancestor_chain",
        lambda _repo_root, **_kwargs: ("develop",),
    )

    preflight = poly_deploy.build_deployment_preflight(tmp_path, env_file)

    assert preflight.production_sensitive is True
    assert preflight.current_branch == "main"
    assert preflight.production_branch == "main"


def test_build_deployment_preflight_does_not_mark_branch_derived_from_develop_as_sensitive(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    env_file = poly_deploy.EnvLoadResult(
        display_path=".env",
        resolved_path=(tmp_path / ".env").resolve(),
        exists=True,
        from_envpath=False,
    )
    monkeypatch.setattr(poly_deploy, "configured_production_branch", lambda: "main")
    monkeypatch.setattr(
        poly_deploy, "current_git_branch", lambda _repo_root: "release_prep"
    )
    monkeypatch.setattr(
        poly_deploy, "resolve_git_production_ref", lambda _repo_root, _branch: "main"
    )
    monkeypatch.setattr(
        poly_deploy,
        "is_branch_directly_derived_from_production",
        lambda _repo_root, **_kwargs: False,
    )
    monkeypatch.setattr(
        poly_deploy,
        "branch_ancestor_chain",
        lambda _repo_root, **_kwargs: ("develop", "main"),
    )

    preflight = poly_deploy.build_deployment_preflight(tmp_path, env_file)

    assert preflight.production_sensitive is False
    assert preflight.current_branch_parent == "develop"
    assert preflight.sensitive_reason is None
    assert preflight.safeguard_exemption_reason == "derived from exempt branch develop"


def test_configured_production_skip_branches_rejects_production_branch(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROD_SKIP_BRANCHES", "develop, main ,qa")

    with pytest.raises(
        poly_deploy.DeployError,
        match=r"GIT_PROD_SKIP_BRANCHES cannot include the configured production branch `main`\.",
    ):
        poly_deploy.configured_production_skip_branches("main")


def test_configured_production_skip_branches_rejects_exact_match(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROD_SKIP_BRANCHES", "main")

    with pytest.raises(
        poly_deploy.DeployError,
        match=r"GIT_PROD_SKIP_BRANCHES cannot include the configured production branch `main`\.",
    ):
        poly_deploy.configured_production_skip_branches("main")


def test_configured_production_skip_branch_configs_support_env_mappings(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROD_SKIP_BRANCHES", "develop:dev.env, qa:qa.env")

    assert poly_deploy.configured_production_skip_branch_configs("main") == (
        poly_deploy.BranchEnvBinding(branch="develop", env_file="dev.env"),
        poly_deploy.BranchEnvBinding(branch="qa", env_file="qa.env"),
    )
    assert poly_deploy.configured_production_skip_branches("main") == ("develop", "qa")


def test_configured_production_skip_branches_rejects_production_branch_with_env_mapping(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROD_SKIP_BRANCHES", "develop:dev.env,main:prod.env")

    with pytest.raises(
        poly_deploy.DeployError,
        match=r"GIT_PROD_SKIP_BRANCHES cannot include the configured production branch `main`\.",
    ):
        poly_deploy.configured_production_skip_branch_configs("main")


def test_configured_production_skip_branch_configs_reject_conflicting_duplicates(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_PROD_SKIP_BRANCHES", "develop:dev.env,develop:qa.env")

    with pytest.raises(
        poly_deploy.DeployError,
        match=r"GIT_PROD_SKIP_BRANCHES configures branch `develop` more than once with different env files\.",
    ):
        poly_deploy.configured_production_skip_branch_configs("main")


def test_configured_production_skip_branches_defaults_to_develop(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("GIT_PROD_SKIP_BRANCHES", raising=False)

    assert poly_deploy.configured_production_skip_branches("main") == ("develop",)


def test_build_deployment_preflight_exempts_configured_branch_and_descendants(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    env_file = poly_deploy.EnvLoadResult(
        display_path=".env",
        resolved_path=(tmp_path / ".env").resolve(),
        exists=True,
        from_envpath=False,
    )
    monkeypatch.setenv("GIT_PROD_SKIP_BRANCHES", "develop")
    monkeypatch.setattr(poly_deploy, "configured_production_branch", lambda: "main")
    monkeypatch.setattr(
        poly_deploy, "current_git_branch", lambda _repo_root: "release_prep"
    )
    monkeypatch.setattr(
        poly_deploy, "resolve_git_production_ref", lambda _repo_root, _branch: "main"
    )
    monkeypatch.setattr(
        poly_deploy,
        "branch_ancestor_chain",
        lambda _repo_root, **_kwargs: ("develop", "main"),
    )
    monkeypatch.setattr(
        poly_deploy,
        "is_branch_directly_derived_from_production",
        lambda _repo_root, **_kwargs: True,
    )

    preflight = poly_deploy.build_deployment_preflight(tmp_path, env_file)

    assert preflight.production_sensitive is False
    assert preflight.current_branch_parent == "develop"
    assert preflight.safeguard_exemption_reason == "derived from exempt branch develop"


def test_build_deployment_preflight_carries_expected_env_for_exempt_branch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    env_file = poly_deploy.EnvLoadResult(
        display_path="./env/dev.env",
        resolved_path=(tmp_path / "env" / "dev.env").resolve(),
        exists=True,
        from_envpath=True,
    )
    monkeypatch.setenv("GIT_PROD_SKIP_BRANCHES", "develop:dev.env")
    monkeypatch.setattr(
        poly_deploy, "current_git_branch", lambda _repo_root: "release_prep"
    )
    monkeypatch.setattr(
        poly_deploy, "resolve_git_production_ref", lambda _repo_root, _branch: "main"
    )
    monkeypatch.setattr(
        poly_deploy,
        "branch_ancestor_chain",
        lambda _repo_root, **_kwargs: ("develop", "main"),
    )
    monkeypatch.setattr(
        poly_deploy,
        "is_branch_directly_derived_from_production",
        lambda _repo_root, **_kwargs: True,
    )

    preflight = poly_deploy.build_deployment_preflight(tmp_path, env_file)

    assert preflight.production_sensitive is False
    assert preflight.expected_env_branch == "develop"
    assert preflight.expected_env_file == "dev.env"


def test_is_branch_directly_derived_from_production_rejects_closer_develop_ancestor(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        poly_deploy,
        "list_local_git_branches",
        lambda _repo_root: ("main", "develop", "release_prep"),
    )

    def fake_resolve_branch_base_commit(
        _repo_root: Path,
        branch_ref: str,
        *,
        head_ref: str = "HEAD",
    ) -> str | None:
        mapping = {
            ("main", "release_prep"): "base-main",
            ("develop", "release_prep"): "base-develop",
            ("main", "HEAD"): "base-main",
            ("develop", "HEAD"): "base-develop",
        }
        return mapping.get((branch_ref, head_ref))

    monkeypatch.setattr(
        poly_deploy, "resolve_branch_base_commit", fake_resolve_branch_base_commit
    )
    monkeypatch.setattr(
        poly_deploy,
        "commits_since_base",
        lambda _repo_root, base_commit, *, head_ref="HEAD": {
            ("base-main", "release_prep"): 12,
            ("base-develop", "release_prep"): 2,
            ("base-main", "HEAD"): 12,
            ("base-develop", "HEAD"): 2,
        }[(base_commit, head_ref)],
    )

    assert (
        poly_deploy.is_branch_directly_derived_from_production(
            tmp_path,
            current_branch="release_prep",
            production_branch="main",
            production_ref="main",
        )
        is False
    )


def test_nearest_ancestor_branch_returns_closest_local_branch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        poly_deploy,
        "list_local_git_branches",
        lambda _repo_root: ("main", "develop", "release_prep"),
    )

    def fake_resolve_branch_base_commit(
        _repo_root: Path,
        branch_ref: str,
        *,
        head_ref: str = "HEAD",
    ) -> str | None:
        mapping = {
            ("main", "release_prep"): "base-main",
            ("develop", "release_prep"): "base-develop",
        }
        return mapping.get((branch_ref, head_ref))

    monkeypatch.setattr(
        poly_deploy, "resolve_branch_base_commit", fake_resolve_branch_base_commit
    )
    monkeypatch.setattr(
        poly_deploy,
        "commits_since_base",
        lambda _repo_root, base_commit, *, head_ref="HEAD": {
            ("base-main", "release_prep"): 12,
            ("base-develop", "release_prep"): 2,
        }[(base_commit, head_ref)],
    )

    assert (
        poly_deploy.nearest_ancestor_branch(
            tmp_path,
            current_branch="release_prep",
            head_ref="release_prep",
        )
        == "develop"
    )


def test_branch_ancestor_chain_walks_back_through_nearest_ancestors(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        poly_deploy,
        "list_local_git_branches",
        lambda _repo_root: ("main", "develop", "release_prep"),
    )

    def fake_resolve_branch_base_commit(
        _repo_root: Path,
        branch_ref: str,
        *,
        head_ref: str = "HEAD",
    ) -> str | None:
        mapping = {
            ("main", "release_prep"): "base-main-release_prep",
            ("develop", "release_prep"): "base-develop-release_prep",
            ("main", "develop"): "base-main-develop",
        }
        return mapping.get((branch_ref, head_ref))

    monkeypatch.setattr(
        poly_deploy, "resolve_branch_base_commit", fake_resolve_branch_base_commit
    )
    monkeypatch.setattr(
        poly_deploy,
        "commits_since_base",
        lambda _repo_root, base_commit, *, head_ref="HEAD": {
            ("base-main-release_prep", "release_prep"): 12,
            ("base-develop-release_prep", "release_prep"): 2,
            ("base-main-develop", "develop"): 4,
        }[(base_commit, head_ref)],
    )

    assert poly_deploy.branch_ancestor_chain(
        tmp_path,
        current_branch="release_prep",
    ) == ("develop", "main")


def test_production_safeguards_triggered_skips_dry_run_and_adopt_orphans() -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path=".env",
            resolved_path=Path(".env"),
            exists=True,
            from_envpath=False,
        ),
        production_branch="main",
        current_branch="release_prep",
        production_sensitive=True,
        current_branch_parent="main",
        sensitive_reason="current branch is directly derived from the configured production branch",
    )

    dry_run_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    adopt_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
        adopt_orphans=True,
    )
    apply_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    assert poly_deploy.production_safeguards_triggered(preflight, dry_run_args) is False
    assert poly_deploy.production_safeguards_triggered(preflight, adopt_args) is False
    assert poly_deploy.production_safeguards_triggered(preflight, apply_args) is True
    assert (
        poly_deploy.describe_production_safeguards(preflight, dry_run_args)
        == "not triggered (dry-run)"
    )
    assert (
        poly_deploy.describe_production_safeguards(
            preflight,
            adopt_args,
        )
        == "not triggered (adopt-orphans mode)"
    )
    assert (
        poly_deploy.describe_production_safeguards(preflight, apply_args) == "triggered"
    )


def test_print_deployment_visibility_matches_expected_order_for_non_prod_branch(
    capsys: CaptureFixture[str],
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path=".env",
            resolved_path=Path(".env"),
            exists=False,
            from_envpath=False,
        ),
        production_branch="main",
        current_branch="release_prep",
        production_sensitive=False,
        current_branch_parent="develop",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    poly_deploy.print_deployment_visibility(
        poly_deploy.Logger(verbose=False), preflight, cli_args, "env-abc123"
    )

    captured = capsys.readouterr()

    lines = [line for line in captured.out.splitlines() if line]

    assert lines == [
        "env file: .env (missing, not loaded)",
        "target environment: env-abc123",
        "current git branch: release_prep (branch of develop)",
        "production branch: main",
        "production safeguards: not triggered",
    ]


def test_print_deployment_visibility_includes_exemption_reason(
    capsys: CaptureFixture[str],
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path=".env",
            resolved_path=Path(".env"),
            exists=True,
            from_envpath=False,
        ),
        production_branch="main",
        current_branch="release_prep",
        production_sensitive=False,
        current_branch_parent="develop",
        safeguard_exemption_reason="derived from exempt branch develop",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    poly_deploy.print_deployment_visibility(
        poly_deploy.Logger(verbose=False), preflight, cli_args, "env-abc123"
    )

    captured = capsys.readouterr()

    lines = [line for line in captured.out.splitlines() if line]

    assert lines == [
        "env file: .env",
        "target environment: env-abc123",
        "current git branch: release_prep (branch of develop)",
        "production branch: main",
        "production safeguards: not triggered (derived from exempt branch develop)",
    ]


def test_enforce_production_safeguards_requires_exec_key_when_triggered() -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path=".env",
            resolved_path=Path(".env"),
            exists=True,
            from_envpath=False,
        ),
        production_branch="main",
        current_branch="release_prep",
        production_sensitive=True,
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )

    with pytest.raises(poly_deploy.DeployError, match="POLY_EXEC_API_KEY is not set"):
        poly_deploy.enforce_production_safeguards(preflight, cli_args, None)


def test_enforce_production_safeguards_requires_exact_prod_confirmation(
    monkeypatch: MonkeyPatch,
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path=".env",
            resolved_path=Path(".env"),
            exists=True,
            from_envpath=False,
        ),
        production_branch="main",
        current_branch="release_prep",
        production_sensitive=True,
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "prod")

    with pytest.raises(
        poly_deploy.DeployError, match="Type `PROD` exactly to continue"
    ):
        poly_deploy.enforce_production_safeguards(preflight, cli_args, "secret-key")


def test_maybe_confirm_branch_env_file_match_skips_when_env_matches(
    tmp_path: Path,
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path="./env/dev.env",
            resolved_path=(tmp_path / "env" / "dev.env").resolve(),
            exists=True,
            from_envpath=True,
        ),
        production_branch="main",
        current_branch="feature",
        production_sensitive=False,
        expected_env_branch="develop",
        expected_env_file="dev.env",
    )

    poly_deploy.maybe_confirm_branch_env_file_match(
        tmp_path,
        preflight,
        poly_deploy.Logger(verbose=False),
    )


def test_maybe_confirm_branch_env_file_match_warns_and_allows_confirmation_when_branch_has_no_env_mapping(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path=".env",
            resolved_path=Path(".env"),
            exists=True,
            from_envpath=False,
        ),
        production_branch="main",
        current_branch="feature",
        production_sensitive=False,
        expected_env_branch="develop",
        expected_env_file=None,
    )
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    poly_deploy.maybe_confirm_branch_env_file_match(
        Path.cwd(),
        preflight,
        poly_deploy.Logger(verbose=False),
    )

    captured = capsys.readouterr()

    assert (
        "WARN: Branch `develop` does not pin an env file in "
        "`GIT_PROD_BRANCH`/`GIT_PROD_SKIP_BRANCHES`. "
        "Flow loaded `.env`. Confirm that this is the correct env file."
    ) in captured.out


def test_maybe_confirm_branch_env_file_match_requires_interactive_confirmation_when_branch_has_no_env_mapping(
    monkeypatch: MonkeyPatch,
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path=".env",
            resolved_path=Path(".env"),
            exists=True,
            from_envpath=False,
        ),
        production_branch="main",
        current_branch="feature",
        production_sensitive=False,
        expected_env_branch="develop",
        expected_env_file=None,
    )
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: False)

    with pytest.raises(
        poly_deploy.DeployError,
        match=(
            r"Branch `develop` does not pin an env file in "
            r"`GIT_PROD_BRANCH`/`GIT_PROD_SKIP_BRANCHES`\. "
            r"Flow loaded `\.env`\. Confirm that this is the correct env file\. "
            r"Run interactively to confirm or update ENVPATH\."
        ),
    ):
        poly_deploy.maybe_confirm_branch_env_file_match(
            Path.cwd(),
            preflight,
            poly_deploy.Logger(verbose=False),
        )


def test_maybe_confirm_branch_env_file_match_warns_and_allows_confirmation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path="./env/prod.env",
            resolved_path=(tmp_path / "env" / "prod.env").resolve(),
            exists=True,
            from_envpath=True,
        ),
        production_branch="main",
        current_branch="feature",
        production_sensitive=False,
        expected_env_branch="develop",
        expected_env_file="dev.env",
    )
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    poly_deploy.maybe_confirm_branch_env_file_match(
        tmp_path,
        preflight,
        poly_deploy.Logger(verbose=False),
    )

    captured = capsys.readouterr()

    assert (
        "WARN: Branch `develop` expects env file `env/dev.env`, "
        "but Flow loaded `./env/prod.env`."
    ) in captured.out


def test_maybe_confirm_branch_env_file_match_requires_interactive_confirmation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    preflight = poly_deploy.DeploymentPreflight(
        env_file=poly_deploy.EnvLoadResult(
            display_path="./env/prod.env",
            resolved_path=(tmp_path / "env" / "prod.env").resolve(),
            exists=True,
            from_envpath=True,
        ),
        production_branch="main",
        current_branch="feature",
        production_sensitive=False,
        expected_env_branch="develop",
        expected_env_file="dev.env",
    )
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: False)

    with pytest.raises(
        poly_deploy.DeployError,
        match=r"Branch `develop` expects env file `env/dev\.env`, but Flow loaded `\./env/prod\.env`\. Run interactively to confirm or update ENVPATH\.",
    ):
        poly_deploy.maybe_confirm_branch_env_file_match(
            tmp_path,
            preflight,
            poly_deploy.Logger(verbose=False),
        )


def test_load_function_specs_respects_function_type_filter(tmp_path: Path) -> None:
    repo_root = tmp_path
    deployment_dir = repo_root / "workflows/src" / "playground" / "deployment"
    client_dir = repo_root / "workflows/src" / "playground" / "client"
    server_dir = repo_root / "workflows/src" / "playground" / "server"
    deployment_dir.mkdir(parents=True)
    client_dir.mkdir(parents=True)
    server_dir.mkdir(parents=True)
    (client_dir / "worker_cfx.py").write_text(
        "async def worker():\n    return {}\n", encoding="utf-8"
    )
    (server_dir / "router_sfx.py").write_text(
        "async def router():\n    return {}\n", encoding="utf-8"
    )
    deployment_script = (
        "\n".join(
            (
                "python -m polyapi function add worker ../client/worker_cfx.py --context example.test --client",
                "python -m polyapi function add router ../server/router_sfx.py --context example.test --server",
            )
        )
        + "\n"
    )
    (deployment_dir / "deployment.sh").write_text(
        deployment_script,
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=("workflows/src/**/deployment/*.sh",),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=("functions",),
        show_config=False,
        function_types=("server",),
    )

    specs = poly_deploy.load_function_specs(
        config, cli_args, poly_deploy.Logger(verbose=False)
    )

    assert [spec.name for spec in specs] == ["router"]
    assert specs[0].function_type == "server"


def test_load_function_specs_respects_orphan_excluded_contexts(tmp_path: Path) -> None:
    repo_root = tmp_path
    active_deployment_dir = repo_root / "workflows/src" / "active" / "deployment"
    active_client_dir = repo_root / "workflows/src" / "active" / "client"
    ignored_deployment_dir = repo_root / "workflows/src" / "ignored_path" / "deployment"
    ignored_client_dir = repo_root / "workflows/src" / "ignored_path" / "client"
    active_deployment_dir.mkdir(parents=True)
    active_client_dir.mkdir(parents=True)
    ignored_deployment_dir.mkdir(parents=True)
    ignored_client_dir.mkdir(parents=True)
    (active_client_dir / "active_cfx.py").write_text(
        "async def active():\n    return {}\n", encoding="utf-8"
    )
    (ignored_client_dir / "ignored_cfx.py").write_text(
        "async def ignored():\n    return {}\n", encoding="utf-8"
    )
    (active_deployment_dir / "deployment.sh").write_text(
        "python -m polyapi function add active ../client/active_cfx.py --context example.active --client\n",
        encoding="utf-8",
    )
    (ignored_deployment_dir / "deployment.sh").write_text(
        "python -m polyapi function add ignored ../client/ignored_cfx.py --context example.ignored --client\n",
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=("workflows/src/**/deployment/*.sh",),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=("example.ignored",),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=("functions",),
        show_config=False,
    )

    specs = poly_deploy.load_function_specs(
        config, cli_args, poly_deploy.Logger(verbose=False)
    )

    assert [spec.name for spec in specs] == ["active"]


def test_load_function_specs_from_python_skips_excluded_context_before_parse(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "ignored_cfx.py"
    source_path.write_text(
        "\n".join(
            (
                "from polyapi.example.excluded import broken_generated_sdk",
                "from polyapi.typedefs import PolyClientFunction",
                "",
                "polyConfig: PolyClientFunction = {",
                '    "name": "ignored",',
                '    "context": "example.excluded.v1",',
                "}",
                "",
                "async def ignored() -> dict:",
                "    return {}",
            )
        ),
        encoding="utf-8",
    )

    def fail_parse(*_: object) -> dict[str, object]:
        raise AssertionError("excluded context should not be parsed")

    monkeypatch.setattr(
        poly_deploy, "parse_function_code_with_import_fallback", fail_parse
    )

    specs = poly_deploy.load_function_specs_from_python(
        source_path, ("example.excluded",)
    )

    assert specs == []


def test_load_function_specs_from_python_uses_poly_config_without_importing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_path = tmp_path / "active_cfx.py"
    source_path.write_text(
        "\n".join(
            (
                "from polyapi.example.excluded import broken_generated_sdk",
                "from polyapi.typedefs import PolyClientFunction",
                "",
                "polyConfig: PolyClientFunction = {",
                '    "name": "active",',
                '    "context": "example.active",',
                "}",
                "",
                "async def active() -> dict:",
                "    return {}",
            )
        ),
        encoding="utf-8",
    )

    def fail_parse(*_: object) -> dict[str, object]:
        raise AssertionError("function discovery should not execute imports")

    monkeypatch.setattr(
        poly_deploy, "parse_function_code_with_import_fallback", fail_parse
    )

    specs = poly_deploy.load_function_specs_from_python(
        source_path, ("example.excluded",)
    )

    assert len(specs) == 1
    assert specs[0].name == "active"
    assert specs[0].context == "example.active"
    assert specs[0].function_type == "client"


def test_parse_context_path_map_parses_prefixes(tmp_path: Path) -> None:
    mappings = poly_deploy.parse_context_path_map(
        tmp_path,
        "example.loggers.datadog=workflows/src/utilities/common,example.sample_workflow=workflows/src/sample_workflow",
    )

    assert mappings == (
        poly_deploy.ContextPathMapping(
            context_prefix="example.loggers.datadog",
            root_path=(tmp_path / "workflows/src" / "utilities" / "common").resolve(),
        ),
        poly_deploy.ContextPathMapping(
            context_prefix="example.sample_workflow",
            root_path=(tmp_path / "workflows/src" / "sample_workflow").resolve(),
        ),
    )


def test_resolve_orphan_root_path_prompts_for_unmapped_context(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    target_root = repo_root / "workflows/src" / "utilities" / "common"
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    orphan = poly_deploy.OrphanedResource(
        artifact_type="variables",
        name="platform",
        context="example.loggers.datadog.config",
        resource_id="var-1",
        delete_path="/variables/var-1",
        detail_path="/variables/var-1",
        contexts=("example.loggers.datadog.config",),
    )
    runtime_mappings: dict[str, Any] = {}

    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: "example.loggers.datadog=workflows/src/utilities/common",
    )

    resolved = poly_deploy.resolve_orphan_root_path(
        orphan,
        config=config,
        runtime_mappings=runtime_mappings,
    )

    assert resolved == target_root.resolve()
    assert runtime_mappings == {
        "example.loggers.datadog": poly_deploy.ContextPathMapping(
            context_prefix="example.loggers.datadog",
            root_path=target_root.resolve(),
        )
    }


def test_resolve_orphan_root_path_uses_trigger_default_mapping(tmp_path: Path) -> None:
    repo_root = tmp_path
    trigger_root = repo_root / "workflows/src" / "utilities" / "common"
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("triggers",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        context_path_mappings=(
            poly_deploy.ContextPathMapping(
                context_prefix="triggers",
                root_path=trigger_root.resolve(),
            ),
        ),
    )
    orphan = poly_deploy.OrphanedResource(
        artifact_type="triggers",
        name="webhook:wh:function:fn",
        context="",
        resource_id="trg-1",
        delete_path="/triggers/trg-1",
        detail_path="/triggers/trg-1",
        contexts=(),
    )

    resolved = poly_deploy.resolve_orphan_root_path(
        orphan,
        config=config,
        runtime_mappings={},
    )

    assert resolved == trigger_root.resolve()


def test_resolve_orphan_root_path_uses_trigger_default_mapping_with_contexts(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    trigger_root = (
        repo_root
        / "workflows/src"
        / "utilities"
        / "services_router"
        / "artefacts"
        / "triggers"
    )
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("triggers",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        context_path_mappings=(
            poly_deploy.ContextPathMapping(
                context_prefix="triggers",
                root_path=trigger_root.resolve(),
            ),
            poly_deploy.ContextPathMapping(
                context_prefix="example.billing",
                root_path=(repo_root / "workflows/src" / "billing").resolve(),
            ),
            poly_deploy.ContextPathMapping(
                context_prefix="example.utils.generic",
                root_path=(
                    repo_root / "workflows/src" / "utilities" / "common"
                ).resolve(),
            ),
        ),
    )
    orphan = poly_deploy.OrphanedResource(
        artifact_type="triggers",
        name="get_setup_hook_trigger",
        context="example.billing, example.utils.generic",
        resource_id="trg-1",
        delete_path="/triggers/trg-1",
        detail_path="/triggers/trg-1",
        contexts=("example.billing", "example.utils.generic"),
    )

    resolved = poly_deploy.resolve_orphan_root_path(
        orphan,
        config=config,
        runtime_mappings={},
    )

    assert resolved == trigger_root.resolve()


def test_parse_context_path_map_supports_explicit_filename_override(
    tmp_path: Path,
) -> None:
    mappings = poly_deploy.parse_context_path_map(
        tmp_path,
        "example.test.json=workflows/src/utilities/common/artefacts/vari",
    )

    assert mappings == (
        poly_deploy.ContextPathMapping(
            context_prefix="example.test",
            root_path=(
                tmp_path
                / "workflows/src"
                / "utilities"
                / "common"
                / "artefacts"
                / "vari"
            ).resolve(),
            target_filename="example.test.json",
        ),
    )


def test_resolve_orphan_mapping_uses_full_identifier_filename_override(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    target_dir = (
        repo_root / "workflows/src" / "utilities" / "common" / "artefacts" / "vari"
    )
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        context_path_mappings=(
            poly_deploy.ContextPathMapping(
                context_prefix="example.test",
                root_path=target_dir.resolve(),
                target_filename="example.test.json",
            ),
        ),
    )
    orphan = poly_deploy.OrphanedResource(
        artifact_type="variables",
        name="test",
        context="example",
        resource_id="var-1",
        delete_path="/variables/var-1",
        detail_path="/variables/var-1",
        contexts=("example",),
    )

    resolved_root = poly_deploy.resolve_orphan_root_path(
        orphan,
        config=config,
        runtime_mappings={},
    )
    resolved_filename = poly_deploy.resolve_orphan_target_filename(
        orphan,
        config=config,
        runtime_mappings={},
    )

    assert resolved_root == target_dir.resolve()
    assert resolved_filename == "example.test.json"


def test_build_local_variable_document_strips_download_metadata() -> None:
    document = poly_deploy.build_local_variable_document(
        {
            "context": "example.test",
            "name": "platform",
            "value": "argos",
            "visibility": "ENVIRONMENT",
            "ownerUserId": "user-1",
            "createdAt": "2026-04-06T12:00:00.000Z",
            "id": "var-1",
        }
    )

    assert document == {
        "context": "example.test",
        "name": "platform",
        "value": "argos",
        "visibility": "ENVIRONMENT",
    }


def test_build_orphan_target_path_respects_explicit_filename_and_folder(
    tmp_path: Path,
) -> None:
    target_dir = (
        tmp_path / "workflows/src" / "utilities" / "common" / "artefacts" / "vari"
    )
    orphan = poly_deploy.OrphanedResource(
        artifact_type="variables",
        name="platform",
        context="example.test",
        resource_id="var-1",
        delete_path="/variables/var-1",
        detail_path="/variables/var-1",
        contexts=("example.test",),
    )

    target_path = poly_deploy.build_orphan_target_path(
        orphan,
        root_path=target_dir,
        target_filename="example.test.json",
    )

    assert target_path == target_dir / "example.test.json"


def test_maybe_prompt_for_missing_env_tokens_can_continue(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    logger = poly_deploy.Logger(verbose=False)

    monkeypatch.setattr(
        poly_deploy,
        "collect_missing_env_token_refs",
        lambda _config, _cli_args: [
            poly_deploy.MissingEnvTokenRef(
                env_name="IDENTITY_HOST_URL",
                path=repo_root
                / "workflows/src"
                / "sample_workflow"
                / "artefacts"
                / "vari"
                / "service_config.json",
                line_number=10,
            )
        ],
    )
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy, "prompt_for_missing_env_tokens", lambda: True)

    poly_deploy.maybe_prompt_for_missing_env_tokens(config, cli_args, logger)


def test_maybe_prompt_for_missing_env_tokens_stops_in_non_interactive_mode(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    logger = poly_deploy.Logger(verbose=False)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            poly_deploy,
            "collect_missing_env_token_refs",
            lambda _config, _cli_args: [
                poly_deploy.MissingEnvTokenRef(
                    env_name="IDENTITY_HOST_URL",
                    path=repo_root
                    / "workflows/src"
                    / "sample_workflow"
                    / "artefacts"
                    / "vari"
                    / "service_config.json",
                    line_number=10,
                )
            ],
        )
        monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: False)

        with pytest.raises(poly_deploy.DeployError, match="Missing env-file values"):
            poly_deploy.maybe_prompt_for_missing_env_tokens(config, cli_args, logger)


def test_process_orphans_reports_without_deleting(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo_root = tmp_path
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions", "variables"),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    expected_orphans = [
        poly_deploy.OrphanedResource(
            artifact_type="functions",
            name="worker",
            context="example.test",
            resource_id="fn-1",
            delete_path="/functions/server/fn-1",
            function_type="server",
        ),
        poly_deploy.OrphanedResource(
            artifact_type="variables",
            name="config",
            context="example.test",
            resource_id="var-1",
            delete_path="/variables/var-1",
        ),
    ]

    monkeypatch.setattr(
        poly_deploy,
        "collect_orphans",
        lambda **_: (expected_orphans, {"functions": 1}),
    )

    changed = poly_deploy.process_orphans(
        client=object(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=poly_deploy.FunctionRegistry(),
        webhook_index={},
        otp_manager=poly_deploy.OtpManager(False),
    )

    assert changed is False
    assert summary.orphans == expected_orphans


def test_process_orphans_deletes_and_marks_function_changes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions", "variables"),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=True,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    requests: list[tuple[str, str, object | None]] = []

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> None:
            requests.append((method, path, _.get("headers")))
            return None

    monkeypatch.setattr(
        poly_deploy,
        "collect_orphans",
        lambda **_: (
            [
                poly_deploy.OrphanedResource(
                    artifact_type="functions",
                    name="worker",
                    context="example.test",
                    resource_id="fn-1",
                    delete_path="/functions/server/fn-1",
                    function_type="server",
                ),
                poly_deploy.OrphanedResource(
                    artifact_type="variables",
                    name="config",
                    context="example.test",
                    resource_id="var-1",
                    delete_path="/variables/var-1",
                ),
            ],
            {},
        ),
    )

    changed = poly_deploy.process_orphans(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=poly_deploy.FunctionRegistry(),
        webhook_index={},
        otp_manager=poly_deploy.OtpManager(False),
    )

    assert changed is True
    assert requests == [
        ("DELETE", "/functions/server/fn-1", {}),
        ("DELETE", "/variables/var-1", {}),
    ]
    assert summary.functions.deleted == 1
    assert summary.variables.deleted == 1


def test_process_orphans_uses_x_otp_for_all_deletes_when_enabled(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        otp_enabled=True,
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=True,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    requests: list[tuple[str, str, object | None]] = []
    prompts: list[str] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> None:
            requests.append((method, path, kwargs.get("headers")))
            return None

    monkeypatch.setattr(
        poly_deploy,
        "collect_orphans",
        lambda **_: (
            [
                poly_deploy.OrphanedResource(
                    artifact_type="functions",
                    name="worker",
                    context="example.test",
                    resource_id="fn-1",
                    delete_path="/functions/server/fn-1",
                    function_type="server",
                ),
                poly_deploy.OrphanedResource(
                    artifact_type="variables",
                    name="config",
                    context="example.test",
                    resource_id="var-1",
                    delete_path="/variables/var-1",
                ),
            ],
            {},
        ),
    )

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return "123456"

    monkeypatch.setattr(poly_deploy.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)

    changed = poly_deploy.process_orphans(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=poly_deploy.FunctionRegistry(),
        webhook_index={},
        otp_manager=poly_deploy.OtpManager(True),
    )

    assert changed is True
    assert requests == [
        ("DELETE", "/functions/server/fn-1", {"x-otp": "123456"}),
        ("DELETE", "/variables/var-1", {"x-otp": "123456"}),
    ]
    assert prompts == ["OTP is required to write resources. Enter OTP: "]
    assert summary.functions.deleted == 1
    assert summary.variables.deleted == 1


def test_process_orphans_adopts_variable_locally_using_context_mapping(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    target_root = repo_root / "workflows/src" / "utilities" / "common"
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        context_path_mappings=(
            poly_deploy.ContextPathMapping(
                context_prefix="example.loggers.datadog",
                root_path=target_root.resolve(),
            ),
        ),
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
        adopt_orphans=True,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    orphan = poly_deploy.OrphanedResource(
        artifact_type="variables",
        name="platform",
        context="example.loggers.datadog.config",
        resource_id="var-1",
        delete_path="/variables/var-1",
        detail_path="/variables/var-1",
        contexts=("example.loggers.datadog.config",),
    )

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> dict[str, object]:
            assert method == "GET"
            assert path == "/variables/var-1"
            return {
                "context": "example.loggers.datadog.config",
                "name": "platform",
                "description": "Datadog platform name",
                "value": "argos",
                "secret": False,
                "visibility": "ENVIRONMENT",
            }

    monkeypatch.setattr(
        poly_deploy,
        "collect_orphans",
        lambda **_: ([orphan], {}),
    )

    changed = poly_deploy.process_orphans(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=poly_deploy.FunctionRegistry(),
        webhook_index={},
        otp_manager=poly_deploy.OtpManager(False),
    )

    target_path = target_root / "artefacts" / "vari" / "platform.json"
    assert changed is False
    assert summary.orphans == []
    assert target_path.exists()
    assert json.loads(target_path.read_text(encoding="utf-8")) == {
        "context": "example.loggers.datadog.config",
        "name": "platform",
        "description": "Datadog platform name",
        "value": "argos",
        "secret": False,
        "visibility": "ENVIRONMENT",
    }


@pytest.mark.parametrize("remote_secrecy", ["SECRET", "OBSCURED", "PARTIAL"])
@pytest.mark.parametrize("allow_value_update", [False, True])
def test_sync_variables_recreate_on_secrecy_change_uses_otp_headers(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    allow_value_update: bool,
    remote_secrecy: str,
) -> None:
    repo_root = tmp_path
    variable_dir = repo_root / "workflows/src" / "playground" / "artefacts" / "vari"
    variable_dir.mkdir(parents=True)
    variable_path = variable_dir / "config.json"
    variable_path.write_text(
        json.dumps(
            {
                "context": "example.test",
                "name": "config",
                "value": {"clientSecret": "synthetic-new"},
                "secret": False,
                "secrecy": "NONE",
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=allow_value_update,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        otp_enabled=True,
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    requests: list[tuple[str, str, object | None]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            requests.append((method, path, kwargs.get("headers")))
            if method == "GET" and path == "/variables":
                return [{"id": "var-1", "context": "example.test", "name": "config"}]
            if method == "GET" and path == "/variables/var-1":
                return {
                    "id": "var-1",
                    "context": "example.test",
                    "name": "config",
                    "value": {"clientSecret": "********"},
                    "secret": remote_secrecy == "SECRET",
                    "secrecy": remote_secrecy,
                }
            if method == "PATCH" and path == "/variables/var-1":
                payload = kwargs["payload"]
                assert isinstance(payload, dict)
                assert ("value" in payload) is allow_value_update
                raise poly_deploy.ApiError(
                    "PATCH", path, 400, secrecy_change_rejected=True
                )
            if method == "DELETE" and path == "/variables/var-1":
                return None
            if method == "POST" and path == "/variables":
                return {"id": "var-2", "context": "example.test", "name": "config"}
            raise AssertionError((method, path, kwargs))

    monkeypatch.setattr(poly_deploy.getpass, "getpass", lambda _prompt: "123456")
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)

    poly_deploy.sync_variables(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        otp_manager=poly_deploy.OtpManager(True),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    if allow_value_update:
        assert ("DELETE", "/variables/var-1", {"x-otp": "123456"}) in requests
        assert summary.variables.updated == 1
    else:
        assert not any(method in {"DELETE", "POST"} for method, _, _ in requests)
        assert summary.variables.failed == 1
        assert summary.variables.updated == 0


def test_sync_variables_force_updates_unchanged_non_secret_variable(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    variable_dir = repo_root / "workflows/src" / "playground" / "artefacts" / "vari"
    variable_dir.mkdir(parents=True)
    variable_path = variable_dir / "config.json"
    variable_payload = {
        "context": "example.test",
        "name": "config",
        "value": {"enabled": True},
        "secrecy": "NONE",
        "visibility": "ENVIRONMENT",
    }
    variable_path.write_text(json.dumps(variable_payload), encoding="utf-8")

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    summary = poly_deploy.Summary()
    requests: list[tuple[str, str]] = []

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> object:
            requests.append((method, path))
            if method == "GET" and path == "/variables":
                return [{"id": "var-1", "context": "example.test", "name": "config"}]
            if method == "GET" and path == "/variables/var-1":
                return {
                    "id": "var-1",
                    "updatedAt": "2099-01-01T00:00:00Z",
                    **variable_payload,
                }
            if method == "PATCH" and path == "/variables/var-1":
                return {"id": "var-1", **variable_payload}
            raise AssertionError((method, path))

    poly_deploy.sync_variables(
        client=DummyClient(),
        config=config,
        cli_args=poly_deploy.CliArgs(
            dry_run=False,
            force=True,
            verbose=False,
            delete_orphans=False,
            path_filters=(),
            enabled_types=None,
            show_config=False,
        ),
        logger=poly_deploy.Logger(verbose=False),
        summary=summary,
        otp_manager=poly_deploy.OtpManager(False),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert ("PATCH", "/variables/var-1") in requests
    assert summary.variables.updated == 1
    assert summary.variables.skipped == 0


def test_sync_variables_skips_existing_obscured_variable_without_force(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    variable_dir = repo_root / "workflows/src" / "playground" / "artefacts" / "vari"
    variable_dir.mkdir(parents=True)
    variable_path = variable_dir / "token.json"
    variable_path.write_text(
        json.dumps(
            {
                "context": "example.test",
                "name": "token",
                "value": "do-not-deploy",
                "secrecy": "OBSCURED",
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    summary = poly_deploy.Summary()
    requests: list[tuple[str, str]] = []

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> object:
            requests.append((method, path))
            if method == "GET" and path == "/variables":
                return [{"id": "var-1", "context": "example.test", "name": "token"}]
            if method == "GET" and path == "/variables/var-1":
                return {
                    "id": "var-1",
                    "context": "example.test",
                    "name": "token",
                    "value": {},
                    "secrecy": "OBSCURED",
                }
            raise AssertionError((method, path))

    poly_deploy.sync_variables(
        client=DummyClient(),
        config=config,
        cli_args=poly_deploy.CliArgs(
            dry_run=False,
            force=False,
            verbose=False,
            delete_orphans=False,
            path_filters=(),
            enabled_types=None,
            show_config=False,
        ),
        logger=poly_deploy.Logger(verbose=False),
        summary=summary,
        otp_manager=poly_deploy.OtpManager(False),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert ("PATCH", "/variables/var-1") not in requests
    assert summary.variables.updated == 0
    assert summary.variables.skipped == 1


def test_sync_variables_creates_new_obscured_variable_with_value(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    variable_dir = repo_root / "workflows/src" / "playground" / "artefacts" / "vari"
    variable_dir.mkdir(parents=True)
    variable_path = variable_dir / "token.json"
    variable_path.write_text(
        json.dumps(
            {
                "context": "example.test",
                "name": "token",
                "value": {"username": "user", "password": "pass"},
                "secrecy": "OBSCURED",
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    summary = poly_deploy.Summary()
    post_payloads: list[dict[str, object]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            if method == "GET" and path == "/variables":
                return []
            if method == "POST" and path == "/variables":
                payload = kwargs.get("payload")
                if isinstance(payload, dict):
                    post_payloads.append(payload)
                return {"id": "var-1", "context": "example.test", "name": "token"}
            raise AssertionError((method, path))

    poly_deploy.sync_variables(
        client=DummyClient(),
        config=config,
        cli_args=poly_deploy.CliArgs(
            dry_run=False,
            force=False,
            verbose=False,
            delete_orphans=False,
            path_filters=(),
            enabled_types=None,
            show_config=False,
        ),
        logger=poly_deploy.Logger(verbose=False),
        summary=summary,
        otp_manager=poly_deploy.OtpManager(False),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert post_payloads == [
        {
            "context": "example.test",
            "name": "token",
            "value": {"username": "user", "password": "pass"},
            "secrecy": "OBSCURED",
        }
    ]
    assert summary.variables.created == 1


@pytest.mark.parametrize("allow_value_update", [False, True])
def test_sync_variables_force_updates_existing_obscured_variable(
    tmp_path: Path,
    allow_value_update: bool,
) -> None:
    repo_root = tmp_path
    variable_dir = repo_root / "workflows/src" / "playground" / "artefacts" / "vari"
    variable_dir.mkdir(parents=True)
    variable_path = variable_dir / "token.json"
    variable_path.write_text(
        json.dumps(
            {
                "context": "example.test",
                "name": "token",
                "value": "updated-token",
                "secrecy": "OBSCURED",
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=allow_value_update,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    summary = poly_deploy.Summary()
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            payload = kwargs.get("payload")
            requests.append(
                (method, path, payload if isinstance(payload, dict) else None)
            )
            if method == "GET" and path == "/variables":
                return [{"id": "var-1", "context": "example.test", "name": "token"}]
            if method == "GET" and path == "/variables/var-1":
                return {
                    "id": "var-1",
                    "context": "example.test",
                    "name": "token",
                    "value": {},
                    "secrecy": "OBSCURED",
                }
            if method == "PATCH" and path == "/variables/var-1":
                return {"id": "var-1"}
            raise AssertionError((method, path))

    poly_deploy.sync_variables(
        client=DummyClient(),
        config=config,
        cli_args=poly_deploy.CliArgs(
            dry_run=False,
            force=True,
            verbose=False,
            delete_orphans=False,
            path_filters=(),
            enabled_types=None,
            show_config=False,
        ),
        logger=poly_deploy.Logger(verbose=False),
        summary=summary,
        otp_manager=poly_deploy.OtpManager(False),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    patch_requests = [
        payload
        for method, path, payload in requests
        if method == "PATCH" and path == "/variables/var-1"
    ]
    assert patch_requests == [
        {
            "context": "example.test",
            "name": "token",
            **({"value": "updated-token"} if allow_value_update else {}),
            "secrecy": "OBSCURED",
        }
    ]
    assert summary.variables.updated == 1
    assert summary.variables.skipped == 0


def test_sync_variables_force_omits_empty_existing_obscured_value(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    variable_dir = repo_root / "workflows/src" / "playground" / "artefacts" / "vari"
    variable_dir.mkdir(parents=True)
    variable_path = variable_dir / "token.json"
    variable_path.write_text(
        json.dumps(
            {
                "context": "example.test",
                "name": "token",
                "value": "",
                "secrecy": "OBSCURED",
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("variables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=("workflows/src/**/artefacts/vari/*.json",),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    summary = poly_deploy.Summary()
    patch_payloads: list[dict[str, object]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            if method == "GET" and path == "/variables":
                return [{"id": "var-1", "context": "example.test", "name": "token"}]
            if method == "GET" and path == "/variables/var-1":
                return {
                    "id": "var-1",
                    "context": "example.test",
                    "name": "token",
                    "value": {},
                    "secrecy": "OBSCURED",
                }
            if method == "PATCH" and path == "/variables/var-1":
                payload = kwargs.get("payload")
                if isinstance(payload, dict):
                    patch_payloads.append(payload)
                return {"id": "var-1"}
            raise AssertionError((method, path))

    poly_deploy.sync_variables(
        client=DummyClient(),
        config=config,
        cli_args=poly_deploy.CliArgs(
            dry_run=False,
            force=True,
            verbose=False,
            delete_orphans=False,
            path_filters=(),
            enabled_types=None,
            show_config=False,
        ),
        logger=poly_deploy.Logger(verbose=False),
        summary=summary,
        otp_manager=poly_deploy.OtpManager(False),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert patch_payloads == [
        {
            "context": "example.test",
            "name": "token",
            "secrecy": "OBSCURED",
        }
    ]
    assert summary.variables.updated == 1


def test_sync_triggers_recreate_uses_otp_headers(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    trigger_dir = repo_root / "workflows/src" / "playground" / "artefacts" / "triggers"
    trigger_dir.mkdir(parents=True)
    trigger_path = trigger_dir / "worker_trigger.json"
    trigger_path.write_text(json.dumps({"name": "worker_trigger"}), encoding="utf-8")

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("triggers",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=("workflows/src/**/artefacts/triggers/*.json",),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        otp_enabled=True,
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    requests: list[tuple[str, str, object | None]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            requests.append((method, path, kwargs.get("headers")))
            if method == "GET" and path == "/triggers":
                return [{"id": "trg-1"}]
            if method == "GET" and path == "/triggers/trg-1":
                return {"id": "trg-1", "name": "worker_trigger", "enabled": True}
            if method == "DELETE" and path == "/triggers/trg-1":
                return None
            if method == "POST" and path == "/triggers":
                return {"id": "trg-2"}
            raise AssertionError((method, path, kwargs))

    monkeypatch.setattr(poly_deploy.getpass, "getpass", lambda _prompt: "123456")
    monkeypatch.setattr(poly_deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(poly_deploy.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        poly_deploy,
        "build_trigger_spec",
        lambda *_args, **_kwargs: {
            "create_payload": {
                "name": "worker_trigger",
                "enabled": False,
                "source": {"errorHandler": {"path": "example"}},
                "destination": {"serverFunctionId": "owned-worker"},
            },
            "semantic": {"name": "worker_trigger", "enabled": False},
            "label": "worker_trigger",
        },
    )
    monkeypatch.setattr(
        poly_deploy, "find_existing_trigger", lambda *_args, **_kwargs: {"id": "trg-1"}
    )
    monkeypatch.setattr(
        poly_deploy,
        "trigger_semantic_from_remote",
        lambda _detail: {
            "name": "worker_trigger",
            "enabled": True,
            "serverFunctionId": "owned-worker",
        },
    )
    monkeypatch.setattr(
        poly_deploy, "build_trigger_patch_payload", lambda *_args, **_kwargs: None
    )

    registry = poly_deploy.FunctionRegistry()
    registry.load(
        "server",
        [{"id": "owned-worker", "name": "worker", "context": "example.playground"}],
    )
    poly_deploy.sync_triggers(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=registry,
        webhook_index={},
        otp_manager=poly_deploy.OtpManager(True),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert ("DELETE", "/triggers/trg-1", {"x-otp": "123456"}) in requests
    assert summary.triggers.updated == 1


def test_main_adopt_orphans_skips_normal_sync(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
        adopt_orphans=True,
    )
    config = poly_deploy.DeployConfig(
        repo_root=tmp_path,
        enabled_types=(
            "functions",
            "variables",
            "webhooks",
            "jobs",
            "tables",
            "triggers",
        ),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
    )
    called: dict[str, bool] = {"process_orphans": False}
    env_file = poly_deploy.EnvLoadResult(
        display_path=".env",
        resolved_path=(tmp_path / ".env").resolve(),
        exists=True,
        from_envpath=False,
    )
    preflight = poly_deploy.DeploymentPreflight(
        env_file=env_file,
        production_branch="main",
        current_branch="feature",
        production_sensitive=False,
    )

    monkeypatch.setattr(poly_deploy, "parse_args", lambda: cli_args)
    monkeypatch.setattr(poly_deploy, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(poly_deploy, "load_env", lambda _repo_root: env_file)
    monkeypatch.setattr(
        poly_deploy, "warn_for_missing_default_env_file", lambda *_: None
    )
    monkeypatch.setattr(
        poly_deploy, "normalize_cli_path_filters", lambda _repo_root, args: args
    )
    monkeypatch.setattr(
        poly_deploy, "build_config", lambda _repo_root, _cli_args: config
    )
    monkeypatch.setattr(
        poly_deploy,
        "build_deployment_preflight",
        lambda _repo_root, _env_file: preflight,
    )
    monkeypatch.setattr(poly_deploy, "print_startup_banner", lambda **_: None)
    monkeypatch.setattr(poly_deploy, "print_deployment_visibility", lambda *_: None)
    monkeypatch.setattr(poly_deploy, "get_target_env_id", lambda _env_file: "env-test")
    monkeypatch.setattr(poly_deploy, "enforce_target_environment", lambda *_: None)
    monkeypatch.setattr(
        poly_deploy,
        "maybe_prompt_for_initial_scope",
        lambda args, _config, _logger: args,
    )
    monkeypatch.setattr(
        poly_deploy, "maybe_confirm_branch_env_file_match", lambda *_: None
    )
    monkeypatch.setattr(poly_deploy, "enforce_production_safeguards", lambda *_: None)
    monkeypatch.setattr(
        poly_deploy, "maybe_prompt_for_missing_env_tokens", lambda *_: None
    )
    monkeypatch.setattr(
        poly_deploy, "get_api_key_and_url", lambda: ("token", "https://example.com")
    )
    monkeypatch.setattr(
        poly_deploy,
        "fetch_function_registry",
        lambda _client: poly_deploy.FunctionRegistry(),
    )
    monkeypatch.setattr(poly_deploy, "build_remote_webhook_index", lambda _client: {})
    monkeypatch.setattr(
        poly_deploy, "reference_registry", lambda _client, registry: registry
    )
    monkeypatch.setattr(poly_deploy, "reference_webhooks", lambda _client, index: index)
    monkeypatch.setattr(poly_deploy, "print_summary", lambda *_: None)

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("normal sync should not run in adopt-orphans mode")

    monkeypatch.setattr(poly_deploy, "sync_variables", fail_if_called)
    monkeypatch.setattr(poly_deploy, "sync_functions", fail_if_called)
    monkeypatch.setattr(poly_deploy, "sync_tables", fail_if_called)
    monkeypatch.setattr(poly_deploy, "sync_webhooks", fail_if_called)
    monkeypatch.setattr(poly_deploy, "sync_jobs", fail_if_called)
    monkeypatch.setattr(poly_deploy, "sync_triggers", fail_if_called)
    monkeypatch.setattr(poly_deploy, "run_poly_generate", fail_if_called)

    def fake_process_orphans(**_: object) -> bool:
        called["process_orphans"] = True
        return False

    monkeypatch.setattr(poly_deploy, "process_orphans", fake_process_orphans)

    assert poly_deploy.main() == 0
    assert called["process_orphans"] is True


def test_main_runs_sync_steps_in_dependency_order(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    config = poly_deploy.DeployConfig(
        repo_root=tmp_path,
        enabled_types=(
            "functions",
            "variables",
            "webhooks",
            "jobs",
            "tables",
            "triggers",
            "schemas",
            "ai-functions",
            "canopy",
            "subscriptions",
        ),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=tmp_path / ".poly_deploy.state.json",
    )
    calls: list[str] = []
    env_file = poly_deploy.EnvLoadResult(
        display_path=".env",
        resolved_path=(tmp_path / ".env").resolve(),
        exists=True,
        from_envpath=False,
    )
    preflight = poly_deploy.DeploymentPreflight(
        env_file=env_file,
        production_branch="main",
        current_branch="feature",
        production_sensitive=False,
    )

    monkeypatch.setattr(poly_deploy, "parse_args", lambda: cli_args)
    monkeypatch.setattr(poly_deploy, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(poly_deploy, "load_env", lambda _repo_root: env_file)
    monkeypatch.setattr(
        poly_deploy, "warn_for_missing_default_env_file", lambda *_: None
    )
    monkeypatch.setattr(
        poly_deploy, "normalize_cli_path_filters", lambda _repo_root, args: args
    )
    monkeypatch.setattr(
        poly_deploy, "build_config", lambda _repo_root, _cli_args: config
    )
    monkeypatch.setattr(
        poly_deploy,
        "build_deployment_preflight",
        lambda _repo_root, _env_file: preflight,
    )
    monkeypatch.setattr(poly_deploy, "print_startup_banner", lambda **_: None)
    monkeypatch.setattr(poly_deploy, "print_deployment_visibility", lambda *_: None)
    monkeypatch.setattr(poly_deploy, "get_target_env_id", lambda _env_file: "env-test")
    monkeypatch.setattr(poly_deploy, "enforce_target_environment", lambda *_: None)
    monkeypatch.setattr(
        poly_deploy,
        "maybe_prompt_for_initial_scope",
        lambda args, _config, _logger: args,
    )
    monkeypatch.setattr(
        poly_deploy, "maybe_confirm_branch_env_file_match", lambda *_: None
    )
    monkeypatch.setattr(poly_deploy, "enforce_production_safeguards", lambda *_: None)
    monkeypatch.setattr(
        poly_deploy, "maybe_prompt_for_missing_env_tokens", lambda *_: None
    )
    monkeypatch.setattr(
        poly_deploy, "get_api_key_and_url", lambda: ("token", "https://example.com")
    )
    monkeypatch.setattr(poly_deploy, "print_summary", lambda *_: None)
    monkeypatch.setattr(poly_deploy, "run_poly_generate", lambda *_: None)

    registry = poly_deploy.FunctionRegistry()
    monkeypatch.setattr(
        poly_deploy, "fetch_function_registry", lambda _client: registry
    )

    def fake_sync_variables(**_: object) -> None:
        calls.append("variables")

    def fake_sync_tables(**_: object) -> None:
        calls.append("tables")

    def fake_sync_functions(**_: object) -> tuple[Any, bool]:
        calls.append("functions")
        return registry, False

    def fake_sync_webhooks(**_: object) -> dict[tuple[str, str], dict[str, object]]:
        calls.append("webhooks")
        return {}

    def fake_sync_triggers(**_: object) -> None:
        calls.append("triggers")

    def fake_sync_jobs(**_: object) -> None:
        calls.append("jobs")

    def fake_process_orphans(**options: object) -> bool:
        assert options["function_registry"] is registry
        assert options["webhook_index"] == {}
        calls.append("orphans")
        return False

    monkeypatch.setattr(poly_deploy, "sync_variables", fake_sync_variables)
    monkeypatch.setattr(poly_deploy, "sync_canopy", lambda *_: calls.append("canopy"))
    monkeypatch.setattr(poly_deploy, "sync_tables", fake_sync_tables)
    monkeypatch.setattr(
        poly_deploy, "sync_schemas", lambda **_: calls.append("schemas")
    )
    monkeypatch.setattr(poly_deploy, "sync_functions", fake_sync_functions)
    monkeypatch.setattr(
        poly_deploy, "sync_ai_functions", lambda *_: calls.append("ai-functions")
    )
    monkeypatch.setattr(poly_deploy, "sync_webhooks", fake_sync_webhooks)
    monkeypatch.setattr(poly_deploy, "sync_triggers", fake_sync_triggers)
    monkeypatch.setattr(poly_deploy, "sync_jobs", fake_sync_jobs)
    monkeypatch.setattr(
        poly_deploy, "sync_subscriptions", lambda *_: calls.append("subscriptions")
    )
    monkeypatch.setattr(poly_deploy, "process_orphans", fake_process_orphans)
    monkeypatch.setattr(poly_deploy, "build_remote_webhook_index", lambda _client: {})

    monkeypatch.setattr(
        poly_deploy,
        "reference_registry",
        lambda _client, registry: poly_deploy.FunctionRegistry(),
    )
    monkeypatch.setattr(
        poly_deploy,
        "reference_webhooks",
        lambda _client, index: {("shared", "hook"): {"id": "shared"}},
    )
    assert poly_deploy.main() == 0
    assert calls == [
        "variables",
        "canopy",
        "tables",
        "schemas",
        "functions",
        "ai-functions",
        "webhooks",
        "triggers",
        "jobs",
        "subscriptions",
        "orphans",
    ]


def test_build_local_job_document_rewrites_server_function_ids_to_context_and_name() -> (
    None
):
    registry = poly_deploy.FunctionRegistry()
    registry.upsert(
        "server",
        {
            "id": "fn-job-dispatcher",
            "context": "example.sample_workflow",
            "name": "job_dispatcher",
        },
    )

    document = poly_deploy.build_local_job_document(
        {
            "id": "job-1",
            "name": "example.ra.put_extracts_job",
            "functions": [
                {
                    "id": "fn-job-dispatcher",
                    "headersPayload": {"min_status_log_override": "debug"},
                    "eventPayload": {},
                    "paramsPayload": {"includeTransactions": "N"},
                }
            ],
            "executionType": "sequential",
        },
        registry,
    )

    assert document == {
        "name": "example.ra.put_extracts_job",
        "functions": [
            {
                "functionContext": "example.sample_workflow",
                "functionName": "job_dispatcher",
                "headersPayload": {"min_status_log_override": "debug"},
                "eventPayload": {},
                "paramsPayload": {"includeTransactions": "N"},
            }
        ],
        "executionType": "sequential",
    }


def test_build_job_payload_resolves_function_context_and_name_to_id() -> None:
    registry = poly_deploy.FunctionRegistry()
    registry.upsert(
        "server",
        {
            "id": "fn-currency-job-dispatcher",
            "context": "example.sample_workflow",
            "name": "job_dispatcher",
        },
    )

    payload = poly_deploy.build_job_payload(
        Path("sample_workflow_job.json"),
        {
            "name": "example.sample_workflow.put_exchange_rates_job",
            "functions": [
                {
                    "functionContext": "example.sample_workflow",
                    "functionName": "job_dispatcher",
                    "headersPayload": {"min_status_log_override": "debug"},
                    "eventPayload": {},
                    "paramsPayload": {},
                }
            ],
            "executionType": "sequential",
        },
        registry,
    )

    assert payload == {
        "name": "example.sample_workflow.put_exchange_rates_job",
        "functions": [
            {
                "id": "fn-currency-job-dispatcher",
                "headersPayload": {"min_status_log_override": "debug"},
                "eventPayload": {},
                "paramsPayload": {},
            }
        ],
        "executionType": "sequential",
    }


def test_normalize_job_subset_ignores_unreported_empty_and_default_fields() -> None:
    local, remote = poly_deploy.normalize_job_subset(
        {
            "name": "example.ra.put_extracts_job",
            "enabled": False,
            "schedule": {
                "type": "periodical",
                "value": "0 0 */2 * * *",
                "acceptableDelayTime": 30,
            },
            "functions": [
                {
                    "id": "fn-job-dispatcher",
                    "headersPayload": {"min_status_log_override": "debug"},
                    "eventPayload": {},
                    "paramsPayload": {},
                }
            ],
        },
        {
            "name": "example.ra.put_extracts_job",
            "schedule": {
                "type": "periodical",
                "value": "0 0 */2 * * *",
            },
            "functions": [
                {
                    "id": "fn-job-dispatcher",
                    "headersPayload": {"min_status_log_override": "debug"},
                }
            ],
        },
    )

    assert local == remote


def test_normalize_job_subset_ignores_unreported_execution_limits() -> None:
    local, remote = poly_deploy.normalize_job_subset(
        {
            "name": "example.ra.put_extracts_job",
            "maxConcurrentJobs": 1,
            "maxExecutionTime": 60,
        },
        {
            "name": "example.ra.put_extracts_job",
            "maxConcurrentJobs": 0,
            "maxExecutionTime": 3600,
        },
    )

    assert local == remote


def test_normalize_job_subset_keeps_non_default_delay_when_unreported() -> None:
    local, remote = poly_deploy.normalize_job_subset(
        {
            "name": "example.ra.put_extracts_job",
            "schedule": {
                "type": "periodical",
                "value": "0 0 */2 * * *",
                "acceptableDelayTime": 60,
            },
        },
        {
            "name": "example.ra.put_extracts_job",
            "schedule": {
                "type": "periodical",
                "value": "0 0 */2 * * *",
            },
        },
    )

    assert local != remote


def test_sync_jobs_force_updates_when_payload_is_unchanged(tmp_path: Path) -> None:
    repo_root = tmp_path
    job_dir = repo_root / "workflows/src" / "sample_workflow" / "artefacts" / "jobs"
    job_dir.mkdir(parents=True)
    job_path = job_dir / "sample_workflow.json"
    job_path.write_text(
        json.dumps(
            {
                "name": "example.ra.put_extracts_job",
                "functions": [
                    {
                        "functionContext": "example.sample_workflow",
                        "functionName": "job_dispatcher",
                        "eventPayload": {},
                        "paramsPayload": {},
                        "headersPayload": {},
                    }
                ],
                "executionType": "sequential",
            }
        ),
        encoding="utf-8",
    )
    registry = poly_deploy.FunctionRegistry()
    registry.upsert(
        "server",
        {
            "id": "fn-job-dispatcher",
            "context": "example.sample_workflow",
            "name": "job_dispatcher",
        },
    )
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("jobs",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=("workflows/src/**/artefacts/jobs/*.json",),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=True,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    requests: list[tuple[str, str]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            requests.append((method, path))
            if method == "GET" and path == "/jobs":
                return [{"id": "job-1", "name": "example.ra.put_extracts_job"}]
            if method == "GET" and path == "/jobs/job-1":
                return {
                    "id": "job-1",
                    "name": "example.ra.put_extracts_job",
                    "functions": [
                        {
                            "id": "fn-job-dispatcher",
                            "eventPayload": {},
                            "paramsPayload": {},
                            "headersPayload": {},
                        }
                    ],
                    "executionType": "sequential",
                }
            if method == "PATCH" and path == "/jobs/job-1":
                assert kwargs["payload"]["name"] == "example.ra.put_extracts_job"
                return {}
            raise AssertionError((method, path, kwargs))

    poly_deploy.sync_jobs(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=poly_deploy.Logger(verbose=False),
        summary=summary,
        function_registry=registry,
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert ("PATCH", "/jobs/job-1") in requests
    assert summary.jobs.updated == 1
    assert summary.jobs.skipped == 0


def test_build_local_trigger_document_rewrites_ids_to_context_and_name() -> None:
    registry = poly_deploy.FunctionRegistry()
    registry.upsert(
        "server",
        {
            "id": "fn-router",
            "context": "example.profiles",
            "name": "router_handler",
        },
    )
    webhook_index = {
        ("example.profiles", "post_sabre_lookup_hook"): {
            "id": "wh-sabre",
            "context": "example.profiles",
            "name": "post_sabre_lookup_hook",
        }
    }

    document = poly_deploy.build_local_trigger_document(
        {
            "id": "trg-1",
            "webhookHandleId": "wh-sabre",
            "serverFunctionId": "fn-router",
            "waitForResponse": True,
            "enabled": True,
            "createdAt": "2026-04-06T12:00:00.000Z",
            "ownerUserId": "user-1",
        },
        function_registry=registry,
        webhook_index=webhook_index,
    )

    assert document == {
        "enabled": True,
        "source": {
            "webhookContext": "example.profiles",
            "webhookName": "post_sabre_lookup_hook",
        },
        "waitForResponse": True,
        "destination": {
            "functionContext": "example.profiles",
            "functionName": "router_handler",
        },
    }


def test_build_webhook_payload_drops_schema_when_event_payload_type_is_not_object() -> (
    None
):
    payload = poly_deploy.build_webhook_payload(
        Path("hook.json"),
        {
            "name": "get_brand_mapping_hook",
            "context": "example.billing",
            "method": "GET",
            "slug": "example-dev",
            "subpath": "/billing/brand-mapping",
            "eventPayloadType": "null",
            "eventPayloadTypeSchema": {},
        },
        poly_deploy.FunctionRegistry(),
    )

    assert "eventPayloadTypeSchema" not in payload


def test_normalize_webhook_subset_ignores_query_order_and_placeholder_style() -> None:
    local, remote = poly_deploy.normalize_webhook_subset(
        {
            "name": "get_match_profile_hook",
            "context": "example.profiles",
            "method": "get",
            "slug": "example-dev",
            "subpath": "/profile/{nameId}?b={second}&a={first}",
            "eventPayloadType": "NULL",
        },
        {
            "name": "get_match_profile_hook",
            "context": "example.profiles",
            "method": "GET",
            "slug": "/example-dev/",
            "subpath": "profile/{{nameId}}?a={{first}}&b={{second}}",
            "eventPayloadType": "null",
        },
    )

    assert local == remote


def test_sync_webhooks_matches_existing_by_slug_and_subpath(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    monkeypatch.setenv("Example_SLUG", "example-dev")
    webhook_dir = repo_root / "workflows/src" / "profiles" / "artefacts" / "webhooks"
    webhook_dir.mkdir(parents=True)
    webhook_path = webhook_dir / "post_loyalty_email_hook.json"
    webhook_path.write_text(
        json.dumps(
            {
                "name": "post_loyalty_email_hook",
                "context": "example.profiles",
                "method": "POST",
                "slug": "{{Example_SLUG}}",
                "subpath": "profile/{nameId}/loyalty/email?source={source}&channel={channel}&user={user}",
                "eventPayloadType": "object",
                "eventPayloadTypeSchema": {"type": "object"},
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("webhooks",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=("workflows/src/**/artefacts/webhooks/*.json",),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    requests: list[tuple[str, str]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            requests.append((method, path))
            if method == "GET" and path == "/webhooks":
                return [
                    {
                        "id": "wh-1",
                        "name": "legacy_loyalty_email_hook",
                        "context": "example.profiles",
                        "subpath": "profile/{nameId}/loyalty/email?source={source}&channel={channel}&user={user}",
                        "slug": None,
                    }
                ]
            if method == "GET" and path == "/webhooks/wh-1":
                return {
                    "id": "wh-1",
                    "name": "legacy_loyalty_email_hook",
                    "context": "example.legacy",
                    "method": "POST",
                    "slug": "example-dev",
                    "subpath": "profile/{{name_id}}/loyalty/email?source={{source}}&channel={{channel}}&user={{user}}",
                    "eventPayloadType": "object",
                    "eventPayloadTypeSchema": {"type": "object"},
                }
            if method == "PATCH" and path == "/webhooks/wh-1":
                return {
                    "id": "wh-1",
                    "name": "post_loyalty_email_hook",
                    "context": "example.profiles",
                    "method": "POST",
                    "slug": "example-dev",
                    "subpath": "profile/{nameId}/loyalty/email?source={source}&channel={channel}&user={user}",
                }
            raise AssertionError((method, path, kwargs))

    index = poly_deploy.sync_webhooks(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=poly_deploy.FunctionRegistry(),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert ("PATCH", "/webhooks/wh-1") in requests
    assert ("POST", "/webhooks") not in requests
    assert index[("example.profiles", "post_loyalty_email_hook")]["id"] == "wh-1"
    assert summary.webhooks.updated == 1


def test_sync_webhooks_skips_when_only_route_formatting_differs(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    webhook_dir = repo_root / "workflows/src" / "billing" / "artefacts" / "webhooks"
    webhook_dir.mkdir(parents=True)
    webhook_path = webhook_dir / "get_brand_mapping_hook.json"
    webhook_path.write_text(
        json.dumps(
            {
                "name": "get_brand_mapping_hook",
                "context": "example.billing",
                "method": "get",
                "slug": "example-dev",
                "subpath": "/billing/brand-mapping",
                "eventPayloadType": "null",
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("webhooks",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=("workflows/src/**/artefacts/webhooks/*.json",),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    requests: list[tuple[str, str]] = []

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            requests.append((method, path))
            if method == "GET" and path == "/webhooks":
                return [
                    {
                        "id": "wh-2",
                        "name": "get_brand_mapping_hook",
                        "context": "example.billing",
                        "slug": "example-dev",
                        "subpath": "billing/brand-mapping",
                    }
                ]
            if method == "GET" and path == "/webhooks/wh-2":
                return {
                    "id": "wh-2",
                    "name": "get_brand_mapping_hook",
                    "context": "example.billing",
                    "method": "GET",
                    "slug": "example-dev",
                    "subpath": "billing/brand-mapping",
                    "eventPayloadType": "null",
                }
            raise AssertionError((method, path, kwargs))

    index = poly_deploy.sync_webhooks(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=poly_deploy.FunctionRegistry(),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert ("PATCH", "/webhooks/wh-2") not in requests
    assert ("POST", "/webhooks") not in requests
    assert index[("example.billing", "get_brand_mapping_hook")]["id"] == "wh-2"
    assert summary.webhooks.skipped == 1


def test_sync_webhooks_recovers_from_post_conflict_by_refreshing_route_lookup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    monkeypatch.setenv("Example_SLUG", "example-dev")
    webhook_dir = repo_root / "workflows/src" / "profiles" / "artefacts" / "webhooks"
    webhook_dir.mkdir(parents=True)
    webhook_path = webhook_dir / "post_profile_matches_hook.json"
    webhook_path.write_text(
        json.dumps(
            {
                "name": "post_profile_matches_hook",
                "context": "example.profiles",
                "description": "Test webhook",
                "method": "POST",
                "slug": "{{Example_SLUG}}",
                "subpath": "profile/matches",
                "eventPayloadType": "object",
                "eventPayloadTypeSchema": {"type": "object"},
            }
        ),
        encoding="utf-8",
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("webhooks",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=("workflows/src/**/artefacts/webhooks/*.json",),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
        otp_enabled=False,
        context_path_mappings=(),
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
        adopt_orphans=False,
    )
    summary = poly_deploy.Summary()
    logger = poly_deploy.Logger(verbose=False)
    requests: list[tuple[str, str]] = []
    get_webhooks_calls = 0

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> object:
            nonlocal get_webhooks_calls
            requests.append((method, path))
            if method == "GET" and path == "/webhooks":
                get_webhooks_calls += 1
                if get_webhooks_calls == 1:
                    return []
                return [
                    {
                        "id": "wh-2",
                        "name": "set_profile_matches_hook",
                        "context": "example.profiles",
                        "description": "Test webhook",
                        "subpath": "profile/matches",
                        "slug": None,
                    }
                ]
            if method == "GET" and path == "/webhooks/wh-2":
                return {
                    "id": "wh-2",
                    "name": "set_profile_matches_hook",
                    "context": "example.profiles",
                    "description": "Test webhook",
                    "method": "POST",
                    "slug": "example-dev",
                    "subpath": "profile/matches",
                    "eventPayloadType": "object",
                    "eventPayloadTypeSchema": {"type": "object"},
                }
            if method == "POST" and path == "/webhooks":
                raise poly_deploy.ApiError("POST", path, 409)
            if method == "PATCH" and path == "/webhooks/wh-2":
                return {
                    "id": "wh-2",
                    "name": "post_profile_matches_hook",
                    "context": "example.profiles",
                    "description": "Test webhook",
                    "method": "POST",
                    "slug": "example-dev",
                    "subpath": "profile/matches",
                }
            raise AssertionError((method, path, kwargs))

    index = poly_deploy.sync_webhooks(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=poly_deploy.FunctionRegistry(),
        resume_state=poly_deploy.ResumeState(tmp_path, load=False),
    )

    assert requests.count(("GET", "/webhooks")) == 2
    assert ("POST", "/webhooks") in requests
    assert ("PATCH", "/webhooks/wh-2") in requests
    assert index[("example.profiles", "post_profile_matches_hook")]["id"] == "wh-2"
    assert summary.webhooks.updated == 1


def test_collect_orphans_uses_repo_wide_local_keys_for_scoped_function_sync(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    scoped_source = (
        repo_root
        / "workflows/src"
        / "utilities"
        / "services_router"
        / "server"
        / "router_sfx.py"
    )
    sibling_source = (
        repo_root
        / "workflows/src"
        / "utilities"
        / "common"
        / "client"
        / "sdk_call_with_retry_cfx.py"
    )
    scoped_source.parent.mkdir(parents=True)
    sibling_source.parent.mkdir(parents=True)
    scoped_source.write_text(
        "async def router_handler():\n    return {}\n", encoding="utf-8"
    )
    sibling_source.write_text(
        "async def sdk_call_with_retry():\n    return {}\n", encoding="utf-8"
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(repo_root / "workflows/src" / "utilities" / "services_router",),
        enabled_types=None,
        show_config=False,
    )
    logger = poly_deploy.Logger(verbose=False)

    def fake_load_function_specs(
        _config: Any,
        scoped_cli_args: Any,
        _logger: Any,
    ) -> list[Any]:
        scoped_spec = poly_deploy.FunctionSpec(
            name="router_handler",
            context="example.utils.generic",
            function_type="server",
            source_path=scoped_source,
            discovery_path=scoped_source,
        )
        sibling_spec = poly_deploy.FunctionSpec(
            name="sdk_call_with_retry",
            context="example.utils.generic",
            function_type="client",
            source_path=sibling_source,
            discovery_path=sibling_source,
        )
        if scoped_cli_args.path_filters:
            return [scoped_spec]
        return [scoped_spec, sibling_spec]

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> list[dict[str, str]]:
            assert method == "GET"
            if path == "/functions/server":
                return [
                    {
                        "id": "fn-router",
                        "name": "router_handler",
                        "context": "example.utils.generic",
                    }
                ]
            if path == "/functions/client":
                return [
                    {
                        "id": "fn-sdk",
                        "name": "sdk_call_with_retry",
                        "context": "example.utils.generic",
                    }
                ]
            raise AssertionError(path)

    monkeypatch.setattr(
        poly_deploy,
        "collect_local_context_prefixes",
        lambda *_: ("example.utils.generic",),
    )
    monkeypatch.setattr(poly_deploy, "load_function_specs", fake_load_function_specs)

    orphans, excluded = poly_deploy.collect_orphans(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        function_registry=poly_deploy.FunctionRegistry(),
        webhook_index={},
    )

    assert orphans == []
    assert excluded == {}


def test_collect_orphans_preserves_excluded_contexts_when_scanning_repo_wide(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions",),
        skip_paths=(repo_root / "workflows/src" / "normal_skip",),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=("example.ignored",),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(repo_root / "workflows/src" / "sample_workflow",),
        enabled_types=None,
        show_config=False,
    )
    captured: dict[str, Any] = {}

    def fake_load_function_specs(
        scanned_config: Any,
        scanned_cli_args: Any,
        _logger: Any,
    ) -> list[Any]:
        captured["skip_paths"] = scanned_config.skip_paths
        captured["orphan_excluded_contexts"] = scanned_config.orphan_excluded_contexts
        captured["path_filters"] = scanned_cli_args.path_filters
        return [
            poly_deploy.FunctionSpec(
                name="active",
                context="example.active",
                function_type="client",
                source_path=repo_root
                / "workflows/src"
                / "active"
                / "client"
                / "active_cfx.py",
                discovery_path=repo_root
                / "workflows/src"
                / "active"
                / "deployment"
                / "deployment.sh",
            )
        ]

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> list[dict[str, str]]:
            assert method == "GET"
            if path == "/functions/server":
                return []
            if path == "/functions/client":
                return [
                    {
                        "id": "fn-active",
                        "name": "active",
                        "context": "example.active",
                    },
                    {
                        "id": "fn-ignored",
                        "name": "ignored",
                        "context": "example.ignored",
                    },
                ]
            raise AssertionError(path)

    monkeypatch.setattr(poly_deploy, "load_function_specs", fake_load_function_specs)

    orphans, excluded = poly_deploy.collect_orphans(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=poly_deploy.Logger(verbose=False),
        function_registry=poly_deploy.FunctionRegistry(),
        webhook_index={},
    )

    assert captured["skip_paths"] == ()
    assert captured["orphan_excluded_contexts"] == ("example.ignored",)
    assert captured["path_filters"] == ()
    assert orphans == []
    assert excluded == {"functions": 1}


def test_collect_orphans_does_not_treat_client_context_as_managed_server_context(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo_root = tmp_path
    client_source = (
        repo_root
        / "workflows/src"
        / "utilities"
        / "common"
        / "client"
        / "oracle_date_cfx.py"
    )
    client_source.parent.mkdir(parents=True)
    client_source.write_text(
        "async def _oracle_date():\n    return {}\n", encoding="utf-8"
    )

    config = poly_deploy.DeployConfig(
        repo_root=repo_root,
        enabled_types=("functions",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=True,
        remote_timestamp_fields=("createdAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=True,
        generate_after_function_sync=True,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=True,
        initial_scope_state_file=repo_root / ".poly_deploy.state.json",
    )
    cli_args = poly_deploy.CliArgs(
        dry_run=True,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=None,
        show_config=False,
    )
    logger = poly_deploy.Logger(verbose=False)

    def fake_load_function_specs(
        _config: Any,
        _scoped_cli_args: Any,
        _logger: Any,
    ) -> list[Any]:
        return [
            poly_deploy.FunctionSpec(
                name="_oracle_date",
                context="example.utils.database",
                function_type="client",
                source_path=client_source,
                discovery_path=client_source,
            )
        ]

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> list[dict[str, str]]:
            assert method == "GET"
            if path == "/functions/server":
                return [
                    {
                        "id": "fn-server-1",
                        "name": "oracle_db_adapter",
                        "context": "example.utils.database",
                    }
                ]
            if path == "/functions/client":
                return [
                    {
                        "id": "fn-client-1",
                        "name": "_oracle_date",
                        "context": "example.utils.database",
                    }
                ]
            raise AssertionError(path)

    monkeypatch.setattr(
        poly_deploy,
        "collect_local_context_prefixes",
        lambda *_: ("example.utils.database",),
    )
    monkeypatch.setattr(poly_deploy, "load_function_specs", fake_load_function_specs)

    orphans, excluded = poly_deploy.collect_orphans(
        client=DummyClient(),
        config=config,
        cli_args=cli_args,
        logger=logger,
        function_registry=poly_deploy.FunctionRegistry(),
        webhook_index={},
    )

    assert len(orphans) == 1
    assert orphans[0].artifact_type == "functions"
    assert orphans[0].function_type == "server"
    assert orphans[0].name == "oracle_db_adapter"
    assert orphans[0].context == "example.utils.database"
    assert excluded == {}


def _table_sync_config(tmp_path: Path) -> Any:
    return poly_deploy.DeployConfig(
        repo_root=tmp_path,
        enabled_types=("tables",),
        skip_paths=(),
        priority_paths=(),
        function_discovery_globs=(),
        variable_globs=(),
        webhook_globs=(),
        job_globs=(),
        table_globs=(),
        schema_globs=(),
        trigger_globs=(),
        use_mtime_guard=False,
        remote_timestamp_fields=("updatedAt",),
        update_existing_secret_variables=False,
        recreate_variables_on_secrecy_change=False,
        generate_after_function_sync=False,
        generate_contexts=(),
        generate_names=(),
        generate_ids=(),
        orphan_excluded_contexts=(),
        initial_scope_prompt_enabled=False,
        initial_scope_state_file=tmp_path / ".scope",
    )


def _table_sync_args() -> Any:
    return poly_deploy.CliArgs(
        dry_run=False,
        force=False,
        verbose=False,
        delete_orphans=False,
        path_filters=(),
        enabled_types=("tables",),
        show_config=False,
    )


def test_sync_tables_skips_poly_managed_columns(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    source = {
        "name": "ledger",
        "context": "example.stays",
        "columns": [
            {"name": "id", "type": "uuid", "primary": True},
            {"name": "createdAt", "type": "timestamptz"},
            {"name": "updatedAt", "type": "timestamptz"},
            {"name": "record_key", "type": "string", "required": True},
        ],
    }
    source_path = tmp_path / "ledger.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    monkeypatch.setattr(
        poly_deploy,
        "load_artifact_files",
        lambda *_: [poly_deploy.ArtifactFile(source_path, source)],
    )

    class DummyClient:
        def request(self, method: str, path: str, **_: object) -> Any:
            assert method == "GET"
            if path == "/tables":
                return {
                    "results": [
                        {"id": "table-1", "name": "ledger", "context": "example.stays"}
                    ],
                    "pagination": {"pages": 1},
                }
            if path == "/tables/table-1":
                return {
                    "id": "table-1",
                    "name": "ledger",
                    "context": "example.stays",
                    "columns": [
                        {"name": "id", "type": "uuid", "default": "generated"},
                        {"name": "createdAt", "type": "timestamp", "default": "now"},
                        {"name": "updatedAt", "type": "timestamp", "default": "now"},
                        {"name": "record_key", "type": "string", "required": True},
                    ],
                }
            raise AssertionError(path)

    summary = poly_deploy.Summary()
    poly_deploy.sync_tables(
        client=DummyClient(),
        config=_table_sync_config(tmp_path),
        cli_args=_table_sync_args(),
        logger=poly_deploy.Logger(False),
        summary=summary,
        resume_state=poly_deploy.ResumeState(tmp_path, False),
    )

    assert summary.tables.skipped == 1
    assert summary.tables.updated == 0
    assert source["columns"][0]["name"] == "id"


def test_sync_tables_omits_poly_managed_columns_on_create(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    source = {
        "name": "ledger",
        "context": "example.stays",
        "columns": [
            {"name": "id", "type": "uuid", "primary": True},
            {"name": "createdAt", "type": "timestamptz"},
            {"name": "updatedAt", "type": "timestamptz"},
            {"name": "record_key", "type": "string", "required": True},
        ],
    }
    source_path = tmp_path / "ledger.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    monkeypatch.setattr(
        poly_deploy,
        "load_artifact_files",
        lambda *_: [poly_deploy.ArtifactFile(source_path, source)],
    )

    class DummyClient:
        def request(self, method: str, path: str, **kwargs: object) -> Any:
            if method == "GET" and path == "/tables":
                return {"results": [], "pagination": {"pages": 0}}
            assert method == "POST" and path == "/tables"
            payload = kwargs["payload"]
            assert isinstance(payload, dict)
            assert [column["name"] for column in payload["columns"]] == ["record_key"]
            return {"id": "table-1"}

    summary = poly_deploy.Summary()
    poly_deploy.sync_tables(
        client=DummyClient(),
        config=_table_sync_config(tmp_path),
        cli_args=_table_sync_args(),
        logger=poly_deploy.Logger(False),
        summary=summary,
        resume_state=poly_deploy.ResumeState(tmp_path, False),
    )

    assert summary.tables.created == 1


def test_current_python_source_seeds_transitive_sdk_dependencies(tmp_path):
    path = tmp_path / "worker_sfx.py"
    code = (
        "from polyapi import poly\n"
        "async def worker() -> dict:\n"
        "    await poly.new_api.v2.fetch_async()\n"
        "    await poly.other.remote.run_async()\n"
        "    return await poly.shared.helpers.start()\n"
    )
    path.write_text(code)
    spec = poly_deploy.FunctionSpec("worker", "example.demo", "server", path, path)
    stale = {
        "example.demo.worker": {"calls": ["old_api.v1.fetch"], "contexts": ["old_api"]},
        "shared.helpers.start": {
            "calls": ["shared.helpers.finish"],
            "contexts": ["shared"],
        },
        "shared.helpers.finish": {"calls": [], "contexts": ["vendor"]},
    }
    # This uses the real pinned Python SDK parser, including the generated-import fallback.
    payload = poly_deploy.build_function_payload(spec, stale)
    assert payload["language"] == "python"
    assert set(payload["generateContexts"]) == {"new_api", "other", "shared", "vendor"}
    fetched = []
    writes = []

    class Client:
        def request(self, method, route, **options):
            if method == "POST":
                assert route == "/functions/server"
                writes.append(options["payload"])
                return {"id": "worker"}
            assert method == "GET"
            fetched.append(route)
            if route == "/functions/server/worker":
                return {
                    "id": "worker",
                    "code": "from polyapi import poly\nasync def worker() -> dict:\n    return await poly.old_api.v1.fetch_async()\n",
                }
            if route == "/specs":
                return [
                    {
                        "id": "start",
                        "context": "shared.helpers",
                        "name": "start",
                        "language": "python",
                        "type": "customFunction",
                        "code": "from polyapi import poly\nasync def start():\n    return await poly.shared.helpers.finish()\n",
                    },
                    {
                        "id": "finish",
                        "context": "shared.helpers",
                        "name": "finish",
                        "language": "python",
                        "type": "customFunction",
                        "code": "from polyapi import poly\nasync def finish():\n    await poly.shared.helpers.start()\n    return await poly.vendor.v1.fetch_async()\n",
                    },
                    {
                        "id": "unrelated-ts",
                        "context": "elsewhere",
                        "name": "helper",
                        "language": "typescript",
                        "type": "customFunction",
                    },
                    {
                        "id": "unrelated-broken",
                        "context": "elsewhere",
                        "name": "broken",
                        "language": "python",
                        "type": "customFunction",
                    },
                ]
            pytest.fail(f"Unexpected remote implementation fetch: {route}")

    local = poly_deploy.build_local_func_context([spec])
    graph = poly_deploy.build_remote_func_context(Client(), local_func_context=local)
    assert poly_deploy.resolve_server_generate_contexts(spec, code, graph, ()) == [
        "new_api",
        "other",
        "shared",
        "vendor",
    ]
    assert fetched == ["/specs"]
    # Explicit contexts remain authoritative.
    explicit = poly_deploy.replace(spec, generate_contexts=("selected",))
    assert poly_deploy.resolve_server_generate_contexts(explicit, code, graph, ()) == [
        "selected"
    ]

    args = poly_deploy.CliArgs(False, False, False, False, (), ("functions",), False)
    config = poly_deploy.build_config(tmp_path, args)
    registry = poly_deploy.FunctionRegistry()
    registry.upsert(
        "server", {"id": "worker", "name": "worker", "context": "example.demo"}
    )
    stats = poly_deploy.ArtifactStats()
    assert poly_deploy.sync_function_group(
        specs=[spec],
        client=Client(),
        registry=registry,
        detail_cache={},
        config=config,
        cli_args=args,
        logger=poly_deploy.Logger(False),
        stats=stats,
        resume_state=poly_deploy.ResumeState(tmp_path, False),
    )
    assert stats.updated == 1 and stats.failed == 0
    assert len(writes) == 1
    assert set(writes[0]["generateContexts"]) == {
        "new_api",
        "other",
        "shared",
        "vendor",
    }


@pytest.mark.parametrize("language", ["python", "typescript", None])
def test_remote_dependency_discovery_respects_language(language):
    calls = []

    class Client:
        def request(self, method, path, **options):
            assert (method, path, options) == (
                "GET",
                "/specs",
                {"params": {"noTypes": "true"}},
            )
            calls.append(path)
            return [
                {
                    "id": "helper",
                    "type": "customFunction",
                    "name": "helper",
                    "context": "shared",
                    **({"language": language} if language is not None else {}),
                    "code": (
                        "export function helper() { return true; }"
                        if language == "typescript"
                        else "def helper():\n    return True\n"
                    ),
                }
            ]

    local = {"example.worker": {"calls": ["shared.helper"], "contexts": ["shared"]}}
    graph = poly_deploy.build_remote_func_context(Client(), local_func_context=local)
    if language == "typescript":
        assert graph == local
    else:
        assert graph["shared.helper"]["kind"] == "client"
        assert graph["shared.helper"]["calls"] == []
    assert calls == ["/specs"]


@pytest.mark.parametrize(
    "next_target",
    [
        ("https://example.invalid", "prod"),
        ("https://another.invalid", "dev"),
        None,
    ],
)
def test_resume_never_reuses_another_target(tmp_path, next_target):
    original = poly_deploy.ResumeState(
        tmp_path, False, target=("https://example.invalid/", "dev")
    )
    original.mark_done("fn:server:example:worker", "payload")
    assert not poly_deploy.ResumeState(tmp_path, True, target=next_target).is_done(
        "fn:server:example:worker", "payload"
    )
    same = poly_deploy.ResumeState(
        tmp_path, True, target=("https://example.invalid", "dev")
    )
    assert same.is_done("fn:server:example:worker", "payload")
    saved = json.loads((tmp_path / poly_deploy.ResumeState.FILENAME).read_text())
    assert saved["target"] == {
        "baseUrl": "https://example.invalid",
        "environmentId": "dev",
    }


@pytest.mark.parametrize("kind", ["functions", "ai-functions"])
@pytest.mark.parametrize(
    "mode", ["normal", "resume", "dry-run", "skip", "disabled", "retry"]
)
def test_main_regenerates_unchanged_functions_and_recovers_generation_failure(
    tmp_path, monkeypatch, kind, mode
):
    from unittest.mock import Mock

    args = poly_deploy.CliArgs(
        mode == "dry-run",
        False,
        False,
        False,
        (),
        (kind,),
        False,
        resume=mode == "resume",
        skip_generate=mode == "skip",
    )
    config = poly_deploy.build_config(tmp_path, args)
    config = poly_deploy.replace(
        config, generate_after_function_sync=mode != "disabled"
    )
    monkeypatch.setattr(poly_deploy, "parse_args", lambda: args)
    monkeypatch.setattr(poly_deploy, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(poly_deploy, "load_env", lambda _: Mock(exists=True))
    monkeypatch.setattr(poly_deploy, "get_target_env_id", lambda _: "dev")
    monkeypatch.setattr(poly_deploy, "build_config", lambda *_: config)
    monkeypatch.setattr(
        poly_deploy,
        "build_deployment_preflight",
        lambda *_: Mock(production_sensitive=False),
    )
    for name in (
        "print_startup_banner",
        "print_deployment_visibility",
        "maybe_confirm_branch_env_file_match",
        "enforce_production_safeguards",
        "maybe_prompt_for_missing_env_tokens",
        "print_summary",
    ):
        monkeypatch.setattr(poly_deploy, name, Mock())
    monkeypatch.setattr(
        poly_deploy, "maybe_prompt_for_initial_scope", lambda args, *_: args
    )
    monkeypatch.setattr(
        poly_deploy,
        "get_api_key_and_url",
        lambda: ("test-token", "https://example.invalid"),
    )
    auth = Mock()
    monkeypatch.setattr(poly_deploy, "enforce_target_environment", auth)
    original_state = poly_deploy.ResumeState

    def verified_state(*positional, **keywords):
        assert auth.called
        assert keywords["target"] == ("https://example.invalid", "dev")
        return original_state(*positional, **keywords)

    monkeypatch.setattr(poly_deploy, "ResumeState", verified_state)
    registry = poly_deploy.FunctionRegistry()
    monkeypatch.setattr(poly_deploy, "fetch_function_registry", lambda _: registry)
    monkeypatch.setattr(poly_deploy, "sync_functions", lambda **_: (registry, False))
    monkeypatch.setattr(poly_deploy, "sync_ai_functions", lambda *_: False)
    monkeypatch.setattr(poly_deploy, "build_remote_webhook_index", lambda _: {})
    monkeypatch.setattr(poly_deploy, "process_orphans", lambda **_: False)
    generate = Mock()
    monkeypatch.setattr(poly_deploy, "run_poly_generate", generate)
    if mode == "retry":
        generate.side_effect = [poly_deploy.DeployError("SDK generation failed"), None]
        with pytest.raises(poly_deploy.DeployError, match="SDK generation failed"):
            poly_deploy.main()
        args = poly_deploy.replace(args, resume=True)
    assert poly_deploy.main() == 0
    assert generate.call_count == (
        0 if mode in {"dry-run", "skip", "disabled"} else 2 if mode == "retry" else 1
    )


@pytest.mark.parametrize("function_type", ["server", "client"])
def test_environment_execution_api_key_reaches_only_server_deployments(
    tmp_path: Path, monkeypatch: MonkeyPatch, function_type: str
) -> None:
    monkeypatch.setenv("POLY_EXEC_API_KEY", "execution-key-for-test")
    path = tmp_path / "worker.py"
    path.write_text("def worker() -> dict:\n    return {}\n")
    spec = poly_deploy.FunctionSpec(
        name="worker",
        context="example.demo",
        function_type=function_type,
        source_path=path,
        discovery_path=path,
    )
    payloads = []

    class Client:
        def request(self, method: str, route: str, **options: Any) -> Any:
            if method == "GET" and route == "/specs":
                return []
            assert (method, route) == ("POST", f"/functions/{function_type}")
            payloads.append(options["payload"])
            return {"id": "worker"}

    args = poly_deploy.CliArgs(False, False, False, False, (), ("functions",), False)
    config = poly_deploy.build_config(tmp_path, args)
    stats = poly_deploy.ArtifactStats()
    assert poly_deploy.sync_function_group(
        specs=[spec],
        client=Client(),
        registry=poly_deploy.FunctionRegistry(),
        detail_cache={},
        config=config,
        cli_args=args,
        logger=poly_deploy.Logger(False),
        stats=stats,
        resume_state=poly_deploy.ResumeState(tmp_path, False),
    )
    assert stats.created == 1 and stats.failed == 0
    assert len(payloads) == 1
    if function_type == "server":
        assert payloads[0]["executionApiKey"] == "execution-key-for-test"
    else:
        assert "executionApiKey" not in payloads[0]


@pytest.mark.parametrize("secrecy", ["OBSCURED", "SECRET", "PARTIAL"])
@pytest.mark.parametrize("protected_side", ["local", "remote"])
@pytest.mark.parametrize("allowed", [False, True])
def test_protected_variable_update_values_require_explicit_opt_in(
    secrecy, protected_side, allowed
):
    local = {"context": "example", "name": "config", "value": "synthetic-new"}
    remote = {"value": "synthetic-old"}
    (local if protected_side == "local" else remote)["secrecy"] = secrecy
    result = poly_deploy.build_variable_update_payload(
        local, remote, update_secret_values=allowed
    )
    assert ("value" in result) is allowed
    assert result["name"] == "config"
    assert local["value"] == "synthetic-new"
    assert remote["value"] == "synthetic-old"


@pytest.mark.parametrize("allow_value_update", [False, True])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("local_secrecy", [None, "PARTIAL"])
def test_sync_variables_preserves_partial_values_unless_opted_in(
    tmp_path: Path, allow_value_update: bool, force: bool, local_secrecy: str | None
) -> None:
    path = tmp_path / "workflows/src/demo/artefacts/vari/settings.json"
    path.parent.mkdir(parents=True)
    document = {
        "context": "example.demo",
        "name": "settings",
        "description": "Updated metadata",
        "value": {"clientSecret": "synthetic-new", "public": "new"},
    }
    if local_secrecy is not None:
        document["secrecy"] = local_secrecy
    path.write_text(json.dumps(document))
    args = poly_deploy.CliArgs(False, force, False, False, (), ("variables",), False)
    config = replace(
        poly_deploy.build_config(tmp_path, args),
        use_mtime_guard=False,
        update_existing_secret_variables=allow_value_update,
    )
    writes: list[dict[str, Any]] = []

    class Client:
        def request(self, method: str, route: str, **options: Any) -> Any:
            if method == "GET" and route == "/variables":
                return [{"id": "var-1", "context": "example.demo", "name": "settings"}]
            if method == "GET" and route == "/variables/var-1":
                return {
                    "id": "var-1",
                    "context": "example.demo",
                    "name": "settings",
                    "description": "Original metadata",
                    "secrecy": "PARTIAL",
                    "secret": False,
                    "value": {"clientSecret": "********", "public": "old"},
                }
            assert (method, route) == ("PATCH", "/variables/var-1")
            writes.append(options["payload"])
            return {"id": "var-1"}

    summary = poly_deploy.Summary()
    poly_deploy.sync_variables(
        client=Client(),
        config=config,
        cli_args=args,
        logger=poly_deploy.Logger(False),
        summary=summary,
        otp_manager=poly_deploy.OtpManager(False),
        resume_state=poly_deploy.ResumeState(tmp_path, False),
    )
    assert summary.variables.updated == 1
    assert summary.variables.failed == 0
    assert len(writes) == 1
    assert writes[0]["description"] == "Updated metadata"
    assert ("value" in writes[0]) is allow_value_update
    if allow_value_update:
        assert writes[0]["value"] == document["value"]
    assert json.loads(path.read_text()) == document


def test_async_client_contexts_use_current_source_and_respect_exclusions(tmp_path):
    path = tmp_path / "worker_sfx.py"
    path.write_text(
        "from polyapi import poly\n"
        "async def worker() -> bool:\n"
        "    return await poly.shared.helpers.fetch_async()\n"
    )
    spec = poly_deploy.FunctionSpec("worker", "example.demo", "server", path, path)
    contexts = {
        "example.demo.worker": {"calls": ["stale.helper"], "contexts": ["stale"]},
        "stale.helper": {"calls": [], "contexts": ["stale"]},
        "shared.helpers.fetch": {"calls": [], "contexts": ["wrong"]},
        "shared.helpers.fetch_async": {
            "calls": [],
            "contexts": ["vendor.allowed", "vendor.excluded"],
        },
    }
    payload = poly_deploy.build_function_payload(spec, contexts, ("vendor.excluded",))
    assert set(payload["generateContexts"]) == {"shared.helpers", "vendor.allowed"}
    path.write_text("async def worker() -> bool:\n    return True\n")
    assert "generateContexts" not in poly_deploy.build_function_payload(
        spec, contexts, ("vendor.excluded",)
    )


def test_remote_async_client_names_keep_exact_transitive_dependencies(tmp_path):
    fetched = []

    class Client:
        def request(self, method, route, **options):
            assert (method, route, options) == (
                "GET",
                "/specs",
                {"params": {"noTypes": "true"}},
            )
            fetched.append(route)
            codes = {
                "fetch": "unreferenced invalid Python",
                "fetch_async": "from polyapi import poly\nasync def fetch_async():\n    return await poly.shared.helpers.finish_async()\n",
                "finish_async": "from polyapi import vari\nasync def finish_async():\n    return await vari.OOB.provider.settings.get_async()\n",
            }
            return [
                {
                    "id": name,
                    "name": name,
                    "context": "shared.helpers",
                    "type": "customFunction",
                    "language": "python",
                    "code": code,
                }
                for name, code in codes.items()
            ]

    path = tmp_path / "worker_sfx.py"
    path.write_text(
        "from polyapi import poly\n"
        "async def worker() -> bool:\n"
        "    return await poly.shared.helpers.fetch_async()\n"
    )
    spec = poly_deploy.FunctionSpec("worker", "example.demo", "server", path, path)
    local = poly_deploy.build_local_func_context([spec])
    contexts = poly_deploy.build_remote_func_context(Client(), local_func_context=local)
    payload = poly_deploy.build_function_payload(spec, contexts)
    assert set(payload["generateContexts"]) == {"shared", "OOB.provider"}
    assert fetched == ["/specs"]


@pytest.mark.parametrize("reader", ["helper", "tuple", "lexical", "module-alias"])
@pytest.mark.parametrize("variable_name", ["setting", "get"])
def test_vari_lexical_bindings_keep_scoped_generation(reader, variable_name):
    bodies = {
        "helper": f"def read(v):\n    return v.get()\nread(vari.catalog.{variable_name})\n",
        "tuple": f"v, = (vari.catalog.{variable_name},)\nv.get()\n",
        "lexical": (
            f"def read():\n    v = vari.catalog.{variable_name}\n    return v.get()\n"
            "def other():\n    v = vari.catalog.other.setting\n    return v.inject()\n"
        ),
        "module-alias": f"v = vari\nv.catalog.{variable_name}.get()\n",
    }
    source = "from polyapi import vari\n" + bodies[reader]
    found = poly_deploy.scan_function_dependencies("", source, ("private",))
    assert found["root_variables"] == []
    assert set(found["contexts"]) == (
        {"catalog", "catalog.other"} if reader == "lexical" else {"catalog"}
    )


@pytest.mark.parametrize("reader", ["helper", "tuple", "lexical"])
def test_vari_dynamic_context_guards_follow_lexical_bindings(reader):
    sources = {
        "helper": "def read(v):\n    return getattr(v, name).get()\nread(vari.example)\n",
        "tuple": "v, = (vari.example,)\ngetattr(v, name).get()\n",
        "lexical": (
            "def read():\n    v = vari.example\n    return getattr(v, name).get()\n"
            "def other():\n    v = vari.safe\n    return getattr(v, name).get()\n"
        ),
    }
    with pytest.raises(poly_deploy.DeployError, match="broad context 'example'"):
        poly_deploy.infer_generate_contexts(
            "from polyapi import vari\n" + sources[reader], ("example.excluded",)
        )
