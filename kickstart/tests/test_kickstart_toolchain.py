"""Exercise uv installation with stub tools; no network or real installer runs."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLCHAIN = ROOT / "kickstart/toolchain.sh"
SYSTEM_TOOLS = (
    "bash",
    "cat",
    "chmod",
    "dirname",
    "env",
    "grep",
    "mkdir",
    "mktemp",
    "rm",
    "sh",
)
UV_STUB = "#!/bin/sh\necho 'uv 0.0.0-synthetic'\n"


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def toolchain(tmp_path):
    """Run toolchain.sh with only stubs and base system tools on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    for tool in SYSTEM_TOOLS:
        found = shutil.which(tool, path="/usr/bin:/bin")
        assert found, f"missing system tool: {tool}"
        (bin_dir / tool).symlink_to(found)
    # The stub installer places uv where the real installer would.
    executable(
        bin_dir / "curl",
        f"""#!/bin/sh
echo "curl $*" >> "$TOOLCHAIN_LOG"
[ -z "${{TOOLCHAIN_CURL_FAIL:-}}" ] || exit 22
while [ "$#" -gt 0 ]; do
  if [ "$1" = --output ]; then out="$2"; fi
  shift
done
cat > "$out" <<'INSTALLER'
echo installer >> "$TOOLCHAIN_LOG"
target="${{XDG_BIN_HOME:-$HOME/.local/bin}}"
mkdir -p "$target"
cat > "$target/uv" <<'UV'
{UV_STUB}UV
chmod +x "$target/uv"
INSTALLER
""",
    )

    def run(**extra):
        environment = {
            "HOME": str(tmp_path),
            "PATH": str(bin_dir),
            "TMPDIR": str(tmp_path),
            "TOOLCHAIN": str(TOOLCHAIN),
            "TOOLCHAIN_LOG": str(log),
            **extra,
        }
        return subprocess.run(
            [
                str(bin_dir / "bash"),
                "-c",
                'set -euo pipefail\nsource "$TOOLCHAIN"\nensure_uv\n'
                'echo "installed=$toolchain_installed"\n'
                'echo "dir=${KICKSTART_UV_INSTALLED_DIR:-}"\nuv --version',
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    return run, bin_dir, log, tmp_path


def test_working_uv_is_left_untouched(toolchain):
    run, bin_dir, log, _ = toolchain
    executable(bin_dir / "uv", UV_STUB)
    result = run()
    assert result.returncode == 0, result.stderr
    assert "installed=false" in result.stdout
    assert log.read_text() == ""


def test_missing_uv_is_installed_for_the_user_and_put_on_path(toolchain):
    run, _, log, home = toolchain
    result = run()
    assert result.returncode == 0, result.stderr
    assert "installed=true" in result.stdout
    assert f"dir={home}/.local/bin" in result.stdout
    assert "uv 0.0.0-synthetic" in result.stdout
    calls = log.read_text()
    assert "--proto =https https://astral.sh/uv/install.sh" in calls
    assert "installer" in calls
    assert list(home.glob("kickstart-uv.*")) == []


def test_installer_location_follows_xdg_bin_home(toolchain):
    run, _, _, home = toolchain
    target = home / "custom-bin"
    result = run(XDG_BIN_HOME=str(target))
    assert result.returncode == 0, result.stderr
    assert f"dir={target}" in result.stdout


def test_broken_uv_is_replaced_and_download_failure_stops_cleanly(toolchain):
    run, bin_dir, log, home = toolchain
    executable(bin_dir / "uv", "#!/bin/sh\nexit 1\n")
    result = run(TOOLCHAIN_CURL_FAIL="1")
    assert result.returncode != 0
    assert "could not download the uv installer" in result.stderr
    assert "installer" not in log.read_text().replace("uv/install.sh", "")
    assert list(home.glob("kickstart-uv.*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell behavior")
def test_toolchain_script_parses():
    assert subprocess.run(["bash", "-n", str(TOOLCHAIN)]).returncode == 0
