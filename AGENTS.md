# AGENTS.md

Generic PolyAPI Python workspace with deployable code under `workflows/src/`
and Flow tooling under `cli/`.

## Source of truth

Read this file, the [root README](README.md), and [CONVENTIONS.md](docs/CONVENTIONS.md),
then the relevant workflow's `OVERVIEW.md`. Verify documentation against
checked-in code, artefacts, and tests before relying on it. Load the relevant
local skill below for the behavior being implemented or reviewed.

## Find the relevant code

| Task | Start here |
| --- | --- |
| Webhooks, API calls, or secret injection | [Sample workflow](workflows/src/sample_workflow/OVERVIEW.md) |
| AI tools and structured weather output | [Weather AI](workflows/src/sample_workflow/OVERVIEW.md#weather-ai) |
| Logging or error notifications | [Common utilities](workflows/src/utilities/common/OVERVIEW.md) |
| Shell setup and editor/target selection | [Interactive setup](cli/README.md#interactive-setup), `cli/setup.py`, and `cli/shell/` |
| New-project bootstrapping | [Getting started](README.md#getting-started), `kickstart/kickstart.sh`, `kickstart/kickstart.py`, and `kickstart/configure.py` |
| Deployment, API training, or schema refresh | [Flow reference](cli/README.md), then `cli/flow.py`, `cli/operations.py`, or `cli/schema_refresh.py` |
| Flow upgrades and recovery | [Upgrading Flow](docs/UPGRADING_FLOW.md), `cli/self_update.py`, and `cli/tests/test_self_update.py` |
| Local checks or CI | `Makefile`, `pyproject.toml`, `conftest.py`, and `.github/workflows/ci.yml` |

Read only the paths relevant to the task. Inspect a maintained example before
adding a pattern. Create directories only when they contain needed code or
artefacts; keep operator scripts outside `workflows/src/`.

## Implementation

- Flow's version lives in `cli/__init__.py`, independently of the workspace
  version and SDK dependency. Starting with 1.1.0, use patch releases for fixes,
  minor releases for compatible features, and major releases for breaking
  changes. Group related unreleased changes in one release, rather than
  bumping for every edit. Version commands must read that single source.
- Use Python 3.12.12, uv, Black, Flake8, and pytest.
- Keep function code, `polyConfig`, JSON artefacts, and tests aligned.
- Use the native `polyapi-python` SDK and generated calls for deployed dependencies.
  Verify signatures in the selected environment's generated SDK; do not invent
  methods, add fallback shims, or use another language's SDK.
- Each deployable file must be self-contained. Flow submits that file's source;
  neighboring Python modules are not bundled. Keep its types and `polyConfig`
  inline, and call shared deployed functions through the generated SDK.
- Keep all locally managed contexts under the configured `POLYAPI_NAMESPACE(S)` roots.
- Use snake_case and `_cfx.py` / `_sfx.py` file suffixes. Preserve external SDK
  identifiers, API field names, generated schema filenames, and resource kinds.
- Prefer existing Flow mechanisms over new wrappers or deployment scripts.

## Validate and finish

Use Bash for documented shell commands. Run commands from the repository root:

```bash
uv sync --locked
uv run --locked pytest cli/tests/test_preflight.py
uv run --locked pytest workflows/src/sample_workflow/tests/test_motd.py
make check
```

Choose the focused test file for the behavior being changed; the sample above
checks the message-of-the-day workflow. The preflight tests validate local
artefacts and namespace ownership without credentials. `make check` runs Black,
Flake8, and all local tests, matching CI and the optional pre-commit hook.

Update affected guides and workflow overviews with code changes. Report changed
behavior, checks actually run, and unresolved issues. Keep runtime tests opt-in;
local tests and dry runs do not prove deployment or live SDK behavior.

## Safety

- Do not read, copy, print, or commit real environment files or credentials.
  `workflows/dev.env.example` contains placeholders only.
- Never access private keys, certificates, or SDK credential stores.
- Preserve the environment pin, Git production safeguards, OTP handling,
  namespace checks, and explicit orphan-deletion flag.
- The `--skip-validation` acknowledgement bypasses confirmation
  prompts only; it never bypasses keys, OTP, target identity, or schema checks.
- Keep commits, publishing, training, deployments, cleanup, and live tests
  within the user's explicit authorization. A passing local check authorizes
  none of these actions. Review deployment dry runs before a real push.
- GraphQL subscription recreation deletes and creates a stream; preserve its
  separate interactive confirmation and warn that offsets may reset.
- Do not silently upgrade dependencies or change CI/CD or production safeguards.
- Preserve unrelated user changes and avoid destructive Git operations.

## Agent skills

Focused skills are bundled under `.agents/skills/`. Read only the ones relevant
to the task; they provide guidance, not authorization to deploy or publish.

| Work | Local skill |
| --- | --- |
| Python functions, SDK contracts, and secret injection | [polyapi-sdk](.agents/skills/polyapi-sdk/SKILL.md) |
| Workflow resources, references, and ownership | [artefacts-usage](.agents/skills/artefacts-usage/SKILL.md) |
| Names, contexts, and resource renames | [coding-conventions](.agents/skills/coding-conventions/SKILL.md) |
| Error handling, logging, and redaction | [coding-standards](.agents/skills/coding-standards/SKILL.md) |
| Workflow documentation after behavior changes | [generate-overview](.agents/skills/generate-overview/SKILL.md) |

These are complete copies of the four [Python skills](https://github.com/polyapi/poly-skills/tree/main/polyapi-python/skills)
and the common [generate-overview skill](https://github.com/polyapi/poly-skills/tree/main/polyapi-common/skills/generate-overview)
maintained in `poly-skills`. Keep those package names and copy their supporting
references when refreshing reviewed updates.

Keep this template's paths, SDK pin, commands, and deployment safeguards in this
file and [CONVENTIONS.md](docs/CONVENTIONS.md). Check shared guidance against the
current implementation before applying an update.
