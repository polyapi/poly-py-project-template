# PolyAPI Python Project Template

A Python 3.12.12 starter project for building, testing, and deploying PolyAPI
workflows with the native Python SDK. It includes example integrations,
shared utilities, local tests, and Flow tooling for managing platform resources.

Flow synchronizes Python functions, Vari, Tabi, schemas, webhooks, triggers,
jobs, AI functions, Canopy applications, and GraphQL subscriptions. Its
`push-dynamic` command rebuilds consuming server functions when a shared client
changes, including dependencies through other client functions.

## Structure

```text
.agents/skills/             Python and PolyAPI guidance for coding agents
.github/workflows/ci.yml     Python formatting, lint, and tests
AGENTS.md                   Coding-agent navigation and instructions
cli/                         Python Flow and its regression tests
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

## Local setup

Install [uv](https://docs.astral.sh/uv/), then run these Bash commands:

```bash
uv python install 3.12.12
uv sync --locked
make check
uv run --locked python cli/flow.py push --help
```

Python is pinned to 3.12.12 in `.python-version` and CI to match the verified
SDK 0.3.20 server base image. `pyproject.toml` permits compatible 3.12 patch
releases (`>=3.12.12,<3.13`) because Dependabot may resolve dependencies with a
newer 3.12 interpreter. This compatibility range does not change the setup or
CI runtime pin.

Local checks need no API key and do not deploy anything. The SDK is pinned to
`polyapi-python==0.3.20`. Review SDK upgrades and regenerate `uv.lock`
together.

Workflows and Flow tooling run in Python.
After configuring a target, use `ENVPATH=workflows/dev.env make build-schema`
to refresh platform JSON validators from that instance, or add `--build-schema`
to a Flow push. See the [CLI guide](cli/README.md#schema-refresh) for schema-source
overrides, AI enrichment, and validation flags.

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

## Configure a deployment target

Create a Git repository from the template with an initial commit on `main`.
Flow uses Git branch history to select production safeguards.

```bash
cp workflows/dev.env.example workflows/dev.env
```

Edit the copy with your target's values:

| Variable | Purpose |
| --- | --- |
| `POLYAPI_NAMESPACE` | Root context owned by this project; the sample uses `example` |
| `LOG_LEVEL` / `SLACK_CHANNEL_ID` | Log threshold and alert channel when deploying common utilities |
| `POLY_API_BASE_URL` | Base URL of your selected PolyAPI instance |
| `POLY_API_KEY` | Key for the selected environment |
| `POLY_EXEC_API_KEY` | Execution key for deployed server functions; required when production safeguards apply |
| `TARGET_ENV_ID` | Environment ID declared in this file, checked against `GET /whoami` |

Python uses the SDK names `POLY_API_BASE_URL` and `POLY_API_KEY`.
Set `POLY_API_BASE_URL` to `https://eu1.polyapi.io`, `https://na1.polyapi.io`,
`https://na2.polyapi.io`, or the base URL of another PolyAPI instance. The
template has no fixed regional default. Use the API key and environment ID
for that instance; set `POLY_TEST_WEBHOOK_URL` to its deployed webhook when
running integration tests.

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
