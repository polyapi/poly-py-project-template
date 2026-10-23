---
name: polyapi-sdk
description: Reference guide for correct PolyAPI Python setup, typing rules, deployment commands, and SDK quirks. Use before writing or deploying PolyAPI Python code in PolyAPI projects.
---

# Skill: PolyAPI SDK

## Purpose

Reference guide for writing correct PolyAPI Python code: setup, type system rules, deployment commands, and known SDK quirks that must be handled to avoid runtime and generation errors.

## When to use

Load this skill before writing any PolyAPI Python code. The rules here are non-obvious and diverge from standard Python conventions in ways that cause silent failures if ignored.
For exception ordering and logging behaviour, also follow `coding-standards`.

---

## Python version

Match the target repo's pinned Python and SDK versions. Verify with `python --version`.

---

## TypedDict — critical rule

**Always import `TypedDict` from `typing_extensions`, not from `typing`.**

```python
# CORRECT
from typing_extensions import TypedDict, NotRequired, Literal, Any

# WRONG — will cause type inference failures in the PolyAPI SDK
from typing import TypedDict
```

Use annotations supported by the pinned SDK at deployed function boundaries.
For example, `0.3.20.dev4` parses `list[str]`, `dict[str, str]`, `str | None`,
and a `None` return as `Any`; it recognizes `List[str]`, `Dict[str, str]`, and
named local types. Verify emitted argument/return schemas, including optional
and no-return types. Internal annotations follow a separate path.

```python
from typing import List

def echo_tags(tags: List[str]) -> List[str]:
    return tags
```

---

## Circular import pattern — critical rule

**Never reference `poly.<context>.<Type>` inside a `TypedDict` definition when deploying to the same context.**

The SDK generator creates `__init__.py` files that import all functions. Cross-referencing a type from a sibling function in the same context causes a circular import at SDK load time.

Redeclare the `TypedDict` locally in each file that needs it. Do not import sibling SDK types from the same context.

A single-source-file deployer does not bundle neighboring `_types.py` or helper
modules. Keep required runtime types/helpers inline unless bundling is supported.

---

## Webhook-linked SFX contract — critical rule

A common webhook-trigger mapping passes these three arguments. Verify the actual
trigger and function signature; ordinary and security functions can differ:

| Argument | Type | Contents |
| --- | --- | --- |
| `__event` | `dict` | Request body or event payload |
| `headers` | `dict` | HTTP or trigger headers |
| `params` | `dict` | Query and path parameters |

When a shared HTTP router sits in front of the SFX, `headers` often includes keys such as `X-Http-Method`, `X-Original-Uri`, `Authorization`, and `X-Force-Log`.

---

## SFX function shape

Match the actual return contract: a security function can return `bool`, while an
HTTP handler may return a `dict`. Follow `coding-standards` for exception ordering and logging, and follow any project-specific standards skill for the boundary payload and status-setting pattern.

---

## Core rules

- Use the repo's pinned SDK, environment, and existing setup/deploy commands;
  do not introduce Flow as a prerequisite for a native-CLI repo.
- Refresh the SDK after catalog changes unless the deploy command already does so.
- Import `TypedDict` from `typing_extensions`, never from `typing`.
- Verify deployed argument and return schemas with the pinned SDK parser.
- The `--context` must match the active repo's naming conventions.
- Match webhook handler arguments to the actual trigger mapping.
- Match the function's return contract and handle top-level failures cleanly.
- Use `coding-standards` for exception handling structure, plus any project-specific standards skill for repo-specific boundary behaviour.

## Reference

See `./references/REFERENCE.md` for setup commands, deployment commands, type mapping, circular import examples, webhook-linked SFX conventions, and virtual environment notes.
