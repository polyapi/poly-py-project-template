# Python Flow CLI

Python Flow validates and synchronizes PolyAPI resources from local Python
functions and JSON artefacts. It also provides OpenAPI training with AI metadata
enrichment, scoped resource cleanup, function logs, and schema refresh.

See [setup and development](../docs/SETUP.md) for Python 3.12.12 setup,
dependencies, and environment configuration. The
[template homepage](../README.md#learn-by-example) links to the sample workflows.

Run from the repository root:

```bash
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --dry-run
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_workflow --types functions,variables,webhooks,triggers,jobs
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
AI functions → webhooks → triggers → jobs → subscriptions. Regular `push`
synchronizes client functions before server functions; `push-dynamic` uses
the dependency graph to order its planned functions. Pushes selecting Python or AI functions
regenerate the SDK even when remote functions are unchanged, unless `--skip-generate`
is supplied or generation is disabled in configuration. Dry runs do not generate.
A failed generation is retried by the next push, including `--resume`.

## Interactive setup

For a new project, the root README's [kickstart command](../README.md#getting-started)
handles cloning, dependencies, skills, namespaces, SDK generation, and editor
configuration. It reuses the same identity verification, environment pin, and
target/editor save path described here. The following manual setup remains
available for existing checkouts and target changes.

Load the [Bash, Fish, or PowerShell helper](../docs/SETUP.md#existing-checkout-or-manual-setup)
once, then run `flow setup`. The shell function activates the project's `.venv`
and forwards commands to `uv run --locked python cli/flow.py`, with the selected
`ENVPATH`. It finds the nearest Python Flow project from the current directory;
it does not bind future commands to the checkout where the profile was installed.

Setup uses the pinned Python SDK's native interactive setup and generation.
It removes inherited `POLY_API_KEY` and `POLY_API_BASE_URL` only in its child
process so the SDK does not bypass its prompts or reuse exported credentials.
The SDK's saved values remain the defaults. Credentials are never placed in
shell aliases, arguments, editor settings, or the non-secret selection state.

`GET /whoami` must return an authenticated environment ID and tenant/environment
names before project configuration is written. Redirects and failed identity
requests stop setup. A production name suggests prod; unclear or mixed names
suggest dev. Both file choices remain available, and the selected tenant and
environment are shown at confirmation. Previous selection wins only for the
same base URL and environment ID.

Setup saves `TARGET_ENV_ID` in the selected env file. New files use saved SDK
setup without duplicating the URL or key. Existing explicit connection bindings
(including aliases) are updated to the confirmed target. A new target gets an
empty `POLY_EXEC_API_KEY` to prevent an inherited execution key being used;
an existing file's target change requires
explicit confirmation and clears that key too. Other workflow configuration is
preserved. Existing env files must be untracked and Git-ignored. Setup does not
rename contexts, train APIs, deploy resources, or weaken branch/OTP safeguards.
Native SDK configuration/generation occurs first and is not rolled back when a
later prompt is cancelled.

Editor settings live in ignored `.vscode/settings.json`; selection metadata
lives in ignored `.flow/setup.json`. Bash uses `.bash_profile` on macOS and
`.bashrc` elsewhere, Fish uses its `conf.d/poly_flow.fish`, and PowerShell uses
`$PROFILE.CurrentUserAllHosts`. Profile registration is optional, shown before
writing, and not duplicated on later runs. An existing command named `flow` is
preserved; inspect or remove it yourself before loading this implementation.
Remove the marked loader entry to uninstall it from future terminals.

For use without shell integration:

```bash
uv run --locked python cli/flow.py setup
```

That command configures the SDK, env file, and editor but cannot activate the
calling shell or define a shell function. The implementation is kept in
`cli/setup.py` plus three thin `cli/shell/flow.*` loaders. Reusing it in another
project currently requires the same Flow entrypoint, locked uv project,
`.venv`, env example, and ignore rules; it is not a globally installed package
or GitHub app. It adds no runtime dependency.

## Dependency-aware deployments

Use `push-dynamic` when updating shared client functions. It uses Python's AST
to find generated SDK references, then deploys changed functions, intermediate
client functions, and their consuming server functions in dependency order.
A client function is embedded in its consumers' generated SDK; an unchanged
server source therefore still needs rebuilding after its client code changes.
Traversal stops at server functions because subsequent server calls run over HTTP.

```bash
# Compare selected local functions with the selected instance and preview consumers.
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic --dry-run
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic --paths sample_workflow --dry-run

# Force one named function and its consumers, including unchanged functions.
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic example.utilities.log --dry-run
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic example.utilities.log

# Resume an interrupted dependency deployment against the same target.
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic --resume
```

- `--paths` and `--types client-functions` / `server-functions` select where
  changes are detected. Consumers can be outside that selection; the plan lists
  those additional functions before any resource writes.
- Positional seeds and comma-separated `--seeds` accept exact `context.name`,
  source files, or directories. Named seeds default to functions only when
  comparison is omitted or `--seed-from none` is selected. Explicit `remote` or
  `specs` comparison retains the configured resource set in live runs. Use
  `--types` to choose resources and `--paths` to scope detection and resource
  synchronization. Interactive runs selecting other resources offer the normal
  scope prompt. The effective resource selection, including
  `POLY_DEPLOY_ENABLED_TYPES`, must include functions; an invalid selection stops
  before resource writes or resume-state changes. Offline reports remain
  functions-only. Named or scoped runs do not inspect or delete orphans.
- By default, changes are compared using native Python SDK payloads, including
  source, metadata, requirements, and generated contexts. A changed intermediate
  client becomes a seed so its other consumers are included too.
- Server payloads include contexts and third-party package requirements from
  reachable Python client functions, including nested clients and accessible
  tenant/public clients. Live discovery reads the selected target's `/specs`
  catalog using its SDK visibility, namespace, language, and precedence rules,
  including generated `_async` wrappers for server, API, AI, and auth functions
  (`introspectToken`, `refreshToken`, and `revokeToken`; `getToken` has no alias);
  the key needs library-generation access. Local source takes precedence for
  the same declared function. A live plan rejects local declarations whose SDK
  names, including server `_async` aliases, are exported by a different function;
  rename the conflicting function before deploying. Shared clients supply
  dependencies only and are never deployed or added to orphan ownership. Missing
  source for a referenced Python client stops the plan. Offline reports cannot
  inspect these shared clients.
  Transitive client requirements are collected from import statements, including
  function-local imports, without executing client source or importing its
  modules. Resolution uses the pinned Python SDK's package-name mapping and
  base-image exclusions; requirements are deduplicated. Required packages must
  have installed distribution metadata in the local environment. Traversal stops
  at HTTP wrappers, auth helpers, and webhook listeners. Local deployable schema
  extraction still uses the SDK's full parser, which executes local module-level code.
  Referenced root-level functions such as `poly.helper()`, Vari such as
  `vari.root_setting.get_async()`, and Tabi such as
  `tabi.root_table.select_many({})` require unfiltered SDK
  generation because the platform's context filter cannot select an empty root
  context. Flow clears the server's generation filter and retains transitive
  client package requirements. Root Vari references are retained from both server
  source and nested local or shared clients, including reads, updates, injection,
  and `onUpdate` listeners. Vari and Tabi aliases respect Python lexical scopes,
  fixed tuple positions, and resource values passed to same-file helpers, so
  separate helpers can reuse a local name without losing dependencies.
  Flow also matches returned resource values against the live SDK catalog and
  table query/row types against native SDK exports, without executing those definitions.
  Direct scoped resource values retain their owning contexts before newly
  synchronized resources appear in that catalog.
  Table-rendering failures are checked against each server's effective generation
  scope and referenced exports. Unsupported tables outside that scope do not block
  deployment; required tables and tables included by unfiltered generation still do.
  When Tabi is synchronized in the same run, Flow rechecks the updated catalog
  against the prepared server scopes before writing any functions.
  Catalog-backed module imports and aliases narrow to their referenced children;
  excluded sibling resources do not become dependencies just because they share a
  parent module. A visible root Tabi or Vari with the same name as that module does not
  override a narrowed child reference. An explicit child generation scope can
  also satisfy an unused module import or alias when it survives context exclusions.
  Actual root-resource values, methods, and table types still require unfiltered generation
  and retain its scope guards.
  Actual table/type values and namespaces passed to unknown code remain dependencies.
  Scoped table exports keep their owning contexts. Root HTTP wrappers and webhook listeners retain
  the same generation requirement without loading their implementation. Webhook
  listeners use their declared name, such as `poly.events(callback)`, with no
  `_async` alias. Explicit `generate_contexts` or context exclusions make this
  combination invalid; Flow stops before
  deploying the affected server.
- Every planned function must pass SDK parsing, payload validation, namespace
  ownership, and exclusion checks. An excluded consumer blocks the plan rather
  than leaving it stale. `--skip-invalid` is unsupported for dependency plans.
- Identical intermediate clients are redeployed by default.
  `--skip-unchanged-cfx` skips those clients after comparison; affected servers
  are still rebuilt. Offline `--seed-from none` cannot compare clients, so it
  retains intermediates and prints a warning even with `--skip-unchanged-cfx`.
  `--force` also bypasses the existing timestamp guard;
  an unchanged consumer rebuild does not require bypassing that guard.
- Live runs ask for confirmation after printing the plan. Above `--max-leaves`
  (default 10), also type the server count. `--yes` (or `-y`) accepts this plan prompt
  non-interactively; production, execution-key, environment, and OTP checks
  still apply. The existing `--skip-validation` acknowledgement also covers
  this confirmation prompt.
- Failed or blocked prerequisite resource sync prevents function deployment.
  A failed function deployment stops the remaining pipeline. Pending function identities
  stay in target-bound resume state, so `--resume` rebuilds unfinished consumers
  even if their source and the already-deployed clients now match the platform.
  A subsequent run cannot silently discard an unfinished tree. Dry runs do not
  write or clear that state. Deployments are sequential, not atomic.

Python import aliases, chained aliases, `global`/`nonlocal` bindings, tuple
assignment (including starred unpacking), `typing.cast`, function references,
and namespace arguments forwarded to same-file helpers are recognized. Unpacking
resolves fixed positions; starred captures and uncertain expanded values retain
conservative SDK references. Helper calls track fixed positional arguments before
the first `*args` expansion and named keyword arguments; positional arguments
after an expansion retain conservative references. Function, Tabi, and Vari
arguments and defaults remain SDK dependencies even when the helper ignores them.
Unused function imports and aliases also retain the exports Python evaluates.
Namespaces still narrow to the referenced children, without including siblings.
Server SDK `_async` wrappers resolve to their server identity; client functions
retain their declared names, including a literal
`_async` suffix, when resolving dependencies and transitive SDK generation contexts.
Callable attributes such as `poly.example.helper.__call__()` resolve to the longest
known function prefix, so their consumers rebuild and inherit client requirements.
Exact identities take precedence; matching remains case-sensitive.
Shared consumers are deployed once. Cycles have a stable internal order, followed
by their downstream consumers. Namespace values or dynamic `getattr` access
include all matching local functions conservatively and appear as a warning.
Class-held SDK namespaces and namespace arguments captured by `**kwargs` receive
the same conservative treatment because instance attributes and keyword mappings
can escape lexical alias tracking.

The graph covers local deployable functions. It cannot discover consumers in
other repositories or references constructed entirely at runtime with `eval`,
`exec`, or dynamic module imports. Wildcard SDK imports are rejected. Use explicit
generated SDK calls for dependency tracking. Live payload comparison is the
default; cached-code comparison is an explicit option described below.

### Comparison and graph reports

| Option | Behavior |
| --- | --- |
| `--seed-from remote` | Detect changes using live native SDK payload comparison, including metadata. Default when no named seeds are supplied, or when `--paths` is supplied. |
| `--seed-from specs` | Detect code changes using the native Python SDK's `polyapi/poly/specs.json` cache, applying its client-language filter and callable-export precedence, including root entries and generated `_async` wrappers. Wrappers are not separate function identities. Explicit seeds are still included. |
| `--seed-from none` | Start only from forced seeds. Live runs still compare affected clients and promote changed intermediates. |
| `--json` | Write one structured report to stdout, with progress on stderr. Implies `--dry-run`, even with `--yes` or `--force`. Cannot be combined with `--build-schema`. Includes seeds, deployment order, cycles, nodes, and final status; excludes payloads and credentials. |
| `--why context.name` | Show paths from seeds to that function, then exit without deploying. Reports at most 25 paths and indicates truncation. |
| `--hubs` | List local client functions ranked by affected server count and transitive client caller count, then exit offline. |
| `--through-sfx` | Expand reporting through server HTTP calls. Additional callers are marked as report-only and are never added to the deployment order. |

Named seeds disable initial automatic detection unless `--paths` or an explicit
`--seed-from remote` / `specs` is supplied. Source files and directories are also
accepted as seeds. A cache comparison checks code only, so use live comparison
for metadata-only changes. Missing cached functions or missing implementation
code conservatively count as changed. Missing code is common with filtered caches
and server specifications. A missing or invalid cache stops the command. Comparison
preserves whitespace within source lines, including multiline strings.

The SDK cache does not establish the target environment; reports label its
environment as unknown. Live runs using cached seeds still load the selected
target, check ownership and timestamps, parse native deployment payloads,
resolve remote client dependencies, and apply the normal confirmation and
production safeguards. Use a live `--seed-from remote --dry-run` to verify the
actual target before deploying.

`--dry-run` (including the dry run implied by `--json`) with an explicit
`--seed-from specs` or `none` is an offline, functions-only graph preview.
`--why` with either of those modes is also offline.
These modes and `--hubs` do not load an environment file, request credentials,
parse deployment payloads, refresh schemas, or modify resume state. Supply the
non-secret namespace (and any discovery/exclusion settings) in the process
environment. They cannot verify target state or remote-only dependencies.
`--why`, `--hubs`, and offline previews reject schema refresh, orphan deletion,
and resume flags. Live JSON previews use the normal dry-run pipeline for selected
resources; they never deploy, generate the SDK, or change resume state. Schema
refresh is also rejected with `--json`. Without an explicit offline mode, `--why`
and `--json` read the verified live target.

```bash
# Offline: inspect shared clients without credentials or an environment file.
POLYAPI_NAMESPACE=example uv run --locked python cli/flow.py push-dynamic --hubs --json

# Offline: compare with the last native SDK cache and include HTTP callers in the report.
POLYAPI_NAMESPACE=example uv run --locked python cli/flow.py push-dynamic --seed-from specs --through-sfx --json

# Offline: explain one forced client's path to a server.
POLYAPI_NAMESPACE=example uv run --locked python cli/flow.py push-dynamic example.utilities.log --seed-from none --why example.sample_workflow.motd_server

# Live read: validate the plan against the selected instance.
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push-dynamic --seed-from remote --json
```

`push` retains its existing source/payload-only synchronization behavior.
Use `push-dynamic` for nested consumer rebuilds. Both commands use the same
resource pipeline, Python SDK parser, target pin, and SDK generation step.

## Target and update safeguards

Flow uses the connection saved by this project's native Python SDK setup.
`POLY_API_BASE_URL` and `POLY_API_KEY` are optional overrides, not required env
file entries. `BASE_URL` / `API_KEY` and older `POLYAPI_*` aliases also work.
Precedence is selected file, shell, then saved SDK setup, resolved per field.
For example, a key-only override retains the configured URL. Explicit empty
values do not silently use saved defaults. Native SDK calls and generation use
the same resolved pair. `build-schema` also uses the saved URL unless an explicit
spec source or URL override is provided.

Keep separate environment files for separate targets and select one with
`ENVPATH`. Each must retain its own verified `TARGET_ENV_ID`, regardless of
where the connection comes from.

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

Existing SECRET, OBSCURED, and PARTIAL Vari values are protected even with
`--force` unless
`POLY_DEPLOY_UPDATE_EXISTING_SECRET_VARIABLES=true`. Forced OBSCURED updates can
change metadata while preserving the value. Protection uses both local and remote
secrecy, and blocks recreation of a protected remote variable without that opt-in.
Existing PARTIAL variables can receive metadata updates while preserving their
entire value; this does not enable creation of deprecated PARTIAL variables.
`POLY_DEPLOY_RECREATE_VARIABLE_ON_SECRECY_CHANGE` controls recreation on
secrecy-level changes. Review these settings before modifying secret Vari.

`{{UPPERCASE_ENV_NAME}}` placeholders are substituted from environment values.
Missing values produce a confirmation prompt before synchronization.
Do not put resolved secrets in artefact files.

## AI functions

See `workflows/src/sample_ai_fx/artefacts/ai-functions/uplifting_weather.json`.
The enabled weather AI example uses two Open-Meteo API tools trained from its
bundled specifications. It needs no weather account, API key, or trial for
evaluation. Follow [the weather setup](../workflows/src/sample_ai_fx/OVERVIEW.md)
to train those tools in the project namespace before deploying the AI function.
The target still needs the selected AI model/provider. Generic variable-backed
tool arguments remain supported as shown below.

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
state is ignored only when it has no unfinished dependency deployment. Saves
atomically replace the previous snapshot after writing and flushing the new file.
Unreadable or malformed state stops the run; restore a valid snapshot before
continuing. If recovery is impossible, explicitly rebuild the affected dependency
tree before discarding the state. Completed entries are skipped only when their
current payload matches the saved fingerprint; identity-only entries are reconciled again.
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

```bash
# Local input preview; no provider requests.
ENVPATH=workflows/dev.env uv run --locked python -m cli.operations train-openapi workflows/src/sample_workflow/artefacts/specs --context example.sample_workflow --dry-run
# Translate, enrich with AI, save models, train, and regenerate the Python SDK.
ENVPATH=workflows/dev.env uv run --locked python -m cli.operations train-openapi workflows/src/sample_workflow/artefacts/specs --context example.sample_workflow
# Preview cleanup in this exact context. Add --include-children for descendants.
ENVPATH=workflows/dev.env uv run --locked python -m cli.operations delete-content --context example.sample_workflow --types api-functions,variables
# Retrieve a single log page from an explicitly selected server function.
ENVPATH=workflows/dev.env uv run --locked python -m cli.operations logs FUNCTION_ID --page 1 --page-size 100 --limit 100
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
is explicit. Schema dependencies are ordered before training; valid recursive references
are retained, and cross-file identity collisions fail before resource upserts. A failed training
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

After loading the shell helper, the same commands are also available as
`flow train-openapi`, `flow train-model`, and `flow delete-content`. The
`python -m cli.operations` entry point remains supported.

Repeat `--operation-id ID` on `train-openapi` to select original operation IDs
before translation or AI enrichment. Each requested ID must match exactly once
in each input spec. Only selected operations, shared path parameters and their
transitively referenced schemas are retained. Selection requires bundled local
references; bundle external `$ref` files first. Omit the option to train all
operations. For example:

```bash
flow train-openapi designs/crm.json --context example.crm --operation-id validatePhone --operation-id getProfile
```

Descriptions are converted from HTML to readable plain text, including argument
and schema descriptions, after AI enrichment. Request bodies, examples, defaults
and the original spec are unchanged. Invalid schema identifiers such as `200`
are normalized to `Schema200` with references updated in a temporary document.
Valid recursive schemas are preserved; other dependencies are ordered before
the schemas and functions that use them.

Recursive schema training is enabled by default. The pinned Python SDK can skip
recursive schema types during SDK generation; inspect its warnings when a
contract contains recursion. Platform training and SDK type generation are
separate checks.

## Schema refresh

```bash
ENVPATH=workflows/dev.env make build-schema
uv run --locked python -m cli.schema_refresh --source designs/platform-openapi.json
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --build-schema --dry-run
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
