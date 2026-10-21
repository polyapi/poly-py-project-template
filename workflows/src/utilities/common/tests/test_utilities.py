import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
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
    send, alert, log = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setitem(
        polyapi.__dict__,
        "vari",
        SimpleNamespace(
            example=SimpleNamespace(
                configurations=SimpleNamespace(
                    logging=SimpleNamespace(log_level=SimpleNamespace(get_async=level)),
                    slack=SimpleNamespace(
                        channel_id=SimpleNamespace(get_async=channel)
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
        level=level, channel=channel, send=send, alert=alert, log=log
    )


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


async def test_alert_uses_exact_sdk_and_logs_matching_correlation(sdk):
    await load_source("client/send_alert_cfx.py").send_alert({"message": "Safe event"})
    sdk.channel.assert_awaited_once_with()
    payload = sdk.send.await_args.args[0]
    assert payload["slack"]["channelId"] == "synthetic-channel"
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
        "message": "useful detail",
        "password": "[REDACTED]",
    }
    sdk.log.assert_not_called()
    assert "synthetic-secret" not in capsys.readouterr().out
