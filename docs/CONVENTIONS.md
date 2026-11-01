# Project conventions

Use these conventions when extending a project created from the Python template.
The checked-in code, JSON artefacts, and Flow tooling define current behavior;
verify implementation before relying on older documentation. Start setup in
[Getting started](../README.md#getting-started), then follow a sample lesson.

## Workflow layout

```text
workflows/src/<workflow>/
  OVERVIEW.md
  client/<name>_cfx.py
  server/<name>_sfx.py
  artefacts/
    vari/
    tabi/
    schemas/
    webhooks/
    triggers/
    jobs/
    ai-functions/
    specs/
    canopy/
    subscriptions/
  tests/test_<behavior>.py
```

Create only the folders a workflow uses. Shared deployable functions live under
`workflows/src/utilities/common/`. Keep upstream specifications under `designs/`
or workflow-local `artefacts/specs/`. Inactive starter artefacts belong under
`designs/`.

Keep Flow tooling in `cli/`. If one-off local operator scripts are needed, place
them in a root `scripts/` directory outside Flow's workflow discovery paths.
Add `workflows/src/globals/` only when the project has shared platform resources
that need it. Do not add empty `types/`, `deployment/`, `scripts/`, or `globals/`
directories to reserve space.

Keep general developer guides in `docs/`, including these conventions and
[setup and development](SETUP.md). Keep the root README focused on getting
started and finding examples. Workflow lessons stay in their same-folder
`OVERVIEW.md`; repository-wide agent instructions stay in [AGENTS.md](../AGENTS.md).

## Naming

- Use snake_case for Python modules, functions, variables, workflow directories,
  and authored artefact filenames, such as `advice_api.json`.
- Use `_cfx.py` for client functions, `_sfx.py` for server functions, and
  `test_<behavior>.py` for tests.
- Keep artefact names descriptive. Their directory identifies the resource
  type; a redundant suffix is optional, not required.
- Preserve platform-defined directories such as `ai-functions`, generated
  schema names such as `CreateJobDto.schema.json`, and standard filenames such
  as `README.md`, `OVERVIEW.md`, and `AGENTS.md`.
- Preserve API payload keys, external resource names, and generated SDK names
  exactly. Renaming a local file does not rename a deployed resource; its
  identity comes from `polyConfig` or the JSON payload.

## Python functions

Each deployable file contains one typed entrypoint and a literal, typed
`polyConfig` dictionary. Use `PolyClientFunction` or `PolyServerFunction` from
`polyapi.typedefs`, with the same snake_case name as the entrypoint.

Flow uploads the individual source file. Keep its runtime types and metadata
inline; neighboring modules are not packaged with it. Use the native Python SDK
for shared deployed functions rather than importing another deployable file.

A client function executes in its caller's process. A server function executes
on PolyAPI and can be a webhook or job destination. Webhook and job handlers
accept three arguments: the event body, headers, and parameters. Error-handler
triggers supply one error object instead; do not reuse those signatures blindly.

- Call deployed dependencies through `polyapi.poly`, not local imports.
- Generated asynchronous API/server calls use `_async`; asynchronous client
  functions keep their declared names.
- Read Vari through its exact generated path, for example
  `await vari.example.sample_workflow.advice_api_config.get_async()`.
  Parse JSON values explicitly when the generated method returns text.
- For SECRET values, pass `.inject("path")` unchanged into a generated API/server
  call. Do not unwrap, cache, stringify, or pass it through a client function.
- Use `TypedDict` from `typing_extensions` for structured runtime contracts.
- The SDK pinned in [pyproject.toml](../pyproject.toml) is 0.3.21.dev1. Its deployable
  boundaries use `Dict`/`List`, supported scalars, or local
  named `TypedDict` types. Builtin generics, unions, `Literal`, and `None` returns
  can silently become `any`; verify native emitted schemas. Keep enum checks at
  runtime. The common logging helpers return booleans instead of an untyped void.
- Do not cache OBSCURED or SECRET values. A bounded cache of NONE configuration
  such as the log level is allowed.
- Set HTTP response status through `polyCustom.responseStatusCode` when
  returning a handled webhook error.

Flow discovers inline metadata from `client/*.py` and `server/*.py` under
`workflows/src/`. New workflows should use this pattern.

Use `push-dynamic` to rebuild local server consumers after client changes,
including chains of client functions. Scope filters select initial changes;
affected consumers can extend beyond that scope. Review the expanded plan before
deploying. Keep dependency calls explicit through the generated SDK; functions
in other repositories and runtime-built imports are outside the local graph.
Server deployment payloads include the contexts and native SDK package requirements
of nested Python clients. Offline graph reports do not validate deployment payloads
or establish the target environment; use a live dry run before deploying.

## Ownership and references

**The context root of every locally owned resource must match
`POLYAPI_NAMESPACE`.** This setting declares ownership; it does not select a
PolyAPI instance or environment. It must be a Python-compatible dotted context,
with no keywords as segments. Alternatively, `POLYAPI_NAMESPACES` selects an
explicit comma-separated list of owned roots and takes precedence.

For example, with `POLYAPI_NAMESPACE=my_project`:

| Item | Matching value |
| --- | --- |
| Workflow `polyConfig.context` and JSON `context` | `my_project.sample_workflow` |
| Shared logger context | `my_project.utilities` |
| Generated function call | `poly.my_project.utilities.log(...)` |
| Generated configuration read | `vari.my_project.configurations.log_settings.log_level.get_async()` |
| Webhook security reference | `{{my_project.sample_workflow.validate_motd_payload}}` |

Kickstart replaces the starter context root in samples and pre-included
utilities, including SDK calls, portable references, webhook slugs, and relevant
error-handler paths. It also personalizes the code examples in project docs and
sample guides, and writes the selected namespace to the Flow file.
For manual adoption, update all of those together and review project-prefixed
job/trigger names. Editing only the environment variable does not rewrite code
or rename existing deployed resources.

Use portable `{functionName, functionContext}` and
`{variableName, variableContext}` references where supported. Webhook security
functions use `{{example.sample_workflow.validate_motd_payload}}`.
Keep names, contexts, and reference paths aligned when renaming resources.

Webhook security functions, triggers, jobs, AI tools, and subscription
destinations may reference accessible
external functions by exact context and name. This does not make those
external functions eligible for deletion. Vari references in these artefacts
must belong to the project namespace.

Keep external contexts such as `OOB.polyapi.internal.alerting` and provider URLs
unchanged. Only rename project-owned roots. After changing a deployed resource's
identity, regenerate the SDK and update consumers; review any old resources
separately rather than assuming they were renamed remotely.

Jobs and triggers have no resource context. Use project-prefixed names and
destination functions. External destinations require names beginning with an
owned namespace followed by `.`, `_`, or `-`. Updates must resolve every existing
function ID. Orphan inference still uses namespace-owned functions; shared
references never make external functions or webhooks eligible for deletion.

Canopy applications support explicit ID overrides, saved IDs, exact subpaths,
and unique names. Prefer project-prefixed subpaths such as `example-workflow`.
They are never deleted as inferred orphans. Creation writes only the new ID back
to the unresolved source; it never persists substituted environment values.

Scoped pushes skip orphan detection. Full pushes report namespace-owned
orphans; deletion requires `--delete-orphans`. Skipping a local directory
does not make its resources orphans.

## Target selection and generated SDK

Keep `POLYAPI_NAMESPACE` and the verified `TARGET_ENV_ID` in the selected local
Flow environment file. Flow remote commands verify that pin against `/whoami`,
including deployment dry runs. Shell exports and confirmation bypasses do not
replace the file's pin. An offline training preview checks input only and does
not prove target identity or translated SDK names.

Flow reuses the native SDK's saved connection by default. Its connection aliases
are resolved per field before SDK operations. Direct `python -m polyapi` commands
and generated SDK calls accept the canonical `POLY_API_BASE_URL` / `POLY_API_KEY`
overrides; they do not apply Flow's other aliases or target-pin check. Confirm
the connection and generated bindings before direct calls. Never commit real
environment files, keys, or generated SDK credentials.

Keep source OpenAPI specs in the workflow's `artefacts/specs/` folder. Training
creates outbound API Functions, while a push deploys other selected resources;
a specs folder is not automatically trained by a push. Review translated
`.poly.json` models, names, and argument bindings. `--disable-ai` skips optional
metadata enrichment, `train-model` retries a saved model, and `--overwrite-model`
deliberately regenerates it. See [Flow training](../cli/README.md#openapi-training-cleanup-and-logs).

## Tests and maintenance

Keep unit tests beside workflows under `tests/`. Mock generated SDK boundaries
in tests without adding runtime compatibility shims.
Runtime tests use the `integration` marker and require
`RUN_POLY_INTEGRATION=true`. Provider calls, deployments, and AI usage require
an explicitly selected target.
The weather integration test additionally requires `RUN_AI_INTEGRATION=true`.
Direct HTTP webhook tests require `POLY_TEST_WEBHOOK_URL` and `POLY_API_KEY` in
the test process; they do not reuse the SDK's saved key. These live calls do not
perform Flow's target-pin check.

Run `make check` after changes. CI installs the locked dependencies on Python
3.12.12 and runs the same command on pushes and pull requests. Live integration
tests remain disabled in CI; no PolyAPI credentials are required. CI does not
train APIs, refresh schemas, generate an environment SDK, or deploy resources.

Keep each workflow's `OVERVIEW.md` useful to engineers and coding agents: describe
entrypoints, resource dependencies, configuration, runtime contracts, and the
focused checks. Update it when those facts change, and verify them against code
and artefacts. Keep agent-wide navigation and instructions in `AGENTS.md` and
Flow command details in [the CLI guide](../cli/README.md). Preserve the
`artefacts` spelling. Lessons should include one first-run path, observable
checkpoints, a request/result example, focused verification, and exercises.
Keep advanced setup and operational reference after the first successful run.

Log fixed event names and safe operational fields. Do not log raw payloads,
credentials, or exception text that may contain them. Global error handlers
must remain best-effort and must not throw again when alert delivery fails.
