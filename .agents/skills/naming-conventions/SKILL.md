---
name: naming-conventions
description: Choose or review Python filenames, PolyAPI resource names, contexts, and safe renames in this template.
---

# PolyAPI naming

[CONVENTIONS.md](../../../CONVENTIONS.md) is the naming reference. Before a rename,
distinguish the source filename, Python entrypoint, deployed identity, and any
external SDK identifier; they are not interchangeable.

## Apply the correct naming rule

- Use snake_case for authored Python modules, functions, workflow directories,
  and JSON artefact filenames. Use `_cfx.py` and `_sfx.py` for deployable client
  and server files, and `test_<behavior>.py` for tests.
- Keep `polyConfig.name` aligned with the Python entrypoint. Function names and
  dotted context segments must be valid Python identifiers, not keywords.
- Use the configured namespace roots for project-owned contexts. Do not invent
  a fixed organization, environment, or regional prefix for a reusable example.
- Use descriptive artefact filenames. Category directories identify resource
  kinds; suffixes such as `_hook`, `_trigger`, and `_vari` may be retained but
  are not required for every resource.
- Preserve external function names, API payload fields, OpenAPI operation IDs,
  generated schema filenames, platform category names, and standard documentation
  filenames. Skill directories use the standard hyphenated skill naming format.

## Review a rename end to end

Search the affected source tree for the old filename and identity, excluding
real environment files, generated SDK credential stores, and private data.
Inspect generated call paths, portable references, webhook security references,
jobs, triggers, AI tools, tests, and documentation links as applicable.

A filename-only change should preserve `polyConfig` or JSON identity fields.
Changing a deployed name or context can create a new resource and affect orphan
inference; treat that as a resource migration with an explicit deployment plan.
OpenAPI training can derive contexts from filenames when `{file}` is used, so
check the training command before calling a spec rename behavior-neutral.

Run focused discovery/reference tests and `make check`. Verify that old file
references are gone and new local links resolve. Do not deploy or delete the
old resource merely to complete a local rename.
