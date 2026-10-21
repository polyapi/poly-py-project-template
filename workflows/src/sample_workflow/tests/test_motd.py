import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import polyapi
import pytest

WORKFLOW = Path(__file__).resolve().parents[1]


def load_source(relative: str):
    spec = importlib.util.spec_from_file_location(
        "sample_under_test", WORKFLOW / relative
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpaqueReference:
    def __str__(self):
        raise AssertionError("Secret reference must not be inspected or serialized")

    __repr__ = __str__


@pytest.fixture
def sdk(monkeypatch):
    secret = OpaqueReference()
    inject = Mock(return_value=secret)
    advice_config = AsyncMock(
        return_value=json.dumps({"hostUrl": "https://api.adviceslip.com"})
    )
    joke = AsyncMock(
        return_value={
            "status": 200,
            "data": {"setup": "Setup", "punchline": "Punchline"},
        }
    )
    advice = AsyncMock(
        return_value={"status": 200, "data": {"slip": {"advice": "Advice."}}}
    )
    client = AsyncMock(return_value="Advice.")
    handler = AsyncMock(return_value={"message": "Handled failure"})
    functions = SimpleNamespace(
        motd_client=client,
        motd_error_handler_async=handler,
        getRandomJoke_async=joke,
        getRandomAdvice_async=advice,
    )
    monkeypatch.setitem(
        polyapi.__dict__,
        "poly",
        SimpleNamespace(example=SimpleNamespace(sample_workflow=functions)),
    )
    monkeypatch.setitem(
        polyapi.__dict__,
        "vari",
        SimpleNamespace(
            example=SimpleNamespace(
                sample_workflow=SimpleNamespace(
                    joke_api_config=SimpleNamespace(inject=inject),
                    advice_api_config=SimpleNamespace(get_async=advice_config),
                )
            )
        ),
    )
    monkeypatch.setattr("polyapi.config.get_direct_execute_config", lambda: False)
    scope = polyapi.polyCustom.push_scope()
    yield SimpleNamespace(
        secret=secret,
        inject=inject,
        config=advice_config,
        joke=joke,
        advice=advice,
        client=client,
        handler=handler,
    )
    polyapi.polyCustom.pop_scope(scope)


async def test_secret_reference_crosses_the_generated_api_boundary_unchanged(sdk):
    result = await load_source("client/motd_client_cfx.py").motd_client("funny")
    assert result == "Setup ... Punchline"
    sdk.inject.assert_called_once_with("hostUrl")
    assert sdk.joke.await_args.args[0] is sdk.secret
    sdk.config.assert_not_called()
    sdk.advice.assert_not_called()


async def test_obscured_config_is_read_and_decoded_without_caching(sdk):
    client = load_source("client/motd_client_cfx.py").motd_client
    for _ in range(2):
        assert await client("wise") == "Advice."
    assert sdk.config.await_count == 2
    sdk.advice.assert_awaited_with("https://api.adviceslip.com")
    sdk.inject.assert_not_called()


async def test_injection_refuses_direct_api_execution(sdk, monkeypatch):
    monkeypatch.setattr("polyapi.config.get_direct_execute_config", lambda: True)
    with pytest.raises(ValueError, match="normal API execution"):
        await load_source("client/motd_client_cfx.py").motd_client("funny")
    sdk.inject.assert_not_called()
    sdk.joke.assert_not_called()


async def test_invalid_mood_has_no_side_effects(sdk):
    with pytest.raises(ValueError, match="Unsupported"):
        await load_source("client/motd_client_cfx.py").motd_client("other")
    sdk.config.assert_not_called()
    sdk.inject.assert_not_called()


@pytest.mark.parametrize(
    "value", ["[]", "{}", '{"hostUrl": ""}', '{"hostUrl": 42}', "invalid"]
)
async def test_invalid_config_never_calls_the_api(sdk, value):
    sdk.config.return_value = value
    with pytest.raises(ValueError, match="Invalid advice API configuration"):
        await load_source("client/motd_client_cfx.py").motd_client("wise")
    sdk.advice.assert_not_called()


@pytest.mark.parametrize("mood,api_name", [("funny", "joke"), ("wise", "advice")])
@pytest.mark.parametrize(
    "response",
    [
        {"status": 503, "data": {"private": "synthetic-secret"}},
        {"status": 200, "data": {}},
        {"status": 200, "data": None},
    ],
)
async def test_bad_api_response_has_bounded_error(sdk, mood, api_name, response):
    getattr(sdk, api_name).return_value = response
    with pytest.raises(RuntimeError, match="failed with status") as caught:
        await load_source("client/motd_client_cfx.py").motd_client(mood)
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("mood,api_name", [("funny", "joke"), ("wise", "advice")])
async def test_transport_errors_do_not_disclose_upstream_values(sdk, mood, api_name):
    getattr(sdk, api_name).side_effect = RuntimeError("synthetic-secret")
    with pytest.raises(RuntimeError, match="request failed") as caught:
        await load_source("client/motd_client_cfx.py").motd_client(mood)
    assert "synthetic-secret" not in str(caught.value)


async def test_server_calls_generated_client_and_sets_http_status(sdk):
    result = await load_source("server/motd_server_sfx.py").motd_server(
        {"name": " Reader ", "mood": "wise"}, {}, {}
    )
    assert result == {"statusCode": 200, "message": "Hi Reader! Advice."}
    sdk.client.assert_awaited_once_with("wise")
    sdk.handler.assert_not_called()
    assert polyapi.polyCustom.responseStatusCode == 200


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        {},
        {"name": " ", "mood": "wise"},
        {"name": "Reader", "mood": "invalid"},
    ],
)
async def test_invalid_request_never_calls_client(sdk, event):
    result = await load_source("server/motd_server_sfx.py").motd_server(event, {}, {})
    assert result["statusCode"] == 400
    assert polyapi.polyCustom.responseStatusCode == 400
    sdk.client.assert_not_called()
    assert (
        load_source("server/validate_motd_payload_sfx.py").validate_motd_payload(
            event, {}, {}
        )
        is False
    )


@pytest.mark.parametrize(
    "detail,expected",
    [
        (
            "getRandomJoke failed with status 503",
            "getRandomJoke failed with status 503",
        ),
        ("synthetic-secret", "Message of the day is unavailable."),
    ],
)
async def test_failure_calls_shared_server_handler_and_uses_its_response(
    sdk, detail, expected
):
    sdk.client.side_effect = RuntimeError(detail)
    result = await load_source("server/motd_server_sfx.py").motd_server(
        {"name": "Reader", "mood": "wise"}, {}, {}
    )
    assert result == {"statusCode": 500, "message": "Handled failure"}
    assert polyapi.polyCustom.responseStatusCode == 500
    sdk.handler.assert_awaited_once_with(
        {
            "message": expected,
            "functionName": "motd_client",
            "functionContext": "example.sample_workflow",
        }
    )


def test_error_handler_keeps_bounded_details_and_does_not_dump_unknown_events(
    sdk, capsys
):
    handler = load_source("server/motd_error_handler_sfx.py").motd_error_handler
    assert handler(
        {"functionName": "motd_client", "message": "getRandomJoke request failed"}
    ) == {"message": "motd request failed in motd_client: getRandomJoke request failed"}
    assert "getRandomJoke request failed" in capsys.readouterr().err
    handler({"message": "synthetic-secret", "apiKey": "synthetic-secret"})
    assert "synthetic-secret" not in capsys.readouterr().err


def test_valid_request_passes_security_function(sdk):
    assert load_source("server/validate_motd_payload_sfx.py").validate_motd_payload(
        {"name": "Reader", "mood": "funny"}, {}, {}
    )
