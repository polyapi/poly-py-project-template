---
name: artefacts-usage
description: Defines generic PolyAPI artefact structures, folder layout, and field rules. Use before creating webhook, trigger, variable, job, tabi, canopy, or workflow artefact files.
---

# Skill: Artefacts Usage

Resolve the workflow root from the active repo first. In the rules and examples
below, substitute that root for `src/` (for example, `workflows/src/`).

## Purpose

Define the correct structure, field requirements, and placement for PolyAPI JSON artefacts, plus the baseline folder layout for workflows and shared utilities.

## When to use

Load this skill before creating any artefact JSON file, before laying out a new workflow or shared utility folder, or when reviewing generated artefacts for structural compliance.

---

## Rules

- One webhook JSON per public endpoint — never collapse multiple endpoints into one generic webhook.
- Every webhook must have an explicit subpath — never use a catch-all pattern for new public endpoints.
- Use the selected instance's `/swagger-json` or its saved contract for JSON
  field shapes. When present, also inspect the repo's adapter/validators; local
  artefact references may be translated before reaching the platform API.
- Keep one workflow per `src/<workflow_name>/` folder.
- Put shared reusable implementations under `src/utilities/<utility_name>/`.
- Keep `scripts/` folders one-off and local; do not treat them as shared runtime dependencies.
- Use the repo's portable function references for jobs when supported; otherwise
  use verified function IDs from the selected target, not empty IDs.
- Never embed secret values in artefact files — record names only.
- Treat `canopy/` as an optional artefact directory used only when the target
  repo checks in Canopy UI definitions.

## Minimum checklist

- Follow the workflow folder layout under `src/<workflow_name>/`, creating only needed folders.
- Follow the shared utility layout under `src/utilities/<utility_name>/` when code is reused across workflows.
- Use `snake_case` for all Python files and JSON artefact files.
- Create one webhook JSON per public endpoint.
- Add a paired trigger JSON when the target repo checks in trigger artefacts for public endpoints.
- Preserve existing public status codes and explicit request and response schemas.
- Use secret variables for passwords, tokens, and API keys.
- Treat XML handling as an explicit webhook boundary concern.
- Add or update `canopy/` artefacts only when the target repo actually uses
  Canopy UI definitions.

## Reference

See `./references/REFERENCE.md` for the folder trees, the authoritative
swagger mapping, JSON examples, field rules, XML parser options, job and
variable examples, and `OVERVIEW.md` expectations.
