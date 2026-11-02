"""Test optional kickstart profile registration with synthetic configuration only."""

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cli import setup, shell_profile
from kickstart import kickstart as parent
from kickstart import configure as kickstart


def answers(monkeypatch, *values):
    supplied = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt: next(supplied))


@pytest.fixture
def profile_project(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "zsh-config"))
    monkeypatch.delenv("SHELL", raising=False)
    return root


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_kickstart_reuses_confirmed_profile_writer(
    shell, profile_project, tmp_path, monkeypatch, capsys
):
    if shell == "powershell":
        profile = tmp_path / "powershell/profile.ps1"
        answers(monkeypatch, shell, str(profile), "yes")
        suffix = "ps1"
    else:
        answers(monkeypatch, shell, "yes")
        suffix = "fish" if shell == "fish" else "bash"
        profile = {
            "bash": tmp_path
            / (".bash_profile" if sys.platform == "darwin" else ".bashrc"),
            "zsh": tmp_path / "zsh-config/.zshrc",
            "fish": tmp_path / "config/fish/conf.d/poly_flow.fish",
        }[shell]
    shell_profile.offer_shell_profile(profile_project)
    content = profile.read_text()
    assert content.count("# Python Flow shell loader") == 1
    assert f"cli/shell/flow.{suffix}" in content
    output = capsys.readouterr().out
    assert str(profile) in output
    assert "Optional shell profile" in output


def test_login_shell_is_only_a_default_and_save_remains_opt_in(
    profile_project, tmp_path, monkeypatch
):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    answers(monkeypatch, "", "")
    shell_profile.offer_shell_profile(profile_project)
    assert not (tmp_path / "config/fish/conf.d/poly_flow.fish").exists()
    answers(monkeypatch, "bash", "yes")
    shell_profile.offer_shell_profile(profile_project)
    profile = tmp_path / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    assert profile.exists()


@pytest.mark.parametrize("hint, reply", [("", ""), ("/bin/fish", "skip")])
def test_skip_does_not_register_a_profile(profile_project, monkeypatch, hint, reply):
    monkeypatch.setenv("SHELL", hint)
    register = Mock()
    monkeypatch.setattr(shell_profile, "offer_profile", register)
    answers(monkeypatch, reply)
    shell_profile.offer_shell_profile(profile_project)
    register.assert_not_called()


def test_powershell_requires_an_explicit_profile_path(profile_project, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/pwsh")
    register = Mock()
    monkeypatch.setattr(shell_profile, "offer_profile", register)
    answers(monkeypatch, "", "")
    shell_profile.offer_shell_profile(profile_project)
    register.assert_not_called()


def test_invalid_shell_reprompts_and_existing_registration_is_preserved(
    profile_project, tmp_path, monkeypatch
):
    answers(monkeypatch, "unknown", "fish", "yes")
    shell_profile.offer_shell_profile(profile_project)
    profile = tmp_path / "config/fish/conf.d/poly_flow.fish"
    previous = profile.read_text()
    answers(monkeypatch, "fish")
    shell_profile.offer_shell_profile(profile_project)
    assert profile.read_text() == previous


@pytest.fixture
def configured_project(tmp_path, monkeypatch):
    root = tmp_path / "configured-project"
    (root / "workflows").mkdir(parents=True)
    (root / "workflows/dev.env.example").write_text(
        "POLYAPI_NAMESPACE=example\nTARGET_ENV_ID=placeholder\n"
    )
    monkeypatch.setenv("POLY_API_KEY", "synthetic-key")
    monkeypatch.setenv("POLY_API_BASE_URL", "https://test.invalid")
    monkeypatch.setenv("KICKSTART_NAMESPACE", "demo")
    identity = setup.Identity("Synthetic tenant", "Development", "test-env")
    monkeypatch.setattr(setup, "fetch_identity", lambda *args: identity)
    monkeypatch.setattr(setup, "choose_target", lambda *args: "workflows/dev.env")
    monkeypatch.setattr(setup, "run_sdk_setup", lambda *args: None)
    monkeypatch.setattr(setup, "save_target_configuration", lambda *args: None)
    monkeypatch.setattr(
        kickstart,
        "get_api_key_and_url",
        lambda: ("synthetic-key", "https://test.invalid"),
    )
    generate = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(kickstart.subprocess, "run", generate)
    return root, generate


def test_sdk_configuration_does_not_prompt_for_profile(configured_project, monkeypatch):
    root, generate = configured_project
    offer = Mock()
    monkeypatch.setattr(shell_profile, "offer_shell_profile", offer)
    kickstart.configure(root)
    generate.assert_called_once_with(
        [sys.executable, "-m", "polyapi", "generate"], cwd=root, check=True
    )
    offer.assert_not_called()


@pytest.mark.parametrize(
    "failure", [EOFError, KeyboardInterrupt, OSError, UnicodeError]
)
def test_optional_profile_failure_keeps_successful_configuration(
    profile_project, monkeypatch, capsys, failure
):
    monkeypatch.setattr(shell_profile, "offer_shell_profile", Mock(side_effect=failure))
    parent.offer_optional_profile(profile_project)
    assert "continuing project setup" in capsys.readouterr().out.lower()


def test_failed_sdk_verification_never_offers_registration(
    configured_project, monkeypatch
):
    root, generate = configured_project
    monkeypatch.setattr(
        kickstart,
        "get_api_key_and_url",
        lambda: ("wrong-synthetic-key", "https://test.invalid"),
    )
    offer = Mock()
    monkeypatch.setattr(shell_profile, "offer_shell_profile", offer)
    with pytest.raises(setup.SetupError, match="did not save"):
        kickstart.configure(root)
    offer.assert_not_called()
    generate.assert_not_called()
