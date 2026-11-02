"""Synthetic connection tests; never consult an operator's saved SDK setup."""

import os

from dotenv import dotenv_values
import pytest

from cli import environment, flow


@pytest.fixture(autouse=True)
def isolated_connection(monkeypatch):
    snapshot = dict(os.environ)
    for names in environment.CONNECTION_ALIASES:
        for name in names:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        environment,
        "get_saved_sdk_connection",
        lambda: ("placeholder-saved-key", "https://saved.invalid"),
    )
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_namespace_and_pin_only_file_uses_saved_setup(tmp_path, monkeypatch):
    selected = tmp_path / "selected.env"
    text = "POLYAPI_NAMESPACE=demo\nTARGET_ENV_ID=selected-target\n"
    selected.write_text(text)
    monkeypatch.setenv("ENVPATH", str(selected))
    env_file = flow.load_env(tmp_path)
    assert flow.get_target_env_id(env_file) == "selected-target"
    assert flow.get_api_key_and_url() == (
        "placeholder-saved-key",
        "https://saved.invalid",
    )
    assert os.environ["POLY_API_KEY"] == "placeholder-saved-key"
    assert selected.read_text() == text
    assert "API_KEY" not in dotenv_values(selected)


@pytest.mark.parametrize("aliases", list(zip(*environment.CONNECTION_ALIASES)))
@pytest.mark.parametrize("field", ["key", "url", "both"])
def test_selected_file_overrides_shell_and_saved_setup_per_field(
    tmp_path, monkeypatch, aliases, field
):
    url_name, key_name = aliases
    monkeypatch.setenv("POLY_API_BASE_URL", "https://shell.invalid")
    monkeypatch.setenv("POLY_API_KEY", "placeholder-shell-key")
    lines = []
    if field in {"key", "both"}:
        lines.append(f"{key_name}=placeholder-selected-key")
    if field in {"url", "both"}:
        lines.append(f"{url_name}=https://selected.invalid")
    selected = tmp_path / "selected.env"
    selected.write_text("\n".join(lines))
    monkeypatch.setenv("ENVPATH", str(selected))
    flow.load_env(tmp_path)
    key, url = flow.get_api_key_and_url()
    assert key == (
        "placeholder-selected-key" if field != "url" else "placeholder-shell-key"
    )
    assert url == (
        "https://selected.invalid" if field != "key" else "https://shell.invalid"
    )
    for name in environment.CONNECTION_ALIASES[0]:
        assert os.environ[name] == url
    for name in environment.CONNECTION_ALIASES[1]:
        assert os.environ[name] == key


@pytest.mark.parametrize("field", ["key", "url"])
def test_partial_override_fills_only_missing_field_from_saved_setup(monkeypatch, field):
    if field == "key":
        monkeypatch.setenv("API_KEY", "placeholder-override-key")
    else:
        monkeypatch.setenv("BASE_URL", "https://override.invalid")

    def saved():
        assert not any(
            name in os.environ
            for names in environment.CONNECTION_ALIASES
            for name in names
        )
        return "placeholder-saved-key", "https://saved.invalid"

    monkeypatch.setattr(environment, "get_saved_sdk_connection", saved)
    assert flow.get_api_key_and_url() == (
        "placeholder-override-key" if field == "key" else "placeholder-saved-key",
        "https://override.invalid" if field == "url" else "https://saved.invalid",
    )


def test_complete_explicit_connection_does_not_consult_sdk(monkeypatch):
    monkeypatch.setenv("API_KEY", "placeholder-explicit-key")
    monkeypatch.setenv("BASE_URL", "https://explicit.invalid")
    monkeypatch.setattr(
        environment,
        "get_saved_sdk_connection",
        lambda: pytest.fail("A complete explicit connection must not read SDK setup"),
    )
    assert flow.get_api_key_and_url() == (
        "placeholder-explicit-key",
        "https://explicit.invalid",
    )


def test_explicit_empty_field_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("API_KEY", "")
    assert flow.get_api_key_and_url() == ("", "https://saved.invalid")


def test_sdk_failure_restores_overrides_without_exposing_error_details(monkeypatch):
    monkeypatch.setenv("API_KEY", "placeholder-override-key")

    def fail():
        raise OSError("placeholder-sensitive-sdk-error")

    monkeypatch.setattr(environment, "get_saved_sdk_connection", fail)
    with pytest.raises(flow.DeployError) as error:
        flow.get_api_key_and_url()
    assert "placeholder-sensitive" not in str(error.value)
    assert os.environ["API_KEY"] == "placeholder-override-key"
