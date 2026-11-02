"""Synthetic onboarding tests: never load an operator's SDK or env credentials."""

import json
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

from dotenv import dotenv_values
import pytest
import requests

from cli import flow
from cli import setup as onboarding

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = onboarding.Identity("Test tenant", "Development", "test-environment")


def answers(monkeypatch, *values):
    supplied = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt: next(supplied))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with spaces"
    (root / "workflows").mkdir(parents=True)
    (root / "workflows/dev.env.example").write_text(
        "# Existing comment\nPOLYAPI_NAMESPACE=my_project\n"
        "POLY_API_KEY=placeholder\nPOLY_API_BASE_URL=https://placeholder.invalid\n"
        "TARGET_ENV_ID=placeholder\nLOG_LEVEL=debug\n"
    )
    return root


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Production", "prod"),
        ("tenant-prod", "prod"),
        ("PROD EU", "prod"),
        ("Default", "dev"),
        ("Pre-Production", "dev"),
        ("Non Production", "dev"),
        ("QA prod mirror", "dev"),
        ("Staging", "dev"),
        ("Sandbox", "dev"),
        ("product-test", "dev"),
        ("", "dev"),
    ],
)
def test_suggestion_fails_safe(name, expected):
    assert onboarding.suggest_environment(name) == expected


def test_selection_shows_identity_and_retains_previous_only_for_same_target(
    monkeypatch, capsys
):
    prior = {
        "envpath": "workflows/prod.env",
        "environment_id": IDENTITY.environment_id,
        "base_url": "https://test.invalid",
    }
    answers(monkeypatch, "invalid", "")
    assert (
        onboarding.choose_target(IDENTITY, "https://test.invalid", prior)
        == "workflows/prod.env"
    )
    answers(monkeypatch, "")
    assert (
        onboarding.choose_target(IDENTITY, "https://different.invalid", prior)
        == "workflows/dev.env"
    )
    assert "Test tenant" in capsys.readouterr().out


def test_sdk_setup_is_interactive_without_inherited_keys(monkeypatch, project):
    monkeypatch.setenv("POLY_API_KEY", "synthetic-inherited")
    monkeypatch.setenv("POLY_API_BASE_URL", "https://inherited.invalid")
    from polyapi import config

    monkeypatch.setattr(
        config, "get_api_key_and_url", lambda: ("synthetic-new", "https://new.invalid/")
    )
    calls = []

    def run(command, **kwargs):
        assert "POLY_API_KEY" not in os.environ
        assert "POLY_API_BASE_URL" not in os.environ
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(onboarding.subprocess, "run", run)
    with onboarding.sdk_environment():
        assert onboarding.run_sdk_setup(project) == (
            "synthetic-new",
            "https://new.invalid",
        )
    assert calls == [([sys.executable, "-m", "polyapi", "setup"], {"cwd": project})]
    assert os.environ["POLY_API_KEY"] == "synthetic-inherited"


def test_native_sdk_setup_contract_retains_defaults_and_generates(
    monkeypatch, tmp_path
):
    """Exercise the real pinned SDK CLI with a temporary native config store."""
    from polyapi import cli, config

    monkeypatch.delenv("POLY_API_KEY", raising=False)
    monkeypatch.delenv("POLY_API_BASE_URL", raising=False)
    monkeypatch.setattr(
        config, "get_config_file_path", lambda: str(tmp_path / "synthetic-config")
    )
    monkeypatch.setattr(config, "API_KEY", "synthetic-existing-key")
    monkeypatch.setattr(config, "API_URL", "https://dev.polyapi.io")
    monkeypatch.setattr(sys, "argv", ["polyapi", "setup"])
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "cache"))
    generated = []
    monkeypatch.setattr(cli, "generate", lambda: generated.append(True))
    answers(monkeypatch, "", "")
    cli.execute_from_cli()
    assert config.get_api_key_and_url() == (
        "synthetic-existing-key",
        "https://dev.polyapi.io",
    )
    assert generated == [True]


def test_identity_uses_sdk_auth_and_does_not_follow_redirects(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "tenantName": "Test tenant",
                "environmentName": "Development",
                "environmentId": "test-environment",
                "unneeded": "discarded",
            },
        )

    monkeypatch.setattr(onboarding.requests, "get", get)
    assert (
        onboarding.fetch_identity("synthetic-key", "https://test.invalid") == IDENTITY
    )
    assert callable(calls[0][1].pop("auth"))
    assert calls == [
        (
            "https://test.invalid/whoami",
            {
                "headers": {"Authorization": "Bearer synthetic-key"},
                "timeout": 30,
                "allow_redirects": False,
            },
        )
    ]


@pytest.mark.parametrize(
    "status,payload",
    [
        (401, {}),
        (500, {}),
        (302, {}),
        (200, {"userId": None}),
        (200, []),
        (200, {"environmentId": "id"}),
    ],
)
def test_identity_failures_are_sanitized(monkeypatch, status, payload):
    monkeypatch.setattr(
        onboarding.requests,
        "get",
        lambda *a, **k: SimpleNamespace(status_code=status, json=lambda: payload),
    )
    with pytest.raises(onboarding.SetupError) as error:
        onboarding.fetch_identity("synthetic-secret", "https://test.invalid")
    assert "synthetic-secret" not in str(error.value)


def test_transport_errors_do_not_expose_response_or_credentials(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("synthetic-secret-response")

    monkeypatch.setattr(onboarding.requests, "get", fail)
    with pytest.raises(onboarding.SetupError, match="Could not verify") as error:
        onboarding.fetch_identity("synthetic-secret", "https://test.invalid")
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.invalid",
        "https://user:password@test.invalid",
        "https://test.invalid?key=foo",
    ],
)
def test_unsafe_base_urls_are_rejected_before_http(url):
    with pytest.raises(onboarding.SetupError):
        onboarding.fetch_identity("synthetic-key", url)


def test_setup_writes_identity_preserves_config_and_saves_no_secret_in_state(
    monkeypatch, project
):
    monkeypatch.setattr(
        onboarding,
        "run_sdk_setup",
        lambda root: ("synthetic-key", "https://test.invalid"),
    )
    monkeypatch.setattr(onboarding, "fetch_identity", lambda *a: IDENTITY)
    answers(monkeypatch, "", "", "")
    onboarding.setup(project)
    target = project / "workflows/dev.env"
    values = dotenv_values(target)
    assert "POLY_API_KEY" not in values
    assert "POLY_API_BASE_URL" not in values
    assert values["TARGET_ENV_ID"] == IDENTITY.environment_id
    assert values["POLYAPI_NAMESPACE"] == "my_project"
    assert values["LOG_LEVEL"] == "debug"
    assert "# Existing comment" in target.read_text()
    state = onboarding.read_state(project)
    assert state["envpath"] == "workflows/dev.env"
    assert "synthetic-key" not in (project / onboarding.STATE_PATH).read_text()
    settings = json.loads((project / ".vscode/settings.json").read_text())
    assert settings["python.envFile"] == "${workspaceFolder}/workflows/dev.env"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_target_switch_requires_confirmation_and_removes_execution_key(
    monkeypatch, project
):
    path = project / "workflows/dev.env"
    path.write_text(
        "POLY_API_BASE_URL=https://old.invalid\nTARGET_ENV_ID=old\nPOLY_EXEC_API_KEY=synthetic-old-exec\nLOG_LEVEL=info\n"
    )
    answers(monkeypatch, "")
    with pytest.raises(onboarding.SetupError, match="Cancelled"):
        onboarding.prepare_env(
            project,
            "workflows/dev.env",
            IDENTITY,
            "synthetic-new",
            "https://new.invalid",
        )
    assert "synthetic-old-exec" in path.read_text()
    answers(monkeypatch, "yes")
    result = onboarding.prepare_env(
        project, "workflows/dev.env", IDENTITY, "synthetic-new", "https://new.invalid"
    )
    assert "synthetic-old-exec" not in result
    assert "LOG_LEVEL=info" in result


def test_duplicate_keys_are_replaced_and_shell_metacharacters_roundtrip(tmp_path):
    result = onboarding.update_dotenv(
        "POLY_API_KEY=old\nexport POLY_API_KEY=duplicate\n# keep\n",
        {"POLY_API_KEY": "fake'\\$`&key"},
    )
    path = tmp_path / "synthetic.env"
    path.write_text(result)
    assert dotenv_values(path)["POLY_API_KEY"] == "fake'\\$`&key"
    assert result.count("POLY_API_KEY=") == 1
    assert "# keep" in result


@pytest.mark.parametrize("value", ["${KEY}", "fake\nINJECT=bad", "fake\0key"])
def test_env_value_injection_rejected(value):
    with pytest.raises(onboarding.SetupError):
        onboarding.update_dotenv("", {"POLY_API_KEY": value})


def test_jsonc_editor_settings_preserve_unrelated_values_and_comments(project):
    folder = project / ".vscode"
    folder.mkdir()
    (folder / "settings.json").write_text("""// comment with { brace
{
  // Keep formatter
  "editor.formatOnSave": true,
  "python.envFile": "old",
  "terminal.integrated.env.osx": {"KEEP": "yes",},
}
""")
    result = onboarding.vscode_settings(project, "workflows/prod.env")
    values, _, _ = onboarding.jsonc_values(result)
    assert values["editor.formatOnSave"] is True
    assert values["terminal.integrated.env.osx"] == {
        "KEEP": "yes",
        "ENVPATH": "workflows/prod.env",
    }
    assert values["python.envFile"].endswith("workflows/prod.env")
    assert "// Keep formatter" in result
    assert result.startswith("// comment with { brace")


def test_failed_whoami_does_not_change_project_selection(monkeypatch, project):
    monkeypatch.setattr(
        onboarding,
        "run_sdk_setup",
        lambda root: ("synthetic-key", "https://test.invalid"),
    )

    def fail(*args):
        raise onboarding.SetupError("whoami failed")

    monkeypatch.setattr(onboarding, "fetch_identity", fail)
    with pytest.raises(onboarding.SetupError):
        onboarding.setup(project)
    assert not (project / "workflows/dev.env").exists()
    assert not (project / onboarding.STATE_PATH).exists()


def test_atomic_state_save_retains_previous_on_failure(monkeypatch, tmp_path):
    path = tmp_path / "state"
    path.write_text("previous")

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(onboarding.os, "replace", fail)
    with pytest.raises(OSError):
        onboarding.atomic_write(path, "next")
    assert path.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [path]


def test_profile_is_opt_in_and_idempotent(monkeypatch, tmp_path, project):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    answers(monkeypatch, "")
    onboarding.offer_profile(project, "fish", None)
    path = tmp_path / "config/fish/conf.d/poly_flow.fish"
    assert not path.exists()
    answers(monkeypatch, "yes")
    onboarding.offer_profile(project, "fish", None)
    first = path.read_text()
    onboarding.offer_profile(project, "fish", None)
    assert path.read_text() == first
    assert "source '" in first


def test_flow_routes_setup_without_deployment(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["flow.py", "setup", "--help"])
    calls = []
    monkeypatch.setattr(onboarding, "main", lambda args: calls.append(args) or 7)
    assert flow.main() == 7
    assert calls == [["--help"]]


@pytest.fixture
def shell_project(project, tmp_path):
    """Real shell, stub uv runner: capture argument/venv/target/cwd contracts."""
    (project / "cli").mkdir()
    (project / "cli/flow.py").touch()
    (project / "pyproject.toml").touch()
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
        check=True,
        capture_output=True,
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f"""#!{sys.executable}
import json, os, pathlib, sys
if '--print-envpath' in sys.argv:
    print('workflows/prod.env')
elif 'setup' not in sys.argv and 'sync' not in sys.argv:
    pathlib.Path('call.json').write_text(json.dumps({{'argv':sys.argv[1:], 'envpath':os.getenv('ENVPATH'), 'venv':os.getenv('VIRTUAL_ENV'), 'cwd':os.getcwd()}}))
    sys.exit(23)
""")
    uv.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
    env.pop("VIRTUAL_ENV", None)
    env.pop("ENVPATH", None)
    return project, env


@pytest.mark.parametrize("shell", ["bash", "fish", "pwsh"])
def test_real_shell_activation_forwarding_and_status(shell, shell_project):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is not installed")
    project, env = shell_project
    suffix = {"bash": "bash", "fish": "fish", "pwsh": "ps1"}[shell]
    loader = ROOT / f"cli/shell/flow.{suffix}"
    if shell == "pwsh":
        command = f". '{loader}'; flow setup; flow push-dynamic --paths 'path with spaces' --dry-run; $s = $LASTEXITCODE; if ($env:VIRTUAL_ENV -ne '{project}/.venv') {{ exit 99 }}; exit $s"
        args = [executable, "-NoProfile", "-Command", command]
    else:
        status = "$?" if shell == "bash" else "$status"
        command = f"source '{loader}'; flow setup; flow push-dynamic --paths 'path with spaces' --dry-run; exit {status}"
        args = (
            [executable, "--noprofile", "--norc", "-c", command]
            if shell == "bash"
            else [executable, "--no-config", "-c", command]
        )
    result = subprocess.run(args, cwd=project, env=env, text=True, capture_output=True)
    assert result.returncode == 23, result.stdout + result.stderr
    call = json.loads((project / "call.json").read_text())
    assert call["argv"] == [
        "run",
        "--locked",
        "python",
        "cli/flow.py",
        "push-dynamic",
        "--paths",
        "path with spaces",
        "--dry-run",
    ]
    assert call["envpath"] == "workflows/prod.env"
    assert Path(call["venv"]).resolve() == (project / ".venv").resolve()
    assert Path(call["cwd"]).resolve() == project.resolve()


@pytest.mark.parametrize("shell", ["bash", "fish", "pwsh"])
def test_existing_flow_command_is_not_replaced(shell):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is not installed")
    suffix = {"bash": "bash", "fish": "fish", "pwsh": "ps1"}[shell]
    loader = ROOT / f"cli/shell/flow.{suffix}"
    if shell == "bash":
        command = f"flow() {{ echo existing; }}; source '{loader}'; flow"
        args = [executable, "--noprofile", "--norc", "-c", command]
    elif shell == "fish":
        command = f"function flow; echo existing; end; source '{loader}'; flow"
        args = [executable, "--no-config", "-c", command]
    else:
        command = f"function flow {{ 'existing' }}; . '{loader}'; flow"
        args = [executable, "-NoProfile", "-Command", command]
    result = subprocess.run(args, text=True, capture_output=True)
    assert result.returncode == 0
    assert result.stdout.rstrip().endswith("existing")


def test_partial_project_save_rolls_back(monkeypatch, project):
    env = project / "workflows/dev.env"
    env.write_text("previous synthetic env")
    state = project / onboarding.STATE_PATH
    writer = onboarding.atomic_write

    def fail_state(path, content):
        if path == state:
            raise OSError("synthetic write failure")
        writer(path, content)

    monkeypatch.setattr(onboarding, "atomic_write", fail_state)
    settings = project / ".vscode/settings.json"
    with pytest.raises(OSError):
        onboarding.save_project_files({env: "new", settings: "{}", state: "{}"})
    assert env.read_text() == "previous synthetic env"
    assert not settings.exists()
    assert not state.exists()


def test_missing_env_creation_can_be_declined(monkeypatch, project):
    answers(monkeypatch, "no")
    with pytest.raises(onboarding.SetupError, match="Cancelled"):
        onboarding.prepare_env(
            project, "workflows/dev.env", IDENTITY, "fake", "https://test.invalid"
        )
    assert not (project / "workflows/dev.env").exists()


def test_existing_target_keeps_execution_key_on_repeat(monkeypatch, project):
    path = project / "workflows/dev.env"
    path.write_text(
        "POLY_API_BASE_URL=https://test.invalid\nTARGET_ENV_ID=test-environment\nPOLY_EXEC_API_KEY=synthetic-exec\n"
    )
    answers(monkeypatch, "")
    content = onboarding.prepare_env(
        project, "workflows/dev.env", IDENTITY, "synthetic-new", "https://test.invalid"
    )
    assert "POLY_EXEC_API_KEY=synthetic-exec" in content


def test_namespace_only_existing_target_keeps_execution_key(monkeypatch, project):
    path = project / "workflows/dev.env"
    path.write_text(
        "POLYAPI_NAMESPACE=demo\nTARGET_ENV_ID=test-environment\n"
        "POLY_EXEC_API_KEY=synthetic-exec\n"
    )
    answers(monkeypatch, "")
    values = dotenv_values(
        stream=StringIO(
            onboarding.prepare_env(
                project,
                "workflows/dev.env",
                IDENTITY,
                "synthetic-new",
                "https://test.invalid",
            )
        )
    )
    assert values["POLY_EXEC_API_KEY"] == "synthetic-exec"
    assert "POLY_API_KEY" not in values
    assert "POLY_API_BASE_URL" not in values


@pytest.mark.parametrize("aliases", list(zip(*onboarding.CONNECTION_ALIASES)))
def test_setup_keeps_existing_connection_overrides_aligned(
    monkeypatch, project, aliases
):
    url_name, key_name = aliases
    path = project / "workflows/dev.env"
    path.write_text(
        f"{url_name}=https://old.invalid\n{key_name}=synthetic-old\nTARGET_ENV_ID=old\n"
    )
    answers(monkeypatch, "yes")

    values = dotenv_values(
        stream=StringIO(
            onboarding.prepare_env(
                project,
                "workflows/dev.env",
                IDENTITY,
                "synthetic-new",
                "https://test.invalid",
            )
        )
    )
    assert values[url_name] == "https://test.invalid"
    assert values[key_name] == "synthetic-new"
    assert values["TARGET_ENV_ID"] == IDENTITY.environment_id


def test_symlink_config_is_not_followed(project, tmp_path):
    other = tmp_path / "outside"
    other.write_text("unchanged")
    (project / "workflows/dev.env").symlink_to(other)
    with pytest.raises(onboarding.SetupError, match="symlinks"):
        onboarding.prepare_env(
            project, "workflows/dev.env", IDENTITY, "fake", "https://test.invalid"
        )
    assert other.read_text() == "unchanged"


def test_tracked_env_is_never_overwritten(monkeypatch, project):
    (project / ".git").mkdir()
    monkeypatch.setattr(
        onboarding.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1)
    )
    with pytest.raises(onboarding.SetupError, match="untracked and Git-ignored"):
        onboarding.prepare_env(
            project, "workflows/dev.env", IDENTITY, "fake", "https://test.invalid"
        )


def test_repeat_setup_retains_defaults_and_file_content(monkeypatch, project):
    monkeypatch.setattr(
        onboarding,
        "run_sdk_setup",
        lambda root: ("synthetic-key", "https://test.invalid"),
    )
    monkeypatch.setattr(onboarding, "fetch_identity", lambda *a: IDENTITY)
    answers(monkeypatch, "", "", "")
    onboarding.setup(project)
    env = project / "workflows/dev.env"
    first = env.read_text()
    answers(monkeypatch, "", "")
    onboarding.setup(project)
    assert env.read_text() == first
    assert onboarding.read_state(project)["envpath"] == "workflows/dev.env"


def test_invalid_editor_settings_prevent_env_save(monkeypatch, project):
    (project / ".vscode").mkdir()
    (project / ".vscode/settings.json").write_text("not json")
    monkeypatch.setattr(
        onboarding,
        "run_sdk_setup",
        lambda root: ("synthetic-key", "https://test.invalid"),
    )
    monkeypatch.setattr(onboarding, "fetch_identity", lambda *a: IDENTITY)
    answers(monkeypatch, "", "", "")
    with pytest.raises(onboarding.SetupError, match="JSONC"):
        onboarding.setup(project)
    assert not (project / "workflows/dev.env").exists()


def test_netrc_cannot_override_sdk_identity(monkeypatch, tmp_path):
    netrc = tmp_path / "synthetic-netrc"
    netrc.write_text("machine test.invalid login fake password synthetic-wrong-key\n")
    monkeypatch.setenv("NETRC", str(netrc))

    def send(session, prepared, **kwargs):
        assert prepared.headers["Authorization"] == "Bearer synthetic-sdk-key"
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "tenantName": "Test",
                "environmentName": "Dev",
                "environmentId": "test-env",
            }
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    assert (
        onboarding.fetch_identity(
            "synthetic-sdk-key", "https://test.invalid"
        ).environment_id
        == "test-env"
    )


def test_cancelled_optional_profile_keeps_successful_setup(monkeypatch, project):
    monkeypatch.setattr(
        onboarding,
        "run_sdk_setup",
        lambda root: ("synthetic-key", "https://test.invalid"),
    )
    monkeypatch.setattr(onboarding, "fetch_identity", lambda *a: IDENTITY)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(onboarding, "offer_profile", interrupt)
    answers(monkeypatch, "", "", "")
    onboarding.setup(project, "fish")
    assert onboarding.read_state(project)["envpath"] == "workflows/dev.env"


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "pwsh"])
def test_saved_profile_loads_function_in_fresh_shell(
    shell, monkeypatch, project, tmp_path
):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is not installed")
    shutil.copytree(ROOT / "cli/shell", project / "cli/shell")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    answers(monkeypatch, "yes")
    if shell == "bash":
        profile = tmp_path / (
            ".bash_profile" if sys.platform == "darwin" else ".bashrc"
        )
        onboarding.offer_profile(project, "bash", None)
        args = [
            executable,
            "--noprofile",
            "--norc",
            "-c",
            f"source '{profile}'; test \"$(type -t flow)\" = function",
        ]
    elif shell == "zsh":
        monkeypatch.setenv("ZDOTDIR", str(tmp_path))
        profile = tmp_path / ".zshrc"
        onboarding.offer_profile(project, "zsh", None)
        args = [
            executable,
            "-f",
            "-c",
            f"source '{profile}'; functions flow >/dev/null",
        ]
    elif shell == "fish":
        profile = tmp_path / "config/fish/conf.d/poly_flow.fish"
        onboarding.offer_profile(project, "fish", None)
        args = [
            executable,
            "--no-config",
            "-c",
            f"source '{profile}'; functions -q flow",
        ]
    else:
        profile = tmp_path / "profile.ps1"
        onboarding.offer_profile(project, "powershell", str(profile))
        args = [
            executable,
            "-NoProfile",
            "-Command",
            f". '{profile}'; if ((Get-Command flow).CommandType -ne 'Function') {{ exit 1 }}",
        ]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
