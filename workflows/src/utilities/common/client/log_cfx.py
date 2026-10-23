import json
import sys
import time
from typing import Any, Dict

from polyapi import poly, vari
from polyapi.typedefs import PolyClientFunction

polyConfig: PolyClientFunction = {
    "name": "log",
    "context": "example.utilities",
    "description": "Filter and redact structured logs, forwarding error/fatal entries to Slack.",
    "visibility": "ENVIRONMENT",
}


LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "fatal": 50, "silent": 100}
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
_log_level = "debug"
_refreshed_at = float("-inf")


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


async def log(
    level: str,
    function_name: str,
    message: str,
    context: Dict[str, Any] = {},
) -> bool:
    # The SDK supports Dict, not Optional/union boundaries. Never mutate this default.
    global _log_level, _refreshed_at
    if level not in LEVELS or level == "silent":
        raise ValueError("Unsupported log level")
    now = time.monotonic()
    if now - _refreshed_at >= 60:
        _refreshed_at = now
        try:
            configured = await vari.example.configurations.logging.log_level.get_async()
            normalized = configured.strip().lower()
            if normalized in LEVELS:
                _log_level = normalized
        except Exception:
            _log_level = "debug"
    if LEVELS[level] < LEVELS[_log_level]:
        return False
    safe_context = sanitize(context) if context is not None else None
    prefix = f"[{level}][{function_name}] {message}"
    suffix = f" | context={json.dumps(safe_context)}" if context is not None else ""
    print(
        prefix + suffix,
        file=sys.stderr if level in {"warn", "error", "fatal"} else sys.stdout,
    )
    if level in {"error", "fatal"}:
        await poly.example.utilities.slack.send_alert(
            {
                "message": json.dumps(
                    {
                        "message": prefix,
                        "level": level,
                        "functionName": function_name,
                        "context": safe_context,
                    }
                )
            }
        )
    return True
