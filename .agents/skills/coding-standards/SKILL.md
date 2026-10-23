---
name: coding-standards
description: Enforces generic PolyAPI Python error handling, logging, and
  overview-maintenance standards. Use before writing or reviewing server
  functions, client functions, or utilities that need structured errors,
  consistent logs, and up-to-date `OVERVIEW.md` files.
compatibility: Designed for PolyAPI Python work. Examples assume Python 3.12+ and a shared structured logger, but project-specific logger wiring and boundary payloads come from the active repo.
---

# Skill: Coding Standards

Resolve the workflow root from the active repo first. In the rules and examples
below, substitute that root for `src/` (for example, `workflows/src/`).

## Purpose

Enforce reusable error handling, logging, and overview-maintenance standards
when writing PolyAPI Python code. These patterns should stay stable across
repos even when response envelopes, loggers, or alerting helpers differ.

## When to use

Load this skill before writing any error handling or logging code. Apply it
when reviewing generated code for compliance. Also apply it whenever
behavior-changing work is done under `src/<workflow_name>/` or
`src/utilities/<utility_name>/`.

---

## Core rules

- Catch the most specific exception type you can handle.
- Re-raise with `from error` when adding context.
- Log before returning from handled error paths.
- Order `except` clauses from most specific to least specific.
- Do not swallow unexpected programming errors below the top-level boundary.
- At the top-level boundary, log unexpected failures and convert them into the repo's established error payload and status behaviour.
- Preserve repo-specific alerting or status-setting side effects that existing sibling code already uses.
- Do not log secrets, tokens, or passwords.
- Keep INFO logs to operational milestones, outcomes, counters, and elapsed
  timing. Do not put request bodies, response bodies, headers, URLs, or
  transport internals in INFO logs.
- Use DEBUG logs for payload-first troubleshooting when payloads materially
  affect branching behaviour, external interactions, mapping, or decisions.
- Include sanitized request and response payloads in ERROR logs when those
  payloads are available and materially help troubleshoot the failure. Prefer
  the exact outbound request payload, useful upstream response body, upstream
  status, and retry decision over metadata-only errors.
- Prefer fewer, richer DEBUG logs over many fragmented metadata-only logs.
- Do not replace useful request/response payloads with derived field spam.
- Suppress or raise the level of noisy third-party transport loggers when they
  pollute workflow logs.
- Do not create generic data-shape helpers that guess alternative field names.
  Use the exact checked-in implementation contract, SDK type, or known upstream
  payload shape; fail clearly when that shape is absent.
- After behavior-changing edits under `src/<workflow_name>/`, create or refresh
  that folder's `OVERVIEW.md`.
- After behavior-changing edits under `src/utilities/<utility_name>/`, create
  or refresh that utility folder's `OVERVIEW.md`.
- For new workflow or utility folders, ensure `OVERVIEW.md` exists before
  finishing.
- Formatting-only, comment-only, and non-runtime changes do not require an
  overview refresh.
- Use the `generate-overview` skill for overview creation or refresh work.

## Boundary and logging guidance

- Error return shape is repo-specific. Copy the nearest sibling workflow or utility instead of inventing a new global envelope.
- If the repo sets HTTP status metadata separately from the returned payload, set that metadata before returning.
- Use the repo's shared structured logger interface and prefer a message plus context dict where supported.
- For externally observable workflows, define the operator timeline first:
  started, finished, skipped, posted, retryable failure, terminal failure, and
  final counters. Keep those INFO entries compact.
- At DEBUG, log the business payloads needed to replay or validate decisions:
  outbound request, inbound response, extracted candidates, selected records,
  rules or thresholds, and the final decision reason.
- Treat overview freshness as part of the implementation boundary: if behavior
  changed, the same-folder overview must reflect the new behavior before the
  task is complete.

## Reference

See `./references/REFERENCE.md` for specific exception examples, network and
subprocess patterns, logging level guidance, code snippets, and the overview
maintenance checklist.
