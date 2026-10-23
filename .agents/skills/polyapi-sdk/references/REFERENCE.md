# polyapi-sdk Reference

## Setup commands

Use the repo's pinned environment and package manager first. The native CLI
examples below apply when that is the repo's chosen workflow; use its existing
Flow or other deploy commands where configured. Setup, generation, and deployment
require an explicitly selected, authorized target.

```bash
python -m polyapi setup
python -m polyapi generate
```

Refresh the SDK after catalog changes unless the deploy command already does so.
Native `function add` in `0.3.20.dev4` regenerates unless `--skip-generate` is used.

## Python version

Match the target repo's pinned Python and SDK versions.

## Type mapping reference

| JSON Schema type | Python type |
| --- | --- |
| `integer` | `int` |
| `number` | `float` |
| `string` | `str` |
| `boolean` | `bool` |
| `array` | `List[ItemType]` from `typing`, or `list[ItemType]` with verified parser support |
| `object` | local `TypedDict` or a verified dictionary annotation |
| `void` | Verify the emitted schema; `None` alone may parse as `Any` |

In `0.3.20.dev4`, top-level `list[str]`, `dict[str, str]`, `str | None`, and a
`None` return parse as `Any`; `List[str]`, `Dict[str, str]`, and named local types
are recognized. Check emitted argument/return schemas. `TypedDict` fields follow
a separate schema path.

## Deployment commands

```bash
python -m polyapi function add process_event ./server/process_event_sfx.py --context example.events --server
python -m polyapi function add normalize_event ./client/normalize_event_cfx.py --context example.events --client
```

## Webhook-linked SFX contract

A common webhook-trigger mapping passes these arguments. Verify the actual
contract; this is not every server or security function's signature:

| Argument | Type | Notes |
| --- | --- | --- |
| `__event` | `dict` | Request body or event payload |
| `headers` | `dict` | HTTP or trigger headers |
| `params` | `dict` | Query and path parameters |

In router-driven HTTP flows, `headers` often includes `X-Http-Method`, `X-Original-Uri`, `Authorization`, and `X-Force-Log`.

## Circular import pattern

Never reference `poly.<context>.<Type>` inside a `TypedDict` definition when deploying to the same context.

**Symptom:**

```text
AttributeError: partially initialized module 'polyapi.poly' has no attribute 'utils'
(most likely due to a circular import)
```

**Fix:** redeclare the `TypedDict` locally in each file that needs it. A
single-file deployer needs required runtime types/helpers in the submitted file
unless it explicitly supports bundling neighboring modules.

```python
from typing_extensions import TypedDict

class PlatformLoggingConfig(TypedDict):
    site: str
    api_key: str

class InitLoggerParams(TypedDict):
    platform_config: PlatformLoggingConfig
```

After fixing, regenerate:

```bash
python -m polyapi generate
```

## SFX function shape

Match the actual return contract: a security function can return `bool`, and other
functions may return supported scalar or structured types. The HTTP-handler
example below returns a `dict`; use `coding-standards` for exception ordering,
logging, and boundary behavior.

```python
async def my_function_sfx(__event: dict, headers: dict, params: dict) -> dict:
    if "required_param" not in params:
        return {"error": "missing required_param"}  # Adapt to the repo boundary contract
    return {"ok": True}
```

Use the active repo's standards skill for project-specific logger wiring and boundary payload rules.

## Virtual environment

```bash
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\activate.bat
source .venv/bin/activate
pip install -e ".[dev]"
```
