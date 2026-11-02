# PolyAPI Python Project Template

![Your next integration. Built in Python. A PolyAPI project template with workflows, artefacts, and development tools.](.images/project-template-banner.svg)

Build, test, and deploy PolyAPI workflows with the native Python SDK. This Python
3.12.12 workspace includes sample integrations, shared utilities, local tests,
agent guidance, and Flow tooling for managing platform resources.

New to PolyAPI? Start with the [message-of-the-day tutorial](workflows/src/sample_workflow/README.md),
then explore the [AI and shared utility samples](#learn-by-example). Each lesson
walks through a working integration and explains the pieces as you use them.

## Getting started

Requires [uv](https://docs.astral.sh/uv/), curl, tar, Git, and the VS Code `code`
command (on macOS, run
**Shell Command: Install 'code' command in PATH** from the VS Code command
palette). GitHub authentication, Node.js, and GitHub CLI (`gh`) are not required.

```bash
curl -fsSL https://raw.githubusercontent.com/polyapi/poly-py-project-template/main/kickstart/kickstart.sh | bash
```

Kickstart downloads the public template into a temporary directory and fetches
skills with a shallow HTTPS Git clone. You do not need to clone the template
first. From an existing template checkout, `bash kickstart/kickstart.sh` also works.

The parent directory defaults to your current terminal directory. Your project
is created inside it using the project name you enter.

Follow the prompts. Kickstart installs dependencies and skills, configures your
namespace and verified target, updates starter contexts, SDK references, and tutorial examples,
generates the SDK, creates `main` and `develop`,
and opens VS Code. It does not deploy resources or train the sample APIs.

After SDK generation, kickstart offers to save the `flow` helper for future
Bash, zsh, Fish, or PowerShell terminals. Choose your shell and confirm the shown
profile file before it writes anything; Enter at the save prompt declines.
Skipping this step leaves setup complete. For your current terminal, load the
[appropriate helper](docs/SETUP.md#existing-checkout-or-manual-setup), or use the
explicit `uv run` commands below without it.

Already have a checkout or need to change targets? Follow the
[guided setup](docs/SETUP.md#existing-checkout-or-manual-setup).

## Learn by example

Follow the samples in this order. Each guide includes a first run, expected
results, and exercises, with deeper reference material below the walkthrough.

| Sample | What you will learn |
| --- | --- |
| [Message of the day](workflows/src/sample_workflow/README.md) | Webhooks, triggers, server/client functions, trained APIs, Vari, secret injection, and a scheduled job. |
| [Weather AI](workflows/src/sample_workflow/OVERVIEW.md#weather-ai) | AI tool calls, structured weather guidance, provider configuration, and data attribution. |
| [Shared utilities](workflows/src/utilities/common/OVERVIEW.md) | Structured logging, recursive redaction, global error handling, and Slack alerts. |

The weather sample uses Open-Meteo without a weather API key; AI execution still
needs a configured provider and can incur usage charges. Follow each sample's
setup instructions before deploying or invoking it.

To build your own integration, start from the closest sample and follow the
[workflow layout and naming rules](docs/CONVENTIONS.md#workflow-layout).
Keep the examples for reference while [excluding sample deployments](docs/SETUP.md#skip-sample-deployments)
with `POLY_DEPLOY_SKIP_PATHS` when you no longer want them in normal pushes.

## Everyday commands

Run from the project root after setup:

```bash
export ENVPATH=workflows/dev.env
uv run --locked python cli/flow.py --version
make check
uv run --locked pytest workflows/src/sample_workflow/tests/test_motd.py
uv run --locked python cli/flow.py \
  push-dynamic --paths sample_workflow --types functions,variables,schemas,webhooks,triggers,jobs --dry-run
```

Export your setup-selected path once per terminal session; Python Flow resolves
it from the project root. Keep your owned namespace and verified
[`TARGET_ENV_ID` pin](docs/SETUP.md#pin-the-target-environment) in the selected environment
file. Review the target and expanded deployment plan before a real push. Keep
real environment files and secrets out of Git.

Local checks need no API key and perform no deployment. Live tests require
explicit opt-in; see [verification](docs/SETUP.md#verification) and the
[sample deployment guide](workflows/src/sample_workflow/README.md#run-your-first-request).

## Structure

```text
.agents/skills/ bundled Python and PolyAPI agent guidance
cli/           Python Flow CLI and its regression tests
designs/       upstream specifications and starter artefacts
kickstart/     interactive project bootstrap
docs/          setup, development, and project conventions
workflows/src/ sample integrations, shared utilities, and runtime tests
```

## Further reading

| Guide | Covers |
| --- | --- |
| [Setup and development](docs/SETUP.md) | Shell helpers, manual targets, SDK pins, editor setup, local checks, and live-test opt-ins. |
| [Flow CLI](cli/README.md) | Dependency-aware deployment, API training, Canopy, subscriptions, cleanup, logs, and safeguards. |
| [Upgrading Flow](docs/UPGRADING_FLOW.md) | Self-update, customization review, backups, and recovery. |
| [Project conventions](docs/CONVENTIONS.md) | Workflow layout, naming, generated SDK usage, ownership, and tests. |
| [Agent guidance](AGENTS.md) · [Bundled skills](docs/SETUP.md#agent-skills) | Repository rules and maintained skills from [poly-skills](https://github.com/polyapi/poly-skills). |

Platform references: [PolyAPI overview](https://docs.polyapi.io/platform_overview.html)
and [Python SDK documentation](https://docs.polyapi.io/generated_sdks/python.html).
The template does not bundle customer credentials, generated environment SDKs,
or deployment automation.
