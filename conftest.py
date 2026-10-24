"""Keep deployed tests opt-in and unit tests independent of saved SDK identity."""

import json
import os
from pathlib import Path

# Initialize the SDK's TLS configuration before loading HTTP clients.
import polyapi  # noqa: F401
import pytest
import requests


@pytest.fixture(autouse=True)
def no_unit_test_network(request, monkeypatch):
    if "integration" not in request.node.keywords:

        def reject(*args, **kwargs):
            pytest.fail("Unit tests must mock HTTP transport")

        monkeypatch.setattr(requests.Session, "send", reject)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.getenv("RUN_POLY_INTEGRATION") == "true":
        return
    disabled = pytest.mark.skip(reason="Set RUN_POLY_INTEGRATION=true to opt in")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(disabled)


@pytest.fixture
def sample_namespace() -> str:
    """Read the customized sample root without loading target credentials."""
    artifact = (
        Path(__file__).parent
        / "workflows/src/sample_workflow/artefacts/vari/advice_api_config.json"
    )
    context = json.loads(artifact.read_text())["context"]
    assert context.endswith(".sample_workflow")
    return context.removesuffix(".sample_workflow")
