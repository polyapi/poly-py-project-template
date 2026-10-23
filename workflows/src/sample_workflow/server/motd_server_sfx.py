import re
from typing import Any, Dict

from polyapi import poly, polyCustom
from polyapi.typedefs import PolyServerFunction
from typing_extensions import TypedDict

polyConfig: PolyServerFunction = {
    "name": "motd_server",
    "context": "example.sample_workflow",
    "description": "Greet a caller using the trained external APIs and shared error handler.",
    "visibility": "ENVIRONMENT",
    "logs_enabled": True,
}


class MotdResponse(TypedDict):
    statusCode: int
    message: str


async def motd_server(
    event: Dict[str, Any], headers: Dict[str, str], params: Dict[str, str]
) -> MotdResponse:
    """Webhook and job entrypoint. Clients are invoked through the generated SDK."""
    name = event.get("name") if isinstance(event, dict) else None
    mood = event.get("mood") if isinstance(event, dict) else None
    if not isinstance(name, str) or not name.strip() or mood not in ("funny", "wise"):
        polyCustom.responseStatusCode = 400
        return {"statusCode": 400, "message": "Provide a name and mood: funny or wise."}
    try:
        content = await poly.example.sample_workflow.motd_client(mood)
    except Exception as error:
        # Only our bounded client errors may cross the public response boundary.
        detail = str(error)
        if not re.fullmatch(
            r"getRandom(?:Joke|Advice) (?:request failed|failed with status (?:[0-9]{3}|unknown))"
            r"|Invalid advice API configuration|Secret injection requires normal API execution",
            detail,
        ):
            detail = "Message of the day is unavailable."
        handled = await poly.example.sample_workflow.motd_error_handler_async(
            {
                "message": detail,
                "functionName": "motd_client",
                "functionContext": "example.sample_workflow",
            }
        )
        polyCustom.responseStatusCode = 500
        return {"statusCode": 500, "message": handled["message"]}
    polyCustom.responseStatusCode = 200
    return {"statusCode": 200, "message": f"Hi {name.strip()}! {content}"}
