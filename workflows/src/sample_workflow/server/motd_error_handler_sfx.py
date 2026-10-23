import re
import sys
from typing import Any, Dict

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


def motd_error_handler(error: Dict[str, Any]) -> ErrorResponse:
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
    print(message, file=sys.stderr)
    return {"message": message}
