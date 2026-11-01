"""Use the existing Flow onboarding safeguards for a new Python project."""

import os
from pathlib import Path
import subprocess
import sys

from cli import setup
from cli.environment import CONNECTION_ALIASES, get_api_key_and_url


def configure(root: Path) -> None:
    key = os.environ["POLY_API_KEY"]
    url = os.environ["POLY_API_BASE_URL"]
    namespace = os.environ["KICKSTART_NAMESPACE"]
    identity = setup.fetch_identity(key, url)
    envpath = setup.choose_target(identity, url, {})
    if (root / envpath).exists() or (root / envpath).is_symlink():
        raise setup.SetupError(
            "Kickstart only configures fresh projects. Use `flow setup` to change an existing target."
        )
    # The SDK's supported env inputs avoid putting credentials in argv.
    # With explicit values, native `setup` saves them without generating.
    setup.run_sdk_setup(root)
    previous = {
        name: os.environ.pop(name, None)
        for names in CONNECTION_ALIASES
        for name in names
    }
    try:
        saved_key, saved_url = get_api_key_and_url()
        if saved_key != key or saved_url != url:
            raise setup.SetupError(
                "The native SDK did not save the requested connection."
            )
        verified = setup.fetch_identity(saved_key, saved_url)
        if verified.environment_id != identity.environment_id:
            raise setup.SetupError(
                "The configured PolyAPI target changed during setup."
            )
    finally:
        for name, value in previous.items():
            if value is not None:
                os.environ[name] = value

    example = (root / "workflows/dev.env.example").read_text(encoding="utf-8")
    settings = {
        "POLYAPI_NAMESPACE": namespace,
        "LOG_LEVEL": "debug",
        "SLACK_CHANNEL_ID": "alerts-demos",
        "TARGET_ENV_ID": identity.environment_id,
        "POLY_TEST_WEBHOOK_URL": None,
        "POLY_EXEC_API_KEY": "",
    }
    settings.update({name: None for names in CONNECTION_ALIASES for name in names})
    example = setup.update_dotenv(example, settings)
    setup.atomic_write(root / envpath, example)
    setup.save_target_configuration(root, envpath, identity, key, url)
    print("Generating PolyAPI Python SDK...", flush=True)
    subprocess.run([sys.executable, "-m", "polyapi", "generate"], cwd=root, check=True)
    print(f"Configured {identity.label}. TARGET_ENV_ID saved in {envpath}.")
    print("VS Code will use .venv and the selected environment file.")
    print(
        "For the flow command, source cli/shell/flow.bash in Bash/zsh, or use the Fish/PowerShell loader."
    )


if __name__ == "__main__":
    try:
        configure(Path.cwd())
    except (
        setup.SetupError,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
    ) as error:
        message = (
            str(error)
            if isinstance(error, setup.SetupError)
            else "Python SDK setup or generation failed."
        )
        print(f"kickstart: {message}", file=sys.stderr)
        raise SystemExit(1)
