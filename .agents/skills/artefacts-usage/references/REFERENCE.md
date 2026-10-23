# artefacts-usage Reference

The layout below gives recommended defaults. Follow existing filenames for
every resource, including unsuffixed JSON. Create only needed folders; omit
`types/` when runtime types must be inline for single-file deployment. Keep
one-off scripts outside deployable-function discovery.

Resolve the workflow root from the active repo first. In the rules and examples
below, substitute that root for `src/` (for example, `workflows/src/`).

## Folder layout for a workflow

```text
src/<workflow_name>/
  OVERVIEW.md
  server/
    <name>_sfx.py
  client/
    <name>_cfx.py
  types/
    <name>_types.py
  tests/
    test_<name>.py
  artefacts/
    canopy/
      <name>.json
    webhooks/
      <name>_hook.json
    triggers/
      <name>_trigger.json
    vari/
      <name>_vari.json
    jobs/
      <name>_job.json
    tabi/
      <name>_tabi.json
    specs/
      <name>_spec.json
  deployment/
    <name>.sh
  scripts/
    <one_off_task>.py
```

## Folder layout for a shared utility

```text
src/utilities/<utility_name>/
  OVERVIEW.md
  server/
    <name>_sfx.py
  client/
    <name>_cfx.py
  types/
    <name>_types.py
  tests/
    test_<name>.py
  artefacts/
    canopy/
      <name>.json
    vari/
      <name>_vari.json
    jobs/
      <name>_job.json
    tabi/
      <name>_tabi.json
    specs/
      <name>_spec.json
  deployment/
    <name>.sh
  scripts/
    <one_off_task>.py
```

## Authoritative schema source

Use the selected instance's `/swagger-json` or its saved contract for the JSON
body shape of platform-managed artefacts. The checked-in repo conventions tell
you where files live and how names align; the swagger tells you which fields
the platform accepts. When a repo has an adapter, also check its local validation
and reference transformations before treating local JSON as an API payload.

| Folder | Swagger resource | Create DTO |
| --- | --- | --- |
| `webhooks/` | `/webhooks` | `CreateWebhookHandleDto` |
| `triggers/` | `/triggers` | `CreateTriggerDto` |
| `vari/` | `/variables` | `CreateVariableDto` |
| `schemas/` | `/schemas` | `CreateSchemaDto` |
| `jobs/` | `/jobs` | `CreateJobDto` |
| `tabi/` | `/tables` | `CreateTableDto` |

Notes:
- The platform API names the `tabi` resource `tables`.
- `canopy/` is not exposed under the same name in `swagger-json`; follow
  checked-in repo examples or separate Canopy guidance for that artefact type.

## Webhook artefact

```json
{
  "name": "<name>_hook",
  "context": "<context.name>",
  "description": "<short description of what this endpoint does>",
  "subpath": "<url-path>?<param1>={<param1>}&<param2>={<param2>}",
  "method": "POST",
  "eventPayloadType": "null",
  "responsePayload": {},
  "responseStatus": 200,
  "slug": "<project slug or token>",
  "xmlParserOptions": {
    "enabled": false,
    "explicitArray": false,
    "normalizeTags": false,
    "trim": false
  }
}
```

### Field rules

| Field | Rule |
| --- | --- |
| `name` | Follow the repo's deployed naming convention; `_hook` is an example suffix. Keep paired webhook/trigger base names aligned where practical. |
| `context` | Must follow the target repo's naming conventions. |
| `subpath` | Must match the intended public URL, including path params as `{param}` and query params as `?key={key}`. |
| `method` | Upper case. |
| `eventPayloadTypeSchema` | Full JSON Schema of the request body when the endpoint accepts a body. Omit it or use the repo's established empty-body pattern when there is no body. |
| `responsePayload` | Default response body when a trigger supplies no response data. |
| `responseStatus` | Preserve the endpoint's intended status code. |
| `slug` | Follow the checked-in repo convention. Preserve placeholder tokens when the repo uses them. |
| `xmlParserOptions.enabled` | Set to `true` only when the caller sends XML that must be parsed. |

### XML parsing options

```json
{
  "xmlParserOptions": {
    "enabled": true,
    "explicitArray": true,
    "trim": true,
    "normalizeTags": true
  }
}
```

Choose these flags from the XML contract; this is not an all-true default.
Array shape, whitespace trimming, and lowercasing tags can change the payload.

## Variable artefact

```json
{
  "name": "<name>_vari",
  "context": "<context.name>",
  "description": "<what this variable stores>",
  "value": "",
  "secret": false
}
```

## Job artefact

```json
{
  "name": "<flow_name>",
  "schedule": {
    "type": "interval",
    "value": 60,
    "acceptableDelayTime": 30
  },
  "functions": [
    {
      "id": "<deployed function id>",
      "headersPayload": {},
      "eventPayload": {},
      "paramsPayload": {}
    }
  ],
  "enabled": true,
  "executionType": "sequential"
}
```

Where the repo's resolver supports it, use `functionName` and `functionContext`
in place of a job function's `id`; the resolver supplies the selected target ID.
Keep `enabled` at the intended state.

## Tabi artefact

```json
{
  "name": "<name>_tabi",
  "context": "<context.name>",
  "description": "<what this table contains>",
  "columns": []
}
```

Use the selected table contract for columns; row data uses separate table API/SDK
operations rather than the table-create payload.

## Canopy artefact

Treat `artefacts/canopy/` as optional. Use it only when the target repo checks
in Canopy UI definitions alongside the rest of the workflow artefacts.

## Shared placement guidance

- Put normal workflow implementations under `src/<workflow_name>/`.
- Put reusable cross-workflow code under `src/utilities/<utility_name>/`.
- Keep `scripts/` folders for one-off helper tasks local to the workflow or utility they support.

## OVERVIEW.md

Every workflow folder must have an `OVERVIEW.md` describing:

- integration purpose
- source integration it migrates, when applicable
- PolyAPI components created
- required variables and secrets
- downstream systems
- behavioural parity notes, when migrating
- known gaps or deviations
