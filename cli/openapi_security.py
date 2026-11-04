"""Restore header API keys omitted by OpenAPI model translation."""

from __future__ import annotations

import copy
import json
import keyword
import re
from typing import Any

METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _resolve_scheme(spec: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    seen = set()
    while "$ref" in scheme:
        reference = scheme["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ValueError("Security scheme references must be local")
        if reference in seen:
            raise ValueError("Cyclic security scheme reference")
        seen.add(reference)
        value: Any = spec
        try:
            for part in reference[2:].split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            raise ValueError("Unresolved security scheme reference") from None
        if not isinstance(value, dict):
            raise ValueError("Invalid security scheme reference")
        scheme = value
    return scheme


def _header_key(scheme: dict[str, Any]) -> bool:
    return scheme.get("type") == "apiKey" and scheme.get("in") == "header"


def _native_matches(function: dict[str, Any], scheme: dict[str, Any]) -> bool:
    auth = function.get("source", {}).get("auth", {})
    auth_type = auth.get("type")
    if scheme.get("type") == "apiKey":
        entries = {entry["key"]: entry.get("value") for entry in auth.get("apikey", [])}
        return (
            auth_type == "apikey"
            and entries.get("in") == scheme.get("in")
            and str(entries.get("key", "")).lower() == scheme.get("name", "").lower()
        )
    if scheme.get("type") == "http":
        return auth_type == scheme.get("scheme", "").lower()
    if scheme.get("type") in {"oauth2", "openIdConnect"}:
        return auth_type in {"bearer", "oauth2"}
    return scheme.get("type") == "basic" and auth_type == "basic"


def _has_header(function: dict[str, Any], scheme: dict[str, Any]) -> bool:
    return _native_matches(function, scheme) or any(
        header.get("key", header.get("name", "")).lower() == scheme["name"].lower()
        and not header.get("disabled", False)
        and bool(header.get("value"))
        for header in function.get("source", {}).get("headers", [])
    )


def _path(value: str, *, generated: bool = False) -> str:
    if generated:
        value = value.split("?", 1)[0]
        value = re.sub(r"^\{\{hostUrl\}\}", "", value)
        value = re.sub(r"^https?://[^/]+", "", value)
        value = re.sub(r"\{\{[^{}]+\}\}", "{}", value)
        return re.sub(r"(?: |%20)+$", "", value)
    return re.sub(r"\{[^{}]+\}", "{}", value.rstrip(" "))


def _requirements(
    function: dict[str, Any], spec: dict[str, Any]
) -> list[dict[str, Any]] | None:
    source = function["source"]
    path = _path(source["url"], generated=True)
    matches = []
    for route, item in spec.get("paths", {}).items():
        operation = item.get(source["method"].lower())
        if source["method"].lower() not in METHODS or operation is None:
            continue
        candidate = _path(route)
        if path.endswith(candidate):
            security = operation.get("security", spec.get("security"))
            matches.append((len(candidate), security))
    if not matches:
        raise ValueError("Cannot match generated function to an OpenAPI operation")
    longest = max(length for length, _ in matches)
    selected = [security for length, security in matches if length == longest]
    if len({json.dumps(value, sort_keys=True) for value in selected}) != 1:
        raise ValueError("Ambiguous OpenAPI operation security")
    return selected[0]


def _argument_name(header: str) -> str:
    words = re.findall(r"[a-z0-9]+", header.lower())
    name = words[0] + "".join(word.title() for word in words[1:]) if words else "apiKey"
    if (
        name[0].isdigit()
        or keyword.iskeyword(name)
        or name
        in {
            "default",
            "new",
            "delete",
            "function",
            "switch",
            "case",
            "var",
            "let",
            "const",
            "catch",
            "debugger",
            "do",
            "this",
            "eval",
            "arguments",
            "execute",
            "dict",
            "constructor",
            "prototype",
            "enum",
            "export",
            "extends",
            "false",
            "true",
            "null",
            "super",
            "throw",
            "typeof",
            "void",
            "instanceof",
            "implements",
            "interface",
            "package",
            "private",
            "protected",
            "public",
            "static",
        }
    ):
        name = "header" + name[0].upper() + name[1:]
    return name


def _restore_header(function: dict[str, Any], scheme: dict[str, Any]) -> None:
    if _has_header(function, scheme):
        return
    arguments = function.setdefault("arguments", [])
    source = function["source"]
    base = _argument_name(scheme["name"])
    source_text = json.dumps(source)
    unused = next(
        (
            argument
            for argument in arguments
            if argument["name"] == base
            and argument.get("type") == "string"
            and "{{" + base + "}}" not in source_text
        ),
        None,
    )
    if unused is not None:
        name = base
        unused.update(required=True, removeIfNotPresentOnExecute=False)
    else:
        names = {argument["name"] for argument in arguments}
        name, suffix = base, 2
        while name in names:
            name, suffix = base + str(suffix), suffix + 1
        arguments.append(
            {
                "name": name,
                "type": "string",
                "required": True,
                "removeIfNotPresentOnExecute": False,
                "description": f"API key for the {scheme['name']} request header.",
            }
        )
    # Generated Python/TypeScript signatures require required parameters first.
    arguments.sort(key=lambda argument: not argument.get("required", False))
    source["headers"] = [
        header
        for header in source.get("headers", [])
        if header.get("key", header.get("name", "")).lower() != scheme["name"].lower()
    ] + [{"key": scheme["name"], "value": "{{" + name + "}}"}]


def reconcile_security(model: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Preserve translated auth and restore only keys in one effective alternative."""
    schemes = {
        name: _resolve_scheme(spec, scheme)
        for name, scheme in spec.get("components", {})
        .get("securitySchemes", spec.get("securityDefinitions", {}))
        .items()
    }
    if not any(_header_key(scheme) for scheme in schemes.values()):
        return copy.deepcopy(model)
    result = copy.deepcopy(model)
    for function in result.get("functions", []):
        requirements = _requirements(function, spec)
        if requirements is None:
            continue
        alternatives = []
        for index, requirement in enumerate(requirements):
            try:
                required = [schemes[name] for name in requirement]
            except KeyError:
                raise ValueError("Undefined OpenAPI security scheme") from None
            represented = sum(
                _native_matches(function, scheme)
                or (_header_key(scheme) and _has_header(function, scheme))
                for scheme in required
            )
            alternatives.append(
                ((-represented, len(required) - represented, index), required)
            )
        required = (
            min(alternatives, key=lambda value: value[0])[1] if alternatives else []
        )
        if not required:
            function["source"]["auth"] = {"type": "noauth"}
        for scheme in required:
            if _header_key(scheme):
                _restore_header(function, scheme)
    return result
