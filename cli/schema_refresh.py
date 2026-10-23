"""Refresh Flow validators from OpenAPI using Python only."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

# SDK initialization installs the TLS context before requests caches one.
from cli import flow

import requests
import yaml

TARGETS = {
    "CreateSchemaDto": ("/schemas", "post"),
    "UpdateSchemaDto": ("/schemas/{id}", "patch"),
    "CreateWebhookHandleDto": ("/webhooks", "post"),
    "UpdateWebhookHandleDto": ("/webhooks/{id}", "patch"),
    "CreateTriggerDto": ("/triggers", "post"),
    "GraphQLSubscriptionCreateDto": ("/subscriptions/graphql", "post"),
    "GraphQLSubscriptionUpdateDto": ("/subscriptions/graphql/{id}", "patch"),
    "CreateVariableDto": ("/variables", "post"),
    "UpdateVariableDto": ("/variables/{id}", "patch"),
    "CreateTableDto": ("/tables", "post"),
    "UpdateTableDto": ("/tables/{id}", "patch"),
    "CreateJobDto": ("/jobs", "post"),
    "UpdateJobDto": ("/jobs/{id}", "patch"),
    "CreateServerCustomFunctionDto": ("/functions/server", "post"),
    "CreateClientCustomFunctionDto": ("/functions/client", "post"),
    "CreateApiFunctionDto": ("/functions/api", "post"),
    "CreateAiFunctionV2Dto": ("/functions/ai", "post"),
    "UpdateAiFunctionV2Dto": ("/functions/ai/{id}", "patch"),
}
OPTIONAL_TARGETS = {"CreateAiFunctionV2Dto", "UpdateAiFunctionV2Dto"}


class SchemaRefreshError(ValueError):
    pass


def read_spec(source: str) -> dict[str, Any]:
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        if parsed.username or parsed.password:
            raise SchemaRefreshError("OpenAPI source must not contain credentials")
        # Public schema retrieval must not inherit API headers or netrc credentials.
        with requests.Session() as session:
            session.trust_env = False
            try:
                response = session.get(source, timeout=30, allow_redirects=False)
            except requests.RequestException:
                raise SchemaRefreshError("OpenAPI source request failed") from None
            if response.status_code != 200:
                raise SchemaRefreshError(
                    f"OpenAPI source returned HTTP {response.status_code}"
                )
            text = response.text
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise SchemaRefreshError("Invalid OpenAPI JSON/YAML") from None
    if not isinstance(document, dict) or not str(
        document.get("openapi", "")
    ).startswith("3."):
        raise SchemaRefreshError("Expected an OpenAPI 3.x document")
    return document


def pointer(document: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise SchemaRefreshError("Schema references must be internal JSON pointers")
    value: Any = document
    try:
        for part in unquote(reference[2:]).split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError):
        raise SchemaRefreshError("Unresolved OpenAPI schema reference") from None
    return value


def normalize(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize(item) for key, item in value.items()}
    pattern = result.get("pattern")
    if isinstance(pattern, str):
        match = re.fullmatch(r"/(.+)/[a-z]*", pattern, re.I)
        if match:
            result["pattern"] = match[1]
    if result.get("nullable") and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


def schema_document(schema: Any, spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        raise SchemaRefreshError("Expected a nonempty request-body schema")
    result = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        **copy.deepcopy(schema),
    }
    # Carry only referenced components, including recursive schemas, rather than
    # embedding the complete platform API in every validator.
    visited: set[str] = set()

    def include(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                include(item)
        elif isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference not in visited:
                visited.add(reference)
                target = copy.deepcopy(pointer(spec, reference))
                destination = result
                parts = [
                    part.replace("~1", "/").replace("~0", "~")
                    for part in unquote(reference[2:]).split("/")
                ]
                for part in parts[:-1]:
                    destination = destination.setdefault(part, {})
                destination[parts[-1]] = target
                include(target)
            for key, child in value.items():
                if key != "$ref":
                    include(child)

    include(schema)
    return normalize(result)


def patch_platform_quirks(name: str, document: dict[str, Any]) -> None:
    schemas = document.get("components", {}).get("schemas", {})
    root = pointer(document, document["$ref"]) if "$ref" in document else document
    targets = [root, schemas.get(name, {})]
    if name in {"CreateTableDto", "UpdateTableDto"}:
        properties = schemas.get("TableColumnDto", {}).get("properties", {})
        if "default" in properties:
            properties["default"] = True
    for target in targets:
        properties = target.get("properties", {})
        if (
            name in {"CreateClientCustomFunctionDto", "CreateServerCustomFunctionDto"}
            and "required" in target
        ):
            # CreateCustomFunctionDto marks these fields @IsOptional. Python
            # sends source directly and does not have TS transpilation metadata.
            target["required"] = [
                item
                for item in target["required"]
                if item
                not in {"sourceCode", "internalDependencies", "externalDependencies"}
            ]
        if name in {"CreateVariableDto", "UpdateVariableDto"} and "value" in properties:
            properties["value"] = True
        if (
            name in {"CreateWebhookHandleDto", "UpdateWebhookHandleDto"}
            and "required" in target
        ):
            target["required"] = [
                item for item in target["required"] if item != "templateBody"
            ]
        if name in OPTIONAL_TARGETS and properties:
            properties.update(
                type={"type": "string"},
                apiType={"type": "string"},
                returnType={"type": ["string", "null"]},
                returnTypeSchema={"type": ["object", "null"]},
                outputSchema={"type": ["object", "null"]},
            )


def endpoint_schemas(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise SchemaRefreshError("OpenAPI paths must be an object")
    documents = {}
    for name, (path, method) in TARGETS.items():
        path_item = paths.get(path, {})
        if not isinstance(path_item, dict):
            raise SchemaRefreshError("OpenAPI path items must be objects")
        operation = path_item.get(method)
        if operation is None and name in OPTIONAL_TARGETS:
            print(f"Skipped {name}: endpoint absent; existing validator retained")
            continue
        body = operation.get("requestBody", {}) if isinstance(operation, dict) else {}
        if not isinstance(body, dict):
            raise SchemaRefreshError("OpenAPI request bodies must be objects")
        if "$ref" in body:
            body = pointer(spec, body["$ref"])
        content = body.get("content", {})
        media = content.get("application/json", {}) if isinstance(content, dict) else {}
        schema = media.get("schema") if isinstance(media, dict) else None
        if schema is None:
            raise SchemaRefreshError(
                f"Missing request schema for {method.upper()} {path}"
            )
        document = schema_document(schema, spec)
        patch_platform_quirks(name, document)
        check_schema(document)
        documents[f"{name}.schema.json"] = document
    return documents


def check_schema(document: dict[str, Any]) -> None:
    visited: set[str] = set()

    def check_references(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                check_references(child)
        elif isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference not in visited:
                visited.add(reference)
                target = pointer(document, reference)
                Draft7Validator.check_schema(target)
                check_references(target)
            for child in value.values():
                check_references(child)

    try:
        Draft7Validator.check_schema(document)
        # Draft-07 does not treat OpenAPI's `components` as a schema keyword.
        # Validate referenced definitions explicitly, including recursive ones.
        check_references(document)
    except SchemaError:
        raise SchemaRefreshError("OpenAPI contains an invalid JSON schema") from None


def design_schemas(directory: Path) -> dict[str, Any] | None:
    if not directory.is_dir():
        raise SchemaRefreshError("POLYAPI_DESIGNS_DIR must name a directory")
    specs = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}:
            try:
                document = read_spec(str(path))
            except SchemaRefreshError:
                continue
            specs.append(document)
    if len(specs) > 1:
        raise SchemaRefreshError(
            "Multiple OpenAPI specifications in designs directory; select a narrower directory"
        )
    if not specs:
        return None
    spec = specs[0]
    result = {}
    for name, schema in spec.get("components", {}).get("schemas", {}).items():
        result[name] = schema_document(schema, spec)
        check_schema(result[name])
    return result


def refresh(
    repo: Path, source: str | None = None, designs_dir: Path | None = None
) -> int:
    source = source or os.getenv("POLYAPI_OPENAPI_SPEC_URL")
    if not source:
        base_url = os.getenv("POLY_API_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise SchemaRefreshError(
                "Set POLY_API_BASE_URL, POLYAPI_OPENAPI_SPEC_URL, or pass --source"
            )
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SchemaRefreshError(
                "POLY_API_BASE_URL must be an HTTP(S) instance URL without "
                "credentials, query parameters, or a fragment"
            )
        source = f"{base_url}/swagger-json"
    documents = {
        repo / "cli/schemas" / name: value
        for name, value in endpoint_schemas(read_spec(source)).items()
    }
    configured_designs = designs_dir or os.getenv("POLYAPI_DESIGNS_DIR")
    if configured_designs:
        directory = Path(configured_designs)
        generated = design_schemas(
            directory if directory.is_absolute() else repo / directory
        )
        if generated is not None:
            documents[repo / "cli/generated/openapi-schemas.json"] = generated
    # Fetch, resolve and validate the complete input before replacing any file.
    for path, document in documents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False, encoding="utf-8"
        ) as output:
            temporary = Path(output.name)
            try:
                output.write(json.dumps(document, indent=2) + "\n")
                output.flush()
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    print(f"Refreshed {len(documents)} Python JSON schema files")
    return len(documents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="OpenAPI URL or local JSON/YAML file")
    parser.add_argument("--designs-dir", type=Path)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    flow.load_env(repo)
    refresh(repo, args.source, args.designs_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        message = (
            str(error)
            if isinstance(error, SchemaRefreshError)
            else type(error).__name__
        )
        raise SystemExit(f"Schema refresh failed: {message}")
