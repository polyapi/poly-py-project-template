"""Pin the reviewed MCP tool surface, so widening it fails review.

These checks need no credentials and make no PolyAPI or provider requests.
"""

import json
from pathlib import Path

import pytest

ARTEFACTS = Path(__file__).resolve().parents[1] / "artefacts"
SERVER = json.loads((ARTEFACTS / "mcp-servers/weather_tools.json").read_text())
AI = json.loads((ARTEFACTS / "ai-functions/uplifting_weather.json").read_text())
TOOLS = SERVER["toolFunctions"]
CONTEXT = TOOLS[0]["functionContext"] if TOOLS else ""
ROOT = CONTEXT.removesuffix(".sample_workflow")


def bindings(name, source):
    tool = next((tool for tool in TOOLS if tool["functionName"] == name), {})
    return [
        binding
        for binding in tool.get("argumentConfig", [])
        if binding["source"] == source
    ]


def test_server_stays_private_and_is_named_after_the_project_namespace():
    assert SERVER["isPublic"] is False
    assert SERVER["enabled"] is True
    assert CONTEXT.endswith(".sample_workflow")
    # MCP server names are unique per environment and have no context.
    slug = ROOT.replace(".", "-").replace("_", "-")
    assert SERVER["name"] == f"{slug}-weather-tools"


def test_only_the_two_read_only_weather_tools_are_exposed():
    assert [f"{tool['functionContext']}.{tool['functionName']}" for tool in TOOLS] == [
        f"{CONTEXT}.searchLocations",
        f"{CONTEXT}.getCurrentWeather",
    ]


def test_clients_choose_only_the_place_and_the_coordinates():
    assert [binding["key"] for binding in bindings("searchLocations", "ai")] == ["name"]
    assert [binding["key"] for binding in bindings("getCurrentWeather", "ai")] == [
        "latitude",
        "longitude",
    ]
    for tool in TOOLS:
        assert all(
            binding["source"] in {"ai", "hardcoded"}
            for binding in tool["argumentConfig"]
        ), tool["functionName"]


@pytest.mark.parametrize(
    "name, host",
    [
        ("searchLocations", "https://geocoding-api.open-meteo.com"),
        ("getCurrentWeather", "https://api.open-meteo.com"),
    ],
)
def test_provider_hosts_stay_fixed(name, host):
    hosts = [
        binding["value"]
        for binding in bindings(name, "hardcoded")
        if binding["key"] == "hostUrl"
    ]
    assert hosts == [host]


def test_mcp_tools_use_the_same_bindings_as_the_weather_ai_lesson():
    assert TOOLS == AI["toolFunctions"]


@pytest.mark.parametrize(
    "field",
    [
        "oauthClientId",
        "oauthClientSecretVariable",
        "oauthRedirectUris",
        "toolFunctionIds",
        "environmentId",
        "mcpUrl",
        "id",
    ],
)
def test_artefact_carries_no_oauth_settings_or_remote_ids(field):
    assert field not in SERVER
