"""Self-update remains available without the SDK or a selected target."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("entrypoint", ["script", "module", "outside-project"])
def test_self_update_help_without_site_packages(
    entrypoint: str, tmp_path: Path
) -> None:
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    cli = project / "cli"
    cli.mkdir(parents=True)
    for name in ("flow.py", "self_update.py", "__init__.py"):
        shutil.copyfile(source / name, cli / name)
    if entrypoint != "module":
        (cli / "__init__.py").write_text(
            'raise RuntimeError("Broken CLI package must not block recovery")\n'
        )
    arguments = [str(cli / "flow.py")]
    if entrypoint == "module":
        arguments = ["-m", "cli.flow"]
    result = subprocess.run(
        [sys.executable, "-B", "-S", *arguments, "self-update", "--help"],
        cwd=tmp_path if entrypoint == "outside-project" else project,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for option in ("--check", "--rollback", "--source", "--ref"):
        assert option in result.stdout
    assert result.stderr == ""
    assert not (cli / "__pycache__").exists()


def test_imported_entrypoint_forwards_update_without_running_flow(monkeypatch):
    from cli import flow, self_update

    calls = []
    monkeypatch.setattr(sys, "argv", ["flow", "self-update", "--check"])
    monkeypatch.setattr(self_update, "main", lambda args: calls.append(args) or 0)

    def unexpected_run(*args, **kwargs):
        pytest.fail("Self-update must not load target settings or deploy")

    monkeypatch.setattr(flow, "run_flow", unexpected_run)
    assert flow.main() == 0
    assert calls == [["--check"]]
    assert "self-update" in flow.create_parser().format_help()
