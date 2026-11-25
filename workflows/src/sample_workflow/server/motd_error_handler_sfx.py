import re
from typing import Any, Dict

from polyapi import poly
from polyapi.typedefs import PolyServerFunction
from typing_extensions import TypedDict

polyConfig: PolyServerFunction = {
    "name": "motd_error_handler",
    "context": "example.sample_workflow",
    "description": "Normalize errors for both direct calls and the webhook error trigger.",
    "visibility": "ENVIRONMENT",
    "logs_enabled": True,
}


class ErrorResponse(TypedDict):
    message: str


async def motd_error_handler(error: Dict[str, Any]) -> ErrorResponse:
    source = error.get("functionName", "unknown function")
    if not isinstance(source, str) or not re.fullmatch(
        r"[A-Za-z0-9_. -]{1,80}", source
    ):
        source = "unknown function"
    detail = error.get("message", "")
    if not isinstance(detail, str) or not re.fullmatch(
        r"getRandom(?:Joke|Advice) (?:request failed|failed with status (?:[0-9]{3}|unknown))"
        r"|Invalid advice API configuration|Secret injection requires normal API execution",
        detail,
    ):
        detail = "Message of the day is unavailable."
    message = f"motd request failed in {source}: {detail}"
    # warn, not error: this function is also an error-handler trigger destination,
    # and the shared logger alerts on error and fatal. An alert raised from inside
    # an error handler re-enters the alert path, so the innermost catch that names
    # the failure owns the alert instead.
    await poly.example.utilities.log(
        "warn", "motd_error_handler", message, {"functionName": source}
    )
    return {"message": message}
