# PolyAPI Python Project Template

![Your next integration. Built in Python. A PolyAPI project template with workflows, artefacts, and development tools.](.images/project-template-banner.svg)

Build, test, and deploy PolyAPI workflows with the native Python SDK. This Python
3.12.12 workspace includes sample integrations, shared utilities, local tests,
agent guidance, and Flow tooling for managing platform resources.

New to PolyAPI? Start with the [message-of-the-day tutorial](workflows/src/sample_workflow/README.md),
then explore the [AI and shared utility samples](#learn-by-example). Each lesson
walks through a working integration and explains the pieces as you use them.

## Getting started

Requires only curl on macOS or Linux, which also provide the `tar` command it
uses. On Windows, run it in [WSL](docs/SETUP.md#windows). GitHub authentication,
Node.js, and GitHub CLI (`gh`) are not required.

```bash
curl -fsSL https://raw.githubusercontent.com/polyapi/poly-py-project-template/main/kickstart/kickstart.sh | bash
```

Kickstart downloads public snapshots of the template and skills over HTTPS into
a temporary directory. You do not need to clone the template first. From an
existing template checkout, `bash kickstart/kickstart.sh` also works.

If [uv](https://docs.astral.sh/uv/) is missing, kickstart installs it for your
user account with Astral's official installer, which adds it to your shell
profile; open a new terminal afterwards. uv then downloads the pinned Python
3.12.12 when it is not already available. Git and VS Code are optional. Without
Git, kickstart prints the commands that create the repository later; without a
Git name and email, it skips the initial commit and tells you how to finish.
Without VS Code's `code` command (on macOS, run **Shell Command: Install 'code'
command in PATH** from the VS Code command palette), it prints the project path
instead of opening it.

The parent directory defaults to your current terminal directory. Your project
is created inside it using the project name you enter.

Follow the prompts. Kickstart installs dependencies and skills, configures your
namespace and verified target, updates starter contexts, SDK references, and tutorial examples,
generates the SDK, creates `main` and `develop`,
and opens VS Code. It does not deploy resources or train the sample APIs.

After SDK generation, kickstart offers to save the `flow` helper for future
Bash, zsh, Fish, or PowerShell terminals. Choose your shell and confirm the shown
profile file before it writes anything; Enter at the save prompt declines.
Skipping this step leaves setup complete.

For your current Bash or zsh terminal, change into the new project and load the
helper. Replace `your-project-name` with the name you entered, or use the full
project path printed by kickstart if you chose a different parent directory:

```bash
cd your-project-name
source cli/shell/flow.bash
flow --version
```

Sourcing the helper makes `flow` available in this terminal. When you run project
commands, it automatically activates the project's `.venv` and selects the
environment saved by setup. You do not need to source `.venv/bin/activate`
separately. Load the helper in each new terminal unless your saved profile
already does it; see the [other shell helpers](docs/SETUP.md#existing-checkout-or-manual-setup)
for Fish and PowerShell.

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

Run from the project root after setup and loading the helper:

```bash
flow --version
make check
uv run --locked pytest workflows/src/sample_workflow/tests/test_motd.py
flow push-dynamic --paths sample_workflow --types functions,variables,schemas,webhooks,triggers,jobs --dry-run
```

`pytest` runs Python tests. `uv run` uses the project's managed `.venv` without
manual activation. `--locked` keeps `uv.lock` unchanged and stops if the lockfile
is missing or out of date; see the [uv command reference](https://docs.astral.sh/uv/reference/cli/#uv-run).

`flow` uses the environment saved by setup; rerun `flow setup` to change targets.
If you prefer explicit `uv run --locked python cli/flow.py ...` commands without
the helper, [export `ENVPATH` once](docs/SETUP.md#select-an-environment-once).
Keep your owned namespace and verified
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

## Contributing

Bug fixes, clearer documentation, and better examples are welcome.

1. For larger changes, [open an issue](https://github.com/polyapi/poly-py-project-template/issues)
   first so we can agree on the approach.
2. Fork and clone this repository, then create a branch for your change. Use a
   direct clone rather than kickstart, which creates a personalised project.
3. Follow the [project conventions](docs/CONVENTIONS.md). Keep code, artefacts,
   tests, and affected guides aligned. Never include credentials or customer data.
4. For code changes, run the local checks below. For documentation changes,
   verify links and examples against the current implementation.
5. Open a focused pull request against `main`, explaining the change and how you
   checked it. A maintainer will review it.

Install dependencies and run local checks from the repository root:

```bash
uv sync --locked
make check
```

## License

Licensed under the [MIT License](LICENSE).
