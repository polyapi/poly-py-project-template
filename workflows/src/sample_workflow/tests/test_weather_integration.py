"""Opt-in billable AI execution against an already-configured environment."""

import json
import os
from pathlib import Path

import pytest
from jsonschema import validate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_AI_INTEGRATION") != "true",
        reason="Set RUN_AI_INTEGRATION=true to invoke the deployed AI function",
    ),
]


async def test_deployed_weather_guidance():
    from polyapi import poly

    result = await poly.example.sample_workflow.uplifting_weather_async(
        "London, United Kingdom", "an outdoor walk"
    )
    artifact = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "artefacts/ai-functions/uplifting_weather.json"
        ).read_text()
    )
    validate(result, artifact["outputSchema"])
    assert result["location"] and result["description"] and result["upliftingMessage"]
    assert result["currentWeather"] is not None
    assert result["currentWeather"]["unit"] == "C"
    assert "https://open-meteo.com/" in result["attribution"]
