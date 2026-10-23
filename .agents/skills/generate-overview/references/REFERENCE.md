# generate-overview Reference

Resolve the workflow root from the active repo first. In the rules and examples
below, substitute that root for `src/` (for example, `workflows/src/`).

This reference captures the required structure and validation rules for
`OVERVIEW.md` generation in this repo. It is the canonical source for this
skill.

## Same-Folder Rule

`OVERVIEW.md` must be created or updated in the same folder that is being
analysed.

Examples:

- `src/currency_exchange/OVERVIEW.md`
- `src/utilities/oracledb_adapter/OVERVIEW.md`

## Analysis Scope

- Analyse one workflow or utility folder at a time.
- Use code and artefacts as the primary source of truth.
- Use tests only to confirm behavior.
- Treat an existing `OVERVIEW.md` as secondary reference only.

## Flow Discovery Rules

Verify these common filename patterns against the repo's discovery rules and
function metadata; include supported entrypoints with other filenames.

- Search the analysed folder for server entry points under `server/`.
- Server entry points match:
  - `*_sfx.py`
  - `*SFX.ts`
- If any server entry points exist, each one defines a primary flow.
- If no server entry points exist, inspect `client/` and treat callable client
  functions as flow candidates.
- Client functions match:
  - `*_cfx.py`
  - `*CFX.ts`
- Use the actual entrypoint name; the filename without its suffix is suitable
  when it matches that entrypoint.

## Expansion Rules

- Do not leave client functions as black-box steps.
- Expand all client logic that affects behavior, including:
  - conditionals
  - guard clauses
  - switch or match logic
  - loops
  - retries
  - try or except handling
- Expand utility or global logic when it performs:
  - validation
  - branching
  - retries
  - caching
  - connection pooling
  - error translation
  - business-meaningful transformations
  - external I/O

## Artefacts To Inspect

- `artefacts/jobs/**`
- `artefacts/webhooks/**`
- `artefacts/canopy/**`
- `artefacts/vari/**`
- `artefacts/tabi/**`
- `artefacts/types/**`
- `artefacts/specs/**`
- `deployment/**`

Use them to determine:

- triggers
- contracts and payload shapes
- Vari reads or injections
- TABI reads or writes
- deployment assumptions

## Required `OVERVIEW.md` Structure

For each primary flow, create a section with this heading:

```md
# Flow: <Flow Name>
```

Each flow section must include:

- purpose of the flow
- trigger mechanism or caller entry point
- entrypoint type:
  - server function
  - client function
  - inferred caller
- client functions involved
- utility or global functions involved
- Vari usage
- TABI or other persisted state usage
- external systems involved
- all end states
- assumptions made

## Flowchart Rules

Create a complete Mermaid flowchart for each flow showing:

- all triggers or callers, using `S0` or distinct IDs when there are several
- all server logic if present
- all primary client logic
- expanded helper client logic
- expanded utility or global logic that changes behavior
- decisions and branches
- Vari and TABI operations
- external API or database calls
- all end states

Mermaid constraints:

- use flowchart only
- steps use `[ ]`
- decisions use `{ }`
- labels contain letters, digits, and spaces only
- do not use parentheses in labels
- node IDs use letters, digits, and underscores only
- avoid reserved words such as `end`, `subgraph`, `class`, `click`, `style`,
  and `link`
- Boolean decisions have both `yes` and `no` paths
- multi-way decisions label every applicable outcome
- keep labels short and move detail into step descriptions

## Step Detail Requirements

For every step in the flowchart, include:

1. Step ID
2. Business meaning
3. Context path and function name
4. Implementation file link
5. Classification
6. Business rules applied
7. Error handling behavior

Classification values:

- trigger
- inferred caller
- server logic
- primary client logic
- client logic
- utility logic
- global logic
- TABI
- Vari
- external API
- external database
- deployment or config

Rules:

- every locally implemented step must include at least one file link; label
  external or inferred callers without inventing implementation files
- links must be clickable relative Markdown links that resolve from the
  `OVERVIEW.md` location
- if logic spans multiple files, link all relevant files
- link artefact files when referenced

## Triggers and End States

- List all triggers or inferred callers.
- List all end states.
- Every trigger or caller and every end state must also appear in the
  flowchart.

## Final Validation Checklist

Before finishing, ensure:

- every flow includes all actual or explicitly inferred trigger/caller entry nodes
- every Boolean decision has `yes` and `no` paths; multi-way outcomes are labeled
- every client function referenced by the flow is expanded
- business-relevant utility and global logic is expanded
- Vari and TABI usage appear in both text and diagram when used
- external systems and databases are shown where called
- all triggers or callers and all end states appear in both text and diagram
- every locally implemented step includes valid clickable file links relative to the
  `OVERVIEW.md` location
- `OVERVIEW.md` is in the correct folder
- assumptions are stated explicitly
