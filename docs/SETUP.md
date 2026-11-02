# Setup and development

[Getting started](../README.md#getting-started) lives on the template homepage.
Use this guide for existing checkouts, target changes, and development details.
Run commands from the project root.

## Existing checkout or manual setup

These setup steps are only needed if you did not use kickstart, or want to
change the target of an existing project. From the project root, load the
helper for your shell once:

| Shell | Load the `flow` command |
| --- | --- |
| Bash or zsh | `source cli/shell/flow.bash` |
| Fish | `source cli/shell/flow.fish` |
| PowerShell | `. ./cli/shell/flow.ps1` |

zsh is the default shell on macOS and in VS Code on macOS. Setup's optional
profile step targets Bash's `~/.bash_profile`, which zsh does not read; to keep
`flow` in future zsh terminals, add the same `source` line with the checkout's
absolute path to `~/.zshrc` instead.

Then run:

```bash
flow setup
```

This creates and activates `.venv`, runs the Python SDK setup, verifies your
tenant and environment, and asks you to confirm **dev or prod**. It configures
the selected env file and VS Code, and offers to save `flow` for future terminals.

Next, follow the [sample workflow setup](../workflows/src/sample_workflow/README.md#run-your-first-request)
to train its APIs and configure its secrets, then preview deployment:

```bash
flow push-dynamic --paths sample_workflow --types functions,variables,schemas,webhooks,triggers,jobs --dry-run
```

See [guided setup details](#guided-setup-with-flow) for target changes, existing
commands, and configuration safeguards. Setup itself does not deploy resources.

## Python SDK version and target

Python SDK compatibility matters too, but this template does not select SDK
packages using TypeScript's instance tags such as `polyapi@na2`. It installs
the same `polyapi-python` version pinned in `pyproject.toml` and `uv.lock` for
each region. Kickstart and the `flow setup` shell helper install those pinned
dependencies; they do not select a newer SDK release based on the target URL.

For an existing checkout, restore the pinned dependencies and check the
installed **SDK** version:

```bash
uv sync --locked
uv run --locked python -m polyapi --version
```

Use `flow setup` to choose the intended URL and key, generate the SDK bindings,
and confirm the target pin. Changing from one instance to another does not
by itself select a different Python SDK package. Flow's `--version` reports
Flow's own version, which is separate from the SDK version above.

To update Flow itself, use `flow self-update --check`, then `flow self-update`.
The [upgrade guide](UPGRADING_FLOW.md) covers backups, compatibility review, and
rollback. Updating Flow preserves the SDK dependency and connection setup.

If your target requires a different Python SDK release, confirm the supported
SDK release with the release guidance or your platform owner, then review
the SDK pin and `uv.lock` change together. Python and TypeScript SDK versions
are independent; do not infer a Python version from a TypeScript update prompt.
After an approved SDK update, rerun setup and generation, then the local checks
before deployment. See the [native Python SDK guide](https://docs.polyapi.io/generated_sdks/python.html#setup-and-generate)
for its connection and generation commands.

## Select an environment once

For the explicit Python commands used in the sample guides, select the file
configured by setup once per terminal session:

```bash
export ENVPATH=workflows/dev.env
```

Use your selected file if different. Python Flow resolves relative paths from
the project root: `dev.env` means the root file, not `workflows/dev.env`.
Without `ENVPATH`, the Python CLI defaults to root `.env`. The `flow` shell
helper and new VS Code terminals can already select the file saved by setup.

Flow reads the selected file itself. Direct SDK commands and runtime tests need
`uv run --locked --env-file "$ENVPATH" ...` to load its settings; they do not
apply Flow's target-pin check. Exporting `ENVPATH` does not load the file's
contents into your shell.

## Pin the target environment

`TARGET_ENV_ID` is the ID of the PolyAPI environment this file is allowed to
manage. It is separate from the namespace, tenant ID, and environment name.
Keep it in the selected file, for example `workflows/dev.env`:

```env
TARGET_ENV_ID=replace-with-environment-id
```

Kickstart and `flow setup` verify the configured connection with `GET /whoami`
and save its environment ID after confirmation. For manual setup, use the
intended environment's ID shown in Canopy or the `environmentId` returned by
authenticated `GET /whoami`. If unsure, use [guided setup](#guided-setup-with-flow)
to verify and save it.

Flow compares the file's pin with the authenticated environment before online
pushes (including deployment dry runs), API training, cleanup previews, and logs.
A mismatch stops the operation even when both environments share a base URL.
Changing the pin does not change the saved SDK connection or API key; configure
the intended connection as well. Exporting `TARGET_ENV_ID` only in the shell
does not supply the required file pin. If Flow reports a mismatch, first confirm
which environment you intend to use and correct the selected file or connection.
Change the pin only when you deliberately retarget that file.

Offline training previews and offline dependency reports do not perform this
live check. Direct SDK calls and runtime tests also do not enforce Flow's pin;
confirm their connection separately.

## Skip sample deployments

To keep the learning samples in your repository without deploying them during
normal project pushes, add this setting to the selected Flow environment file:

```env
POLY_DEPLOY_SKIP_PATHS=workflows/src/sample_workflow
```

This is a comma-separated list of file or directory paths relative to the
**project root**. A directory excludes everything below it; these are paths,
not glob patterns. Add the sample paths to any existing list instead of replacing
other exclusions. The setting above leaves shared utilities available to deploy.

Normal `push` and `push-dynamic` runs load these exclusions from the selected
file, including their online deployment previews. Offline graph reports do not
load that file and do not verify its exclusions.
`--paths sample_workflow` does not override exclusions. To follow a sample tutorial,
remove that sample's entry first and make sure no parent folder is excluded.
You can restore the entry afterward when you want normal pushes to skip it.
For dependency rebuilds, an excluded consumer can block the plan; review the
[dynamic deployment rules](../cli/README.md#dependency-aware-deployments).

Skipping a folder does not delete already-deployed samples. Orphan detection
still recognizes their local files. This setting controls deployment scanning;
it does not prevent explicitly requested API training, context-based cleanup,
or calls to an already-deployed sample. The target pin remains required for
online deployment and maintenance operations even when samples are excluded.

## Workspace reference

```text
.agents/skills/             Python and PolyAPI guidance for coding agents
.github/workflows/ci.yml     Python formatting, lint, and tests
AGENTS.md                   Coding-agent navigation and instructions
cli/                         Python Flow and its regression tests
kickstart/                   Interactive project bootstrap
designs/examples/            Canopy and subscription starter JSON
workflows/src/
  sample_workflow/           Message-of-the-day webhook and optional weather AI
  utilities/common/         Logging and global error notifications
pyproject.toml               Runtime and development dependencies
uv.lock                     Reproducible dependency versions
```

Start with the [sample workflow](../workflows/src/sample_workflow/README.md).
See [CONVENTIONS.md](CONVENTIONS.md) for layout and generated SDK rules, and
[cli/README.md](../cli/README.md) for Flow's resource formats and safeguards.

## Local development checks

To install locked dependencies and run checks without configuring a PolyAPI
target, use these Bash commands:

```bash
uv python install 3.12.12
uv sync --locked
make check
uv run --locked python cli/flow.py push --help
```

Python is pinned to 3.12.12 in `.python-version` and CI to match the verified
server runtime. `pyproject.toml` permits compatible 3.12 patch
releases (`>=3.12.12,<3.13`) because Dependabot may resolve dependencies with a
newer 3.12 interpreter. This compatibility range does not change the setup or
CI runtime pin.

Local checks need no API key and do not deploy anything. The SDK is pinned to
`polyapi-python==0.3.21.dev1`. This prerelease includes the Requests 2.33.0
and Socket.IO 5.16.2 dependency fixes. Review SDK upgrades and regenerate
`uv.lock` together. The local SDK pin does not change platform-managed
server-function images.

Workflows and Flow tooling run in Python.
After configuring a target and exporting `ENVPATH`, use `make build-schema`
to refresh platform JSON validators from that instance, or add `--build-schema`
to a Flow push. See the [CLI guide](../cli/README.md#schema-refresh) for schema-source
overrides, AI enrichment, and validation flags.

## Kickstart details

Kickstart asks for the project name, parent directory (default `~/Code/poly`),
namespace (default `demo`), a hidden API key, and region (`dev`, `na1`, `na2`,
or `eu1`). Relative parent paths resolve from your current terminal directory.
It installs the pinned Python dependencies and Python skills (including the
dedicated `api-function-training` skill), updates starter
contexts, SDK references, and code examples in the copied docs, verifies your
target, and generates the SDK.
Confirm whether to use `dev.env` or `prod.env`; the verified `TARGET_ENV_ID` is
saved automatically. The URL/key stay in native SDK setup.

It then creates `main` with an initial commit, switches to `develop`, and opens
VS Code. The current template checkout, including pending source changes, is
copied; ignored local configuration and credential files are excluded.
Kickstart does not deploy resources or train the sample APIs.
The training skill must be available on `poly-skills/main`; setup stops with a
clear message if it is missing.

## Guided setup with `flow`

Use this alternative when you did not use kickstart, or rerun it to change an
existing project's target. Kickstart already configures the new project.

Start with [existing checkout setup](#existing-checkout-or-manual-setup). The helper activates the project
venv even when another venv was active.

Setup runs the native `python -m polyapi setup` prompts and SDK generation,
then verifies the configured key with `GET /whoami`. It shows the tenant and
environment names, suggests `dev` or `prod`, and asks you to confirm the file:
`workflows/dev.env` or `workflows/prod.env`. Unclear names default to dev. This
file choice does not change the actual environment or bypass production guards.

If the file is missing, setup offers to copy `workflows/dev.env.example`. It
saves the verified `TARGET_ENV_ID`, preserving other settings. The URL and API
key stay in native SDK setup; new environment files do not duplicate them.
Existing explicit connection overrides are updated to the confirmed target.
Changing an existing file's target requires a separate confirmation and clears
its execution key; add the correct `POLY_EXEC_API_KEY` before deploying server
functions. The namespace is preserved; follow the [manual target configuration](#configure-a-deployment-target-manually)
when replacing the template's sample namespace.

VS Code gets the `.venv` interpreter, selected Python env file, and `ENVPATH`
for new integrated terminals. Other settings and unrelated JSONC comments are
preserved. If an interpreter was already selected, use **Python: Select
Interpreter** to choose `.venv`; reopen terminals to refresh their environment.

Setup offers to add the loader to your shell profile, showing the path and
command and requiring confirmation. Declining keeps `flow` in this terminal;
source the helper again in another terminal. Existing `flow` commands are never
overwritten. The helper is a shell function because an executable cannot
activate its parent shell. PowerShell uses your existing execution policy.

Once loaded, `flow` works from anywhere inside the project.
`flow --version` and `flow self-update` do not load the selected environment.
Self-update uses the existing interpreter and never runs `uv sync`.

Rerun `flow setup` to change targets. Enter retains the SDK's previous URL/key
and the previous file selection when the authenticated target is unchanged.
Each project remembers its selection in ignored `.flow/setup.json`; no key is
stored in that file or your shell profile. Switching projects resolves that
project's own venv and selection. The profile's loader source must remain at
its installed path; source the new copy if you move or delete that checkout.

The SDK saves its own configuration and generates bindings before the target
confirmation. Cancelling afterward leaves project settings unchanged but does
not roll back that native SDK configuration/generation. Setup performs no
resource deployment. See the [setup reference](../cli/README.md#interactive-setup)
for its configuration boundaries and reuse requirements.

### Enable the pre-commit hook

Run this once from the repository root in each clone:

```bash
git config --local core.hooksPath .githooks
```

The Python hook runs `make check` before each commit and blocks the commit if
formatting, linting, or tests fail. It checks the working tree and does not
format or stage files. Ensure `python3`, `uv`, and `make` are available on the
PATH used by your terminal or Git client.

To run the hook without creating a commit:

```bash
git hook run pre-commit
```

## Configure a deployment target manually

Use this alternative when not using `flow setup`.

Create a Git repository from the template with an initial commit on `main`.
Flow uses Git branch history to select production safeguards.

```bash
cp workflows/dev.env.example workflows/dev.env
```

Edit the copy with your target's values:

| Variable | Purpose |
| --- | --- |
| `POLYAPI_NAMESPACE` | Root context owned by this project; the sample uses `example` |
| `LOG_LEVEL` / `SLACK_CHANNEL_ID` / `SLACK_WORKSPACE_TYPE` | Log threshold, Slack destination, and `community` or `internal` workspace when deploying common utilities |
| `POLY_API_BASE_URL` | Optional override of the instance URL saved by SDK setup |
| `POLY_API_KEY` | Optional Flow/SDK connection override; required in the test process for the direct HTTP webhook integration test |
| `POLY_EXEC_API_KEY` | Execution key for deployed server functions; required when production safeguards apply |
| `TARGET_ENV_ID` | [Pin to the intended environment](#pin-the-target-environment), checked against `GET /whoami` |
| `POLY_DEPLOY_SKIP_PATHS` | Optional [deployment exclusions](#skip-sample-deployments), relative to the project root |

Flow reuses the connection saved by `python -m polyapi setup` in this project's
Python SDK. Neither connection value needs to be in the environment file.
Explicit file values override shell values, which override saved setup, per
field. Flow supports `POLY_API_BASE_URL` / `POLY_API_KEY`, `BASE_URL` /
`API_KEY`, and older `POLYAPI_*` aliases. If only one field is overridden, the
other comes from the shell or saved setup. Empty explicit values do not fall
back silently. Flow passes the resolved pair to its SDK calls and generation.
Standalone `python -m polyapi` commands and SDK-based tests accept the canonical
`POLY_API_BASE_URL` / `POLY_API_KEY` overrides, not Flow's other aliases. They do
not perform Flow's `TARGET_ENV_ID` identity check.

An optional URL override can select `https://eu1.polyapi.io`,
`https://na1.polyapi.io`, `https://na2.polyapi.io`, or another PolyAPI instance.
There is no fixed regional default. Keep the matching `TARGET_ENV_ID` in the
selected file; set `POLY_TEST_WEBHOOK_URL` to its deployed webhook when running
integration tests.

Keep a separate environment file for each target and
[export `ENVPATH` once](#select-an-environment-once) to select it.
Regenerate the Python SDK against the selected target before using its generated
functions; see the [sample's SDK generation command](../workflows/src/sample_workflow/OVERVIEW.md#training-and-sdk-reference).

For multiple owned roots, `POLYAPI_NAMESPACES=example,second` takes precedence.

Choose a namespace that is a valid Python dotted context and is unique to your
project within the target environment, such as `my_project`. It groups the
resources Flow owns; it does not select the instance or environment or grant
access. The base URL, API key, and environment ID identify the target.

The context root must match `POLYAPI_NAMESPACE`: for example, namespace
`my_project` owns contexts such as `my_project.utilities`. Kickstart aligns the
included workflows, utilities, and documentation automatically.

For a fresh manual checkout, choose the same value you put in
`POLYAPI_NAMESPACE` and run this local alignment step before deployment:

```bash
uv run --locked python - <<'PY'
from pathlib import Path
from kickstart.namespace import rewrite_namespace

rewrite_namespace(Path.cwd(), "my_project")
PY
```

This replaces the starter root in code, artefacts, tutorial examples, and
`workflows/dev.env.example`. It does not edit your selected environment file,
rename already-customized roots, or change remote resources. Review
project-prefixed job/trigger names and Canopy subpaths separately. To locate
remaining starter references:

```bash
rg -n '\bexample(?:\b|_)' workflows/src designs/examples
```

Keep external function contexts such as `OOB.polyapi.internal.alerting` and
provider URLs unchanged. Environment files, generated SDK state in `.venv/`,
and Flow's resume state are ignored by Git.

## Verification

[CI](../.github/workflows/ci.yml) runs on pushes, pull requests, and manual dispatch.
It installs Python 3.12.12 and locked dependencies, then runs `make check` for
formatting, lint, and local tests. Live integration tests are explicitly disabled;
CI needs no PolyAPI credentials and performs no deployments.

```bash
make check
make format
uv run --locked pytest cli/tests
uv run --locked pytest workflows/src/sample_workflow/tests/test_motd.py
```

After deploying the sample and configuring its joke secret, set
`POLY_TEST_WEBHOOK_URL` in the selected env file to the webhook `url` returned by
PolyAPI or displayed in Canopy. The HTTP webhook test also requires
`POLY_API_KEY` in its process environment or that ignored env file; it does not
load the SDK's saved key or Flow's aliases. Keep the key out of Git. Webhook
execution uses an environment subdomain, so do not build this URL by appending
`/apis/...` to `POLY_API_BASE_URL`. These direct HTTP/SDK tests do not perform
Flow's `TARGET_ENV_ID` check; confirm that the URL, key, and generated SDK point to
your intended test environment. Then explicitly opt into runtime tests:

```bash
RUN_POLY_INTEGRATION=true uv run --locked --env-file "$ENVPATH" pytest -m integration
```

The webhook tests make two authenticated calls with synthetic input, one per mood;
they do not exercise validation failures, the scheduled job, or the error trigger.
The weather AI test additionally requires `RUN_AI_INTEGRATION=true`, trained tools,
the deployed AI function, a regenerated SDK, and configured provider access. It
checks one successful weather response, not model accuracy or all failure paths.
No local check enables jobs, trains APIs, deploys resources, sends alerts, or
recreates live streams.

## Agent skills

Skills are optional guidance for coding agents, not a prerequisite for following
the samples. Kickstart installs the general, common, and Python skills,
including API Function training. Manual checkouts already include the five core
skills below; optionally add the broader [poly-skills collection](https://github.com/polyapi/poly-skills).

Coding agents should start with [AGENTS.md](../AGENTS.md). It maps common tasks to
the relevant workflow context, explains Python SDK and deployment boundaries,
and provides validation commands. Workflow overviews describe their contracts
and setup. Five bundled skills from [poly-skills](https://github.com/polyapi/poly-skills)
cover SDK usage, artefacts, coding conventions, coding standards, and overview
maintenance; see the [skill index](../AGENTS.md#agent-skills). Keep complete skill
folders, including their references, when refreshing reviewed shared updates.
They are Markdown guidance and need no AI provider credentials or extra runtime
dependencies.

The optional VS Code Code Spell Checker extension uses [cspell.json](../cspell.json)
for shared Python and PolyAPI vocabulary. Personal editor settings stay local.
