# Upgrading Flow

Run these commands from your existing project's root folder. You need Git,
GitHub SSH access to the template repository, and the project's existing Python
environment. GitHub CLI (`gh`) is not required.

- [Upgrading with Flow itself](#upgrading-with-flow-itself)
- [Reviewing customizations and recovering](#reviewing-customizations-and-recovering)

**Python Flow 1.2.0 adds self-update.** Flow's version is maintained in
`cli/__init__.py`, separately from the workspace and PolyAPI SDK. Patch releases
contain fixes, minor releases add compatible features, and major releases
introduce breaking changes. Python and TypeScript Flow advance independently.
For SDK changes, follow [Python SDK version and target](SETUP.md#python-sdk-version-and-target).

## Upgrading with Flow itself

Check the installed version and preview the latest template version:

```bash
flow --version
flow self-update --check
```

The preview shows installed and incoming versions, the exact template commit,
and the files that would change. It does not modify your project or run local
checks. File contents and permissions are compared even when the versions match.

Apply the update when ready:

```bash
flow self-update
```

Flow asks for confirmation, saves a backup, replaces its `cli/` source, and
runs syntax, help, and CLI tests in an isolated copy of the incoming template,
using the existing `.venv`. Live tests are disabled. If those checks fail, it
restores the previous source. Run your own project checks afterward as described
below. The update does not install dependencies or run `uv sync`.

Your `pyproject.toml`, `uv.lock`, workflow code, environment files, SDK setup,
and target pin are preserved. Additional project dependencies are allowed;
missing or changed requirements needed by the incoming template stop the update
for review. Python runtime or relevant uv configuration differences can also
stop it. Review and reconcile those requirements separately before retrying;
`--yes` skips the replacement confirmation only, not compatibility checks.

Updates follow the template's `main` branch by default. Use `--ref` to select
an existing branch, tag, or commit, and `--source` to select a GitHub SSH URL or
a local Git repository. A local source supplies committed files only. The
recorded commit identifies the exact source even when two checkouts share a
version.

Without the shell helper, use the project's existing Python environment
directly:

```bash
.venv/bin/python -B cli/flow.py self-update --check
.venv/bin/python -B cli/flow.py self-update
```

Self-update does not require an `ENVPATH` or a configured PolyAPI connection.
The shell helper bypasses environment selection for this command.

## Reviewing customizations and recovering

Copy the printed compatibility prompt into your coding agent. It points to
the previous CLI source, the pristine incoming source, and the update report
under `.flow/updates/<backup-id>/`. Keep that backup until your review is complete.

The agent can compare intended custom commands and extensions with the new
implementation, restore the parts you still need, and check compatibility with
project settings. Flow does not automatically reapply old code: differences can
include upstream fixes as well as local customizations.

After reviewing customizations, check the version and run your project checks:

```bash
flow --version
make check
```

A successful CLI update does not prove that your workflows meet every current
validation rule. Before your next deployment, preview the intended target:

```bash
flow push-dynamic --dry-run
```

That preview contacts PolyAPI and still requires the selected environment's
verified `TARGET_ENV_ID`. The update itself performs no PolyAPI operations.

To undo the latest applied update:

```bash
flow self-update --rollback
```

Supply a backup ID after `--rollback` to choose a specific update. Each update
also prints a standalone recovery command using the current Python interpreter
and its saved `restore.py`, for use if Flow cannot start. Recovery needs Python
3.11 or newer and works without the installed CLI or SDK dependencies. Keep
backups out of Git and review and commit the source changes yourself; upgrading
does not commit, push, or deploy anything.
