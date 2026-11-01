# PolyAPI Python Project Template

A Python 3.12.12 starter project for building, testing, and deploying PolyAPI
workflows with the native Python SDK. It includes example integrations,
shared utilities, local tests, and Flow tooling for managing platform resources.

Flow synchronizes Python functions, Vari, Tabi, schemas, webhooks, triggers,
jobs, AI functions, Canopy applications, and GraphQL subscriptions. Its
`push-dynamic` command rebuilds consuming server functions when a shared client
changes, including dependencies through other client functions.

## Quick start

Getting started is straightforward. Run this command and follow the prompts
to bootstrap the project locally:

```bash
git clone git@github.com:polyapi/poly-py-project-template.git && ./poly-py-project-template/kickstart/kickstart.sh
```

Requires [uv](https://docs.astral.sh/uv/), Git, GitHub SSH access to the template
and skills repositories, and the VS Code `code` command. GitHub CLI (`gh`) and
Node.js are not required.

Kickstart asks for the project name, parent directory (default `~/Code/poly`),
namespace (default `demo`), a hidden API key, and region (`dev`, `na1`, `na2`,
or `eu1`). Relative parent paths resolve from your current terminal directory.
It installs the pinned Python dependencies and Python skills (including the
dedicated `api-function-training` skill), updates starter
contexts and SDK references, verifies your target, and generates the SDK.
Confirm whether to use `dev.env` or `prod.env`; the verified `TARGET_ENV_ID` is
saved automatically. The URL/key stay in native SDK setup.

It then creates `main` with an initial commit, switches to `develop`, and opens
VS Code. The current template checkout, including pending source changes, is
copied; ignored local configuration and credential files are excluded.
Kickstart does not deploy resources or train the sample APIs.
The training skill must be available on `poly-skills/main`; setup stops with a
clear message if it is missing.

### Existing checkout or manual setup

These setup steps are only needed if you did not use kickstart, or want to
change the target of an existing project. From the project root, load the
helper for your shell once:

| Shell | Load the `flow` command |
| --- | --- |
| Bash | `source cli/shell/flow.bash` |
| Fish | `source cli/shell/flow.fish` |
| PowerShell | `. ./cli/shell/flow.ps1` |

Then run:

```bash
flow setup
```

This creates and activates `.venv`, runs the Python SDK setup, verifies your
tenant and environment, and asks you to confirm **dev or prod**. It configures
the selected env file and VS Code, and offers to save `flow` for future terminals.

Next, follow the [sample workflow setup](workflows/src/sample_workflow/OVERVIEW.md#training-and-configuration)
to train its APIs and configure its secrets, then preview deployment:

```bash
flow push-dynamic --paths sample_workflow --dry-run
```

See [guided setup details](#guided-setup-with-flow) for target changes, existing
commands, and configuration safeguards. Setup itself does not deploy resources.

## Structure

```text
.agents/skills/             Python and PolyAPI guidance for coding agents
.github/workflows/ci.yml     Python formatting, lint, and tests
AGENTS.md                   Coding-agent navigation and instructions
cli/                         Python Flow and its regression tests
kickstart/                   Interactive project bootstrap
designs/examples/            Canopy and subscription starter JSON
workflows/src/
  sample_workflow/           Message-of-the-day webhook workflow
  sample_ai_fx/              Weather AI with no-key weather tools
  utilities/common/         Logging and global error notifications
pyproject.toml               Runtime and development dependencies
uv.lock                     Reproducible dependency versions
```

Start with the [sample workflow](workflows/src/sample_workflow/OVERVIEW.md).
See [CONVENTIONS.md](CONVENTIONS.md) for layout and generated SDK rules, and
[cli/README.md](cli/README.md) for Flow's resource formats and safeguards.

Coding agents should start with [AGENTS.md](AGENTS.md). It maps common tasks to
the relevant workflow context, explains Python SDK and deployment boundaries,
and provides validation commands. Workflow overviews describe their contracts
and setup. Five bundled skills from [poly-skills](https://github.com/polyapi/poly-skills)
cover SDK usage, artefacts, coding conventions, coding standards, and overview
maintenance; see the [skill index](AGENTS.md#agent-skills). Keep complete skill
folders, including their references, when refreshing reviewed shared updates.
They are Markdown guidance and need no AI provider credentials or extra runtime
dependencies.

The optional VS Code Code Spell Checker extension uses [cspell.json](cspell.json)
for shared Python and PolyAPI vocabulary. Personal editor settings stay local.

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
After configuring a target, use `ENVPATH=workflows/dev.env make build-schema`
to refresh platform JSON validators from that instance, or add `--build-schema`
to a Flow push. See the [CLI guide](cli/README.md#schema-refresh) for schema-source
overrides, AI enrichment, and validation flags.

## Guided setup with `flow`

Use this alternative when you did not use kickstart, or rerun it to change an
existing project's target. Kickstart already configures the new project.

Start with the [quick start](#quick-start). The helper activates the project
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
functions. The namespace is preserved; follow the customization instructions
below when replacing the template's sample namespace.

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

Rerun `flow setup` to change targets. Enter retains the SDK's previous URL/key
and the previous file selection when the authenticated target is unchanged.
Each project remembers its selection in ignored `.flow/setup.json`; no key is
stored in that file or your shell profile. Switching projects resolves that
project's own venv and selection. The profile's loader source must remain at
its installed path; source the new copy if you move or delete that checkout.

The SDK saves its own configuration and generates bindings before the target
confirmation. Cancelling afterward leaves project settings unchanged but does
not roll back that native SDK configuration/generation. Setup performs no
resource deployment. See the [setup reference](cli/README.md#interactive-setup)
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
| `POLY_API_KEY` | Optional override of the environment key saved by SDK setup |
| `POLY_EXEC_API_KEY` | Execution key for deployed server functions; required when production safeguards apply |
| `TARGET_ENV_ID` | Environment ID declared in this file, checked against `GET /whoami` |

Flow reuses the connection saved by `python -m polyapi setup` in this project's
Python SDK. Neither connection value needs to be in the environment file.
Explicit file values override shell values, which override saved setup, per
field. Python supports `POLY_API_BASE_URL` / `POLY_API_KEY`, `BASE_URL` /
`API_KEY`, and older `POLYAPI_*` aliases. If only one field is overridden, the
other comes from the shell or saved setup. Empty explicit values do not fall
back silently. The resolved pair also applies to native SDK calls and generation.

An optional URL override can select `https://eu1.polyapi.io`,
`https://na1.polyapi.io`, `https://na2.polyapi.io`, or another PolyAPI instance.
There is no fixed regional default. Keep the matching `TARGET_ENV_ID` in the
selected file; set `POLY_TEST_WEBHOOK_URL` to its deployed webhook when running
integration tests.

`TARGET_ENV_ID` must be declared in the selected environment file; exporting
the pin only in your shell is insufficient.

Keep a separate environment file for each target and select it with `ENVPATH`.
Regenerate the Python SDK against the selected target before using its generated
functions; the SDK generation command is shown below.

For multiple owned roots, `POLYAPI_NAMESPACES=example,second` takes precedence.

Choose a namespace that is a valid Python dotted context and is unique to your
project within the target environment, such as `my_project`. It groups the
resources Flow owns; it does not select the instance or environment or grant
access. The base URL, API key, and environment ID identify the target.

Replace `example` consistently in local contexts, generated SDK calls,
portable references, job/trigger names, webhook slugs, and Canopy subpaths:

```bash
rg -n '\bexample(?:\b|_)' workflows/src designs/examples
```

Keep external function contexts such as `OOB.polyapi.internal.alerting` and
provider URLs unchanged. Environment files, generated SDK state in `.venv/`,
and Flow's resume state are ignored by Git.

## Train, preview, and deploy the sample

The message-of-the-day sample calls external joke and advice APIs through the
generated Python SDK. Follow
[the sample setup](workflows/src/sample_workflow/OVERVIEW.md#training-and-configuration)
to train its OpenAPI specifications with `hostUrl` as an argument. After deploying
Vari, set the SECRET joke configuration in Canopy. The OBSCURED advice
configuration includes its public host URL.

Use a development environment and a branch based on `develop`. The example
configuration binds `develop` to `workflows/dev.env`; branches based directly
on `main` use production safeguards.

```bash
git switch -c develop
git switch -c feature/my-workflow
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic --paths sample_workflow --dry-run
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic --paths sample_workflow
```

Review the dry-run target and expanded dependency plan before running without
`--dry-run`. See [dependency-aware deployments](cli/README.md#dependency-aware-deployments)
for named functions, nested consumers, exclusions, and interrupted-run recovery.
Flow verifies the authenticated environment before resource requests and, by
default, generates the SDK after pushes that include Python or AI functions.
To generate the SDK separately using the same target:

```bash
uv run --locked --env-file workflows/dev.env python -m polyapi generate
```

A full push also includes common utilities and the enabled weather AI function.
Train its bundled Open-Meteo specs using the [weather setup](workflows/src/sample_ai_fx/OVERVIEW.md),
and configure the AI provider/model and utility log/channel settings before
invoking them. The weather API needs no account, key, or trial for evaluation;
AI execution can still incur model-provider usage. The global error notification
trigger starts enabled; the sample scheduled job starts disabled.
Canopy and subscription starter JSON stays under `designs/examples/` until
you configure it and move it into a workflow's `artefacts/` directory.

## Verification

[CI](.github/workflows/ci.yml) runs on pushes, pull requests, and manual dispatch.
It installs Python 3.12.12 and locked dependencies, then runs `make check` for
formatting, lint, and local tests. Live integration tests are explicitly disabled;
CI needs no PolyAPI credentials and performs no deployments.

```bash
make check
make format
uv run --locked pytest cli/tests
uv run --locked pytest workflows/src/sample_workflow/tests/test_motd.py
```

After deploying the sample, set `POLY_TEST_WEBHOOK_URL` in the selected env
file to the webhook `url` returned by PolyAPI or displayed in Canopy. Webhook
execution uses an environment subdomain, so do not build this URL by appending
`/apis/...` to `POLY_API_BASE_URL`. Then explicitly opt into runtime tests:

```bash
RUN_POLY_INTEGRATION=true uv run --locked --env-file workflows/dev.env pytest -m integration
```

The webhook tests make two authenticated calls with synthetic input. The weather
AI test additionally requires `RUN_AI_INTEGRATION=true`. No local check enables
jobs, trains APIs, deploys resources, sends alerts, or recreates live streams.

## Included examples and tools

| Component | Purpose |
| --- | --- |
| Message-of-the-day workflow | Calls external joke/advice APIs using Vari configuration and secret injection; includes a security function, webhook, trigger, and scheduled job |
| Weather AI | Uses bundled Open-Meteo tools without a weather key or trial, with structured output and attribution |
| Shared utilities | Provides log filtering, recursive redaction, Slack alerts, and a global error handler |
| Flow | Validates and synchronizes functions and platform resources; rebuilds nested client consumers and provides offline graph reports |
| Canopy | Creates applications and synchronizes their configuration and descriptions |
| GraphQL subscriptions | Configures event streams that invoke server functions |
| API tooling | Translates OpenAPI specifications, enriches models with AI, and trains API functions |
| Operations | Provides context-scoped cleanup and paginated function logs |

See the [CLI reference](cli/README.md) for resource formats, commands, and
configuration. Local tests verify contracts and planned effects. Deployment,
provider behavior, and secret resolution can be verified with the opt-in
integration tests against a configured environment.

## Additional resources

- [PolyAPI overview](https://docs.polyapi.io/platform_overview.html)
- [Python SDK documentation](https://docs.polyapi.io/generated_sdks/python.html)
- [PolyAPI agent skills](https://github.com/polyapi/poly-skills)

The template does not bundle customer credentials, generated environment SDKs,
or deployment automation.
