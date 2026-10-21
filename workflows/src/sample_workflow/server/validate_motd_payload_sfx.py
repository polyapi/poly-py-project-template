from typing import Any

from polyapi.typedefs import PolyServerFunction

polyConfig: PolyServerFunction = {
    "name": "validate_motd_payload",
    "context": "example.sample_workflow",
    "description": "Validate the message-of-the-day webhook payload.",
    "visibility": "ENVIRONMENT",
}


def validate_motd_payload(
    event: dict[str, Any], headers: dict[str, str], params: dict[str, str]
) -> bool:
    if not isinstance(event, dict):
        return False
    name = event.get("name")
    return (
        isinstance(name, str)
        and bool(name.strip())
        and event.get("mood") in ("funny", "wise")
    )
