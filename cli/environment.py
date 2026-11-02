"""Resolve Flow connection overrides without duplicating the native SDK setup."""

from __future__ import annotations

from configparser import Error as ConfigError
import os
from typing import Mapping

from polyapi.config import get_api_key_and_url as get_saved_sdk_connection

CONNECTION_ALIASES = (
    ("BASE_URL", "POLY_API_BASE_URL", "POLYAPI_BASE_URL"),
    ("API_KEY", "POLY_API_KEY", "POLYAPI_API_KEY"),
)


class ConnectionError(ValueError):
    """Connection configuration could not be loaded safely."""


def normalize_connection_settings(
    source: Mapping[str, str | None],
) -> dict[str, str]:
    """Normalize each layer before merging, including explicit empty overrides."""
    result = {name: value for name, value in source.items() if value is not None}
    for names in CONNECTION_ALIASES:
        selected = next((name for name in names if source.get(name) is not None), None)
        if selected is not None:
            for name in names:
                result[name] = result[selected]
    return result


def get_api_key_and_url() -> tuple[str | None, str | None]:
    """Use explicit fields first and fill only missing fields from SDK setup."""
    explicit = normalize_connection_settings(os.environ)
    key, url = explicit.get("POLY_API_KEY"), explicit.get("POLY_API_BASE_URL")
    if key is None or url is None:
        # The SDK caches a pair. Hide process overrides while requesting its
        # saved defaults, then merge per field so a partial override still wins.
        previous = {
            name: os.environ.pop(name, None)
            for names in CONNECTION_ALIASES
            for name in names
        }
        try:
            saved_key, saved_url = get_saved_sdk_connection()
        except (OSError, ConfigError, ValueError):
            raise ConnectionError(
                "Could not load saved PolyAPI setup. Run `flow setup` or provide "
                "explicit API key and base URL overrides."
            ) from None
        finally:
            for name, value in previous.items():
                if value is not None:
                    os.environ[name] = value
        key = saved_key if key is None else key
        url = saved_url if url is None else url

    # Native SDK calls and child processes must use the same resolved pair as
    # the Flow HTTP client. This changes process memory only, never an env file.
    os.environ.update(
        normalize_connection_settings({"POLY_API_KEY": key, "POLY_API_BASE_URL": url})
    )
    return key, url
