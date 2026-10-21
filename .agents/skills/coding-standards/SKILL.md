---
name: coding-standards
description: Implement or review Python workflow error boundaries, logging, redaction, and focused tests for PolyAPI functions.
---

# PolyAPI Python coding standards

Follow [CONVENTIONS.md](../../../CONVENTIONS.md) and the nearest maintained
workflow's contracts. Keep functions focused and explain only non-obvious
business decisions, secret boundaries, retry behavior, or failure handling.

## Errors and response boundaries

- Handle specific expected failures where the code can recover. Do not turn
  every programming error into a successful response or add fuzzy fallback
  payload shapes to hide an SDK contract mismatch.
- At a public boundary, preserve the documented error envelope and HTTP status
  behavior. Use bounded safe messages; exception text, URLs, and provider
  responses can contain credentials or private data.
- Preserve exception causes when they are safe and useful internally. At a
  boundary that deliberately sanitizes provider failures, do not reintroduce
  their raw text through exception chaining, logging, or returned payloads.
- Await SDK calls and notification work when their outcome matters. Define
  whether an alert failure propagates or is best-effort; global error handlers
  must not raise again because notification delivery failed.
- Do not add retries around side effects without checking idempotency and the
  upstream retry contract. Preserve existing behavior unless the task changes it.

## Logging and secrets

Reuse the [common utilities](../../../workflows/src/utilities/common/OVERVIEW.md)
through generated SDK calls where appropriate. Use stable event names and safe
operational fields: outcome, counters, duration, and approved status details.

Do not log raw request/response bodies, credential headers, secret references,
or arbitrary exception strings. DEBUG level is not permission to expose them.
Recursive key redaction does not remove secrets embedded inside a free-text
message. Select safe fields before they reach the logger or alert sink; do not
rely on redaction to make an arbitrary payload safe.

The shared logger caches only its NONE log-level configuration. Keep sensitive
Vari reads uncached, and preserve opaque injection references at the generated
API/server boundary described in [polyapi-sdk](../polyapi-sdk/SKILL.md).

## Tests and maintenance

Test observable outcomes at the workflow boundary: malformed inputs, exact SDK
arguments, upstream failures, response status, and safe failure messages. For
logging or alert changes, cover nested redaction and delivery failure behavior.
Use synthetic values and mock SDK/network boundaries; live execution is opt-in.

Run the focused tests and `make check`. Update the same-folder `OVERVIEW.md`
when behavior changes, using [generate-overview](../generate-overview/SKILL.md).
Keep source comments concise and synchronized with the code.
