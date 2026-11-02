"""Interactive bootstrap from a checkout or public snapshot; no gh required."""

import getpass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

# The bootstrap interpreter has no SDK dependencies; this helper uses stdlib only.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cli import shell_profile  # noqa: E402
from kickstart.namespace import rewrite_namespace, valid_namespace  # noqa: E402

SKILLS_REPOSITORY = "https://github.com/polyapi/poly-skills.git"
PRIVATE_DIRECTORIES = {
    ".git",
    ".venv",
    ".poly",
    ".flow",
    ".vscode",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
}
PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".crt", ".pyc", ".pyo"}


class KickstartError(ValueError):
    """A safe, actionable bootstrap failure."""


def copyable(relative: Path) -> bool:
    if any(part in PRIVATE_DIRECTORIES for part in relative.parts):
        return False
    name = relative.name.lower()
    if name in {"id_rsa", "id_ed25519", "id_ecdsa", ".netrc", ".npmrc", ".pypirc"}:
        return False
    if name.endswith(".env.example"):
        return True
    return not (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or relative.suffix.lower() in PRIVATE_SUFFIXES
    )


def copy_checkout(source: Path, target: Path) -> None:
    if (source / ".git").exists():
        # Include pending changes in a local checkout, excluding ignored files.
        paths = (
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(source),
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ]
            )
            .decode()
            .split("\0")
        )
    else:
        # A public GitHub snapshot has no Git metadata. Apply the same private
        # file filter below before copying any source file.
        paths = [str(file.relative_to(source)) for file in source.rglob("*")]
    for name in sorted(set(filter(None, paths))):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not copyable(relative):
            continue
        file = source / relative
        if any(
            item.is_symlink() for item in (file, *file.parents) if item != source.parent
        ):
            raise KickstartError(
                "Template source contains a symlink; use regular source files."
            )
        if not file.is_file():
            continue  # A tracked file may have been deleted in the working tree.
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, destination)


def rename_project(root: Path, name: str) -> None:
    project_file = root / "pyproject.toml"
    contents = project_file.read_text(encoding="utf-8")
    old = tomllib.loads(contents)["project"]["name"]
    for file in (project_file, root / "uv.lock"):
        text = contents if file == project_file else file.read_text(encoding="utf-8")
        file.write_text(
            text.replace(f'name = "{old}"', f'name = "{name}"'), encoding="utf-8"
        )


def subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "ENVPATH",
        "BASE_URL",
        "API_KEY",
        "POLY_API_BASE_URL",
        "POLY_API_KEY",
        "POLYAPI_BASE_URL",
        "POLYAPI_API_KEY",
        "POLYAPI_NAMESPACE",
        "POLYAPI_NAMESPACES",
        "POLY_API_NAMESPACE",
        "POLY_NAMESPACE",
        "POLY_EXEC_API_KEY",
        "POLYPROD_EXEC_KEY",
        "VIRTUAL_ENV",
        "UV_PROJECT",
        "UV_WORKING_DIR",
        "UV_PROJECT_ENVIRONMENT",
    ):
        environment.pop(name, None)
    environment["UV_PROJECT_ENVIRONMENT"] = ".venv"
    return environment


def offer_optional_profile(root: Path) -> None:
    try:
        shell_profile.offer_shell_profile(root)
    except (KeyboardInterrupt, EOFError):
        print(
            "\nOptional shell-profile registration skipped; continuing project setup."
        )
    except (OSError, UnicodeError):
        print("Could not update the optional profile; continuing project setup.")
    print(
        "For this terminal, source cli/shell/flow.bash in Bash/zsh, or use the Fish/PowerShell loader."
    )


def main() -> int:
    if not sys.stdin.isatty():
        raise KickstartError("Run kickstart from an interactive terminal.")
    for tool in ("git", "uv", "code"):
        if not shutil.which(tool):
            raise KickstartError(f"Required command not found: {tool}")
    source = Path(__file__).resolve().parents[1]
    while True:
        name = input("Project name: ").strip()
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", name):
            break
        print(
            "Use lowercase letters, numbers, dots, underscores or hyphens; start and end with a letter or number."
        )
    default_parent = Path.cwd()
    while True:
        parent = (
            Path(
                input(f"Parent directory [{default_parent}]: ").strip()
                or default_parent
            )
            .expanduser()
            .resolve()
        )
        target = parent / name
        if parent.exists() and not parent.is_dir():
            print("The parent path is not a directory.")
        elif target.exists() or target.is_symlink():
            print("That project already exists. Choose another parent directory.")
        elif target == source or source.is_relative_to(target):
            print("Choose a destination outside the template checkout's parents.")
        else:
            break
    while True:
        namespace = input("Project namespace [demo]: ").strip() or "demo"
        if valid_namespace(namespace):
            break
        print("Use dotted snake_case identifiers, excluding Python keywords.")
    while True:
        key = getpass.getpass("PolyAPI key (hidden): ")
        if key and not any(char.isspace() for char in key):
            break
        print("Enter a nonempty API key without whitespace.")
    while True:
        region = input("Environment (dev, na1, na2, eu1): ").strip().lower()
        if region in {"dev", "na1", "na2", "eu1"}:
            break
        print("Choose dev, na1, na2 or eu1.")

    environment = subprocess_environment()
    with tempfile.TemporaryDirectory(prefix="poly-py-kickstart-") as temporary:
        skills = Path(temporary) / "skills"
        print("Downloading PolyAPI skills...", flush=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                "main",
                SKILLS_REPOSITORY,
                str(skills),
            ],
            env=environment,
            check=True,
        )
        categories = ("general", "polyapi-common", "polyapi-python")
        for category in categories:
            if not (skills / category / "skills").is_dir():
                raise KickstartError(f"Skills category is missing: {category}")
        training_skill = skills / "polyapi-python/skills/api-function-training/SKILL.md"
        if (
            training_skill.is_symlink()
            or not training_skill.is_file()
            or not training_skill.stat().st_size
        ):
            raise KickstartError(
                "api-function-training is missing from poly-skills/main. "
                "Publish the Python training skill there before retrying."
            )
        target.mkdir(parents=True)
        try:
            copy_checkout(source, target)
            rename_project(target, name)
            for category in categories:
                if any(
                    file.is_symlink()
                    for file in (skills / category / "skills").rglob("*")
                ):
                    raise KickstartError(
                        "Skills must contain regular files, not symlinks."
                    )
                shutil.copytree(
                    skills / category / "skills",
                    target / ".agents/skills",
                    dirs_exist_ok=True,
                    ignore=lambda folder, names: [
                        name
                        for name in names
                        if not copyable(Path(folder).relative_to(skills) / name)
                    ],
                )
            installed_training_skill = (
                target / ".agents/skills/api-function-training/SKILL.md"
            )
            if (
                not installed_training_skill.is_file()
                or not installed_training_skill.stat().st_size
            ):
                raise KickstartError(
                    "Could not install the API Function Training skill."
                )
            rewrite_namespace(target, namespace)
            print("Installing pinned Python dependencies...", flush=True)
            subprocess.run(
                ["uv", "sync", "--locked"], cwd=target, env=environment, check=True
            )
            subprocess.run(
                ["uv", "run", "--locked", "--no-sync", "black", "workflows/src"],
                cwd=target,
                env=environment,
                check=True,
            )
            environment.update(
                POLY_API_KEY=key,
                POLY_API_BASE_URL=f"https://{region}.polyapi.io",
                KICKSTART_NAMESPACE=namespace,
            )
            print("Setting up and verifying PolyAPI...", flush=True)
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--locked",
                    "--no-sync",
                    "python",
                    "-m",
                    "kickstart.configure",
                ],
                cwd=target,
                env=environment,
                check=True,
            )
            environment.pop("POLY_API_KEY")
            environment.pop("POLY_API_BASE_URL")
            key = ""
            offer_optional_profile(target)
            print("Initializing Git and switching to develop...", flush=True)
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=target, env=environment, check=True
            )
            for private in (
                ".venv/",
                ".flow/",
                ".vscode/",
                "workflows/dev.env",
                "workflows/prod.env",
            ):
                subprocess.run(
                    ["git", "check-ignore", "--quiet", "--", private],
                    cwd=target,
                    env=environment,
                    check=True,
                )
            for args in (
                ["add", "."],
                ["commit", "-m", "Initial commit"],
                ["switch", "-c", "develop"],
            ):
                subprocess.run(["git", *args], cwd=target, env=environment, check=True)
            subprocess.run(
                ["code", str(target)], cwd=target, env=environment, check=True
            )
            print(f"Project ready at {target}")
        except BaseException:
            print(
                f"Setup stopped. Project retained at {target} for troubleshooting.",
                file=sys.stderr,
            )
            raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nKickstart cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except subprocess.CalledProcessError as error:
        print(
            f"kickstart: {error.cmd[0]} failed (exit {error.returncode}).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except KickstartError as error:
        print(f"kickstart: {error}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError):
        print(
            "kickstart: could not prepare the project. Check paths, permissions, and Git access.",
            file=sys.stderr,
        )
        raise SystemExit(1)
