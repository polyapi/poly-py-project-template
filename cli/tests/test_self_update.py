"""Self-update exercises committed Git fixtures and never contacts a real server."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from cli import self_update as updater

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = """[project]
name = "fixture"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []
[tool.pytest.ini_options]
pythonpath = ["."]
"""


def git(root, *arguments):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        },
    ).stdout.strip()


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "Fixture source")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def projects(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    git(source, "init", "--quiet", "-b", "main")
    (source / "cli/tests").mkdir(parents=True)
    (source / "cli/__init__.py").write_text('__version__ = "1.3.0"\n')
    (source / "cli/flow.py").write_text('print("Flow fixture help")\n')
    (source / "cli/README.md").write_text("Updated CLI\n")
    (source / "cli/tests/test_local.py").write_text(
        "def test_local():\n    assert 1 + 1 == 2\n"
    )
    (source / "cli/self_update.py").write_bytes(
        (ROOT / "cli/self_update.py").read_bytes()
    )
    (source / "pyproject.toml").write_text(MANIFEST)
    (source / "cli/helper.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "cli/helper.sh").chmod(0o755)
    commit(source)
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "--quiet", "-b", "main")
    shutil.copytree(source / "cli", project / "cli")
    (project / "cli/__init__.py").write_text('__version__ = "1.2.0"\n')
    (project / "cli/custom.py").write_text('CUSTOM = "keep in backup"\n')
    (project / "cli/README.md").write_text("Locally customized CLI\n")
    (project / "pyproject.toml").write_text(MANIFEST)
    (project / "uv.lock").write_text("fixture lock preserved\n")
    (project / "workflows").mkdir()
    (project / "workflows/example.py").write_text("WORKFLOW = 1\n")
    (project / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    return project, source


def execute(projects, *arguments):
    project, source = projects
    return updater.main(
        ["--project", str(project), "--source", str(source), *arguments]
    )


def backups(project):
    return sorted(
        path for path in (project / ".flow/updates").iterdir() if path.is_dir()
    )


def test_preview_reads_committed_source_without_changing_project(projects, capsys):
    project, source = projects
    before = updater.inventory(project / "cli")
    commit_id = git(source, "rev-parse", "HEAD")
    (source / "cli/README.md").write_text("Uncommitted source must not be used\n")
    assert execute(projects, "--check") == 0
    output = capsys.readouterr().out
    assert "Installed Flow version: 1.2.0" in output
    assert "Incoming Flow version: 1.3.0" in output
    assert commit_id in output
    assert "REMOVE cli/custom.py" in output
    assert updater.inventory(project / "cli") == before
    assert not (project / ".flow").exists()


def test_update_preserves_custom_source_and_project_files_then_becomes_noop(
    projects, capsys
):
    project, source = projects
    before = updater.inventory(project / "cli")
    supporting = {
        name: (project / name).read_bytes()
        for name in ("pyproject.toml", "uv.lock", "workflows/example.py")
    }
    assert execute(projects, "--yes") == 0
    backup = backups(project)[0]
    manifest = updater.read_json(backup / "manifest.json")
    assert manifest["status"] == "applied"
    assert manifest["previousVersion"] == "1.2.0"
    assert manifest["incomingVersion"] == "1.3.0"
    assert updater.inventory(backup / "previous") == before
    assert updater.inventory(project / "cli") == updater.inventory(source / "cli")
    assert (backup / "agent-prompt.md").is_file()
    assert (backup / "restore.py").is_file()
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (project / "cli/helper.sh").stat().st_mode & 0o777 == 0o755
    assert all(
        (project / name).read_bytes() == value for name, value in supporting.items()
    )
    assert (
        git(project, "check-ignore", ".flow/updates/anything")
        == ".flow/updates/anything"
    )
    assert execute(projects, "--yes") == 0
    assert len(backups(project)) == 1
    assert "up to date; no source changes" in capsys.readouterr().out


def test_update_executed_from_cli_it_replaces(projects):
    project, source = projects
    result = subprocess.run(
        [
            sys.executable,
            str(project / "cli/self_update.py"),
            "--project",
            str(project),
            "--source",
            str(source),
            "--yes",
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert updater.inventory(project / "cli") == updater.inventory(source / "cli")


def test_standalone_recovery_works_without_installed_cli(projects):
    project, _ = projects
    before = updater.inventory(project / "cli")
    assert execute(projects, "--yes") == 0
    backup = backups(project)[0]
    shutil.rmtree(project / "cli")
    result = subprocess.run(
        [sys.executable, "-S", str(backup / "restore.py"), "--yes"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert updater.inventory(project / "cli") == before
    assert updater.read_json(backup / "manifest.json")["status"] == "rolled-back"


def test_rollback_preserves_post_update_extensions(projects):
    project, _ = projects
    previous = updater.inventory(project / "cli")
    assert execute(projects, "--yes") == 0
    (project / "cli/new_extension.py").write_text("CUSTOM = True\n")
    assert execute(projects, "--rollback", "--yes") == 0
    assert updater.inventory(project / "cli") == previous
    backup = backups(project)[0]
    manifest = updater.read_json(backup / "manifest.json")
    assert (backup / manifest["displaced"] / "new_extension.py").is_file()


@pytest.mark.parametrize("failure", ["syntax", "help", "test", "venv"])
def test_failed_checks_restore_previous_source(projects, failure):
    project, source = projects
    before = updater.inventory(project / "cli")
    if failure == "syntax":
        (source / "cli/flow.py").write_text("def broken(\n")
    elif failure == "help":
        (source / "cli/flow.py").write_text("raise SystemExit(2)\n")
    elif failure == "test":
        (source / "cli/tests/test_local.py").write_text(
            "def test_failure():\n    assert False\n"
        )
    else:
        (project / ".venv").unlink()
    if failure != "venv":
        commit(source)
    assert execute(projects, "--yes") == 1
    assert updater.inventory(project / "cli") == before
    backup = backups(project)[0]
    assert updater.read_json(backup / "manifest.json")["status"] == "failed-restored"
    assert list(backup.glob("failed-replacement-*"))


def test_dependency_review_cannot_be_bypassed_with_yes(projects, capsys):
    project, source = projects
    manifest = source / "pyproject.toml"
    manifest.write_text(
        MANIFEST.replace("dependencies = []", 'dependencies = ["new-package==2.0"]')
    )
    commit(source)
    assert execute(projects, "--check") == 0
    assert "new-package" in capsys.readouterr().out
    assert execute(projects, "--yes") == 1
    assert not (project / ".flow").exists()


def test_extra_project_dependencies_and_metadata_are_preserved(projects):
    project, _ = projects
    content = MANIFEST.replace('name = "fixture"', 'name = "my-project"').replace(
        "dependencies = []", 'dependencies = ["my-extension==4.0"]'
    )
    (project / "pyproject.toml").write_text(content)
    assert execute(projects, "--yes") == 0
    assert (project / "pyproject.toml").read_text() == content


def test_dependency_comparison_normalizes_names_and_rejects_duplicates():
    base = {
        "requires-python": ">=3.11",
        "dependencies": ["PyYAML==6.0"],
        "dependency-groups": {},
        "uv": {},
    }
    current = {**base, "dependencies": ["pyyaml == 6.0", "extra==2"]}
    assert updater.dependency_differences(current, base) == []
    current["uv"] = {"sources": {"PyYAML": {"path": "fork"}}}
    assert updater.dependency_differences(current, base)
    with pytest.raises(updater.UpdateError, match="Duplicate dependency"):
        updater.requirement_map(["PyYAML==6.0", "pyyaml==7.0"])


@pytest.mark.parametrize(
    "name", ["private.env", ".env", ".env.local", "certificate.pem", "credentials.json"]
)
def test_protected_local_cli_files_are_rejected_before_reading(
    projects, monkeypatch, name
):
    project, _ = projects
    forbidden = project / "cli" / name
    forbidden.write_text("test placeholder\n")
    read_bytes = Path.read_bytes

    def guarded(path):
        assert path != forbidden, "Protected file must not be read"
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    assert execute(projects, "--check") == 1
    assert not (project / ".flow").exists()


@pytest.mark.parametrize("where", ["local", "incoming"])
def test_symlink_cli_source_is_rejected(projects, where):
    project, source = projects
    target = project if where == "local" else source
    (target / "cli/escape.py").symlink_to(target / "cli/flow.py")
    if where == "incoming":
        commit(source)
    assert execute(projects, "--yes") == 1
    assert not (project / ".flow").exists()


def test_secret_template_file_is_rejected_without_reading_blob(projects, monkeypatch):
    project, source = projects
    (source / "cli/settings.env").write_text("test placeholder")
    commit(source)
    blob = git(source, "rev-parse", "HEAD:cli/settings.env")
    original = updater.git

    def guarded(root, *arguments):
        assert blob not in arguments
        return original(root, *arguments)

    monkeypatch.setattr(updater, "git", guarded)
    assert execute(projects, "--check") == 1
    assert not (project / ".flow").exists()


def test_modified_previous_backup_is_rejected(projects):
    project, _ = projects
    assert execute(projects, "--yes") == 0
    current = updater.inventory(project / "cli")
    backup = backups(project)[0]
    (backup / "previous/flow.py").write_text("print('changed backup')\n")
    assert execute(projects, "--rollback", "--yes") == 1
    assert updater.inventory(project / "cli") == current


def test_concurrent_edits_during_checks_are_preserved(projects, monkeypatch):
    project, _ = projects
    before = updater.inventory(project / "cli")

    def edit_while_validating(root, _template):
        (root / "cli/edited.py").write_text("LOCAL = True\n")

    monkeypatch.setattr(updater, "validate", edit_while_validating)
    assert execute(projects, "--yes") == 1
    assert updater.inventory(project / "cli") == before
    assert list(backups(project)[0].glob("failed-replacement-*/edited.py"))


def test_requirements_edited_during_checks_stop_update_without_overwriting_edits(
    projects, monkeypatch
):
    project, _ = projects
    before = updater.inventory(project / "cli")
    edited = MANIFEST.replace("dependencies = []", 'dependencies = ["extension==2"]')

    def edit_requirements(root, _template):
        (root / "pyproject.toml").write_text(edited)

    monkeypatch.setattr(updater, "validate", edit_requirements)
    assert execute(projects, "--yes") == 1
    assert updater.inventory(project / "cli") == before
    assert (project / "pyproject.toml").read_text() == edited


@pytest.mark.parametrize("moved", [False, True])
def test_interrupted_replacement_can_recover(projects, monkeypatch, moved):
    project, _ = projects
    before = updater.inventory(project / "cli")

    def leave_interrupted(root, source, files, backup, manifest, expected, **kwargs):
        manifest.update(
            status="applying",
            transaction="update",
            displaced="displaced-12345678",
            transactionBefore=expected,
        )
        updater.save_manifest(backup, manifest)
        if moved:
            (root / "cli").rename(backup / "displaced-12345678")
        raise SystemExit(98)

    monkeypatch.setattr(updater, "replace_tree", leave_interrupted)
    with pytest.raises(SystemExit):
        execute(projects, "--yes")
    assert execute(projects, "--rollback", "--yes") == 0
    assert updater.inventory(project / "cli") == before
    assert (
        updater.read_json(backups(project)[0] / "manifest.json")["status"]
        == "failed-restored"
    )


def test_stale_atomic_manifest_file_does_not_block_recovery(tmp_path):
    path = tmp_path / "manifest.json"
    (tmp_path / "manifest.tmp").write_text("interrupted write")
    updater.write_json(path, {"status": "applied"})
    assert updater.read_json(path) == {"status": "applied"}


def test_permission_changes_are_reported_and_corrected_under_restrictive_umask(
    projects, capsys
):
    project, source = projects
    shutil.rmtree(project / "cli")
    shutil.copytree(source / "cli", project / "cli")
    (project / "cli/README.md").chmod(0o600)
    old = os.umask(0o077)
    try:
        assert execute(projects, "--yes") == 0
    finally:
        os.umask(old)
    assert "PERMISSIONS cli/README.md" in capsys.readouterr().out
    assert (project / "cli/README.md").stat().st_mode & 0o777 == 0o644
    assert execute(projects, "--check") == 0
    assert "no source changes" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name", [".config.env", "credentials.json", "private.key", "client.pem"]
)
def test_validation_guards_real_credential_paths(tmp_path, name):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    external = tmp_path / name
    external.write_text("test placeholder")
    environment = updater.validation_environment(isolated)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).read_text()",
            str(external),
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Credential files are unavailable" in result.stderr
    assert "test placeholder" not in result.stdout


def test_validation_environment_scrubs_credentials_and_disables_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("API_KEY", "test placeholder")
    monkeypatch.setenv("POLY_API_KEY", "test placeholder")
    monkeypatch.setenv("RUN_POLY_INTEGRATION", "true")
    environment = updater.validation_environment(tmp_path)
    assert "API_KEY" not in environment
    assert "POLY_API_KEY" not in environment
    assert environment["RUN_POLY_INTEGRATION"] == "false"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.socket().connect(('127.0.0.1', 9))",
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Network access is disabled" in result.stderr


def test_invalid_ref_and_noninteractive_confirmation_leave_project_unchanged(
    projects, monkeypatch
):
    project, _ = projects
    assert execute(projects, "--ref=--bad", "--check") == 1
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert execute(projects) == 1
    assert not (project / ".flow").exists()


def test_updated_cli_collects_without_importing_unmanaged_kickstart(tmp_path):
    """CLI-only updates must not require a matching project bootstrap version."""
    updater.copy_source(ROOT / "cli", tmp_path / "cli", updater.inventory(ROOT / "cli"))
    (tmp_path / "pyproject.toml").write_text(MANIFEST)
    (tmp_path / "kickstart").mkdir()
    (tmp_path / "kickstart/__init__.py").write_text(
        'raise AssertionError("CLI tests must not import unmanaged kickstart")\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "cli/tests",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
