"""Shell maintenance routing stays independent of SDK setup and uv installs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("shell", ["bash", "fish", "pwsh"])
@pytest.mark.parametrize("installed_venv", [True, False])
@pytest.mark.parametrize(
    ("arguments", "entry", "forwarded"),
    [
        (
            ["self-update", "--check", "--source", "source with spaces"],
            "self_update.py",
            ["--check", "--source", "source with spaces"],
        ),
        (["--version"], "flow.py", ["--version"]),
        (["-V"], "flow.py", ["-V"]),
    ],
)
def test_maintenance_never_selects_target_or_runs_uv(
    tmp_path, shell, installed_venv, arguments, entry, forwarded
):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is not installed")

    project = tmp_path / "project with spaces"
    (project / "cli").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\n")
    (project / "nested").mkdir()
    # Any attempt to select a saved environment fails instead of reading it.
    (project / "cli/setup.py").write_text("raise SystemExit(91)\n")
    recorder = """import json, os, pathlib, sys
pathlib.Path('call.json').write_text(json.dumps({
    'entry': pathlib.Path(__file__).name,
    'argv': sys.argv[1:],
    'cwd': os.getcwd(),
    'envpath': os.getenv('ENVPATH'),
    'venv': os.getenv('VIRTUAL_ENV'),
    'python': sys.executable,
}))
raise SystemExit(23)
"""
    (project / "cli/flow.py").write_text(recorder)
    (project / "cli/self_update.py").write_text(recorder)
    (project / "cli/__init__.py").write_text("")
    if installed_venv:
        python = project / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        # There are deliberately no activation scripts or SDK dependencies.

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    uv = bin_dir / "uv"
    uv.write_text(f"#!{sys.executable}\nraise SystemExit(92)\n")
    uv.chmod(0o755)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "HOME": str(tmp_path),
        "ENVPATH": "unchanged-target-placeholder",
        "VIRTUAL_ENV": "unchanged-venv-placeholder",
    }
    suffix = {"bash": "bash", "fish": "fish", "pwsh": "ps1"}[shell]
    loader = ROOT / f"cli/shell/flow.{suffix}"
    quoted_args = " ".join(shlex.quote(arg) for arg in arguments)
    if shell == "pwsh":
        ps_args = " ".join("'" + arg.replace("'", "''") + "'" for arg in arguments)
        command = f". '{loader}'; flow {ps_args}; exit $LASTEXITCODE"
        invocation = [executable, "-NoProfile", "-Command", command]
    elif shell == "fish":
        command = f"source '{loader}'; flow {quoted_args}; exit $status"
        invocation = [executable, "--no-config", "-c", command]
    else:
        command = f"source '{loader}'; flow {quoted_args}; exit $?"
        invocation = [executable, "--noprofile", "--norc", "-c", command]
    result = subprocess.run(
        invocation,
        cwd=project / "nested",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23, result.stdout + result.stderr
    call = json.loads((project / "call.json").read_text())
    assert call["entry"] == entry
    assert call["argv"] == forwarded
    assert Path(call["cwd"]).resolve() == project.resolve()
    assert call["envpath"] == "unchanged-target-placeholder"
    assert call["venv"] == "unchanged-venv-placeholder"
    if installed_venv:
        assert Path(call["python"]).parent == project / ".venv/bin"
    assert not list((project / "cli").rglob("__pycache__"))
