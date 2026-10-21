# Project conventions

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

- Call deployed dependencies through `polyapi.poly`, not local imports.
- Generated asynchronous API/server calls use `_async`; asynchronous client
  functions keep their declared names.
- Read Vari through its exact generated path, for example
  `await vari.example.sample_workflow.advice_api_config.get_async()`.
  Parse JSON values explicitly when the generated method returns text.
- For SECRET values, pass `.inject("path")` unchanged into a generated API/server
  call. Do not unwrap, cache, stringify, or pass it through a client function.
- Use `TypedDict` from `typing_extensions` for structured runtime contracts.
- Do not cache OBSCURED or SECRET values. A bounded cache of NONE configuration
  such as the log level is allowed.
- Set HTTP response status through `polyCustom.responseStatusCode` when
  returning a handled webhook error.

Flow discovers inline metadata from `client/*.py` and `server/*.py` under
`workflows/src/`. New workflows should use this pattern.

## Ownership and references

`POLYAPI_NAMESPACE` must be a Python-compatible dotted context. Alternatively,
`POLYAPI_NAMESPACES` selects an explicit comma-separated list of owned roots.
Local functions and context-bearing artefacts must use that root or a child,
for example `example.sample_workflow`.

Use portable `{functionName, functionContext}` and
`{variableName, variableContext}` references where supported. Webhook security
functions use `{{example.sample_workflow.validate_motd_payload}}`.
Keep names, contexts, and reference paths aligned when renaming resources.

Webhook security functions, triggers, jobs, AI tools, and subscription
destinations may reference accessible
external functions by exact context and name. This does not make those
external functions eligible for deletion. Vari references in these artefacts
must belong to the project namespace.

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

## Tests and maintenance

Keep unit tests beside workflows under `tests/`. Mock generated SDK boundaries
in tests without adding runtime compatibility shims.
Runtime tests use the `integration` marker and require
`RUN_POLY_INTEGRATION=true`. Provider calls, deployments, and AI usage require
an explicitly selected target.

Run `make check` after changes. CI installs the locked dependencies on Python
3.12 and runs the same command on pushes and pull requests. Live integration
tests remain disabled in CI; no PolyAPI credentials are required. CI does not
train APIs, refresh schemas, generate an environment SDK, or deploy resources.

Keep each workflow's `OVERVIEW.md` useful to engineers and coding agents: describe
entrypoints, resource dependencies, configuration, runtime contracts, and the
focused checks. Update it when those facts change, and verify them against code
and artefacts. Keep agent-wide navigation and instructions in `AGENTS.md` and
Flow command details in `cli/README.md`. Preserve the `artefacts` spelling.

Log fixed event names and safe operational fields. Do not log raw payloads,
credentials, or exception text that may contain them. Global error handlers
must remain best-effort and must not throw again when alert delivery fails.
