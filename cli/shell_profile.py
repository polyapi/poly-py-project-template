"""Standard-library shell registration shared by Flow setup and kickstart."""

import os
from pathlib import Path
import sys
import tempfile


def confirm(message: str, default: bool = True) -> bool:
    while True:
        answer = input(f"{message} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "n", "no"}:
            return answer in {"y", "yes"}
        print("Choose yes or no.")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def offer_profile(root: Path, shell: str | None, profile: str | None) -> None:
    if not shell:
        return
    suffix = {"bash": "bash", "zsh": "bash", "fish": "fish", "powershell": "ps1"}[shell]
    loader = root / "cli/shell" / f"flow.{suffix}"
    if shell == "bash":
        path = Path.home() / (
            ".bash_profile" if sys.platform == "darwin" else ".bashrc"
        )
        import shlex

        command = f"source {shlex.quote(str(loader))}"
    elif shell == "zsh":
        path = (
            Path(os.environ.get("ZDOTDIR") or str(Path.home())).expanduser() / ".zshrc"
        )
        import shlex

        command = f"source {shlex.quote(str(loader))}"
    elif shell == "fish":
        path = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "fish/conf.d/poly_flow.fish"
        )
        command = (
            "source '" + str(loader).replace("\\", "\\\\").replace("'", "\\'") + "'"
        )
    else:
        if not profile:
            return
        path = Path(profile)
        command = ". '" + str(loader).replace("'", "''") + "'"
    marker = "# Python Flow shell loader"
    if path.is_symlink():
        print("Shell profile is a symlink; add the loader manually if desired.")
        return
    previous = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if marker in previous:
        print(f"Shell loader already registered in {path}; left unchanged.")
        return
    print(f"Optional shell profile: {path}\n{command}")
    if confirm("Add this loader for future terminals?", default=False):
        atomic_write(path, previous.rstrip() + f"\n\n{marker}\n{command}\n")


def offer_shell_profile(root: Path) -> None:
    shells = {"bash", "zsh", "fish", "powershell"}
    hint = Path(os.environ.get("SHELL", "")).name.lower()
    if hint in {"pwsh", "pwsh.exe", "powershell.exe"}:
        hint = "powershell"
    default = hint if hint in shells else "skip"
    while True:
        shell = (
            input(
                "Shell for optional flow shortcut "
                f"(bash, zsh, fish, powershell, skip) [{default}]: "
            )
            .strip()
            .lower()
            or default
        )
        if shell == "skip":
            return
        if shell in shells:
            break
        print("Choose bash, zsh, fish, powershell, or skip.")
    profile = None
    if shell == "powershell":
        profile = input(
            "PowerShell profile path ($PROFILE.CurrentUserAllHosts; Enter to skip): "
        ).strip()
        if not profile:
            return
        profile = str(Path(profile).expanduser())
    offer_profile(root, shell, profile)
