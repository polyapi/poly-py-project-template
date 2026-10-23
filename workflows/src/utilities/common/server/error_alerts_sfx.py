import json
import re
from typing import Any, Dict

from polyapi import poly
from polyapi.typedefs import PolyServerFunction
from typing_extensions import TypedDict

polyConfig: PolyServerFunction = {
    "name": "error_alerts",
    "context": "example.diagnostics.services",
    "description": "Best-effort global error notification with redacted diagnostic context.",
    "visibility": "ENVIRONMENT",
    "logs_enabled": True,
}


class AlertResult(TypedDict):
    notified: bool


def safe_diagnostics(error: Dict[str, Any]) -> Dict[str, Any]:
    """Select bounded platform metadata; never forward arbitrary error text."""
    diagnostic: Dict[str, Any] = {"message": "PolyAPI function execution failed"}
    for key in ("functionName", "functionContext"):
        value = error.get(key)
        if (
            isinstance(value, str)
            and len(value) <= 160
            and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15}", value
            )
        ):
            diagnostic[key] = value
    status = error.get("statusCode")
    if type(status) is int and 100 <= status <= 599:
        diagnostic["statusCode"] = status
    return diagnostic


async def error_alerts(error: Dict[str, Any]) -> AlertResult:
    try:
        # Global error delivery must work even when the log level is silent.
        await poly.example.utilities.slack.send_alert(
            {"message": json.dumps(safe_diagnostics(error))}
        )
        return {"notified": True}
    except Exception:
        # Error handlers must not recursively trigger themselves on alert failure.
        print("error_notification_failed")
        return {"notified": False}
