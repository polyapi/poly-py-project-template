"""Exercise the public shell entry point without network access or credentials."""

import importlib.util
import io
import os
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import tarfile
import time

import pytest

pty = pytest.importorskip("pty")
ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "kickstart/kickstart.sh"


def run_piped_with_terminal(script, cwd, environment, response=None):
    """Pipe the script to Bash while retaining an interactive controlling TTY."""
    read_fd, write_fd = os.pipe()
    child, terminal = pty.fork()
    if child == 0:
        os.close(write_fd)
        os.dup2(read_fd, 0)
        os.close(read_fd)
        os.chdir(cwd)
        os.execve("/bin/bash", ["bash"], environment)
    os.close(read_fd)
    output = bytearray()
    responded = False
    try:
        with os.fdopen(write_fd, "wb") as stream:
            stream.write(script)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([terminal], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(terminal, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            if (
                response
                and not responded
                and any(line.startswith(response[0]) for line in output.splitlines())
            ):
                if response[1] == "sigint":
                    os.killpg(child, signal.SIGINT)
                else:
                    os.write(terminal, response[1])
                responded = True
        else:
            raise AssertionError("Kickstart did not finish within ten seconds")
        _, status = os.waitpid(child, 0)
        child = None
        return os.waitstatus_to_exitcode(status), output.decode()
    finally:
        if child is not None:
            os.killpg(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(terminal)


@pytest.fixture
def bootstrap_fixture(tmp_path):
    launch = tmp_path / "launch folder"
    launch.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner = b"""#!/usr/bin/env bash
set -eu
test -t 0
printf 'runner cwd=%s\n' "$PWD"
printf ready > "$PWD/project-marker"
exit "${BOOTSTRAP_RUNNER_STATUS:-0}"
"""
    archive = tmp_path / "template.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        entry = tarfile.TarInfo("template-main/kickstart/kickstart.sh")
        entry.size = len(runner)
        entry.mode = 0o755
        stream.addfile(entry, io.BytesIO(runner))
    curl = bin_dir / "curl"
    curl.write_text("""#!/usr/bin/env bash
set -eu
printf '%s\n' "$@" > "$BOOTSTRAP_CURL_LOG"
if [[ "${BOOTSTRAP_DOWNLOAD_MODE:-ok}" == fail ]]; then exit 22; fi
output=''
while [[ $# -gt 0 ]]; do
  if [[ "$1" == --output ]]; then output="$2"; shift; fi
  shift
done
if [[ "${BOOTSTRAP_DOWNLOAD_MODE:-ok}" == invalid ]]; then
  printf invalid > "$output"
else
  cp "$BOOTSTRAP_ARCHIVE" "$output"
fi
""")
    curl.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 99\n")
    uv.chmod(0o755)
    environment = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TMPDIR": str(downloads),
        "BOOTSTRAP_ARCHIVE": str(archive),
        "BOOTSTRAP_CURL_LOG": str(tmp_path / "curl-arguments"),
    }
    return launch, downloads, environment


def test_piped_kickstart_preserves_working_directory_and_terminal(bootstrap_fixture):
    launch, downloads, environment = bootstrap_fixture
    status, output = run_piped_with_terminal(ENTRY.read_bytes(), launch, environment)
    assert status == 0, output
    assert f"runner cwd={launch}" in output
    assert (launch / "project-marker").read_text() == "ready"
    assert list(downloads.iterdir()) == []
    arguments = Path(environment["BOOTSTRAP_CURL_LOG"]).read_text().splitlines()
    assert (
        "https://codeload.github.com/polyapi/poly-py-project-template/"
        "tar.gz/refs/heads/main"
    ) in arguments
    assert "--proto" in arguments and "=https" in arguments


def test_downloaded_wrapper_runs_pinned_python_without_downloading_again(
    bootstrap_fixture,
):
    launch, downloads, environment = bootstrap_fixture
    with tarfile.open(environment["BOOTSTRAP_ARCHIVE"], "w:gz") as stream:
        for name, data in {
            "kickstart/kickstart.sh": ENTRY.read_bytes(),
            "kickstart/toolchain.sh": (ROOT / "kickstart/toolchain.sh").read_bytes(),
            "kickstart/kickstart.py": b"# Synthetic setup entry point\n",
            ".python-version": b"3.12.12\n",
        }.items():
            entry = tarfile.TarInfo("template-main/" + name)
            entry.size = len(data)
            stream.addfile(entry, io.BytesIO(data))
    uv = Path(environment["PATH"].split(":")[0]) / "uv"
    uv.write_text("""#!/usr/bin/env bash
set -eu
if [[ "$1" == --version ]]; then echo 'uv 0.0.0-synthetic'; exit 0; fi
test -t 0
[[ "$1 $2 $3 $4 $5 $6" == 'run --no-project --no-config --python 3.12.12 '* ]]
test -f "$6"
printf 'pinned setup cwd=%s\n' "$PWD"
""")
    status, output = run_piped_with_terminal(ENTRY.read_bytes(), launch, environment)
    assert status == 0, output
    assert f"pinned setup cwd={launch}" in output
    assert output.count("Downloading the public project template") == 1
    assert list(downloads.iterdir()) == []


@pytest.mark.parametrize("mode", ["fail", "invalid"])
def test_download_failure_cleans_temporary_files(bootstrap_fixture, mode):
    launch, downloads, environment = bootstrap_fixture
    environment["BOOTSTRAP_DOWNLOAD_MODE"] = mode
    status, output = run_piped_with_terminal(ENTRY.read_bytes(), launch, environment)
    assert status != 0, output
    assert list(downloads.iterdir()) == []
    assert not (launch / "project-marker").exists()


def test_setup_failure_preserves_created_project_and_cleans_download(bootstrap_fixture):
    launch, downloads, environment = bootstrap_fixture
    environment["BOOTSTRAP_RUNNER_STATUS"] = "17"
    status, output = run_piped_with_terminal(ENTRY.read_bytes(), launch, environment)
    assert status == 17, output
    assert (launch / "project-marker").read_text() == "ready"
    assert list(downloads.iterdir()) == []


def test_missing_terminal_stops_before_downloading(bootstrap_fixture):
    launch, downloads, environment = bootstrap_fixture
    result = subprocess.run(
        ["bash"],
        input=ENTRY.read_bytes(),
        capture_output=True,
        cwd=launch,
        env=environment,
        start_new_session=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert b"interactive terminal" in result.stderr
    assert not Path(environment["BOOTSTRAP_CURL_LOG"]).exists()
    assert list(downloads.iterdir()) == []


def test_public_snapshot_copy_works_without_git_and_excludes_private_paths(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(ROOT / "kickstart"))
    spec = importlib.util.spec_from_file_location(
        "public_snapshot_kickstart", ROOT / "kickstart/kickstart.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "README.md").write_text("Public starter documentation\n")
    private = source / ".poly"
    private.mkdir()
    (private / "local.json").write_text("{}")
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *args, **kwargs: pytest.fail("Snapshot copying must not call Git"),
    )
    target = tmp_path / "project"
    module.copy_checkout(source, target)
    assert (target / "README.md").read_text() == "Public starter documentation\n"
    assert not (target / ".poly").exists()


def test_public_snapshot_rejects_symlinks(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "kickstart"))
    spec = importlib.util.spec_from_file_location(
        "public_snapshot_kickstart", ROOT / "kickstart/kickstart.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "README.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(module.KickstartError, match="symlink"):
        module.copy_checkout(source, tmp_path / "project")


SIGNAL_WORKER = r'''
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

repo = Path(os.environ["SIGNAL_REPO"])
sys.path.insert(0, str(repo / "kickstart"))
spec = importlib.util.spec_from_file_location("signal_kickstart", repo / "kickstart/kickstart.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
folder = Path(os.environ["SIGNAL_FOLDER"])
answers = iter(("signal-project", str(folder), "synthetic", "dev"))
module.input = lambda prompt: next(answers)
module.getpass.getpass = lambda prompt: "synthetic-key"
module.shutil.which = lambda name: "/synthetic/" + name
module.rename_project = lambda *args: None
module.rewrite_namespace = lambda *args: None

def copy_source(source, target):
    (target / "workflows").mkdir()
    (target / "workflows/dev.env.example").write_text("POLYAPI_NAMESPACE=example\nTARGET_ENV_ID=placeholder\n")

module.copy_checkout = copy_source
real_run = subprocess.run

child_code = r"""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0, os.environ["SIGNAL_REPO"])
from kickstart import configure
from cli import setup
root = Path.cwd()
identity = setup.Identity("Synthetic tenant", "Development", "synthetic-env")
setup.fetch_identity = lambda *args: identity
setup.choose_target = lambda *args: "workflows/dev.env"
setup.save_target_configuration = lambda *args: None
saved = (os.environ["POLY_API_KEY"], os.environ["POLY_API_BASE_URL"])
configure.get_api_key_and_url = lambda: saved

def sdk_setup(*args):
    if os.environ["SIGNAL_STAGE"] == "sdk-setup":
        print("SDK_SETUP_WAIT", flush=True)
        input()

setup.run_sdk_setup = sdk_setup

def generate(*args, **kwargs):
    if os.environ["SIGNAL_STAGE"] == "sdk-generation":
        print("SDK_GENERATION_WAIT", flush=True)
        input()
    return SimpleNamespace(returncode=0)

configure.subprocess.run = generate
configure.configure(root)
"""

def fake_skills(skills, environment):
    for category in ("general", "polyapi-common", "polyapi-python"):
        (skills / category / "skills").mkdir(parents=True)
    training = skills / "polyapi-python/skills/api-function-training"
    training.mkdir()
    (training / "SKILL.md").write_text("Synthetic training fixture\n")
    return skills.with_name("skills.tar.gz")

module.download_skills = fake_skills

def controlled_run(command, **kwargs):
    if command[-1:] == ["kickstart.configure"]:
        return real_run([
            os.environ["SIGNAL_UV"], "run", "--offline", "--no-project", "--no-config",
            "--python", os.environ["SIGNAL_PYTHON"], "python", "-B", "-u", "-c", child_code
        ], **kwargs)
    elif command[5:8] == ["cli/flow.py", "skills", "add"]:
        # Flow installs from the archive kickstart downloaded and checked.
        assert command[command.index("--source") + 1].endswith("skills.tar.gz")
        training = Path(kwargs["cwd"]) / ".agents/skills/api-function-training"
        training.mkdir(parents=True)
        (training / "SKILL.md").write_text("Synthetic training fixture\n")
    elif command[:2] == ["git", "init"]:
        (folder / "git-phase").touch()
    elif command[0] == "code":
        (folder / "editor-phase").touch()
    return subprocess.CompletedProcess(command, 0)

module.subprocess.run = controlled_run
try:
    raise SystemExit(module.main())
except (KeyboardInterrupt, EOFError):
    raise SystemExit(130)
'''


@pytest.mark.parametrize(
    "stage, response, expected_status",
    [
        ("profile", "sigint", 0),
        ("profile", b"\x04", 0),
        ("profile", b"skip\n", 0),
        ("sdk-setup", "sigint", 130),
        ("sdk-generation", "sigint", 130),
    ],
)
def test_terminal_cancellation_respects_optional_profile_boundary(
    bootstrap_fixture, stage, response, expected_status
):
    """Use real Bash/uv/PTY signals; SDK, Git and provider operations are mocked."""
    launch, downloads, environment = bootstrap_fixture
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is not installed")
    environment.update(
        SIGNAL_REPO=str(ROOT),
        SIGNAL_FOLDER=str(launch),
        SIGNAL_STAGE=stage,
        SIGNAL_UV=uv,
        SIGNAL_PYTHON=sys.executable,
        SHELL="/bin/bash",
    )
    with tarfile.open(environment["BOOTSTRAP_ARCHIVE"], "w:gz") as stream:
        for name, data in {
            "kickstart/kickstart.sh": ENTRY.read_bytes(),
            "kickstart/toolchain.sh": (ROOT / "kickstart/toolchain.sh").read_bytes(),
            "kickstart/kickstart.py": SIGNAL_WORKER.encode(),
            ".python-version": (ROOT / ".python-version").read_bytes(),
        }.items():
            entry = tarfile.TarInfo("template-main/" + name)
            entry.size = len(data)
            stream.addfile(entry, io.BytesIO(data))
    fake_uv = Path(environment["PATH"].split(":")[0]) / "uv"
    fake_uv.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then exec "$SIGNAL_UV" --version; fi\n'
        'exec "$SIGNAL_UV" run --offline --no-project --no-config '
        '--python "$SIGNAL_PYTHON" python -B -u "$6"\n'
    )
    marker = {
        "profile": b"Shell for optional flow shortcut",
        "sdk-setup": b"SDK_SETUP_WAIT",
        "sdk-generation": b"SDK_GENERATION_WAIT",
    }[stage]
    status, output = run_piped_with_terminal(
        ENTRY.read_bytes(), launch, environment, response=(marker, response)
    )
    assert marker.decode() in output
    assert status == expected_status, output
    completed = stage == "profile"
    assert (launch / "git-phase").exists() == completed
    assert (launch / "editor-phase").exists() == completed
    assert ("Project ready at" in output) == completed
    assert list(downloads.iterdir()) == []


def test_bootstrap_imports_without_sdk_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import kickstart.kickstart; "
            "assert 'polyapi' not in sys.modules; "
            "assert 'dotenv' not in sys.modules; "
            "assert 'requests' not in sys.modules",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
