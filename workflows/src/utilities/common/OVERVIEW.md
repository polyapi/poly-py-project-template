# Common utilities

`example.utilities.log(level, function_name, message, context=None)` filters by
`example.configurations.logging.log_level` and formats structured context. The
NONE log-level value is cached for 60 seconds; invalid values retain the current
level, and failed refreshes fall back to `debug`.

Context is recursively redacted for API keys, authorization, app keys, client
secrets, execution IDs, passwords, secrets, tokens, and correlation headers.
Circular values are handled safely. Use safe messages: redaction of named fields
does not remove credentials embedded in arbitrary text.

`error` and `fatal` entries also call
`example.utilities.slack.send_alert({"message": "..."})`. This utility reads the
exact channel Vari, logs start/success with a shared correlation ID, and awaits
`OOB.polyapi.internal.alerting.sendAlertToPoly_async`. It does not log message
previews. An unavailable channel or delivery failure propagates to its caller.

`example.diagnostics.services.error_alerts` redacts structured error context and
calls `send_alert` directly, even when logging is set to `silent`. The global
error trigger starts enabled. The handler catches notification failures to avoid
an error-handler loop.

Set `LOG_LEVEL` and `SLACK_CHANNEL_ID` in the selected local environment before
pushing the utilities. They populate the two NONE Vari artefacts. The target
also needs the generated internal alert function.

```fish
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths utilities/common --dry-run
```

## Project utility commands

OpenAPI training, context-scoped cleanup, and function logs are implemented in
`cli/operations.py` and share Flow's target, OTP, and production safeguards.
See the [CLI reference](../../../../cli/README.md) for exact commands. Training
and cleanup are operator actions; runtime functions do not invoke them.
