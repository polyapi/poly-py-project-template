"""Kickstart needs only curl: skills arrive as an archive; Git and VS Code are optional."""

import io
from pathlib import Path
import subprocess
import tarfile

import pytest

from kickstart import kickstart


def skills_archive(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))


@pytest.fixture
def fake_curl(tmp_path, monkeypatch):
    archive = tmp_path / "published.tar.gz"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == "curl" and "=https" in command
        Path(command[command.index("--output") + 1]).write_bytes(archive.read_bytes())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(kickstart.subprocess, "run", run)
    return archive, calls


def test_skills_archive_is_downloaded_with_curl_and_its_root_removed(
    tmp_path, fake_curl
):
    archive, calls = fake_curl
    skills_archive(
        archive,
        {"poly-skills-main/polyapi-python/skills/demo/SKILL.md": b"Synthetic\n"},
    )
    destination = tmp_path / "work" / "skills"
    destination.parent.mkdir()
    kickstart.download_skills(destination, {})
    assert (destination / "polyapi-python/skills/demo/SKILL.md").read_text() == (
        "Synthetic\n"
    )
    assert kickstart.SKILLS_ARCHIVE in calls[0]
    assert not destination.with_name("skills.tar.gz").exists()


def test_skills_archive_cannot_write_outside_its_destination(tmp_path, fake_curl):
    archive, _ = fake_curl
    skills_archive(archive, {"poly-skills-main/../../escaped.txt": b"unsafe\n"})
    destination = tmp_path / "work" / "skills"
    destination.parent.mkdir()
    with pytest.raises(tarfile.TarError):
        kickstart.download_skills(destination, {})
    assert not list(tmp_path.rglob("escaped.txt"))


def test_missing_git_explains_how_to_create_the_repository(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(kickstart, "git_usable", lambda environment=None: False)
    monkeypatch.setattr(
        kickstart.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Git must not run when it is missing"),
    )
    kickstart.initialize_git(tmp_path, {})
    output = capsys.readouterr().out
    assert "Git was not found" in output
    assert "git init -b main && git add ." in output


def test_git_without_identity_initializes_but_does_not_commit(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(kickstart, "git_usable", lambda environment=None: True)
    commands = []

    def run(command, **kwargs):
        commands.append(command[:2])
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(kickstart.subprocess, "run", run)
    kickstart.initialize_git(tmp_path, {})
    assert ["git", "init"] in commands
    assert ["git", "commit"] not in commands
    assert "no commit was made" in capsys.readouterr().out


def test_missing_editor_prints_the_project_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kickstart.shutil, "which", lambda name: None)
    monkeypatch.setattr(kickstart.sys, "platform", "linux")
    kickstart.open_editor(tmp_path, {})
    assert f"Open {tmp_path} in your editor" in capsys.readouterr().out
