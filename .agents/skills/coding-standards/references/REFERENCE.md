# coding-standards Reference

Resolve the workflow root from the active repo first. In the rules and examples
below, substitute that root for `src/` (for example, `workflows/src/`).

## Overview maintenance

- Every workflow folder under `src/<workflow_name>/` should have an
  up-to-date `OVERVIEW.md`.
- Every utility folder under `src/utilities/<utility_name>/` should have an
  up-to-date `OVERVIEW.md`.
- When runtime behavior changes, refresh the overview in that same folder
  before considering the task complete.
- For new workflow or utility folders, create `OVERVIEW.md` as part of the
  implementation work.
- Formatting-only, comment-only, and non-runtime tooling changes can skip an
  overview refresh.
- Use the `generate-overview` skill to create or refresh the overview so the
  file stays consistent with that skill's local reference.

## Error handling examples

### Catch the most specific exception type

```python
try:
    data = json.loads(raw)
except json.JSONDecodeError as error:
    call_logger["error"]("Invalid JSON", {"error": str(error)})
    return {"error": f"invalid JSON: {error}"}  # Adapt to the repo boundary contract

try:
    data = json.loads(raw)
except Exception as error:
    call_logger["error"]("Unexpected parsing failure", {"error": str(error)})
    return {"error": "something went wrong"}  # Adapt to the repo boundary contract
```

### When to catch vs when to raise

| Situation | Action |
| --- | --- |
| You can recover or provide a fallback | Catch, handle, return a repo-appropriate error payload |
| You cannot recover but the caller can | Catch, add context, re-raise with `from` chaining |
| Unexpected or programming error in helpers or client functions | Let it propagate |
| Unexpected or programming error at the top-level boundary | Log it and return the repo's established error result |
| Cleanup needed regardless of outcome | Use `finally` or `with` |

### Chain exceptions when re-raising

```python
try:
    data = fetch()
except urllib.error.URLError as error:
    raise RuntimeError("failed to fetch config") from error
```

Use `raise ... from None` only when the original exception contains secrets or tokens.

### Boundary return shape is repo-specific

- Use the nearest sibling workflow or utility as the source of truth for the returned payload shape.
- Some repos use simple HTTP payloads such as `{"message": "Bad request"}`.
- Others use richer envelopes such as `{"status_code": 500, "success": False, "error": {"message": "..."}}`.
- Pick one only if it matches the local pattern. Do not normalize the whole repo to a new envelope.

Always log before returning an error result.
This includes unexpected failures at the top-level boundary.

### Order except clauses from most specific to least specific

```python
try:
    conn = oracledb.connect(...)
except oracledb.DatabaseError as error:
    call_logger["error"]("DB connection failed", {"error": str(error)})
    return {"error": f"DB connection failed: {error}"}  # Adapt to the repo boundary contract
except OSError as error:
    call_logger["error"]("OS error", {"error": str(error)})
    return {"error": f"OS error: {error}"}  # Adapt to the repo boundary contract
except Exception as error:
    call_logger["error"]("Unexpected failure", {"error": str(error)})
    return {"error": "Unexpected failure"}  # Adapt to the repo boundary contract
```

### Subprocess error handling

```python
try:
    proc = subprocess.run([...], timeout=60, capture_output=True, text=True)
except subprocess.TimeoutExpired as error:
    call_logger["error"]("Subprocess timed out", {"timeout": error.timeout})
    return {"error": f"subprocess timed out after {error.timeout}s"}  # Adapt to the repo boundary contract
except Exception as error:
    call_logger["error"]("Subprocess failed", {"error": str(error)})
    return {"error": str(error)}  # Adapt to the repo boundary contract

if proc.returncode == 0 and proc.stdout.strip():
    try:
        result = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as error:
        call_logger["error"]("Subprocess returned invalid JSON", {"error": str(error)})
        return {"error": f"subprocess returned invalid JSON: {error}"}  # Adapt to the repo boundary contract
```

### Network calls

```python
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
except urllib.error.HTTPError as error:
    raise RuntimeError(f"HTTP {error.code} from {url}: {error.reason}") from error
except urllib.error.URLError as error:
    raise RuntimeError(f"Network error: {error.reason}") from error
except OSError as error:
    raise RuntimeError(f"IO error: {error}") from error
```

### Resource cleanup

```python
try:
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(dest)
except zipfile.BadZipFile as error:
    raise RuntimeError(f"archive corrupt: {error}") from error
finally:
    try:
        os.remove(zip_path)
    except OSError:
        pass
```

### Quick reference

| Scenario | Exception type |
| --- | --- |
| Network download | `HTTPError` -> `URLError` -> `OSError` |
| JSON parsing | `json.JSONDecodeError` |
| File or OS operations | `OSError` |
| Subprocess timeout | `subprocess.TimeoutExpired` |
| DB operations | library-specific |
| Missing dict key | `KeyError` |

## Logging

### Log level rules

| What | When | Level |
| --- | --- | --- |
| Workflow started or finished | Operator needs to follow a production timeline | INFO |
| Success or skip outcome | State-changing or branch outcome needs completion evidence | INFO |
| Final workflow counters | Completion of an integration workflow | INFO |
| Performance timing | Integration latency measurement | INFO |
| Input request payload | Payload drives external call or branching behaviour | DEBUG |
| Response payload | Outcome requires body detail | DEBUG |
| Mapping or decision logic | Conditional logic affects mapping or action | DEBUG |
| Extracted candidates | Engineers must validate duplicate detection, matching, or filtering | DEBUG |
| Retryable failure | Transient failure triggers retry handling | WARN |
| Terminal or unexpected failure | Failed operation or unexpected condition, with sanitized request/response payloads when available | ERROR |
| Transport internals | HTTP client connection noise with no business value | Suppress |

### INFO vs DEBUG boundary

INFO logs are the high-volume operator timeline. Keep them compact and focused
on what happened, where it happened, the business entity involved, the
success/failure outcome, counters, and elapsed duration. Avoid full URLs,
headers, request bodies, response bodies, payload fragments, credential
diagnostics, and low-level transport details at INFO.

DEBUG logs are for human troubleshooting. They should be payload-centric:
include the outbound request payload, the inbound response payload, the exact
records evaluated, the selected record, rules or thresholds, and the final
decision reason when those details explain why the workflow acted.

ERROR logs should also be troubleshooting-ready. When a failed operation has an
available request payload, upstream response body, upstream status, retry
decision, or parsed error payload, include the sanitized useful parts directly
on the ERROR log. Avoid metadata-only errors when the payload is needed to
understand why the operation failed.

Prefer fewer rich DEBUG entries over many metadata-only entries. Do not log
only derived booleans or counts when the actual compact payload is needed to
replay or validate the decision.

### Log format

```python
call_logger["debug"]("Input payload", {"payload": payload})
call_logger["info"]("Member balance retrieved", {"member_id": member_id, "status": 200})
call_logger["warn"]("Retry attempt", {"attempt": 2, "reason": str(error)})
call_logger["error"]("Downstream call failed", {"error": str(error), "url": url})
```

### Logging rules

- Always log before returning an error result.
- Do not log secrets, tokens, or passwords at any level.
- Mask credential-like keys such as `token`, `authorization`, `appKey`,
  `api_key`, `password`, and `secret` before logging payloads.
- Cap very large strings and lists before they reach Datadog or the shared log
  sink.
- Use DEBUG payload logging when the integration has materially complex
  conditional behaviour, matching, duplicate detection, or external payload
  construction.
- Keep DEBUG payloads business-focused. Do not include low-level HTTP transport
  internals unless they are the bug being investigated.
- For ERROR logs, include sanitized request/response payloads whenever they
  materially help troubleshoot and are available in the current scope. If the
  full payload is too large, include a compacted version plus the exact status,
  operation name, and retry decision.
- If the repo uses alerting on fatal boundary paths, preserve that alert call before returning.

### Payload-shape and decision logging checklist

When adding or refactoring workflow logging:

1. Identify the workflow milestones that belong at INFO.
2. Define stable correlation fields before adding log calls.
3. Log request and response payloads at DEBUG for external boundaries.
4. Log extracted candidates and selected records at DEBUG for matching,
   duplicate detection, or filtering.
5. Log the rules, thresholds, and reason for important branch decisions.
6. Avoid generic field-name guessing helpers. Use the exact SDK type,
   checked-in implementation contract, or known upstream payload shape.
7. If the expected shape is absent, fail clearly or return the local controlled
   integration error instead of silently falling back to another shape.
8. Suppress noisy third-party loggers such as HTTP transport libraries when
   they pollute workflow logs.
9. Add or update tests for exact payload paths and important structured log
   fields when logs are part of the debugging contract.
