# PolyAPI Python Project Template

A Python 3.12 starter project for building, testing, and deploying PolyAPI
workflows with the native Python SDK. It includes example integrations,
shared utilities, local tests, and Flow tooling for managing platform resources.

Flow synchronizes Python functions, Vari, Tabi, schemas, webhooks, triggers,
jobs, AI functions, Canopy applications, and GraphQL subscriptions.

## Structure

```text
.agents/skills/             Python and PolyAPI guidance for coding agents
.github/workflows/ci.yml     Python formatting, lint, and tests
AGENTS.md                   Coding-agent navigation and instructions
cli/                         Python Flow and its regression tests
designs/examples/            Canopy and subscription starter JSON
workflows/src/
  sample_workflow/           Message-of-the-day webhook workflow
  sample_ai_fx/              Weather AI with tools and secret binding
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
and setup. Five local skills cover SDK usage, artefacts, naming, coding
standards, and overview maintenance; see the [skill index](AGENTS.md#agent-skills).
They are Markdown guidance and need no AI provider credentials or extra runtime
dependencies.

## Local setup

Install Python 3.12 and [uv](https://docs.astral.sh/uv/), then run:

```fish
uv sync --locked
make check
uv run --locked python cli/flow.py push --help
```

Local checks need no API key and do not deploy anything. The SDK is pinned to
`polyapi-python==0.3.20.dev4`. Review SDK upgrades and regenerate `uv.lock`
together.

Workflows and Flow tooling run in Python.
After configuring a target, use `env ENVPATH=workflows/dev.env make build-schema`
to refresh platform JSON validators from that instance, or add `--build-schema`
to a Flow push. See the [CLI guide](cli/README.md#schema-refresh) for schema-source
overrides, AI enrichment, and validation flags.

### Enable the pre-commit hook

Run this once from the repository root in each clone:

```fish
git config --local core.hooksPath .githooks
```

The Python hook runs `make check` before each commit and blocks the commit if
formatting, linting, or tests fail. It checks the working tree and does not
format or stage files. Ensure `python3`, `uv`, and `make` are available on the
PATH used by your terminal or Git client.

To run the hook without creating a commit:

```fish
git hook run pre-commit
```

## Configure a deployment target

Create a Git repository from the template with an initial commit on `main`.
Flow uses Git branch history to select production safeguards.

```fish
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

Replace `example` consistently in local contexts, generated SDK calls,
portable references, job/trigger names, webhook slugs, and Canopy subpaths:

```fish
rg -n '\bexample\b' workflows/src designs/examples
```

Keep external function contexts such as `OOB.polyapi.internal.alerting`
unchanged. Environment files, generated SDK state in `.venv/`, and Flow's
resume state are ignored by Git.

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

```fish
git switch -c develop
git switch -c EN/my-workflow
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_workflow --dry-run
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_workflow
```

Review the dry-run target and plan before running the push without `--dry-run`.
Flow verifies the authenticated environment before resource requests and, by
default, generates the SDK after pushes that include Python or AI functions.
To generate the SDK separately using the same target:

```fish
uv run --locked --env-file workflows/dev.env python -m polyapi generate
```

A full push also includes common utilities and the enabled weather AI function.
Configure its AccuWeather secret, accessible tools, provider/model, and the
utility log/channel settings before invoking them. The global error notification
trigger starts enabled; the sample scheduled job starts disabled.
Canopy and subscription starter JSON stays under `designs/examples/` until
you configure it and move it into a workflow's `artefacts/` directory.

## Verification

[CI](.github/workflows/ci.yml) runs on pushes, pull requests, and manual dispatch.
It installs Python 3.12 and locked dependencies, then runs `make check` for
formatting, lint, and local tests. Live integration tests are explicitly disabled;
CI needs no PolyAPI credentials and performs no deployments.

```fish
make check
make format
uv run --locked pytest cli/tests
uv run --locked pytest workflows/src/sample_workflow/tests/test_motd.py
```

After deploying the sample, set `POLY_TEST_WEBHOOK_URL` in the selected env
file to the exact deployed webhook URL, then explicitly opt into runtime tests:

```fish
env RUN_POLY_INTEGRATION=true uv run --locked --env-file workflows/dev.env pytest -m integration
```

The webhook tests make two authenticated calls with synthetic input. The weather
AI test additionally requires `RUN_AI_INTEGRATION=true`. No local check enables
jobs, trains APIs, deploys resources, sends alerts, or recreates live streams.

## Included examples and tools

| Component | Purpose |
| --- | --- |
| Message-of-the-day workflow | Calls external joke/advice APIs using Vari configuration and secret injection; includes a security function, webhook, trigger, and scheduled job |
| Weather AI | Uses two AccuWeather tools, secret bindings, and structured output |
| Shared utilities | Provides log filtering, recursive redaction, Slack alerts, and a global error handler |
| Flow | Validates, previews, and synchronizes functions and platform resources from local code and JSON artefacts |
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
