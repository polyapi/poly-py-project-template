---
name: artefacts-usage
description: Create or review PolyAPI workflow resources, portable references, ownership, and resource layout in this Python template.
---

# PolyAPI artefacts

Start with the relevant workflow's `OVERVIEW.md`, its current resources, and
[CONVENTIONS.md](../../../CONVENTIONS.md). Use checked-in JSON and Flow validation
as evidence; prose examples alone do not establish a platform contract.

## Place resources deliberately

- Deployable workflows live under `workflows/src/<workflow>/`; shared deployable
  functions live under `workflows/src/utilities/common/`.
- Put authored resources under the workflow's `artefacts/` directory using the
  supported category names from the [Flow guide](../../../cli/README.md).
  Preserve the spelling `artefacts` and categories such as `ai-functions`.
- Create only directories the workflow uses. Keep inactive starter resources
  under `designs/`, and local operator scripts outside Flow discovery under a
  root `scripts/` directory when needed.
- Put training inputs under `artefacts/specs/`. Training them is an explicit
  operation; a normal Flow push does not train OpenAPI specifications.

## Keep contracts and ownership aligned

- Keep local contexts under `POLYAPI_NAMESPACE` or the explicitly selected
  `POLYAPI_NAMESPACES` roots. Use project-prefixed names for jobs and triggers,
  which do not have a resource context.
- Prefer portable `{functionName, functionContext}` and
  `{variableName, variableContext}` references where supported. Do not copy
  environment-specific IDs into a portable example to bypass resolution.
- Verify schemas, webhook security functions, trigger destinations, argument
  mapping, response behavior, and enabled state together. Do not weaken a
  contract or authentication setting to make validation pass.
- Use the [sample webhook](../../../workflows/src/sample_workflow/artefacts/webhooks/motd_hook.json)
  and [paired trigger](../../../workflows/src/sample_workflow/artefacts/triggers/motd_trigger.json)
  as a concrete pattern, not a requirement that every integration use that shape.
- Accessible external function references do not transfer ownership to the
  project. Keep the namespace and orphan-deletion behavior described in
  `CONVENTIONS.md`; never broaden ownership to make a reference resolve.
- Never embed credentials in JSON or use environment substitution to persist
  resolved secrets. Declare secret Vari metadata and configure real values
  through an authorized target workflow.
- Keep schedules and streams at their intended enabled state. Stream recreation
  and orphan deletion are distinct effects requiring their existing safeguards.

## Validate

From the repository root, run the local resource checks:

```fish
uv run --locked pytest cli/tests/test_preflight.py cli/tests/test_resources.py
```

The preflight suite includes the checked-in template inputs. Add focused tests
for new references, payloads, or lifecycle behavior, then run `make check`.
Use [cli/schemas](../../../cli/schemas) and the current Flow transformations
when checking API shapes; refresh schemas only as an explicitly selected
operation against a known source. Update the workflow overview with changed
contracts. Local validation does not prove that remote dependencies exist.
