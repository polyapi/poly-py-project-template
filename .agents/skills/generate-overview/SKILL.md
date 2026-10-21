---
name: generate-overview
description: Create or refresh a workflow or utility OVERVIEW.md from its Python source, PolyAPI artefacts, and focused tests when runtime behavior changes.
---

# Workflow overview maintenance

Update the smallest workflow or utility folder that owns the changed behavior
under `workflows/src/`. Use its `OVERVIEW.md`; do not create a second guide for
the same flow. Formatting-only edits do not require an overview rewrite.

## Inspect the actual flow

- Read deployable entrypoints, `polyConfig`, relevant client calls, and the
  artefacts that connect them. Follow shared utilities when their behavior
  changes the result, error handling, logging, caching, or external effects.
- Use code and artefacts as the primary evidence, tests as confirmation, and
  existing prose as secondary context. Distinguish verified local behavior
  from target-dependent setup and unverified runtime assumptions.
- Read only safe source and configuration examples. Never inspect real
  environment files, SDK credential stores, or customer payloads for examples.

## Write what a maintainer needs

Include the pieces relevant to this workflow, without empty sections:

- Purpose and callable entrypoints, with a small flow diagram if useful.
- Inputs, outputs, HTTP status behavior, validation, and handled failures.
- SDK dependencies and exact resource contexts; explain important client and
  utility behavior instead of treating them as unexplained boxes.
- Required configuration names and secrecy levels, injection boundaries, and
  the intended enabled state of jobs or streams. Use placeholders only.
- External effects, retry/idempotency behavior, and operational limits that
  affect safe use.
- Focused local test commands and separately labeled opt-in runtime checks.

Link source files with relative Markdown links that resolve from the overview.
Use repository-root Fish commands, and link the [root setup guide](../../../README.md)
and [Flow reference](../../../cli/README.md) rather than copying their full setup
or deployment procedures. Describe behavior independently of other projects or
languages; do not add customer-specific assumptions to a generic workflow.

## Finish

Verify names, paths, defaults, and commands against the current source. Check
every local link. State which checks were actually run and what still depends
on a configured environment. Do not infer successful deployment from passing
unit tests, or run a deployment to make documentation claims appear verified.
