---
name: polyapi-sdk
description: Implement or review PolyAPI Python functions, generated SDK calls, runtime types, and Vari injection in this template.
---

# PolyAPI Python SDK

Use the pinned `polyapi-python` dependency and Python version in
[pyproject.toml](../../../pyproject.toml). Verify SDK behavior against the installed
version and generated function source, excluding credential stores. Do not infer
signatures from another language or upgrade the SDK to work around uncertainty.

## Function source and types

- Put one typed deployable entrypoint in each `_cfx.py` or `_sfx.py` file with a
  literal `polyConfig` typed as `PolyClientFunction` or `PolyServerFunction` from
  `polyapi.typedefs`. Match its `name` to the entrypoint and use an owned context.
- Flow submits that individual file's source. Keep its runtime types and
  helpers inline; neighboring modules are not bundled. Call shared deployed
  functions through `polyapi.poly` instead of importing their local files.
- Install runtime dependencies in the project environment and preserve the
  pinned SDK's package discovery. Unresolved imports or package metadata must
  fail preflight; generated-namespace parsing support must not hide missing
  third-party dependencies.
- Import `TypedDict` from `typing_extensions`. The pinned SDK parser explicitly
  rejects `typing.TypedDict`. Keep structured types local to the deployable file
  instead of referencing sibling generated SDK types during their definition.
- Match return types to the function's actual contract. A webhook security
  function can return `bool`; server functions do not universally return `dict`.
- The sample webhook handler takes `event`, `headers`, and `params`. Verify the
  actual trigger/job argument mapping for the function being changed; do not
  impose that signature on unrelated server functions.

## Generated calls and Vari

- Inspect the exact generated callable and a maintained caller before using its
  arguments or response fields. API/server async calls typically use `_async`;
  generated async client functions retain their declared names. Await the call
  that the generated signature exposes.
- Preserve external SDK identifiers and payload field casing. Validate required
  nested response shapes instead of guessing alternative keys.
- Read Vari through its exact generated context path. Decode JSON text when
  the generated return contract requires it, then validate the resulting shape.
- Pass SECRET `.inject(...)` references unchanged to a supported generated
  API/server call. Do not read, stringify, cache, or relay them through another
  client function. The sample rejects direct API execution for this path because
  it cannot resolve injected references.
- Do not cache OBSCURED, PARTIAL, or SECRET values. A bounded cache of NONE
  configuration is appropriate when its freshness requirements permit it.
- Use `polyCustom.responseStatusCode` when setting a handled webhook response
  status; a `statusCode` field in the returned dictionary is a separate contract.

## Verify and deploy

Use [the sample client](../../../workflows/src/sample_workflow/client/motd_client_cfx.py)
for external API calls and opaque secret injection, and
[the sample server](../../../workflows/src/sample_workflow/server/motd_server_sfx.py)
for generated client calls and response metadata.

Mock generated SDK boundaries in local tests; assert argument shapes, awaited
calls, response handling, and injection-reference identity where relevant.
Run the focused workflow tests and `make check` from the repository root.
Local tests cannot prove live secret resolution or deployed SDK generation.

For authorized deployment, use the target selection and scoped dry-run procedure
in the [Flow guide](../../../cli/README.md). Preserve target identity, namespace,
execution-key, and production safeguards. Flow generates the SDK after pushes
that include Python or AI functions by default; do not disable generation to
hide a contract mismatch. Runtime verification remains a separate opt-in action.
