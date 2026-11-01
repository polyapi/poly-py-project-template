"""Version reporting needs neither installed SDK dependencies nor target setup."""

from pathlib import Path
import subprocess
import sys

import pytest

from cli import __version__


@pytest.mark.parametrize("flag", ["--version", "-V"])
@pytest.mark.parametrize("entrypoint", ["script", "module", "outside-project"])
def test_version_without_site_packages(
    flag: str, entrypoint: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[2]
    arguments = [str(root / "cli/flow.py")]
    if entrypoint == "module":
        arguments = ["-m", "cli.flow"]
    result = subprocess.run(
        [sys.executable, "-S", *arguments, flag],
        cwd=tmp_path if entrypoint == "outside-project" else root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Flow CLI {__version__}\n"
    assert result.stderr == ""
