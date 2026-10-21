# Python Flow CLI

Python Flow validates and synchronizes PolyAPI resources from local Python
functions and JSON artefacts. It also provides OpenAPI training with AI metadata
enrichment, scoped resource cleanup, function logs, and schema refresh.

See the [project README](../README.md) for Python 3.12 setup, dependencies,
environment configuration, and the sample workflow.

Run from the repository root:

```fish
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --dry-run
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_workflow --types functions,variables,webhooks,triggers,jobs
```

Run utility commands with `uv run --locked python -m cli.operations --help`.

`--paths` accepts comma-separated repository paths or shortcuts below
`workflows/src/`. `--types` accepts the resource types below, plus
`client-functions` and `server-functions` as narrower function selectors.

| Type | Source | Synchronization |
| --- | --- | --- |
| `functions` | `client/*.py`, `server/*.py` | Create/update Python functions from inline `polyConfig` |
| `variables` | `artefacts/vari/**/*.json`, `*.jsonc` | Create/update Vari; existing secret values protected by default |
| `tables` | `artefacts/tabi/*.json` | Synchronize Tabi definitions |
| `schemas` | `artefacts/schemas/*.json`, `*.jsonc` | Create/update named schemas |
| `webhooks` | `artefacts/webhooks/*.json` | Create/update endpoints and security references |
| `triggers` | `artefacts/triggers/*.json` | Resolve webhook and server references; recreate structural changes |
| `jobs` | `artefacts/jobs/*.json` | Resolve server functions and synchronize schedules |
| `ai-functions` | `artefacts/ai-functions/*.json`, `*.jsonc` | Create and patch AI functions |
| `canopy` | `artefacts/canopy/*.json`, `*.jsonc` | Create applications and update configuration/description |
| `subscriptions` | `artefacts/subscriptions/*.json`, `*.jsonc` | Create GraphQL streams; existing streams require confirmed recreation |

Both `artefacts/` and legacy `artifacts/` spellings are discovered.

The push order is Vari → Canopy → Tabi → schemas → Python functions →
AI functions → webhooks → triggers → jobs → subscriptions. Client functions
are synchronized before server functions. Pushes selecting Python or AI functions
regenerate the SDK even when remote functions are unchanged, unless `--skip-generate`
is supplied or generation is disabled in configuration. Dry runs do not generate.
A failed generation is retried by the next push, including `--resume`.

## Target and update safeguards

Set `POLY_API_BASE_URL` to the instance you intend to use: for example,
`https://eu1.polyapi.io`, `https://na1.polyapi.io`, `https://na2.polyapi.io`, or
another PolyAPI instance URL. Set `POLY_API_KEY` and `TARGET_ENV_ID` for the
environment on that instance. Keep separate environment files for separate
targets and select one with `ENVPATH`.

Flow loads `ENVPATH`, or root `.env` when omitted. The selected file must
declare `TARGET_ENV_ID`; `GET /whoami` must report that exact environment
before synchronization proceeds. `POLYAPI_NAMESPACE` limits local contexts
and orphan ownership. `POLYAPI_NAMESPACES` may provide a comma-separated list
of explicitly owned roots instead.

`GIT_PROD_BRANCH` defaults to `main`, and `GIT_PROD_SKIP_BRANCHES` to `develop`.
Bindings use `branch:path/to/file.env`; a bare filename resolves under `env/`.
The sample uses explicit repository-relative paths.
Dry runs resolve references to local resources planned in the same push, so a
first deployment can be previewed before those resources have remote IDs.

`POLY_EXEC_API_KEY` supplies the execution key sent with server-function
deployments as `executionApiKey`. It can be set in any environment.
Production-sensitive real pushes require it and interactive `PROD` confirmation.
`POLY_API_KEY` authenticates deployment requests. Dry runs do not write remote
resources.

`--skip-validation "I know what am doing"` acknowledges the initial
scope, branch/environment-file mismatch, and production confirmation without
prompting. The phrase must match exactly. It does not bypass the environment
pin, production key, namespace checks, JSON schema validation, missing environment
placeholders, OTP, or subscription recreation confirmation.

All selected Python functions and JSON artefacts are validated before deployment
writes. The default stops on an invalid input. `--skip-invalid` reports and skips
invalid files while deploying valid ones. When any file is skipped this way,
orphan detection/deletion is disabled because local ownership is incomplete.

Runtime imports must be installed in the project's Python environment and
resolvable by the pinned SDK's dependency discovery. Missing imports or package
metadata fail preflight. Parsing can substitute generated PolyAPI namespaces
before the first SDK generation; it never substitutes third-party dependencies.

`OTP_ENABLED=true` supplies an interactively entered `x-otp` for writes.
Flow's modification-time guard blocks older local changes. Set
`POLY_DEPLOY_USE_MTIME_GUARD=false` to compare content only; `--force` overrides
the guard and unchanged checks. AI functions and Canopy compare their content
directly. For subscriptions, `--force` has the destructive meaning described
below.

Existing secret/obscured Vari values are protected unless
`POLY_DEPLOY_UPDATE_EXISTING_SECRET_VARIABLES=true`.
`POLY_DEPLOY_RECREATE_VARIABLE_ON_SECRECY_CHANGE` controls recreation on
secrecy-level changes. Review these settings before modifying secret Vari.

`{{UPPERCASE_ENV_NAME}}` placeholders are substituted from environment values.
Missing values produce a confirmation prompt before synchronization.
Do not put resolved secrets in artefact files.

## AI functions

See `workflows/src/sample_ai_fx/artefacts/ai-functions/uplifting_weather.json`.
The enabled weather AI example uses two AccuWeather tools and the SECRET
`weather_api_key` binding. The environment must provide
those tools, the selected model/provider, and the secret value before invocation.

Tool references use exact accessible API, server, or client functions:

```json
{
  "functionName": "lookup",
  "functionContext": "example.tools",
  "argumentConfig": [
    {
      "key": "token",
      "source": "variable",
      "variableName": "provider_token",
      "variableContext": "example.configuration"
    }
  ]
}
```

For a Vari-backed system prompt, replace `systemPrompt` with:

```json
{
  "systemPromptVariable": {
    "variableName": "system_prompt",
    "variableContext": "example.configuration"
  }
}
```

For AI artefacts, use portable references instead of environment-specific `id`,
`variableId`, or `systemPromptVariableId` values. Resolution fails on missing or ambiguous
references. Create/update payloads are checked against the JSON schemas in
`cli/schemas/`. Context/provider changes are not patched
automatically. Omitted fields are preserved; explicit empty arrays remove
tools or arguments.

## Canopy

Copy `designs/examples/canopy.json` into a workflow's `artefacts/canopy/`
folder and replace its empty collections with your application configuration.
Prefer a project-prefixed subpath such as `example-workflow`.

Target selection checks, in order: a `CANOPY_APP_IDS` override by subpath,
a saved artefact `id`, an exact subpath, then a unique name. Overrides accept
JSON (`{"example-workflow":"application-id"}`) or comma-separated
`subpath=id` entries. Overrides must identify an accessible application;
ambiguous targets fail before writes. A saved ID from a different environment
falls back to subpath/name.

`description` and creation-time `visibility` are application metadata.
Configuration goes to `/applications/{id}/config`; description changes use
the metadata endpoint. An omitted description is preserved. Creation writes
the returned ID into the source, leaving environment placeholders unresolved.
JSONC writeback produces ordinary JSON and removes comments. Dry runs never
rewrite the source. Canopy applications are not inferred as orphans.

## GraphQL subscriptions

`designs/examples/subscription.json` is reference-only. Before moving it into
`artefacts/subscriptions/`, supply a real provider URL/query, implement its
destination server function, and define its credential Vari. It deliberately
uses `example.invalid` and remains disabled.

Use `destination.functionContext` / `destination.functionName` for the
handler. Nested `paramsObject` values may use:

```json
{
  "type": "PolyVariable",
  "variableContext": "example.streaming",
  "variableName": "provider_token",
  "path": null
}
```

Flow resolves references to IDs in memory. Raw `functionId`, `paramsVariableId`,
and `paramsSfxId` are not portable template inputs.

Existing subscriptions are reported as blocked because in-place updates are
not supported by this Flow path. `--force` requests recreation: preview first,
then type `YES` interactively for each existing stream. Flow deletes it, waits
30 seconds for its connection to close, and creates the replacement once.
Offsets may reset. If creation fails after deletion, inspect the environment
before rerunning; the old subscription has already been removed.

## Orphans and recovery

Full pushes report managed orphans. `--delete-orphans` explicitly removes
namespace-owned resources missing from the complete local artefact set.
Scoped pushes never delete orphans. Skipped paths retain ownership.
Canopy applications are excluded from orphan inference.

Shared function and webhook references are supported. Jobs and triggers targeting
shared functions must use project-prefixed names to establish ownership. Shared
functions and webhooks are excluded from orphan deletion.

`--resume` reuses `.flow_resume.json` for core resources after an interrupted
push to the same verified instance URL and environment ID. Legacy or different-target
state is ignored. Completed entries are skipped only when their current payload
matches the saved fingerprint; identity-only entries are reconciled again.
Trigger entries also use repository-relative paths, so matching filenames in
different workflows remain distinct. `--force` bypasses resume skips.
Sensitive Vari values are never fingerprinted: OBSCURED, PARTIAL, and SECRET
variables are reconciled under the normal secret-update safeguards on every run.
AI functions, Canopy, and subscriptions re-read remote state instead. A blocked
or failed resource gives the command a nonzero exit status.

`--adopt-orphans` can reconstruct supported core resources locally. Select
explicit core `--types`; adoption is not implemented for AI functions,
Canopy, or subscriptions.

## OpenAPI training, cleanup, and logs

`cli.operations` provides OpenAPI translation, AI metadata enrichment, model
training, scoped cleanup, and server-function logs. Mutating commands use Flow's
environment pin, production checks, and OTP handling. Function parsing and
generated clients use the pinned `polyapi-python` SDK.

```fish
# Local input preview; no provider requests.
env ENVPATH=workflows/dev.env uv run --locked python -m cli.operations train-openapi workflows/src/sample_workflow/artefacts/specs --context example.sample_workflow --dry-run
# Translate, enrich with AI, save models, train, and regenerate the Python SDK.
env ENVPATH=workflows/dev.env uv run --locked python -m cli.operations train-openapi workflows/src/sample_workflow/artefacts/specs --context example.sample_workflow
# Preview cleanup in this exact context. Add --include-children for descendants.
env ENVPATH=workflows/dev.env uv run --locked python -m cli.operations delete-content --context example.sample_workflow --types api-functions,variables
# Retrieve a single log page from an explicitly selected server function.
env ENVPATH=workflows/dev.env uv run --locked python -m cli.operations logs FUNCTION_ID --page 1 --page-size 100 --limit 100
```

Training accepts OpenAPI 3.x and Swagger 2.0 JSON/YAML, a file or folder, and
`{file}` in the target context. `hostUrl` becomes an API argument so the sample
can inject a SECRET host at normal API execution. By default, API functions and
webhooks receive AI-generated names/descriptions; function argument metadata is
enriched too. The requested context remains authoritative.
`--disable-ai` explicitly retains translated metadata, and repeatable
`--rename FROM TO` replaces a whole identifier and its model references.
An enrichment request failure warns and retains the original metadata;
invalid or duplicate final identities stop training.

`--generate-only` saves translated `.poly.json` models for review without
training resources. `train-model PATH` trains a saved model; `--dry-run` validates
and previews it. Existing model files are preserved unless `--overwrite-model`
is explicit. Schema dependencies are ordered before training; cycles and
cross-file identity collisions fail before resource upserts. A failed training
request stops immediately, leaving saved models available for an idempotent
retry. Use `--skip-generate` only when regenerating the SDK separately.

Cleanup always previews metadata first. `--apply` requires typing `DELETE` in an
interactive terminal. Every delete is followed by a 404 readback; an uncertain
result stops the command. Jobs and triggers qualify only when all referenced
server functions are known and inside the requested context scope. Their
references are removed before function/schema resources. No values are printed.

Training, cleanup, and logs prompt for missing source/context/resource/function
arguments in an interactive terminal. Automation must supply those arguments.
Cleanup still needs `--apply` and its separate `DELETE` confirmation.

Logs use the server-function logs endpoint with page, page size, limit, and
optional `--keyword` filters. Output formats timestamps, severity, messages and
details; `--raw` prints JSON. Use `--page` / `--page-size`, or their aliases
`--logs-page` / `--logs-page-size`. Limit defaults to page size.
These commands run only when explicitly invoked.

## Schema refresh

```fish
env ENVPATH=workflows/dev.env make build-schema
uv run --locked python -m cli.schema_refresh --source designs/platform-openapi.json
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --build-schema --dry-run
```

`make build-schema` runs the standalone command. Schema-source precedence is:

1. `--source`: an explicit public OpenAPI URL or local JSON/YAML file.
2. `POLYAPI_OPENAPI_SPEC_URL`: an override from the selected environment.
3. `<POLY_API_BASE_URL>/swagger-json`: the configured instance's platform contract.

There is no fixed regional fallback. If none of these is configured, refresh
stops before making a request or replacing schema files. The selected `ENVPATH`
is loaded by the Flow environment loader, so schema refresh follows the same
instance as deployment unless you explicitly override the schema source.
Downloads carry no API credentials and do not follow redirects.

Refresh rebuilds endpoint JSON validators under `cli/schemas/`, including local
component references and the known platform schema corrections. All contracts
are resolved and checked before files are replaced. Missing optional AI endpoints
retain their existing validators. Review the resulting changes before committing.

`POLYAPI_DESIGNS_DIR` or `--designs-dir` selects a directory containing one local
OpenAPI document. Its component schemas are emitted to
`cli/generated/openapi-schemas.json` for Python's `jsonschema.Draft7Validator`.
Multiple documents require a narrower directory. Deployed SDK types come from
the Python SDK's normal generation step.

`push --build-schema` refreshes validators before preflight and sync. With
`--dry-run`, schema files still change locally; provider resources do not.

## Verification

Run `make check`. Unit tests cover opaque reference identity, API errors, error
handler calls, tool/Vari binding, dry runs, ambiguous/shared references,
namespace deletion boundaries, production/OTP guards, training, and cleanup
confirmation/readback. They mock HTTP transport; only explicitly enabled
integration tests contact a provider. Deployment, live secret resolution,
alerts, AI invocation, and stream recreation require target-specific checks.
