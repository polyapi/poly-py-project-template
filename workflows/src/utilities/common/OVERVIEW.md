# Common utilities

`example.utilities.log(level, function_name, message, context={})` filters by
`example.configurations.log_settings.log_level` and formats structured context. The
NONE log-level value is cached for 60 seconds; invalid values retain the current
level, and failed refreshes fall back to `debug`. The function returns `True`
after logging or `False` when filtered. Its default context is read-only.

The `log_settings` context avoids the SDK's generated `logging` import.
For an existing deployment that uses `configurations.logging.log_level`, deploy
the new Vari and updated logger, regenerate SDK bindings, and rebuild consuming
server functions with Flow's dependency-aware `push-dynamic` workflow. Keep the
old Vari until those consumers have migrated; scoped pushes do not delete it.
`LOG_LEVEL` remains the environment setting for the new Vari.

Context is recursively redacted for API keys, authorization, app keys, client
secrets, execution IDs, passwords, secrets, tokens, and correlation headers.
Circular values are handled safely. Use safe messages: redaction of named fields
does not remove credentials embedded in arbitrary text.

`error` and `fatal` entries also call
`example.utilities.slack.send_alert({"message": "..."})`. This utility reads the
exact channel Vari, logs start/success with a shared correlation ID, and awaits
`OOB.polyapi.internal.alerting.sendAlertToPoly_async`. It does not log message
previews and returns `True` when the shared alert-function call completes.
An unavailable configuration or an error surfaced by the SDK propagates to its
caller. The shared service can handle Slack failures internally; this return
value alone does not prove delivery. Verify the message in Slack. PagerDuty levels
are explicitly empty so this Slack utility cannot inherit paging notifications.

`example.diagnostics.services.error_alerts` sends a fixed failure message plus
validated, bounded function names/contexts and an integer HTTP status when present.
It never forwards arbitrary messages, stacks, URLs, headers, bodies, or nested
error data. It calls `send_alert` directly, even when logging is `silent`.
The global error trigger starts enabled. The handler returns `{"notified": true}`
when the alert-function call completes and catches surfaced notification failures
with `{"notified": false}` to avoid
an error-handler loop.

Set `LOG_LEVEL`, `SLACK_CHANNEL_ID`, and `SLACK_WORKSPACE_TYPE` in the selected
local environment before pushing the utilities. They populate three NONE Vari
artefacts. Use `community` for the PolyAPI Community workspace or `internal` for
the PolyAPI workspace; unknown values stop before alert submission. Existing
environment files must add `SLACK_WORKSPACE_TYPE` before redeploying utilities.
The target also needs the generated internal alert function.

The destination must be accessible to the selected workspace's alerting app.
A Slack user ID can request a direct message from the app; a self-DM ID is not
proof that the app can access that conversation. Verify live delivery in the
intended destination.

```bash
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths utilities/common --dry-run
```

## Project utility commands

OpenAPI training, context-scoped cleanup, and function logs are implemented in
`cli/operations.py` and share Flow's target, OTP, and production safeguards.
See the [CLI reference](../../../../cli/README.md) for exact commands. Training
and cleanup are operator actions; runtime functions do not invoke them.
