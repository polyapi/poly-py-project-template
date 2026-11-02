import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from cli import skills
from cli.skills import (
    SKILLS_LOCK_FILE,
    SKILLS_SOURCE,
    SkillsArgs,
    SkillsError,
    SkillsRuntime,
    fetch_skills,
    format_skills_lock,
    parse_args,
    read_skills_lock,
    run_skills,
)

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is required")


def skill(name: str, body: str | None = None) -> str:
    body = body or name
    return f"---\nname: {name}\ndescription: Use for {body}.\n---\n\n# {body}\n"


UPSTREAM = {
    "general/README.md": "# general\n",
    "general/skills/clean-code/SKILL.md": skill("clean-code"),
    "general/skills/markdownlint-compliance/SKILL.md": skill("markdownlint-compliance"),
    "polyapi-common/skills/polyapi-constructs/SKILL.md": skill("polyapi-constructs"),
    "polyapi-common/skills/polyapi-constructs/references/REFERENCE.md": "# Reference\n",
    "polyapi-common/skills/notes/README.md": "Not a skill: no SKILL.md.\n",
    "polyapi-python/skills/polyapi-sdk/SKILL.md": skill("polyapi-sdk", "Python SDK"),
    "polyapi-python/skills/polyapi-sdk/scripts/check.sh": "#!/bin/sh\n",
    "polyapi-typescript/skills/polyapi-sdk/SKILL.md": skill(
        "polyapi-sdk", "TypeScript SDK"
    ),
    "mulesoft-to-polyapi/skills/compress-source/SKILL.md": skill("compress-source"),
}


class Upstream:
    """A local poly-skills repository; `commit` applies edits (None deletes)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.git("init", "--quiet", "--initial-branch=main", "--template=")
        self.head = self.commit(UPSTREAM)

    def git(self, *args: str) -> str:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="Flow Test",
            GIT_AUTHOR_EMAIL="flow@example.test",
            GIT_COMMITTER_NAME="Flow Test",
            GIT_COMMITTER_EMAIL="flow@example.test",
        )
        result = subprocess.run(
            [GIT, *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def commit(self, files: dict[str, str | None]) -> str:
        for relative, content in files.items():
            path = self.root / relative
            if content is None:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if relative.endswith(".sh"):
                path.chmod(0o755)
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", "Update skills")
        return self.git("rev-parse", "HEAD")


class Run:
    def __init__(
        self, root: Path, interactive: bool = False, answers: list[str] | None = None
    ) -> None:
        self.output: list[str] = []
        self.prompts: list[str] = []
        self.answers = list(answers or [])
        self.runtime = SkillsRuntime(
            root,
            interactive,
            prompt=self.prompt,
            print=self.output.append,
            fetch=fetch_skills,
        )

    def prompt(self, message: str) -> str:
        self.prompts.append(message)
        return self.answers.pop(0) if self.answers else ""

    @property
    def text(self) -> str:
        return "\n".join(self.output)


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path / "poly-skills")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path / "project")


def make_project(root: Path) -> Path:
    (root / ".agents/skills").mkdir(parents=True)
    (root / ".agents/skills/README.md").write_text("# Skills\n", encoding="utf-8")
    return root


def args(upstream: Upstream, *argv: str) -> SkillsArgs:
    return parse_args([*argv, "--source", str(upstream.root)])


def read(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def installed(root: Path) -> list[str]:
    return sorted(path.name for path in (root / ".agents/skills").iterdir())


def lock_skills(root: Path) -> dict:
    lock = read_skills_lock(root)
    assert lock is not None
    return lock["skills"]


def test_parses_skills_commands_and_rejects_unsupported_input(tmp_path):
    assert parse_args([]).command == "help"
    assert parse_args(["add", "--help"]).command == "help"
    assert parse_args(["add", "typescript,Python"]) == SkillsArgs(
        "add", ("typescript", "python"), False, False, SKILLS_SOURCE, "main"
    )
    update = parse_args(
        [
            "update",
            "--dry-run",
            "-y",
            "--ref",
            "release/1.0",
            "--source",
            "git@github.com:example/poly-skills.git",
        ]
    )
    assert (update.dry_run, update.yes, update.ref, update.source) == (
        True,
        True,
        "release/1.0",
        "git@github.com:example/poly-skills.git",
    )
    assert parse_args(["add", "--dry-run", "python"]).setups == ("python",)
    assert parse_args(["add", "--source", str(tmp_path)]).source == str(
        tmp_path.resolve()
    )
    for argv, message in (
        (["add", "java"], 'Unknown skill setup "java"'),
        (["update", "python"], "refreshes the recorded setups"),
        (["remove"], 'Unknown skills command "remove"'),
        (["update", "--ref", "main..other"], "Invalid --ref"),
        (["update", "--source", "https://example.test/skills.git"], "--source must"),
        (["update", "--force"], "Unknown option"),
    ):
        with pytest.raises(SkillsError, match=message):
            parse_args(argv)


def test_add_installs_the_packs_of_the_chosen_setup_and_records_them(upstream, project):
    run = Run(project)
    run_skills(args(upstream, "add", "python"), run.runtime)

    assert installed(project) == [
        "README.md",
        "clean-code",
        "markdownlint-compliance",
        "polyapi-constructs",
        "polyapi-sdk",
    ]
    assert read(project, ".agents/skills/README.md") == "# Skills\n"
    assert "Python SDK" in read(project, ".agents/skills/polyapi-sdk/SKILL.md")
    assert (
        read(project, ".agents/skills/polyapi-constructs/references/REFERENCE.md")
        == "# Reference\n"
    )
    if sys.platform != "win32":
        script = project / ".agents/skills/polyapi-sdk/scripts/check.sh"
        assert script.stat().st_mode & 0o777 == 0o755
    lock = read_skills_lock(project)
    assert lock["setups"] == ["python"]
    assert lock["commit"] == upstream.head
    assert lock["ref"] == "main"
    assert "source" not in lock, "local checkouts are not recorded"
    assert {name: entry["source"] for name, entry in lock["skills"].items()} == {
        "clean-code": "general/skills/clean-code",
        "markdownlint-compliance": "general/skills/markdownlint-compliance",
        "polyapi-constructs": "polyapi-common/skills/polyapi-constructs",
        "polyapi-sdk": "polyapi-python/skills/polyapi-sdk",
    }
    text = read(project, SKILLS_LOCK_FILE)
    assert text == format_skills_lock(lock)
    assert '\n  "setups": ["python"],\n' in text
    assert text.endswith("}\n")
    assert "+ polyapi-sdk" in run.text
    assert "Installed 4 skill folder(s)" in run.text


def test_lock_format_matches_the_typescript_flow_cli():
    lock = {
        "version": 1,
        "setups": ["typescript"],
        "source": SKILLS_SOURCE,
        "ref": "main",
        "commit": "a" * 40,
        "skills": {
            "polyapi-sdk": {"source": "x/skills/polyapi-sdk", "digest": "b" * 64}
        },
    }
    assert format_skills_lock(lock) == (
        "{\n"
        '  "version": 1,\n'
        '  "setups": ["typescript"],\n'
        f'  "source": "{SKILLS_SOURCE}",\n'
        '  "ref": "main",\n'
        f'  "commit": "{"a" * 40}",\n'
        '  "skills": {\n'
        '    "polyapi-sdk": {\n'
        '      "source": "x/skills/polyapi-sdk",\n'
        f'      "digest": "{"b" * 64}"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    # The same files produce the digest the TypeScript CLI records.
    assert skills.digest_files([("b.md", b"two\r\n"), ("a/SKILL.md", b"one\n")]) == (
        skills.digest_files([("a/SKILL.md", b"one\n"), ("b.md", b"two\n")])
    )


def test_update_refreshes_unmodified_skills_and_installs_newly_added_skills(
    upstream, project
):
    run_skills(args(upstream, "add", "python"), Run(project).runtime)
    head = upstream.commit(
        {
            "polyapi-python/skills/polyapi-sdk/SKILL.md": skill(
                "polyapi-sdk", "Python SDK v2"
            ),
            "polyapi-python/skills/api-function-training/SKILL.md": skill(
                "api-function-training"
            ),
            "general/skills/clean-code": None,
        }
    )

    run = Run(project)
    run_skills(args(upstream, "update"), run.runtime)

    assert "Python SDK v2" in read(project, ".agents/skills/polyapi-sdk/SKILL.md")
    assert (project / ".agents/skills/api-function-training/SKILL.md").is_file()
    assert (
        project / ".agents/skills/clean-code/SKILL.md"
    ).is_file(), "skills that leave a setup are left in place"
    assert "+ api-function-training" in run.text
    assert "~ polyapi-sdk" in run.text
    assert "- clean-code" in run.text
    assert "no longer in the selected setups" in run.text
    assert "Installed 1, updated 1 skill folder(s)" in run.text
    assert read_skills_lock(project)["commit"] == head
    assert list(lock_skills(project)) == [
        "api-function-training",
        "clean-code",
        "markdownlint-compliance",
        "polyapi-constructs",
        "polyapi-sdk",
    ]
    assert not any(name.startswith(".flow-staging-") for name in installed(project))

    shutil.rmtree(project / ".agents/skills/clean-code")
    again = Run(project)
    run_skills(args(upstream, "update"), again.runtime)
    assert "are up to date" in again.text
    assert "clean-code" not in lock_skills(project)


def test_local_changes_are_kept_until_upstream_changes_and_replacement_is_confirmed(
    upstream, project
):
    run_skills(args(upstream, "add", "python"), Run(project).runtime)
    installed_digest = lock_skills(project)["polyapi-sdk"]["digest"]
    local_edit = skill("polyapi-sdk", "Python SDK") + "\nProject note.\n"
    skill_file = project / ".agents/skills/polyapi-sdk/SKILL.md"
    skill_file.write_text(local_edit, encoding="utf-8")

    customized = Run(project, interactive=True)
    run_skills(args(upstream, "update"), customized.runtime)
    assert skill_file.read_text(encoding="utf-8") == local_edit
    assert "* polyapi-sdk" in customized.text
    assert customized.prompts == [], "nothing to replace yet"

    upstream.commit(
        {
            "polyapi-python/skills/polyapi-sdk/SKILL.md": skill(
                "polyapi-sdk", "Python SDK v2"
            )
        }
    )
    unattended = Run(project)
    run_skills(args(upstream, "update"), unattended.runtime)
    assert skill_file.read_text(encoding="utf-8") == local_edit
    assert "! polyapi-sdk" in unattended.text
    assert "local changes, and updated in poly-skills" in unattended.text
    assert "Kept 1 folder(s) marked !" in unattended.text
    assert lock_skills(project)["polyapi-sdk"]["digest"] == installed_digest

    declined = Run(project, interactive=True, answers=["n"])
    run_skills(args(upstream, "update"), declined.runtime)
    assert len(declined.prompts) == 1
    assert skill_file.read_text(encoding="utf-8") == local_edit

    accepted = Run(project, interactive=True, answers=["y"])
    run_skills(args(upstream, "update"), accepted.runtime)
    assert "Python SDK v2" in skill_file.read_text(encoding="utf-8")
    assert "Replaced 1 skill folder(s)" in accepted.text
    assert lock_skills(project)["polyapi-sdk"]["digest"] != installed_digest


def test_existing_folders_are_adopted_when_identical_and_otherwise_replaced_on_request(
    upstream, project
):
    folder = project / ".agents/skills"
    (folder / "clean-code").mkdir()
    (folder / "clean-code/SKILL.md").write_text(
        UPSTREAM["general/skills/clean-code/SKILL.md"], encoding="utf-8"
    )
    (folder / "polyapi-sdk").mkdir()
    (folder / "polyapi-sdk/SKILL.md").write_text("A reviewed copy.\n", "utf-8")
    (folder / "polyapi-sdk/stray.md").write_text("Old.\n", encoding="utf-8")

    kept = Run(project)
    run_skills(args(upstream, "add", "python"), kept.runtime)
    assert "! polyapi-sdk" in kept.text
    assert "existing folder that Flow did not install" in kept.text
    assert "clean-code" not in kept.text
    assert list(lock_skills(project)) == [
        "clean-code",
        "markdownlint-compliance",
        "polyapi-constructs",
    ]
    assert read(project, ".agents/skills/polyapi-sdk/SKILL.md") == "A reviewed copy.\n"

    run_skills(args(upstream, "add", "python", "--yes"), Run(project).runtime)
    assert "Python SDK" in read(project, ".agents/skills/polyapi-sdk/SKILL.md")
    assert not (folder / "polyapi-sdk/stray.md").exists()
    assert "polyapi-sdk" in lock_skills(project)


def test_crlf_checkouts_and_file_browser_metadata_are_not_local_changes(
    upstream, project
):
    run_skills(args(upstream, "add", "python"), Run(project).runtime)
    skill_file = project / ".agents/skills/polyapi-constructs/SKILL.md"
    skill_file.write_bytes(skill_file.read_bytes().replace(b"\n", b"\r\n"))
    (project / ".agents/skills/polyapi-constructs/.DS_Store").write_text("metadata")

    run = Run(project)
    run_skills(args(upstream, "update"), run.runtime)
    assert "4 unchanged" in run.text
    assert "are up to date" in run.text


def test_update_needs_a_setup_and_clashing_setups_cannot_be_combined(upstream, project):
    with pytest.raises(SkillsError, match="No skill setup is recorded"):
        run_skills(args(upstream, "update"), Run(project).runtime)

    run_skills(args(upstream, "add", "python"), Run(project).runtime)
    lock_before = read(project, SKILLS_LOCK_FILE)
    with pytest.raises(
        SkillsError,
        match=(
            r"same folder name: polyapi-sdk \(polyapi-typescript/skills/polyapi-sdk "
            r"for Coding in TypeScript, polyapi-python/skills/polyapi-sdk for Coding "
            r"in Python\)"
        ),
    ):
        run_skills(args(upstream, "add", "typescript"), Run(project).runtime)
    assert read(project, SKILLS_LOCK_FILE) == lock_before
    assert "Python SDK" in read(project, ".agents/skills/polyapi-sdk/SKILL.md")


def test_add_asks_what_the_skills_are_needed_for(upstream, tmp_path):
    recommended = make_project(tmp_path / "recommended")
    first = Run(recommended, interactive=True, answers=[""])
    run_skills(args(upstream, "add"), first.runtime)
    assert "What do you need agent skills for?" in first.text
    assert "  1) Coding in TypeScript" in first.output
    assert "  2) Coding in Python (recommended for this project)" in first.output
    assert first.prompts == ["Select one or more, separated by commas [2]: "]
    assert read_skills_lock(recommended)["setups"] == ["python"]

    typescript = make_project(tmp_path / "typescript")
    second = Run(typescript, interactive=True, answers=["3", "1"])
    run_skills(args(upstream, "add"), second.runtime)
    assert 'Unknown skill setup "3"' in second.text
    assert read_skills_lock(typescript)["setups"] == ["typescript"]
    assert "TypeScript SDK" in read(typescript, ".agents/skills/polyapi-sdk/SKILL.md")

    with pytest.raises(SkillsError, match="flow skills add python"):
        run_skills(args(upstream, "add"), Run(make_project(tmp_path / "none")).runtime)


def test_dry_run_prints_the_plan_without_changing_files(upstream, project):
    run = Run(project)
    run_skills(args(upstream, "add", "python", "--dry-run"), run.runtime)
    assert "+ polyapi-sdk" in run.text
    assert "Dry run: no files were changed." in run.text
    assert installed(project) == ["README.md"]
    assert not (project / SKILLS_LOCK_FILE).exists()


def test_fetch_refuses_links_protected_files_and_missing_packs(tmp_path):
    linked = Upstream(tmp_path / "linked")
    (linked.root / "general/skills/clean-code/linked.md").symlink_to(
        "../../../general/README.md"
    )
    linked.commit({})
    with pytest.raises(SkillsError, match="Refusing a link or unsupported entry"):
        fetch_skills(str(linked.root), "main", ("general",))

    secret = Upstream(tmp_path / "secret")
    secret.commit({"general/skills/clean-code/.env": "TOKEN=placeholder\n"})
    with pytest.raises(SkillsError, match="Refusing a protected or unsafe skill path"):
        fetch_skills(str(secret.root), "main", ("general",))

    missing = Upstream(tmp_path / "missing")
    missing.commit({"polyapi-python": None})
    with pytest.raises(SkillsError, match="has no skills in polyapi-python/skills"):
        fetch_skills(str(missing.root), "main", ("general", "polyapi-python"))
    with pytest.raises(SkillsError, match="Fetching no-such-branch from .* failed"):
        fetch_skills(str(missing.root), "no-such-branch", ("general",))


def test_rejects_lock_files_it_cannot_trust(project):
    lock_file = project / SKILLS_LOCK_FILE
    valid = {
        "version": 1,
        "setups": ["python"],
        "ref": "main",
        "commit": "a" * 40,
        "skills": {"polyapi-sdk": {"source": "x", "digest": "b" * 64}},
    }

    def attempt(value):
        lock_file.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )
        return read_skills_lock(project)

    assert attempt(valid) == valid
    for value, message in (
        ("{", "is not valid JSON"),
        ({**valid, "version": 2}, "newer Flow version"),
        ({**valid, "setups": ["java"]}, 'records the "java" setup'),
        (
            {**valid, "skills": {"../escape": valid["skills"]["polyapi-sdk"]}},
            'invalid entry for "../escape"',
        ),
        (
            {**valid, "skills": {"polyapi-sdk": {"source": "x"}}},
            'invalid entry for "polyapi-sdk"',
        ),
    ):
        with pytest.raises(SkillsError, match=message):
            attempt(value)


def test_flow_dispatches_skills_without_an_environment(tmp_path, monkeypatch, capsys):
    from cli import flow

    monkeypatch.setattr(sys, "argv", ["flow.py", "skills", "--help"])
    assert flow.main() == 0
    assert "flow skills add [setup ...]" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["flow.py", "skills", "remove"])
    with pytest.raises(flow.DeployError, match='Unknown skills command "remove"'):
        flow.main()


def test_closed_terminal_input_cancels_the_prompt(monkeypatch):
    def end_of_input(message):
        raise EOFError

    monkeypatch.setattr("builtins.input", end_of_input)
    with pytest.raises(SkillsError, match="Prompt cancelled"):
        skills.prompt_terminal("Select: ")
