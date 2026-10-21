import json
from typing import Literal

from polyapi import poly, vari
from polyapi.config import get_direct_execute_config
from polyapi.typedefs import PolyClientFunction

polyConfig: PolyClientFunction = {
    "name": "motd_client",
    "context": "example.sample_workflow",
    "description": "Call the trained joke or advice API according to the requested mood.",
    "visibility": "ENVIRONMENT",
}


async def motd_client(mood: Literal["funny", "wise"]) -> str:
    if mood not in ("funny", "wise"):
        raise ValueError("Unsupported mood")
    if mood == "funny":
        if get_direct_execute_config():
            raise ValueError("Secret injection requires normal API execution")
        # Only the generated API boundary resolves this reference. Do not read,
        # stringify, copy, cache, or pass it through another client function.
        try:
            response = await poly.example.sample_workflow.getRandomJoke_async(
                vari.example.sample_workflow.joke_api_config.inject("hostUrl")
            )
        except Exception:
            raise RuntimeError("getRandomJoke request failed") from None
        data = response.get("data")
        status = response.get("status")
        if (
            not isinstance(status, int)
            or not 200 <= status < 300
            or not isinstance(data, dict)
            or not isinstance(data.get("setup"), str)
            or not data["setup"]
            or not isinstance(data.get("punchline"), str)
            or not data["punchline"]
        ):
            safe_status = status if type(status) is int else "unknown"
            raise RuntimeError(f"getRandomJoke failed with status {safe_status}")
        return f"{data['setup']} ... {data['punchline']}"

    try:
        # OBSCURED permits reads. The Python SDK returns JSON text for objects.
        config = json.loads(
            await vari.example.sample_workflow.advice_api_config.get_async()
        )
        if not isinstance(config, dict) or not isinstance(config.get("hostUrl"), str):
            raise ValueError()
        host_url = config["hostUrl"]
        if not host_url:
            raise ValueError()
    except Exception:
        raise ValueError("Invalid advice API configuration") from None
    try:
        response = await poly.example.sample_workflow.getRandomAdvice_async(host_url)
    except Exception:
        raise RuntimeError("getRandomAdvice request failed") from None
    data = response.get("data")
    status = response.get("status")
    advice = (
        data.get("slip", {}).get("advice")
        if isinstance(data, dict) and isinstance(data.get("slip"), dict)
        else None
    )
    if (
        not isinstance(status, int)
        or not 200 <= status < 300
        or not isinstance(advice, str)
        or not advice
    ):
        safe_status = status if type(status) is int else "unknown"
        raise RuntimeError(f"getRandomAdvice failed with status {safe_status}")
    return advice
