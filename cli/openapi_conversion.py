"""Convert Swagger 2 transport metadata to OpenAPI 3 without changing schemas."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import unquote

METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
SCHEMA_KEYS = {
    "type",
    "format",
    "items",
    "default",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "enum",
    "multipleOf",
}


def swagger_to_openapi(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("swagger") != "2.0":
        return copy.deepcopy(source)
    source = copy.deepcopy(source)
    consumes = source.get("consumes") or ["application/json"]
    produces = source.get("produces") or ["application/json"]

    def schema(value, mapping=False):
        if isinstance(value, list):
            return [schema(child) for child in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, child in value.items():
            if not mapping and key in {
                "example",
                "examples",
                "default",
                "enum",
                "const",
            }:
                result[key] = child
            elif key == "$ref" and isinstance(child, str):
                for old, new in (
                    ("#/definitions/", "#/components/schemas/"),
                    ("#/parameters/", "#/components/parameters/"),
                    ("#/responses/", "#/components/responses/"),
                ):
                    if child.startswith(old):
                        child = new + child[len(old) :]
                        break
                result[key] = child
            elif key == "x-nullable":
                result["nullable"] = child
            elif not mapping and key == "discriminator" and isinstance(child, str):
                result["discriminator"] = {"propertyName": child}
            else:
                result[key] = schema(
                    child, not mapping and key in {"properties", "patternProperties"}
                )
        if result.get("type") == "file":
            result.update(type="string", format="binary")
        return result

    def resolve(value, group):
        prefix = f"#/{group}/"
        ref = value.get("$ref", "")
        if not ref.startswith(prefix):
            return value
        name = unquote(ref[len(prefix) :]).replace("~1", "/").replace("~0", "~")
        if name not in source.get(group, {}):
            raise ValueError(f"Unresolved Swagger {group} reference")
        return copy.deepcopy(source[group][name])

    def parameter(value):
        if "$ref" in value:
            return schema(value)
        result = {
            key: schema(child)
            for key, child in value.items()
            if key not in SCHEMA_KEYS | {"collectionFormat"}
        }
        result["schema"] = schema(
            {key: child for key, child in value.items() if key in SCHEMA_KEYS}
        )
        if value.get("type") == "array":
            collection = value.get("collectionFormat", "csv")
            location = value.get("in")
            if location == "query":
                styles = {
                    "csv": ("form", False),
                    "multi": ("form", True),
                    "ssv": ("spaceDelimited", False),
                    "pipes": ("pipeDelimited", False),
                }
                if collection not in styles:
                    raise ValueError(
                        f"Unsupported Swagger query collectionFormat: {collection}"
                    )
                result["style"], result["explode"] = styles[collection]
            elif collection == "csv":
                result.update(style="simple", explode=False)
            else:
                raise ValueError(
                    "Swagger path/header arrays require csv collectionFormat for OpenAPI 3 conversion"
                )
        return result

    def request_body(parameters, media):
        bodies = [item for item in parameters if item.get("in") == "body"]
        forms = [item for item in parameters if item.get("in") == "formData"]
        if len(bodies) > 1 or bodies and forms:
            raise ValueError(
                "Swagger operation has conflicting body/formData parameters"
            )
        if bodies:
            item = bodies[0]
            return {
                **{
                    key: item[key] for key in ("description", "required") if key in item
                },
                "content": {
                    mime: {"schema": schema(item.get("schema", {}))} for mime in media
                },
            }
        if not forms:
            return None
        form_schema = {
            "type": "object",
            "properties": {
                item["name"]: schema(
                    {key: child for key, child in item.items() if key in SCHEMA_KEYS}
                )
                for item in forms
            },
        }
        required = [item["name"] for item in forms if item.get("required")]
        if required:
            form_schema["required"] = required
        form_media = [
            mime
            for mime in media
            if mime in {"multipart/form-data", "application/x-www-form-urlencoded"}
        ]
        if not form_media:
            form_media = [
                (
                    "multipart/form-data"
                    if any(item.get("type") == "file" for item in forms)
                    else "application/x-www-form-urlencoded"
                )
            ]
        encoding = {}
        for item in forms:
            if item.get("type") == "array":
                collection = item.get("collectionFormat", "csv")
                if collection not in {"csv", "multi", "ssv", "pipes"}:
                    raise ValueError(
                        f"Unsupported Swagger form collectionFormat: {collection}"
                    )
                encoding[item["name"]] = {
                    "style": {"ssv": "spaceDelimited", "pipes": "pipeDelimited"}.get(
                        collection, "form"
                    ),
                    "explode": collection == "multi",
                }
        content = {
            mime: {
                "schema": form_schema,
                **({"encoding": encoding} if encoding else {}),
            }
            for mime in form_media
        }
        return {"content": content, **({"required": True} if required else {})}

    def response(value, media):
        value = resolve(value, "responses")
        if "$ref" in value:
            return schema(value)
        result = {
            key: child
            for key, child in value.items()
            if key not in {"schema", "examples", "headers"}
        }
        if "headers" in value:
            result["headers"] = {
                name: {
                    **{
                        key: child
                        for key, child in header.items()
                        if key not in SCHEMA_KEYS | {"collectionFormat"}
                    },
                    "schema": schema(
                        {
                            key: child
                            for key, child in header.items()
                            if key in SCHEMA_KEYS
                        }
                    ),
                }
                for name, header in value["headers"].items()
            }
        if "schema" in value or "examples" in value:
            result["content"] = {
                mime: {
                    **(
                        {"schema": schema(value["schema"])} if "schema" in value else {}
                    ),
                    **(
                        {"example": value["examples"][mime]}
                        if mime in value.get("examples", {})
                        else {}
                    ),
                }
                for mime in dict.fromkeys([*media, *value.get("examples", {})])
            }
        return result

    def servers(schemes):
        if source.get("host"):
            return [
                {"url": f"{scheme}://{source['host']}{source.get('basePath', '')}"}
                for scheme in schemes or ["https"]
            ]
        return [{"url": source.get("basePath") or "/"}]

    document = {
        key: child
        for key, child in source.items()
        if key
        not in {
            "swagger",
            "host",
            "basePath",
            "schemes",
            "consumes",
            "produces",
            "definitions",
            "parameters",
            "responses",
            "securityDefinitions",
            "paths",
        }
    }
    document.update(openapi="3.0.3", servers=servers(source.get("schemes")), paths={})
    for route, path_item in source.get("paths", {}).items():
        inherited = [
            resolve(item, "parameters") for item in path_item.get("parameters", [])
        ]
        converted = {
            key: child
            for key, child in path_item.items()
            if key not in METHODS | {"parameters"}
        }
        for method, operation in path_item.items():
            if method not in METHODS:
                continue
            local = [
                resolve(item, "parameters") for item in operation.get("parameters", [])
            ]
            # Operation-level parameters override inherited parameters with the same identity.
            combined = {
                (item.get("in"), item.get("name")): item
                for item in [*inherited, *local]
            }
            params = list(combined.values())
            result = {
                key: child
                for key, child in operation.items()
                if key
                not in {"consumes", "produces", "schemes", "parameters", "responses"}
            }
            ordinary = [
                parameter(item)
                for item in params
                if item.get("in") not in {"body", "formData"}
            ]
            if ordinary:
                result["parameters"] = ordinary
            body = request_body(params, operation.get("consumes") or consumes)
            if body is not None:
                result["requestBody"] = body
            result["responses"] = {
                str(status): response(value, operation.get("produces") or produces)
                for status, value in operation.get("responses", {}).items()
            }
            if "schemes" in operation:
                result["servers"] = servers(operation["schemes"])
            converted[method] = result
        document["paths"][route] = converted
    components = {
        "schemas": {
            str(name): schema(value)
            for name, value in source.get("definitions", {}).items()
        }
    }
    components["parameters"] = {
        name: parameter(value)
        for name, value in source.get("parameters", {}).items()
        if value.get("in") not in {"body", "formData"}
    }
    components["responses"] = {
        name: response(value, produces)
        for name, value in source.get("responses", {}).items()
    }
    security = {}
    for name, value in source.get("securityDefinitions", {}).items():
        if value["type"] == "basic":
            security[name] = {"type": "http", "scheme": "basic"}
        elif value["type"] == "oauth2":
            kind = {
                "implicit": "implicit",
                "password": "password",
                "application": "clientCredentials",
                "accessCode": "authorizationCode",
            }.get(value.get("flow"))
            if kind is None:
                raise ValueError("Unsupported Swagger OAuth2 flow")
            security[name] = {
                "type": "oauth2",
                "flows": {
                    kind: {
                        key: value[key]
                        for key in ("authorizationUrl", "tokenUrl", "scopes")
                        if key in value
                    }
                },
            }
        else:
            security[name] = copy.deepcopy(value)
        if "description" in value:
            security[name]["description"] = value["description"]
    if security:
        components["securitySchemes"] = security
    document["components"] = components
    return document
