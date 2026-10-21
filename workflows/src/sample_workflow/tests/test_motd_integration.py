"""Requires an already-deployed sample and explicit runtime-test opt-in."""

import os
from urllib.parse import urlparse

import pytest
import requests

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mood", ["funny", "wise"])
def test_deployed_webhook(mood: str) -> None:
    url = os.getenv("POLY_TEST_WEBHOOK_URL", "")
    key = os.getenv("POLY_API_KEY", "")
    if not url or not key:
        pytest.fail(
            "Set POLY_TEST_WEBHOOK_URL and POLY_API_KEY for the test environment"
        )
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.hostname and not parsed.username
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        json={"name": "Template QA", "mood": mood},
        timeout=30,
        allow_redirects=False,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["statusCode"] == 200
    assert result["message"].startswith("Hi Template QA! ")
