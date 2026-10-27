import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import polyapi
import pytest

COMMON = Path(__file__).resolve().parents[1]


def load_source(relative):
    spec = importlib.util.spec_from_file_location(
        "common_under_test", COMMON / relative
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sdk(monkeypatch):
    level = AsyncMock(return_value="info")
    channel = AsyncMock(return_value="synthetic-channel")
    workspace = AsyncMock(return_value="community")
    send, alert, log = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setitem(
        polyapi.__dict__,
        "vari",
        SimpleNamespace(
            example=SimpleNamespace(
                configurations=SimpleNamespace(
                    log_settings=SimpleNamespace(
                        log_level=SimpleNamespace(get_async=level)
                    ),
                    slack=SimpleNamespace(
                        channel_id=SimpleNamespace(get_async=channel),
                        workspace_type=SimpleNamespace(get_async=workspace),
                    ),
                )
            )
        ),
    )
    monkeypatch.setitem(
        polyapi.__dict__,
        "poly",
        SimpleNamespace(
            OOB=SimpleNamespace(
                polyapi=SimpleNamespace(
                    internal=SimpleNamespace(
                        alerting=SimpleNamespace(sendAlertToPoly_async=send)
                    )
                )
            ),
            example=SimpleNamespace(
                utilities=SimpleNamespace(
                    slack=SimpleNamespace(send_alert=alert), log=log
                )
            ),
        ),
    )
    monkeypatch.setitem(
        polyapi.__dict__,
        "polyCustom",
        SimpleNamespace(executionId="synthetic-execution"),
    )
    return SimpleNamespace(
        level=level,
        channel=channel,
        workspace=workspace,
        send=send,
        alert=alert,
        log=log,
    )


@pytest.mark.parametrize(
    "configured_level, severity, expected",
    [
        ("info", "debug", False),
        ("warn", "info", False),
        ("warn", "warn", True),
        ("silent", "fatal", False),
    ],
)
async def test_configured_level_through_native_generated_vari(
    sdk, tmp_path, monkeypatch, capsys, configured_level, severity, expected
):
    from polyapi import execute, variables

    artifact = json.loads((COMMON / "artefacts/vari/log_level_vari.json").read_text())
    package_name = f"log_sdk_probe_{configured_level}_{severity}"
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text("")
    # Generate with the installed SDK; do not replace namespace imports with mocks.
    monkeypatch.setattr(variables, "__file__", str(package / "variables.py"))
    variables.create_variable(
        {
            "id": "synthetic-log-level",
            "type": "serverVariable",
            "context": artifact["context"],
            "name": artifact["name"],
            "variable": {
                "secrecy": artifact["secrecy"],
                "valueType": {"kind": "plain", "value": "string"},
            },
        }
    )
    # Full generation supplies this sibling; no listener or real SDK identity is used.
    client_id = ModuleType("polyapi.poly.client_id")
    client_id.__dict__["client_id"] = "synthetic-client"
    monkeypatch.setitem(sys.modules, "polyapi.poly.client_id", client_id)
    getter = AsyncMock(return_value=SimpleNamespace(text=configured_level))
    monkeypatch.setattr(execute, "variable_get_async", getter)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        generated = importlib.import_module(f"{package_name}.vari")
        monkeypatch.setitem(polyapi.__dict__, "vari", generated)
        log = load_source("client/log_cfx.py").log
        assert await log(severity, "function", "threshold_probe") is expected
        getter.assert_awaited_once_with("synthetic-log-level")
        output = capsys.readouterr()
        assert ("threshold_probe" in output.out + output.err) is expected
        sdk.alert.assert_not_called()
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name)


async def test_log_redacts_nested_fields_and_alerts_on_errors(sdk, capsys):
    log = load_source("client/log_cfx.py").log
    await log("debug", "function", "skipped")
    assert capsys.readouterr().out == ""
    sdk.alert.assert_not_called()
    await log(
        "error",
        "function",
        "request_failed",
        {
            "status": 502,
            "nested": [{"API_KEY": "synthetic-secret", "detail": "useful"}],
            "executionId": "synthetic-private",
        },
    )
    output = capsys.readouterr().err
    assert "[error][function] request_failed" in output
    assert "synthetic-secret" not in output and "synthetic-private" not in output
    payload = json.loads(sdk.alert.await_args.args[0]["message"])
    assert payload["context"]["status"] == 502
    assert payload["context"]["nested"] == [
        {"API_KEY": "[REDACTED]", "detail": "useful"}
    ]


async def test_nonsecret_level_cache_refresh_fallback_and_invalid_values(
    sdk, monkeypatch, capsys
):
    module = load_source("client/log_cfx.py")
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    sdk.level.return_value = " WARN "
    await module.log("info", "function", "hidden")
    await module.log("warn", "function", "visible")
    assert sdk.level.await_count == 1
    assert "visible" in capsys.readouterr().err
    clock[0] = 61
    sdk.level.return_value = "invalid"
    await module.log("info", "function", "still hidden")
    assert capsys.readouterr().out == ""
    clock[0] = 122
    sdk.level.side_effect = RuntimeError("synthetic-secret")
    await module.log("debug", "function", "fallback")
    assert "fallback" in capsys.readouterr().out


async def test_circular_context_and_opaque_values_do_not_break_logging(sdk, capsys):
    context = {
        "useful": "data",
        "object": object(),
        "injected": {"type": "PolyVariable", "id": "private"},
    }
    context["self"] = context
    await load_source("client/log_cfx.py").log("fatal", "function", "failed", context)
    output = capsys.readouterr().err
    assert "[Circular]" in output and "[object]" in output
    assert "private" not in output
    sdk.alert.assert_awaited_once()


async def test_silent_disables_logs_and_alerts(sdk, capsys):
    sdk.level.return_value = "silent"
    await load_source("client/log_cfx.py").log("fatal", "function", "hidden")
    assert capsys.readouterr().err == ""
    sdk.alert.assert_not_called()


@pytest.mark.parametrize("workspace", ["community", "internal"])
async def test_alert_uses_exact_sdk_and_logs_matching_correlation(sdk, workspace):
    sdk.workspace.return_value = workspace
    await load_source("client/send_alert_cfx.py").send_alert({"message": "Safe event"})
    sdk.channel.assert_awaited_once_with()
    payload = sdk.send.await_args.args[0]
    assert payload["slack"]["channelId"] == "synthetic-channel"
    assert payload["slack"]["type"] == workspace
    sdk.workspace.assert_awaited_once_with()
    assert payload["pagerDuty"] == {"levels": []}
    assert payload["executionId"] == "synthetic-execution"
    assert payload["message"] == "Safe event"
    calls = sdk.log.await_args_list
    assert [call.args[:3] for call in calls] == [
        ("info", "send_alert", "start"),
        ("info", "send_alert", "success"),
    ]
    assert calls[0].args[3]["correlationId"] == calls[1].args[3]["correlationId"]
    assert "messagePreview" not in calls[0].args[3]


async def test_unconfigured_channel_prevents_delivery(sdk):
    sdk.channel.return_value = ""
    with pytest.raises(ValueError, match="not configured"):
        await load_source("client/send_alert_cfx.py").send_alert(
            {"message": "Safe event"}
        )
    sdk.send.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
async def test_global_handler_redacts_and_alerts_independently_of_log_level(
    sdk, capsys, fails
):
    sdk.level.return_value = "silent"
    if fails:
        sdk.alert.side_effect = RuntimeError("synthetic-secret")
    error = {"message": "useful detail", "password": "synthetic-secret"}
    assert await load_source("server/error_alerts_sfx.py").error_alerts(error) == {
        "notified": not fails
    }
    sdk.alert.assert_awaited_once()
    assert json.loads(sdk.alert.await_args.args[0]["message"]) == {
        "message": "PolyAPI function execution failed",
    }
    sdk.log.assert_not_called()
    assert "synthetic-secret" not in capsys.readouterr().out


async def test_global_error_text_never_reaches_platform_alert(sdk):
    sdk.alert.side_effect = load_source("client/send_alert_cfx.py").send_alert
    marker = "SYNTHETIC_SECRET_MARKER"
    error = {
        "message": f"Authorization: Bearer {marker}",
        "stack": f"request failed with password={marker}",
        "url": f"https://unit.invalid/?api_key={marker}",
        "details": {"response": marker},
        "functionName": "motd_server",
        "functionContext": "example.sample_workflow",
        "statusCode": 502,
    }
    result = await load_source("server/error_alerts_sfx.py").error_alerts(error)
    assert result == {"notified": True}
    delivered = sdk.send.await_args.args[0]
    assert marker not in json.dumps(delivered)
    assert json.loads(delivered["message"]) == {
        "message": "PolyAPI function execution failed",
        "functionName": "motd_server",
        "functionContext": "example.sample_workflow",
        "statusCode": 502,
    }


@pytest.mark.parametrize(
    "invalid", ["Bearer synthetic-token", "https://unit.invalid", "x" * 161, {}, None]
)
async def test_global_error_rejects_unbounded_diagnostic_fields(sdk, invalid):
    await load_source("server/error_alerts_sfx.py").error_alerts(
        {
            "functionName": invalid,
            "functionContext": invalid,
            "statusCode": True,
        }
    )
    assert json.loads(sdk.alert.await_args.args[0]["message"]) == {
        "message": "PolyAPI function execution failed",
    }


async def test_utility_boolean_results_and_read_only_default_context(sdk, capsys):
    module = load_source("client/log_cfx.py")
    assert await module.log("debug", "function", "filtered") is False
    assert await module.log("info", "function", "logged") is True
    assert await module.log("info", "function", "with_context", {"count": 1}) is True
    assert await module.log("info", "function", "default_again") is True
    assert module.log.__defaults__ == ({},)
    assert (
        await load_source("client/send_alert_cfx.py").send_alert({"message": "safe"})
        is True
    )


@pytest.mark.parametrize("workspace", ["", "unexpected", None, {}, []])
async def test_invalid_workspace_prevents_delivery(sdk, workspace):
    sdk.workspace.return_value = workspace
    with pytest.raises(ValueError, match="workspace type"):
        await load_source("client/send_alert_cfx.py").send_alert(
            {"message": "Safe event"}
        )
    sdk.send.assert_not_called()
    sdk.log.assert_not_called()
