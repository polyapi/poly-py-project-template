import json
from typing import Any

from polyapi import poly
from polyapi.typedefs import PolyServerFunction

polyConfig: PolyServerFunction = {
    "name": "error_alerts",
    "context": "example.diagnostics.services",
    "description": "Best-effort global error notification with redacted diagnostic context.",
    "visibility": "ENVIRONMENT",
    "logs_enabled": True,
}


SENSITIVE_KEYS = {
    "apikey",
    "appkey",
    "authorization",
    "clientsecret",
    "executionid",
    "password",
    "secret",
    "token",
    "xcorrelationid",
}


def sanitize(value: Any, ancestors: frozenset[int] = frozenset()) -> Any:
    """Keep diagnostic structure without credential fields or recursion failures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if id(value) in ancestors:
        return "[Circular]"
    if len(ancestors) >= 32:
        return "[Maximum depth]"
    seen = ancestors | {id(value)}
    if isinstance(value, dict):
        if value.get("type") == "PolyVariable":
            return "[REDACTED]"
        return {
            str(key): (
                "[REDACTED]"
                if str(key).lower().replace("_", "").replace("-", "") in SENSITIVE_KEYS
                else sanitize(item, seen)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, seen) for item in value]
    # Arbitrary __str__/repr methods can expose credentials or raise exceptions.
    return f"[{type(value).__name__}]"


async def error_alerts(error: dict[str, Any]) -> dict[str, bool]:
    try:
        # Global error delivery must work even when the log level is silent.
        await poly.example.utilities.slack.send_alert(
            {"message": json.dumps(sanitize(error))}
        )
        return {"notified": True}
    except Exception:
        # Error handlers must not recursively trigger themselves on alert failure.
        print("error_notification_failed")
        return {"notified": False}
