import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".githooks" / "pre-commit"
GIT = shutil.which("git")


@pytest.fixture
def hook_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "project"
    hooks = repo / ".githooks"
    hooks.mkdir(parents=True)
    shutil.copy2(HOOK, hooks / "pre-commit")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(
        [GIT, "init", "--quiet", "--template=", str(repo)],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [GIT, "config", "--local", "core.hooksPath", ".githooks"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    env["PATH"] = str(bin_dir)
    return repo, env


@pytest.mark.parametrize("check_exit_code", [0, 17])
def test_git_hook_runs_make_check_and_propagates_exit_status(
    hook_repo: tuple[Path, dict[str, str]], check_exit_code: int
) -> None:
    repo, env = hook_repo
    make = Path(env["PATH"]) / "make"
    make.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path('invocation.json').write_text(json.dumps({\n"
        "    'args': sys.argv[1:], 'cwd': os.getcwd()\n"
        "}))\n"
        f"sys.exit({check_exit_code})\n"
    )
    make.chmod(0o755)
    nested = repo / "nested"
    nested.mkdir()

    result = subprocess.run(
        [GIT, "hook", "run", "pre-commit"],
        cwd=nested,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == check_exit_code, result.stderr
    assert json.loads((repo / "invocation.json").read_text()) == {
        "args": ["check"],
        "cwd": str(repo.resolve()),
    }


def test_git_hook_blocks_when_make_is_missing(
    hook_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hook_repo
    result = subprocess.run(
        [GIT, "hook", "run", "pre-commit"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "make is required to run make check" in result.stderr
