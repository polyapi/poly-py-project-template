"""Interactive local onboarding; credentials stay inside the native SDK/process."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from dotenv import dotenv_values
from dotenv.parser import parse_stream
import requests

ENV_PATHS = {"dev": "workflows/dev.env", "prod": "workflows/prod.env"}
STATE_PATH = ".flow/setup.json"
SDK_ENV_KEYS = ("POLY_API_KEY", "POLY_API_BASE_URL")


class SetupError(Exception):
    """Safe, operator-facing setup failure."""


@dataclass(frozen=True)
class Identity:
    tenant_name: str
    environment_name: str
    environment_id: str

    @property
    def label(self) -> str:
        return (
            f"tenant {self.tenant_name!r}, environment {self.environment_name!r} "
            f"({self.environment_id})"
        )


def confirm(message: str, default: bool = True) -> bool:
    while True:
        answer = input(f"{message} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "n", "no"}:
            return answer in {"y", "yes"}
        print("Choose yes or no.")


def suggest_environment(name: str) -> str:
    words = set(re.findall(r"[a-z0-9]+", name.lower()))
    nonprod = {
        "dev",
        "development",
        "test",
        "testing",
        "qa",
        "uat",
        "stage",
        "staging",
        "sandbox",
        "nonprod",
        "preprod",
        "non",
        "pre",
    }
    return "prod" if words & {"prod", "production"} and not words & nonprod else "dev"


def check_local_path(path: Path, root: Path) -> None:
    """Do not follow config symlinks outside the project or replace special files."""
    if not path.is_relative_to(root):
        raise SetupError("Configuration path must be inside the project.")
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise SetupError("Setup does not follow configuration symlinks.")
    if path.exists() and not path.is_file():
        raise SetupError("Expected a regular configuration file.")


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


def read_state(root: Path) -> dict:
    path = root / STATE_PATH
    check_local_path(path, root)
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise SetupError(
            "Invalid .flow/setup.json; rerun setup after removing it."
        ) from None
    if not isinstance(state, dict) or state.get("envpath") not in ENV_PATHS.values():
        raise SetupError("Invalid saved environment selection.")
    return state


@contextmanager
def sdk_environment():
    # With BOTH variables set, SDK setup bypasses its interactive prompts.
    previous = {key: os.environ.pop(key, None) for key in SDK_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value


def run_sdk_setup(root: Path) -> tuple[str, str]:
    from polyapi import config

    # Restrict newly created native SDK files without editing the SDK itself.
    old_mask = os.umask(0o077)
    try:
        result = subprocess.run([sys.executable, "-m", "polyapi", "setup"], cwd=root)
    finally:
        os.umask(old_mask)
    if result.returncode:
        raise SetupError(
            "Native SDK setup/generation failed; project selection was not changed."
        )
    key, url = config.get_api_key_and_url()
    if not key or not url:
        raise SetupError("Native SDK setup did not provide a URL and API key.")
    return key, url.rstrip("/")


def fetch_identity(key: str, url: str) -> Identity:
    from polyapi.utils import get_auth_headers

    try:
        parts = urlsplit(url)
    except ValueError:
        raise SetupError("SDK base URL is invalid.") from None
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise SetupError(
            "SDK base URL must be an HTTP(S) URL without credentials or query parameters."
        )
    if parts.scheme == "http" and parts.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise SetupError("Use HTTPS for remote PolyAPI instances.")
    try:
        response = requests.get(
            f"{url}/whoami",
            headers=get_auth_headers(key),
            timeout=30,
            allow_redirects=False,
            # Keep Requests from replacing the SDK Bearer header via .netrc.
            auth=lambda prepared: prepared,
        )
        if response.status_code != 200:
            raise SetupError(
                f"GET /whoami failed (HTTP {response.status_code}); no project target was saved."
            )
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise SetupError(
            "Could not verify SDK identity with GET /whoami; no project target was saved."
        ) from None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("environmentId"), str)
        or not payload["environmentId"].strip()
    ):
        raise SetupError(
            "GET /whoami returned no authenticated environment; check the SDK URL/key."
        )
    names = [payload.get("tenantName"), payload.get("environmentName")]
    if not all(isinstance(name, str) for name in names):
        raise SetupError("GET /whoami did not provide tenant and environment names.")
    return Identity(
        names[0] or "(unnamed tenant)",
        names[1] or "(unnamed environment)",
        payload["environmentId"],
    )


def choose_target(identity: Identity, url: str, previous: dict) -> str:
    suggestion = suggest_environment(identity.environment_name)
    if (
        previous.get("environment_id") == identity.environment_id
        and previous.get("base_url") == url
    ):
        suggestion = next(
            mode for mode, path in ENV_PATHS.items() if path == previous["envpath"]
        )
    print(f"\nVerified {identity.label} at {url}.")
    if previous:
        print(f"Previous selection: {previous['envpath']}")
    print("dev -> workflows/dev.env; prod -> workflows/prod.env")
    print(
        "The name is only a suggestion; choosing dev does not turn a production environment into dev."
    )
    while True:
        selection = (
            input(f"Configure {identity.label} as dev or prod [{suggestion}]: ")
            .strip()
            .lower()
            or suggestion
        )
        if selection in ENV_PATHS:
            return ENV_PATHS[selection]
        print("Choose dev or prod.")


def update_dotenv(text: str, updates: dict[str, str | None]) -> str:
    """Preserve unrelated lines and replace each configured key in place."""
    from io import StringIO

    replacements: dict[str, str] = {}
    for key, value in updates.items():
        if value is None:
            replacements[key] = ""
            continue
        if any(char in value for char in "\r\n\0") or "${" in value:
            raise SetupError(
                "Configuration values cannot contain newlines or dotenv interpolation."
            )
        quoted = value.replace("\\", "\\\\").replace("'", "\\'")
        replacements[key] = f"{key}='{quoted}'\n"
    remaining = dict(replacements)
    lines: list[str] = []
    for binding in parse_stream(StringIO(text)):
        if binding.error:
            raise SetupError(
                "The selected env file has invalid dotenv syntax; fix it before setup."
            )
        if binding.key in replacements:
            lines.append(remaining.pop(binding.key, ""))
        else:
            lines.append(binding.original.string)
    result = "".join(lines)
    if result and not result.endswith("\n"):
        result += "\n"
    return result + "".join(remaining.values())


def prepare_env(
    root: Path, envpath: str, identity: Identity, key: str, url: str
) -> str:
    path = root / envpath
    check_local_path(path, root)
    if (root / ".git").exists():
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", envpath],
            cwd=root,
            capture_output=True,
        )
        if ignored.returncode != 0:
            raise SetupError(
                "The target env file must be untracked and Git-ignored before saving credentials."
            )
    if path.exists():
        text = path.read_text(encoding="utf-8")
        update_dotenv(text, {})  # Validate before dotenv can emit a parser warning.
        from io import StringIO

        existing = dotenv_values(stream=StringIO(text), interpolate=False)
    else:
        if not confirm(
            f"Create {envpath} from workflows/dev.env.example for {identity.label}?"
        ):
            raise SetupError("Cancelled; no project target was saved.")
        example = root / "workflows/dev.env.example"
        check_local_path(example, root)
        text = example.read_text(encoding="utf-8")
        existing = {}
    updates = {
        "POLY_API_BASE_URL": url,
        "POLY_API_KEY": key,
        "TARGET_ENV_ID": identity.environment_id,
    }
    changed_target = bool(existing) and (
        (existing.get("POLY_API_BASE_URL") or "").rstrip("/") != url
        or existing.get("TARGET_ENV_ID") != identity.environment_id
    )
    if not existing:
        updates["POLY_EXEC_API_KEY"] = ""
    if changed_target:
        print(
            "This replaces the file's target identity and removes its old POLY_EXEC_API_KEY."
        )
        updates["POLY_EXEC_API_KEY"] = ""
    if not confirm(f"Save {identity.label} to {envpath}?", default=not changed_target):
        raise SetupError("Cancelled; no project target was saved.")
    return update_dotenv(text, updates)


def jsonc_values(text: str) -> tuple[dict, dict[str, tuple[int, int]], int]:
    """Locate top-level JSONC values so unrelated settings/comments survive."""
    tokens = r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/'
    clean = re.sub(
        tokens,
        lambda m: m[0] if m[0].startswith('"') else re.sub(r"[^\n]", " ", m[0]),
        text,
    )
    clean = re.sub(
        r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])',
        lambda m: " " if m[0] == "," else m[0],
        clean,
    )
    values = json.loads(clean)
    if not isinstance(values, dict):
        raise SetupError("VS Code settings must be a JSON object.")
    decoder = json.JSONDecoder()
    spans = {}
    opening = clean.index("{") + 1
    position = opening
    while True:
        while clean[position].isspace() or clean[position] == ",":
            position += 1
        if clean[position] == "}":
            break
        key, position = decoder.raw_decode(clean, position)
        position = clean.index(":", position) + 1
        while clean[position].isspace():
            position += 1
        _, end = decoder.raw_decode(clean, position)
        if key in spans:
            raise SetupError(
                "Resolve duplicate VS Code setting keys before running setup."
            )
        spans[key] = (position, end)
        position = end
    return values, spans, opening


def vscode_settings(root: Path, envpath: str) -> str:
    path = root / ".vscode/settings.json"
    check_local_path(path, root)
    text = path.read_text(encoding="utf-8") if path.exists() else "{}\n"
    try:
        values, spans, opening = jsonc_values(text)
    except ValueError:
        raise SetupError(
            "VS Code settings are not valid JSONC; fix them before setup."
        ) from None
    updates = {
        "python.defaultInterpreterPath": "${workspaceFolder}/.venv",
        "python.envFile": f"${{workspaceFolder}}/{envpath}",
    }
    for platform in ("linux", "osx", "windows"):
        setting = f"terminal.integrated.env.{platform}"
        current = values.get(setting, {})
        if not isinstance(current, dict):
            raise SetupError("VS Code terminal environment settings must be objects.")
        updates[setting] = {**current, "ENVPATH": envpath}
    for start, end, replacement in sorted(
        (
            (spans[key][0], spans[key][1], json.dumps(value))
            for key, value in updates.items()
            if key in spans
        ),
        reverse=True,
    ):
        text = text[:start] + replacement + text[end:]
    missing = {key: value for key, value in updates.items() if key not in spans}
    if missing:
        # Insert before the FIRST property; existing trailing commas/comments stay valid.
        additions = json.dumps(missing, indent=2)[1:-1].rstrip()
        text = text[:opening] + additions + ("," if values else "") + text[opening:]
    return text


def offer_profile(root: Path, shell: str | None, profile: str | None) -> None:
    if not shell:
        return
    suffix = {"bash": "bash", "fish": "fish", "powershell": "ps1"}[shell]
    loader = root / "cli/shell" / f"flow.{suffix}"
    if shell == "bash":
        path = Path.home() / (
            ".bash_profile" if sys.platform == "darwin" else ".bashrc"
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


def save_project_files(updates: dict[Path, str]) -> None:
    previous = {
        path: path.read_text(encoding="utf-8") if path.exists() else None
        for path in updates
    }
    written: list[Path] = []
    try:
        for path, content in updates.items():
            atomic_write(path, content)
            written.append(path)
    except (OSError, KeyboardInterrupt):
        try:
            for path in reversed(written):
                old = previous[path]
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, old)
        except OSError:
            raise SetupError(
                "Setup could not restore a partial write. Inspect the local target/editor settings before using Flow."
            ) from None
        raise


def setup(root: Path, shell: str | None = None, profile: str | None = None) -> None:
    previous = read_state(root)
    with sdk_environment():
        key, url = run_sdk_setup(root)
        identity = fetch_identity(key, url)
    envpath = choose_target(identity, url, previous)
    content = prepare_env(root, envpath, identity, key, url)
    editor = vscode_settings(root, envpath)
    # Prepare all content before writing; restore prior files on a partial failure.
    updates = {
        root / envpath: content,
        root / ".vscode/settings.json": editor,
        root
        / STATE_PATH: json.dumps(
            {
                "envpath": envpath,
                "base_url": url,
                "environment_id": identity.environment_id,
            },
            indent=2,
        )
        + "\n",
    }
    save_project_files(updates)
    print(f"\nConfigured {identity.label}. ENVPATH={envpath}")
    print(
        "VS Code: .venv interpreter and Python env file configured. Reopen terminals to refresh their environment."
    )
    print(
        "If VS Code already selected an interpreter, use Python: Select Interpreter and choose .venv."
    )
    print(
        "Existing namespace and other workflow settings are preserved. No resources were deployed."
    )
    try:
        offer_profile(root, shell, profile)
    except (KeyboardInterrupt, EOFError):
        print(
            "\nProject setup is complete; optional profile registration was interrupted."
        )
    except (OSError, UnicodeError):
        print(
            "Project setup succeeded, but the optional profile could not be updated. Source the loader manually."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run native SDK setup and confirm a project dev/prod target."
    )
    parser.add_argument("--shell", choices=("bash", "fish", "powershell"))
    parser.add_argument("--profile", help=argparse.SUPPRESS)
    parser.add_argument("--print-envpath", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.print_envpath:
            state = read_state(root)
            if not state:
                raise SetupError(
                    "Run flow setup to select this project's environment first."
                )
            print(state["envpath"])
        else:
            setup(root, args.shell, args.profile)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.", file=sys.stderr)
        return 130
    except (SetupError, OSError, UnicodeError) as error:
        message = (
            str(error)
            if isinstance(error, SetupError)
            else "Could not read or write a local setup file. Check permissions and UTF-8 encoding."
        )
        print(f"ERROR: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
