---
name: generate-overview
description: Create or refresh a same-folder `OVERVIEW.md` for
  `src/<workflow>` or `src/utilities/<utility_name>` after behavior-changing
  edits. Use when server or client code, artefacts, deployment files, local
  scripts, Vari, TABI, or shared utility behavior changes and the overview must
  stay aligned.
compatibility: Aligned to this repo's current PolyAPI workflow layout and the
  local reference file in this skill directory.
---

# Skill: Generate Overview

Resolve the workflow root from the active repo first. In the rules and examples
below, substitute that root for `src/` (for example, `workflows/src/`).

Use this skill to create or refresh authoritative `OVERVIEW.md` files for
runtime-owned folders in this repository.

This skill should stay aligned with `./references/REFERENCE.md`.

## When To Use

Use this skill when:

- a new workflow is created under `src/<workflow_name>/`
- a new utility is created under `src/utilities/<utility_name>/`
- behavior changes in `server/`, `client/`, `artefacts/`, `deployment/`, or
  folder-local `scripts/`
- shared utility behavior changes and the utility overview should be updated
- a workflow or utility overview is missing or obviously stale

Usually skip a full overview refresh for:

- formatting-only changes
- comment-only changes
- root documentation changes
- tooling changes with no runtime behavior effect

## Core Rules

- Target the smallest workflow or utility folder that owns the changed runtime
  behavior.
- Write or update `OVERVIEW.md` in that same folder only.
- Use code and artefacts as the primary source of truth.
- Use tests only to confirm behavior, not define it.
- Use an existing `OVERVIEW.md` only as a secondary reference.
- Ignore generated noise such as `__pycache__/`, `.polyapi-python`, and other
  compiled or generated artefacts.
- Expand client logic instead of leaving client calls as black boxes.
- Expand utility or global logic when it changes behavior through validation,
  branching, retries, caching, state access, or external I/O.
- All file references inside `OVERVIEW.md` must be clickable relative Markdown
  links that resolve from the overview location.
- State assumptions explicitly instead of stopping unless something is genuinely
  unclear.

## Folder Types

The analysed folder will usually be one of:

- `src/<workflow_name>`
- `src/utilities/<utility_name>`
- `src/scripts/<script_name>`

## Flow Discovery

Verify these common filename patterns against the repo's discovery rules and
function metadata; include supported entrypoints with other filenames.

- Treat files under `server/` matching `*_sfx.py` or `*SFX.ts` as server entry
  points.
- Treat files under `client/` matching `*_cfx.py` or `*CFX.ts` as client
  functions.
- If server entry points exist, each one defines a primary flow.
- If no server entry points exist, treat callable client functions as candidate
  entry points and use an inferred caller trigger when needed.

## Artefacts and Dependencies

Inspect the analysed folder for:

- `artefacts/jobs/**`
- `artefacts/webhooks/**`
- `artefacts/canopy/**`
- `artefacts/vari/**`
- `artefacts/tabi/**`
- `artefacts/types/**`
- `artefacts/specs/**`
- `deployment/**`

Use these to determine triggers, contracts, Vari or TABI usage, and deployment
assumptions.

## Output

- Create exactly one same-folder `OVERVIEW.md` for the analysed path.
- Include one `# Flow: <Flow Name>` section per primary flow.
- Include a Mermaid flowchart and step details for each flow.
- Include triggers or inferred callers, end states, assumptions, and all
  business-relevant dependencies.

## Reference

See `./references/REFERENCE.md` for the exact output structure, Mermaid rules,
step-detail fields, and final validation checklist.
