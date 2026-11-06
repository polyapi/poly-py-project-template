"""Install and refresh PolyAPI agent skills from the poly-skills repository.

A setup (what the skills are needed for) selects whole poly-skills packs, so a
skill added to one of those packs upstream is installed by the next update.
The selected setups, the fetched revision and a content digest per installed
skill are recorded in `.agents/flow-skills.json`. The digests separate
unmodified Flow-installed folders, which are refreshed, from local edits and
folders Flow did not install, which are replaced only after confirmation.

The setups, lock format and digest match the TypeScript Flow CLI, so either
CLI reads the record the other wrote.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Callable, Iterable, Literal
import urllib.parse
import uuid
import zlib

import requests

# poly-skills is public: its archive downloads over HTTPS without Git or SSH.
SKILLS_SOURCE = "https://github.com/polyapi/poly-skills"
SKILLS_REF = "main"
SKILLS_DIRECTORY = ".agents/skills"
SKILLS_LOCK_FILE = ".agents/flow-skills.json"
LOCK_VERSION = 1


@dataclass(frozen=True)
class SkillSetup:
    id: str
    label: str
    # poly-skills packs whose `skills/` folders make up this setup.
    packs: tuple[str, ...]


SKILL_SETUPS = (
    SkillSetup(
        "typescript",
        "Coding in TypeScript",
        ("general", "polyapi-common", "polyapi-typescript"),
    ),
    SkillSetup(
        "python", "Coding in Python", ("general", "polyapi-common", "polyapi-python")
    ),
)
# The default answer to the setup prompt in this Python template.
RECOMMENDED_SETUP = "python"

HTTPS_SOURCE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SSH_SOURCE = re.compile(r"^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
ARCHIVE_FILE = re.compile(r"\.(?:tar\.gz|tgz)$", re.I)
COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
# Far above any skills snapshot; a larger archive is refused, not read.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
PROTECTED_SUFFIX = re.compile(
    r"(^\.env($|\.)|\.env($|\.)|\.(key|pem|p12|pfx|crt)$)", re.I
)
PROTECTED_NAME = re.compile(
    r"^(\.git|\.poly|\.ssh|\.aws|\.azure|\.config|\.netrc|\.npmrc|\.pypirc|env"
    r"|credentials(?:\.json)?|secrets(?:\.json)?|id_rsa|id_ed25519)$",
    re.I,
)
# Created by file browsers; they do not make a skill locally modified.
IGNORED_LOCAL_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# Never equal to a real digest, so a link or file in a skill's place is only
# replaced with confirmation, and links are not followed.
NOT_A_FOLDER = "not-a-folder"
NOT_A_REGULAR_FILE = b"\0not-a-regular-file"

PlanKind = Literal[
    "install",
    "update",
    "unchanged",
    # Edited locally; poly-skills has nothing newer, so it is kept as is.
    "customized",
    # Edited locally and changed upstream: replaced only when confirmed.
    "modified",
    # Present but not installed by Flow: replaced only when confirmed.
    "unmanaged",
]
CONFLICTS = ("modified", "unmanaged")
MARKERS = {
    "install": "+",
    "update": "~",
    "unchanged": "=",
    "customized": "*",
    "modified": "!",
    "unmanaged": "!",
}


class SkillsError(ValueError):
    """A safe, actionable skills command failure."""


@dataclass(frozen=True)
class SkillsArgs:
    command: Literal["add", "update", "help"]
    setups: tuple[str, ...] = ()
    dry_run: bool = False
    yes: bool = False
    source: str = SKILLS_SOURCE
    ref: str = SKILLS_REF


@dataclass(frozen=True)
class SkillFile:
    # POSIX path relative to the skill folder.
    path: str
    content: bytes
    executable: bool = False


@dataclass(frozen=True)
class UpstreamSkill:
    name: str
    # Folder in poly-skills, for example `polyapi-python/skills/polyapi-sdk`.
    source: str
    files: tuple[SkillFile, ...]
    digest: str


@dataclass(frozen=True)
class SkillsSnapshot:
    commit: str
    skills: tuple[UpstreamSkill, ...]


@dataclass(frozen=True)
class PlannedSkill:
    kind: PlanKind
    skill: UpstreamSkill
    recorded: dict[str, str] | None = None


@dataclass(frozen=True)
class SkillsPlan:
    skills: tuple[PlannedSkill, ...]
    # Flow-installed folders that are no longer part of the selected setups.
    retired: tuple[tuple[str, dict[str, str]], ...]


def prompt_terminal(message: str) -> str:
    try:
        return input(message)
    except EOFError:
        # Ctrl+D or closed input ends the command instead of a traceback.
        raise SkillsError("Prompt cancelled.") from None


@dataclass
class SkillsRuntime:
    project_root: Path
    interactive: bool
    prompt: Callable[[str], str] = prompt_terminal
    print: Callable[[str], None] = print
    fetch: Callable[[str, str, tuple[str, ...]], SkillsSnapshot] = field(
        default=lambda source, ref, packs: fetch_skills(source, ref, packs)
    )


def setup_by_id(setup_id: str) -> SkillSetup:
    for setup in SKILL_SETUPS:
        if setup.id == setup_id:
            return setup
    raise SkillsError(f'Unknown skill setup "{setup_id}".')


def setup_choices() -> str:
    return ", ".join(f"{setup.id} ({setup.label})" for setup in SKILL_SETUPS)


def normalize_setups(names: Iterable[str]) -> tuple[str, ...]:
    """Return known setup IDs in catalogue order, without duplicates."""
    requested = [part.strip().lower() for name in names for part in name.split(",")]
    requested = [name for name in requested if name]
    for name in requested:
        if not any(setup.id == name for setup in SKILL_SETUPS):
            raise SkillsError(
                f'Unknown skill setup "{name}". Choose {setup_choices()}.'
            )
    return tuple(setup.id for setup in SKILL_SETUPS if setup.id in requested)


def https_repository(source: str) -> tuple[str, str] | None:
    match = HTTPS_SOURCE.match(source)
    if not match or any(set(part) == {"."} for part in match.groups()):
        return None
    return match.group(1), match.group(2)


def is_remote_source(source: str) -> bool:
    return https_repository(source) is not None or bool(SSH_SOURCE.match(source))


def resolve_source(value: str | None) -> str:
    if value is None:
        return SKILLS_SOURCE
    repository = https_repository(value)
    if repository:
        return "https://github.com/" + "/".join(repository)
    if SSH_SOURCE.match(value):
        return value
    path = Path(value).expanduser()
    if not (path.is_dir() or (path.is_file() and ARCHIVE_FILE.search(path.name))):
        raise SkillsError(
            "--source must be https://github.com/owner/repository, "
            "git@github.com:owner/repository.git, a local Git repository, or a "
            ".tar.gz archive downloaded from GitHub."
        )
    return str(path.resolve())


def parse_args(argv: list[str]) -> SkillsArgs:
    parser = argparse.ArgumentParser(
        prog="flow skills", add_help=False, exit_on_error=False
    )
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--ref")
    parser.add_argument("words", nargs="*")
    try:
        values, unknown = parser.parse_known_intermixed_args(argv)
    except argparse.ArgumentError as error:
        raise SkillsError(f"{error}. Run flow skills --help for usage.") from None
    if unknown:
        raise SkillsError(
            f"Unknown option(s) {' '.join(unknown)}. Run flow skills --help for usage."
        )
    words = list(values.words)
    if values.help or not words:
        return SkillsArgs("help")
    command, names = words[0], words[1:]
    if command not in ("add", "update"):
        raise SkillsError(
            f'Unknown skills command "{command}". '
            "Use flow skills add or flow skills update."
        )
    if command == "update" and names:
        raise SkillsError(
            "flow skills update refreshes the recorded setups. "
            "Use flow skills add <setup> to add one."
        )
    ref = SKILLS_REF if values.ref is None else values.ref
    if not REF.match(ref) or ".." in ref:
        raise SkillsError("Invalid --ref. Use a branch, tag, or commit ID.")
    return SkillsArgs(
        command,
        normalize_setups(names),
        values.dry_run,
        values.yes,
        resolve_source(values.source),
        ref,
    )


def skills_help() -> str:
    setups = "\n".join(
        f"  {setup.id:<11} {setup.label}: {', '.join(setup.packs)} packs"
        for setup in SKILL_SETUPS
    )
    return f"""Install and update PolyAPI agent skills from poly-skills.

Usage:
  flow skills add [setup ...] [--dry-run] [--yes]
  flow skills update [--dry-run] [--yes]

Setups (what the skills are needed for):
{setups}

add     Asks what the skills are needed for when no setup is named, installs
        each skill folder into {SKILLS_DIRECTORY}/ and records the setup in
        {SKILLS_LOCK_FILE}. Setups already recorded are kept, and all skills
        are refreshed to the same poly-skills revision.
update  Refreshes the skills of the recorded setups and installs skills that
        poly-skills has since added to them.

Options:
  --dry-run        Show the plan without changing files
  -y, --yes        Without asking, replace folders that have local changes or
                   were not installed by Flow when poly-skills differs
  --source SOURCE  https://github.com/owner/repository (public; no Git needed),
                   git@github.com:owner/repository.git, a local Git repository
                   (committed files only), or a .tar.gz archive downloaded
                   from GitHub (default: {SKILLS_SOURCE})
  --ref REF        Branch, tag, or commit to fetch (default: {SKILLS_REF});
                   for an archive file, the ref it was downloaded from
  -h, --help       Show this help

Local edits are kept until poly-skills changes that skill. Delete a skill
folder and run update to reinstall it. Skills that leave a setup are reported
and left in place. No PolyAPI connection or environment file is used."""


def protected_name(name: str) -> bool:
    return bool(PROTECTED_SUFFIX.search(name) or PROTECTED_NAME.match(name))


def digest_files(files: Iterable[tuple[str, bytes]]) -> str:
    """Digest of a skill folder's paths and contents.

    CRLF is read as LF so a Windows checkout of committed skills is not
    reported as modified.
    """
    digest = hashlib.sha256()
    for path, content in sorted(files, key=lambda item: item[0]):
        content_digest = hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()
        digest.update(f"{path}\0{content_digest}\n".encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class SnapshotEntry:
    """A file or other entry of a poly-skills snapshot, relative to its root."""

    filename: str
    # Only regular files are installed; links and other entries are refused.
    regular: bool
    executable: bool
    read: Callable[[], bytes]


def collect_skills(
    entries: Iterable[SnapshotEntry], commit: str, ref: str, packs: tuple[str, ...]
) -> SkillsSnapshot:
    """Validate the entries under the packs' `skills/` folders and group them."""
    folders: dict[str, tuple[str, list[SkillFile]]] = {}
    for entry in entries:
        filename = entry.filename
        parts = filename.split("/")
        if len(parts) < 3 or parts[0] not in packs or parts[1] != "skills":
            raise SkillsError(f"Unexpected poly-skills path: {filename}")
        pack, _skills, name, *rest = parts
        # Files directly under <pack>/skills/, such as a README, are not skills.
        if not rest:
            continue
        if not SKILL_NAME.match(name):
            raise SkillsError(
                "Refusing a skill folder with an unsupported name: "
                f"{pack}/skills/{name}"
            )
        if "\\" in filename or any(
            not part or part in (".", "..") or protected_name(part) for part in rest
        ):
            raise SkillsError(f"Refusing a protected or unsafe skill path: {filename}")
        if not entry.regular:
            raise SkillsError(
                f"Refusing a link or unsupported entry in poly-skills: {filename}"
            )
        key = f"{pack}/skills/{name}"
        folders.setdefault(key, (name, []))[1].append(
            SkillFile("/".join(rest), entry.read(), entry.executable)
        )
    skills = tuple(
        UpstreamSkill(
            name,
            key,
            tuple(files),
            digest_files((file.path, file.content) for file in files),
        )
        for key, (name, files) in folders.items()
        if any(file.path == "SKILL.md" for file in files)
    )
    for pack in packs:
        if not any(skill.source.startswith(f"{pack}/skills/") for skill in skills):
            raise SkillsError(f"poly-skills {ref} has no skills in {pack}/skills.")
    return SkillsSnapshot(commit, skills)


# Git's repository-local variables (`git rev-parse --local-env-vars`). Inherited
# from a hook or wrapper, they would send the temporary shallow fetch into that
# repository and truncate its history, so Flow clears them as Git does for
# submodules. Credential and SSH settings such as GIT_SSH_COMMAND are kept.
LOCAL_GIT_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)


def fetch_git_skills(source: str, ref: str, packs: tuple[str, ...]) -> SkillsSnapshot:
    """Fetch committed skill folders with Git, without a checkout."""
    environment = {
        key: value for key, value in os.environ.items() if key not in LOCAL_GIT_ENV
    }
    with tempfile.TemporaryDirectory(prefix="flow-skills-") as repository:

        def git(args: list[str], label: str) -> bytes:
            try:
                result = subprocess.run(
                    ["git", "-C", repository, *args],
                    env=environment,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise SkillsError(f"{label} failed: {type(error).__name__}.") from None
            if result.returncode != 0:
                lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
                detail = " ".join(lines[-2:])
                raise SkillsError(f"{label} failed{': ' + detail if detail else '.'}")
            return result.stdout

        git(["init", "--quiet"], "Git init")
        git(
            ["fetch", "--quiet", "--depth=1", "--no-tags", "--", source, ref],
            f"Fetching {ref} from {source} (check repository access and --ref)",
        )
        commit = (
            git(
                ["rev-parse", "--verify", "FETCH_HEAD^{commit}"],
                "Resolving the fetched commit",
            )
            .decode()
            .strip()
        )
        listing = git(
            [
                "ls-tree",
                "-r",
                "-z",
                commit,
                "--",
                *(f"{pack}/skills" for pack in packs),
            ],
            "Listing poly-skills",
        ).decode("utf-8")

        def tree_entry(entry: str) -> SnapshotEntry:
            match = re.fullmatch(r"(\d+) (\w+) ([0-9a-f]+)\t(.+)", entry, re.S)
            if not match:
                raise SkillsError("Unexpected Git tree entry in poly-skills.")
            mode, kind, obj, filename = match.groups()
            return SnapshotEntry(
                filename,
                kind == "blob" and mode in ("100644", "100755"),
                mode == "100755",
                lambda: git(["cat-file", "blob", obj], f"Reading {filename}"),
            )

        return collect_skills(
            [tree_entry(entry) for entry in filter(None, listing.split("\0"))],
            commit,
            ref,
            packs,
        )


def read_skills_archive(
    archive: bytes, ref: str, packs: tuple[str, ...]
) -> SkillsSnapshot:
    """Read the skills of the given packs from a `.tar.gz` archive of poly-skills.

    GitHub and `git archive` record the commit ID in the archive's global pax
    `comment` header.
    """
    invalid = "The skills archive is not a valid .tar.gz file."
    prefixes = tuple(f"{pack}/skills/" for pack in packs)
    entries: list[SnapshotEntry] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            members = bundle.getmembers()
            if sum(member.size for member in members) > MAX_ARCHIVE_BYTES:
                raise SkillsError("The skills archive is too large.")
            commit = bundle.pax_headers.get("comment", "")
            for member in members:
                # GitHub archives hold one root folder, such as poly-skills-main/.
                filename = "/".join(member.name.split("/")[1:])
                if member.isdir() or not filename.startswith(prefixes):
                    continue
                content = b""
                if member.isfile():
                    stream = bundle.extractfile(member)
                    content = stream.read() if stream else b""
                entries.append(
                    SnapshotEntry(
                        filename,
                        member.isfile(),
                        bool(member.mode & 0o111),
                        lambda content=content: content,
                    )
                )
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise SkillsError(invalid) from None
    if not COMMIT.match(commit):
        raise SkillsError(
            "The skills archive does not record its commit. "
            "Use an archive downloaded from GitHub."
        )
    return collect_skills(entries, commit, ref, packs)


def download_archive(source: str, ref: str) -> bytes:
    """Download a public GitHub repository's archive; no Git or sign-in needed."""
    repository = https_repository(source)
    if repository is None:
        raise SkillsError(f"Unsupported skills source: {source}")
    owner, name = repository
    archive_ref = "/".join(urllib.parse.quote(part, safe="") for part in ref.split("/"))
    url = f"https://codeload.github.com/{owner}/{name}/tar.gz/{archive_ref}"
    failed = f"Downloading {ref} from {source} failed"
    try:
        with requests.get(url, timeout=(30, 180), stream=True) as response:
            if not response.ok:
                raise SkillsError(
                    f"{failed} (HTTP {response.status_code}). Check the repository "
                    "and --ref; for a private repository, use --source "
                    f"git@github.com:{owner}/{name}.git."
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise SkillsError(f"{failed}: the archive is too large.")
                chunks.append(chunk)
    except requests.RequestException as error:
        raise SkillsError(f"{failed}: {type(error).__name__}.") from None
    return b"".join(chunks)


def fetch_skills(source: str, ref: str, packs: tuple[str, ...]) -> SkillsSnapshot:
    """Fetch the skill folders of the given packs.

    Sources are a public GitHub archive over HTTPS, a downloaded archive file,
    or, with Git, an SSH repository or local checkout.
    """
    if https_repository(source):
        return read_skills_archive(download_archive(source, ref), ref, packs)
    path = Path(source)
    if not SSH_SOURCE.match(source) and ARCHIVE_FILE.search(source) and path.is_file():
        return read_skills_archive(path.read_bytes(), ref, packs)
    return fetch_git_skills(source, ref, packs)


def resolve_setup_skills(
    snapshot: SkillsSnapshot, setups: tuple[str, ...]
) -> tuple[UpstreamSkill, ...]:
    """Select the skills of the setups, rejecting two skills with one name."""
    selected: dict[str, tuple[UpstreamSkill, str]] = {}
    collisions: list[str] = []
    for setup_id in setups:
        setup = setup_by_id(setup_id)
        for pack in setup.packs:
            for skill in snapshot.skills:
                if not skill.source.startswith(f"{pack}/skills/"):
                    continue
                existing = selected.get(skill.name)
                if existing is None:
                    selected[skill.name] = (skill, setup.label)
                elif existing[0].source != skill.source:
                    collisions.append(
                        f"{skill.name} ({existing[0].source} for {existing[1]}, "
                        f"{skill.source} for {setup.label})"
                    )
    if collisions:
        raise SkillsError(
            "These setups install different skills with the same folder name: "
            f"{'; '.join(collisions)}. A project can use only one of them."
        )
    return tuple(skill for _, (skill, _label) in sorted(selected.items()))


def local_digest(folder: Path) -> str | None:
    """Digest of an installed skill folder, or None when it does not exist."""
    if not folder.is_symlink() and not folder.exists():
        return None
    if folder.is_symlink() or not folder.is_dir():
        return NOT_A_FOLDER
    files: list[tuple[str, bytes]] = []

    def visit(directory: Path, prefix: str) -> None:
        for entry in directory.iterdir():
            if entry.name in IGNORED_LOCAL_FILES:
                continue
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            # Protected entries are not read; poly-skills never contains them.
            if protected_name(entry.name) or entry.is_symlink():
                files.append((relative, NOT_A_REGULAR_FILE))
            elif entry.is_dir():
                visit(entry, relative)
            elif entry.is_file():
                files.append((relative, entry.read_bytes()))
            else:
                files.append((relative, NOT_A_REGULAR_FILE))

    visit(folder, "")
    return digest_files(files)


def plan_skills(
    project_root: Path,
    desired: tuple[UpstreamSkill, ...],
    lock: dict[str, Any] | None,
) -> SkillsPlan:
    directory = project_root / SKILLS_DIRECTORY
    recorded_skills: dict[str, dict[str, str]] = (lock or {}).get("skills", {})
    planned = []
    for skill in desired:
        local = local_digest(directory / skill.name)
        recorded = recorded_skills.get(skill.name)
        kind: PlanKind
        if local is None:
            kind = "install"
        elif local == skill.digest:
            kind = "unchanged"
        elif recorded is None:
            kind = "unmanaged"
        elif local == recorded["digest"]:
            kind = "update"
        elif skill.digest == recorded["digest"]:
            kind = "customized"
        else:
            kind = "modified"
        planned.append(PlannedSkill(kind, skill, recorded))
    names = {skill.name for skill in desired}
    retired = tuple(
        (name, entry)
        for name, entry in recorded_skills.items()
        if name not in names
        and ((directory / name).is_symlink() or (directory / name).exists())
    )
    return SkillsPlan(tuple(planned), retired)


def apply_skill_writes(project_root: Path, writes: list[UpstreamSkill]) -> None:
    """Swap the new skill folders into place, restoring the old ones on failure."""
    if not writes:
        return
    directory = project_root / SKILLS_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    # Staging inside the skills folder keeps every rename on one file system.
    staging = directory / f".flow-staging-{uuid.uuid4()}"
    incoming = staging / "incoming"
    previous = staging / "previous"
    previous.mkdir(parents=True)
    swapped: list[tuple[Path, Path | None]] = []
    keep_staging = False
    try:
        for skill in writes:
            for file in skill.files:
                target = incoming / skill.name / Path(*file.path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file.content)
                target.chmod(0o755 if file.executable else 0o644)
        for skill in writes:
            target = directory / skill.name
            backup = None
            if target.is_symlink() or target.exists():
                backup = previous / skill.name
                os.rename(target, backup)
            swapped.append((target, backup))
            os.rename(incoming / skill.name, target)
    except BaseException as error:
        unrestored = []
        for target, backup in reversed(swapped):
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target)
                if backup is not None:
                    os.rename(backup, target)
            except OSError:
                unrestored.append(target.name)
        if unrestored:
            keep_staging = True
            raise SkillsError(
                f"Installing skills failed and {', '.join(unrestored)} could not "
                f"be restored; the previous folders are in {previous}. "
                f"Cause: {type(error).__name__}."
            ) from None
        raise
    finally:
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)


def read_skills_lock(project_root: Path) -> dict[str, Any] | None:
    filename = project_root / SKILLS_LOCK_FILE
    if not filename.is_symlink() and not filename.exists():
        return None

    def invalid(detail: str) -> SkillsError:
        return SkillsError(
            f"{SKILLS_LOCK_FILE} {detail}. "
            "Restore it from Git, or delete it and run flow skills add."
        )

    try:
        value = json.loads(filename.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise invalid("is not valid JSON") from None
    if not isinstance(value, dict):
        raise invalid("is not a JSON object")
    version = value.get("version")
    if isinstance(version, int) and version > LOCK_VERSION:
        raise SkillsError(
            f"{SKILLS_LOCK_FILE} was written by a newer Flow version. "
            "Update the project's Flow CLI first."
        )
    if version != LOCK_VERSION or isinstance(version, bool):
        raise invalid("has an unsupported version")
    setups = value.get("setups")
    if not isinstance(setups, list) or not all(
        isinstance(item, str) for item in setups
    ):
        raise invalid("has an invalid setups list")
    for setup_id in setups:
        if not any(setup.id == setup_id for setup in SKILL_SETUPS):
            raise SkillsError(
                f'{SKILLS_LOCK_FILE} records the "{setup_id}" setup, which this '
                "Flow version does not provide. Update the project's Flow CLI first."
            )
    if not isinstance(value.get("ref"), str) or not isinstance(
        value.get("commit"), str
    ):
        raise invalid("is missing its ref or commit")
    if "source" in value and not isinstance(value["source"], str):
        raise invalid("has an invalid source")
    skills = value.get("skills")
    if not isinstance(skills, dict):
        raise invalid("has an invalid skills list")
    for name, entry in skills.items():
        if (
            not SKILL_NAME.match(name)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("source"), str)
            or not isinstance(entry.get("digest"), str)
            or not DIGEST.match(entry["digest"])
        ):
            raise invalid(f'has an invalid entry for "{name}"')
    return value


def format_skills_lock(lock: dict[str, Any]) -> str:
    """Serialize the lock exactly as the TypeScript Flow CLI does.

    That output is Biome-formatted JSON: an array stays on one line when it
    fits within 80 columns.
    """
    text = json.dumps(lock, indent=2, ensure_ascii=False)
    inline = '"setups": [' + ", ".join(json.dumps(s) for s in lock["setups"]) + "]"
    if len(f"  {inline},") <= 80:
        text = re.sub(r'"setups": \[[^\]]*\]', lambda _: inline, text, count=1)
    return f"{text}\n"


def write_skills_lock(project_root: Path, lock: dict[str, Any]) -> None:
    filename = project_root / SKILLS_LOCK_FILE
    filename.parent.mkdir(parents=True, exist_ok=True)
    temporary = filename.with_name(f"{filename.name}.{uuid.uuid4()}.tmp")
    try:
        temporary.write_bytes(format_skills_lock(lock).encode("utf-8"))
        os.replace(temporary, filename)
    finally:
        temporary.unlink(missing_ok=True)


def build_lock(
    args: SkillsArgs,
    setups: tuple[str, ...],
    commit: str,
    plan: SkillsPlan,
    replace_conflicts: bool,
) -> dict[str, Any]:
    entries: dict[str, dict[str, str]] = {}
    for item in plan.skills:
        kept = item.kind == "customized" or (
            item.kind in CONFLICTS and not replace_conflicts
        )
        if not kept:
            entries[item.skill.name] = {
                "source": item.skill.source,
                "digest": item.skill.digest,
            }
        # A kept folder keeps the baseline Flow installed, if there is one.
        elif item.recorded is not None:
            entries[item.skill.name] = item.recorded
    entries.update(plan.retired)
    lock: dict[str, Any] = {"version": LOCK_VERSION, "setups": list(setups)}
    # Recorded for remote sources other than the default only; local
    # checkouts and archives are not portable.
    if args.source != SKILLS_SOURCE and is_remote_source(args.source):
        lock["source"] = args.source
    lock.update(ref=args.ref, commit=commit, skills=dict(sorted(entries.items())))
    return lock


def choose_setups(runtime: SkillsRuntime, recorded: tuple[str, ...]) -> tuple[str, ...]:
    defaults = recorded or (RECOMMENDED_SETUP,)
    runtime.print("What do you need agent skills for?")
    for index, setup in enumerate(SKILL_SETUPS, start=1):
        note = ""
        if setup.id in recorded:
            note = " (installed)"
        elif not recorded and setup.id == RECOMMENDED_SETUP:
            note = " (recommended for this project)"
        runtime.print(f"  {index}) {setup.label}{note}")
    default_answer = ",".join(
        str(index)
        for index, setup in enumerate(SKILL_SETUPS, start=1)
        if setup.id in defaults
    )
    for _attempt in range(3):
        answer = runtime.prompt(
            f"Select one or more, separated by commas [{default_answer}]: "
        ).strip()
        if not answer:
            return defaults
        names = [
            (
                SKILL_SETUPS[int(part) - 1].id
                if part.strip().isdigit() and 1 <= int(part) <= len(SKILL_SETUPS)
                else part
            )
            for part in answer.split(",")
        ]
        try:
            setups = normalize_setups(names)
        except SkillsError as error:
            runtime.print(str(error))
            continue
        if setups:
            return setups
    raise SkillsError("No skill setup was selected.")


def describe(item: PlannedSkill) -> str:
    return {
        "install": "missing; reinstalling" if item.recorded else "new",
        "update": "updated in poly-skills",
        "customized": "local changes kept; no poly-skills update",
        "modified": "local changes, and updated in poly-skills",
        "unmanaged": "existing folder that Flow did not install",
        "unchanged": "unchanged",
    }[item.kind]


def run_skills(args: SkillsArgs, runtime: SkillsRuntime) -> None:
    if args.command == "help":
        runtime.print(skills_help())
        return
    lock = read_skills_lock(runtime.project_root)
    recorded = tuple(lock["setups"]) if lock else ()
    if args.command == "update":
        if not recorded:
            raise SkillsError(
                f"No skill setup is recorded in {SKILLS_LOCK_FILE}. "
                "Run flow skills add to choose one."
            )
        setups = recorded
    else:
        if not args.setups and not runtime.interactive:
            raise SkillsError(
                "Name the setup to add, for example: flow skills add "
                f"{RECOMMENDED_SETUP}. Choose {setup_choices()}."
            )
        chosen = args.setups or choose_setups(runtime, recorded)
        setups = normalize_setups((*recorded, *chosen))

    packs = tuple(
        dict.fromkeys(pack for setup in setups for pack in setup_by_id(setup).packs)
    )
    runtime.print(f"Fetching poly-skills {args.ref}...")
    snapshot = runtime.fetch(args.source, args.ref, packs)
    desired = resolve_setup_skills(snapshot, setups)
    plan = plan_skills(runtime.project_root, desired, lock)

    labels = ", ".join(setup_by_id(setup).label for setup in setups)
    runtime.print(
        f"Skills for {labels} from poly-skills {args.ref} ({snapshot.commit[:7]}):"
    )
    width = max((len(skill.name) for skill in desired), default=0)
    for item in plan.skills:
        if item.kind != "unchanged":
            runtime.print(
                f"  {MARKERS[item.kind]} {item.skill.name:<{width}}  {describe(item)}"
            )
    for name, _entry in plan.retired:
        runtime.print(
            f"  - {name:<{width}}  no longer in the selected setups; left in place"
        )
    unchanged = [item for item in plan.skills if item.kind == "unchanged"]
    if unchanged:
        runtime.print(f"  {len(unchanged)} unchanged")

    if args.dry_run:
        runtime.print("Dry run: no files were changed.")
        return

    conflicts = [item for item in plan.skills if item.kind in CONFLICTS]
    replace_conflicts = args.yes
    if conflicts and not replace_conflicts and runtime.interactive:
        answer = runtime.prompt(
            f"Replace the {len(conflicts)} folder(s) marked ! with the poly-skills "
            "version? Answer no to keep them and continue. [y/N] "
        )
        replace_conflicts = answer.strip().lower() in ("y", "yes")
    writes = [
        item
        for item in plan.skills
        if item.kind in ("install", "update")
        or (replace_conflicts and item.kind in CONFLICTS)
    ]
    apply_skill_writes(runtime.project_root, [item.skill for item in writes])
    write_skills_lock(
        runtime.project_root,
        build_lock(args, setups, snapshot.commit, plan, replace_conflicts),
    )

    def count(kinds: tuple[str, ...]) -> int:
        return sum(item.kind in kinds for item in writes)

    changes = [
        f"{verb} {total}"
        for verb, total in (
            ("installed", count(("install",))),
            ("updated", count(("update",))),
            ("replaced", count(CONFLICTS)),
        )
        if total
    ]
    if changes:
        changes[0] = changes[0][0].upper() + changes[0][1:]
        runtime.print(
            f"{', '.join(changes)} skill folder(s) in {SKILLS_DIRECTORY}; "
            f"{len(unchanged)} unchanged."
        )
    else:
        runtime.print(f"Skills in {SKILLS_DIRECTORY} are up to date.")
    if conflicts and not replace_conflicts:
        runtime.print(
            f"Kept {len(conflicts)} folder(s) marked !. Rerun with --yes to "
            "replace them with the poly-skills version."
        )
    if args.command == "add":
        runtime.print(
            f"Recorded {labels} in {SKILLS_LOCK_FILE}. Commit it with "
            f"{SKILLS_DIRECTORY}/, and run flow skills update to refresh the skills."
        )


def main(argv: list[str] | None = None, project_root: Path | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    runtime = SkillsRuntime(
        project_root or Path(__file__).resolve().parents[1],
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
    )
    run_skills(args, runtime)
    return 0
