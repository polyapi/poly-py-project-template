"""Verify personalization without bootstrap, credentials, or remote I/O."""

import ast
import json
from pathlib import Path
import re

import pytest

from kickstart.namespace import rewrite_markdown, rewrite_namespace

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("namespace", ["my_project", "acme.integrations"])
def test_tutorial_examples_match_the_selected_root_without_rewriting_links(namespace):
    original = """# Lesson

For example, keep example.utilities in ordinary explanatory prose.
Use `example`, `example.utilities.log`, `{{example.workflow.validate}}`,
`poly.example.utilities.log(...)`, and `vari.example.configuration.level`.
[Reference](https://example.com/example.utilities)

```bash
POLYAPI_NAMESPACE=example
POLYAPI_NAMESPACES=example,second
flow train-openapi spec.json --context example.workflow
curl 'https://example.com/?context=example.utilities'
rg -n '\\bexample\\b' workflows/src
```

~~~python
result = await poly.example.workflow.run_async()
~~~

Preserve `example.invalid`, `example.json`, `external.example.utilities`,
`OOB.polyapi.internal.alerting`, `docs/example.utilities`, and
`https://provider.test/?context=example.utilities`.
"""
    updated = rewrite_markdown(original, namespace)
    for owned in (
        f"`{namespace}`",
        f"`{namespace}.utilities.log`",
        f"{{{{{namespace}.workflow.validate}}}}",
        f"poly.{namespace}.utilities.log",
        f"vari.{namespace}.configuration.level",
        f"POLYAPI_NAMESPACE={namespace}\n",
        f"POLYAPI_NAMESPACES={namespace},second",
        f"--context {namespace}.workflow",
        f"poly.{namespace}.workflow.run_async()",
    ):
        assert owned in updated
    for unchanged in (
        "For example, keep example.utilities in ordinary explanatory prose.",
        "[Reference](https://example.com/example.utilities)",
        "curl 'https://example.com/?context=example.utilities'",
        "rg -n '\\bexample\\b' workflows/src",
        "`example.invalid`",
        "`example.json`",
        "`external.example.utilities`",
        "`OOB.polyapi.internal.alerting`",
        "`docs/example.utilities`",
        "`https://provider.test/?context=example.utilities`",
    ):
        assert unchanged in updated
    assert rewrite_markdown(updated, namespace) == updated


def test_only_curated_regular_project_guides_are_personalized(tmp_path):
    included = (
        "README.md",
        "docs/SETUP.md",
        "docs/CONVENTIONS.md",
        "cli/README.md",
        "workflows/src/sample/README.md",
        "workflows/src/sample/OVERVIEW.md",
    )
    excluded = (
        "AGENTS.md",
        ".agents/skills/sample/SKILL.md",
        "docs/archive/README.md",
        "workflows/src/sample/notes.md",
        "workflows/src/sample/artefacts/specs/README.md",
        "elsewhere/README.md",
    )
    for name in included + excluded:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("Use `example.utilities.log`.\n")
    (tmp_path / "docs/LINK.md").symlink_to(tmp_path / "elsewhere/README.md")
    rewrite_namespace(tmp_path, "customer")
    for name in included:
        assert (tmp_path / name).read_text() == "Use `customer.utilities.log`.\n"
    for name in excluded:
        assert (tmp_path / name).read_text() == "Use `example.utilities.log`.\n"


@pytest.mark.parametrize("namespace", ["my_project", "acme.integrations"])
def test_real_sample_guides_and_artefacts_stay_aligned(tmp_path, namespace):
    advice = (
        ROOT / "workflows/src/sample_workflow/artefacts/vari/advice_api_config.json"
    )
    if json.loads(advice.read_text())["context"] != "example.sample_workflow":
        pytest.skip("Template-only check; this project's namespace is already set.")
    files = (
        "README.md",
        "docs/SETUP.md",
        "docs/CONVENTIONS.md",
        "cli/README.md",
        "workflows/src/sample_workflow/README.md",
        "workflows/src/sample_workflow/OVERVIEW.md",
        "workflows/src/utilities/common/OVERVIEW.md",
        "workflows/src/sample_workflow/client/motd_client_cfx.py",
        "workflows/src/sample_workflow/artefacts/vari/advice_api_config.json",
        "workflows/src/utilities/common/client/log_cfx.py",
        "workflows/src/sample_workflow/artefacts/ai-functions/uplifting_weather.json",
        "workflows/src/sample_workflow/artefacts/mcp-servers/weather_tools.json",
    )
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / name).read_text())
    rewrite_namespace(tmp_path, namespace)
    for name in files:
        content = (tmp_path / name).read_text()
        if name.endswith(".py"):
            ast.parse(content)
            assert f'"{namespace}.' in content or f"'{namespace}." in content
        if name in files[4:7]:
            assert f"`{namespace}`" in content
            assert not re.search(
                r"\bexample\.(sample_workflow|sample_ai_fx|utilities)", content
            )
            assert "export ENVPATH=workflows/dev.env" in content
        if name.endswith("advice_api_config.json"):
            assert json.loads(content)["context"] == f"{namespace}.sample_workflow"
    motd = (tmp_path / files[4]).read_text()
    assert f"--context {namespace}.sample_workflow" in motd
    ai = (tmp_path / files[5]).read_text()
    assert f"poly.{namespace}.sample_workflow.uplifting_weather_async" in ai
    utilities = (tmp_path / files[6]).read_text()
    assert f"poly.{namespace}.utilities.log" in utilities
    assert "OOB.polyapi.internal.alerting.sendAlertToPoly" in utilities
    ai_definition = json.loads((tmp_path / files[-2]).read_text())
    assert ai_definition["context"] == f"{namespace}.sample_workflow"
    assert {tool["functionContext"] for tool in ai_definition["toolFunctions"]} == {
        f"{namespace}.sample_workflow"
    }
    tool_prefix = namespace.replace(".", "_")
    assert f"`{slug_of(namespace)}-weather-tools`" in ai
    assert f'"name":"{tool_prefix}_sample_workflow_searchLocations"' in ai
    for name in files[:7]:
        content = (tmp_path / name).read_text()
        assert not re.search(r"example-weather-tools|example_sample_workflow_", content)
    server = json.loads((tmp_path / files[-1]).read_text())
    slug = namespace.replace(".", "-").replace("_", "-")
    assert server["name"] == f"{slug}-weather-tools"
    assert server["toolFunctions"] == ai_definition["toolFunctions"]


def slug_of(namespace):
    return namespace.replace(".", "-").replace("_", "-")


def test_env_example_namespace_and_webhook_slug_are_personalized(tmp_path):
    example = tmp_path / "workflows/dev.env.example"
    example.parent.mkdir(parents=True)
    example.write_text(
        "POLYAPI_NAMESPACE=example\n"
        "# POLYAPI_NAMESPACES=example,second\n"
        "POLY_TEST_WEBHOOK_URL=https://env.example.polyapi.io/apis/example-motd/motd\n"
        "SLACK_CHANNEL_ID=example\n"
    )
    rewrite_namespace(tmp_path, "acme.integrations")
    assert example.read_text() == (
        "POLYAPI_NAMESPACE=acme.integrations\n"
        "# POLYAPI_NAMESPACES=example,second\n"
        "POLY_TEST_WEBHOOK_URL="
        "https://env.example.polyapi.io/apis/acme-integrations-motd/motd\n"
        "SLACK_CHANNEL_ID=example\n"
    )


def test_invalid_namespace_does_not_change_project_docs(tmp_path):
    guide = tmp_path / "README.md"
    guide.write_text("Use `example.utilities`.\n")
    with pytest.raises(ValueError):
        rewrite_namespace(tmp_path, "invalid-name")
    assert guide.read_text() == "Use `example.utilities`.\n"
