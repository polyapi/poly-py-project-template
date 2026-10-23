import time
import uuid

from polyapi import poly, polyCustom, vari
from polyapi.typedefs import PolyClientFunction
from typing_extensions import TypedDict

polyConfig: PolyClientFunction = {
    "name": "send_alert",
    "context": "example.utilities.slack",
    "description": "Send a sanitized operational message to the configured Slack channel.",
    "visibility": "ENVIRONMENT",
}


class AlertArgs(TypedDict):
    message: str


async def send_alert(args: AlertArgs) -> bool:
    start = time.monotonic()
    correlation_id = f"slack_{uuid.uuid4().hex}"
    channel_id = await vari.example.configurations.slack.channel_id.get_async()
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise ValueError("Slack channel is not configured")
    await poly.example.utilities.log(
        "info",
        "send_alert",
        "start",
        {
            "correlationId": correlation_id,
            "channelId": channel_id,
        },
    )
    await poly.OOB.polyapi.internal.alerting.sendAlertToPoly_async(
        {
            "level": "error",
            "name": "PolyAPI workflow alert",
            "message": args["message"],
            "slack": {
                "type": "community",
                "channelId": channel_id,
                "levels": ["error"],
            },
            "executionId": polyCustom.executionId,
        }
    )
    await poly.example.utilities.log(
        "info",
        "send_alert",
        "success",
        {
            "correlationId": correlation_id,
            "durationMs": (time.monotonic() - start) * 1000,
        },
    )
    return True
