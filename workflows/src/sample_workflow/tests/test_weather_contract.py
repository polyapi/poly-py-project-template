"""Keep the checked-in weather tools trainable and aligned with the AI bindings."""

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

ARTEFACTS = Path(__file__).resolve().parents[1] / "artefacts"
AI = json.loads((ARTEFACTS / "ai-functions/uplifting_weather.json").read_text())
SPECS = [
    json.loads(path.read_text())
    for path in (ARTEFACTS / "specs/weather").glob("*.json")
    if not path.name.endswith(".poly.json")
]


def operation(spec):
    return next(iter(spec["paths"].values()))["get"]


def response_schema(spec):
    return operation(spec)["responses"]["200"]["content"]["application/json"]["schema"]


def test_weather_tools_match_bundled_training_contracts_without_credentials():
    tools = {tool["functionName"]: tool for tool in AI["toolFunctions"]}
    assert set(tools) == {operation(spec)["operationId"] for spec in SPECS}
    for spec in SPECS:
        endpoint = operation(spec)
        tool = tools[endpoint["operationId"]]
        assert tool["functionContext"] == AI["context"]
        bindings = {entry["key"]: entry for entry in tool["argumentConfig"]}
        parameters = {
            (
                "temperatureUnit"
                if entry["name"] == "temperature_unit"
                else entry["name"]
            ): entry
            for entry in endpoint["parameters"]
        }
        # Flow's translation adds hostUrl to the provider's query arguments.
        assert set(bindings) == set(parameters) | {"hostUrl"}
        assert bindings["hostUrl"] == {
            "key": "hostUrl",
            "source": "hardcoded",
            "value": spec["servers"][0]["url"],
        }
        assert not spec.get("security") and not endpoint.get("security")
        assert not spec.get("components", {}).get("securitySchemes")
        assert all(
            entry["source"] in {"ai", "hardcoded"} for entry in bindings.values()
        )
        for name, parameter in parameters.items():
            assert parameter["in"] == "query"
            if bindings[name]["source"] == "hardcoded":
                validate(bindings[name]["value"], parameter["schema"])
    weather = {
        entry["key"]: entry for entry in tools["getCurrentWeather"]["argumentConfig"]
    }
    spec = next(
        spec for spec in SPECS if operation(spec)["operationId"] == "getCurrentWeather"
    )
    fields = set(weather["current"]["value"].split(","))
    schema = response_schema(spec)
    for section in ("current", "current_units"):
        assert fields <= set(schema["properties"][section]["required"])


@pytest.mark.parametrize("result", [{}, {"results": []}])
def test_location_contract_accepts_no_matches(result):
    spec = next(
        spec for spec in SPECS if operation(spec)["operationId"] == "searchLocations"
    )
    validate(result, response_schema(spec))


def test_location_contract_requires_coordinates_for_a_candidate():
    spec = next(
        spec for spec in SPECS if operation(spec)["operationId"] == "searchLocations"
    )
    with pytest.raises(ValidationError):
        validate({"results": [{"id": 1, "name": "London"}]}, response_schema(spec))


@pytest.mark.parametrize(
    "current", [None, {"condition": "Overcast", "temperature": 12, "unit": "C"}]
)
def test_output_accepts_available_or_unavailable_weather_with_attribution(current):
    output = {
        "location": "London, United Kingdom",
        "currentWeather": current,
        "description": "Current conditions" if current else "Weather unavailable",
        "recommendedItems": [],
        "upliftingMessage": "Enjoy your day.",
        "attribution": AI["outputSchema"]["properties"]["attribution"]["enum"][0],
    }
    validate(output, AI["outputSchema"])
    output.pop("attribution")
    with pytest.raises(ValidationError):
        validate(output, AI["outputSchema"])


def test_weather_contract_allows_null_measurements_but_requires_response_structure():
    spec = next(
        spec for spec in SPECS if operation(spec)["operationId"] == "getCurrentWeather"
    )
    schema = response_schema(spec)
    current = {
        "time": "2026-01-01T12:00",
        "interval": 900,
        "temperature_2m": None,
        "apparent_temperature": None,
        "precipitation": None,
        "weather_code": None,
        "wind_speed_10m": None,
    }
    result = {
        "current": current,
        "current_units": dict.fromkeys(current, "unit"),
    }
    validate(result, schema)
    del result["current_units"]
    with pytest.raises(ValidationError):
        validate(result, schema)
