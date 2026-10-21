"""Template utility commands using Flow's authenticated target and write safeguards."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from cli import flow

ENDPOINTS = {
    "api-functions": "/functions/api",
    "client-functions": "/functions/client",
    "server-functions": "/functions/server",
    "ai-functions": "/functions/ai",
    "schemas": "/schemas",
    "variables": "/variables",
    "webhooks": "/webhooks",
    "triggers": "/triggers",
    "jobs": "/jobs",
    "tables": "/tables",
    "subscriptions": "/subscriptions/graphql",
}
MODEL_TYPES = {
    "schemas": ("/schemas", "CreateSchemaDto"),
    "functions": ("/functions/api", "CreateApiFunctionDto"),
    "webhooks": ("/webhooks", "CreateWebhookHandleDto"),
}


def enrich_model(
    client: flow.PolyApiClient,
    model: Any,
    config: flow.DeployConfig,
    context: str,
) -> dict[str, list[dict[str, Any]]]:
    """Use the same platform description generation as TS model generate."""
    if not isinstance(model, dict):
        raise flow.DeployError("Expected a translated Poly model")
    model = copy.deepcopy(model)
    # Validate the translated contract before sending it to the AI service. An
    # absent operationId is allowed here because enrichment can supply its name.
    preview = copy.deepcopy(model)
    for kind in ("functions", "webhooks"):
        for index, item in enumerate(
            preview.get(kind, []) if isinstance(preview.get(kind, []), list) else []
        ):
            if isinstance(item, dict) and not item.get("name"):
                item["name"] = f"pendingEnrichment{index}"
    validate_model(preview, config)
    for kind, endpoint in (
        ("functions", "/functions/api/description-generation"),
        ("webhooks", "/webhooks/description-generation"),
    ):
        for index, resource in enumerate(model.get(kind, [])):
            payload = {
                key: resource[key]
                for key in ("name", "context", "description")
                if key in resource
            }
            fields = ("name", "description")
            if kind == "functions":
                payload.update(
                    arguments=resource.get("arguments", []), source=resource["source"]
                )
                fields += ("arguments",)
            else:
                payload["eventPayload"] = resource.get("eventPayloadTypeSchema")
            try:
                enriched = client.request("POST", endpoint, payload=payload)
                if not isinstance(enriched, dict) or not all(
                    key in enriched for key in fields
                ):
                    raise flow.DeployError("Invalid description generation response")
                candidate = {
                    **resource,
                    **{key: enriched[key] for key in fields},
                    "context": context,
                }
                flow.validate_endpoint_payload(MODEL_TYPES[kind][1], candidate)
                resource.update(candidate)
                if enriched.get("traceId"):
                    print(
                        f"WARNING: AI returned fallback metadata for {kind}[{index}]",
                        file=sys.stderr,
                    )
            except flow.DeployError:
                # TS also retains translated metadata when enrichment fails.
                # Provider errors can contain sensitive specification fragments.
                print(
                    f"WARNING: AI enrichment failed for {kind}[{index}]; retaining translated metadata",
                    file=sys.stderr,
                )
    return validate_model(model, config)


def rename_model(model: dict[str, Any], renames: list[list[str]]) -> dict[str, Any]:
    """Apply explicit whole-word renames to names and their model references."""
    text = json.dumps(model)
    for old, new in renames:
        if not re.fullmatch(r"[A-Za-z_]\w*", new, flags=re.ASCII) or not old:
            raise flow.DeployError(
                "Model renames require a nonempty old name and an identifier new name"
            )
        text = re.sub(r"\b" + re.escape(old) + r"\b", lambda _: new, text)
    return json.loads(text)


def load_specs(
    source: Path, context: str, namespace: str | tuple[str, ...]
) -> list[tuple[Path, str, str]]:
    namespaces = (namespace,) if isinstance(namespace, str) else namespace
    paths = sorted(source.rglob("*")) if source.is_dir() else [source]
    specs = []
    for path in paths:
        if path.suffix.lower() not in {".json", ".yaml", ".yml"} or path.name.endswith(
            ".poly.json"
        ):
            continue
        if not path.is_file():
            raise flow.DeployError(f"Specification does not exist: {path}")
        text = path.read_text(encoding="utf-8")
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            raise flow.DeployError(f"Invalid JSON/YAML specification: {path}") from None
        if not isinstance(document, dict) or not (
            str(document.get("openapi", "")).startswith("3.")
            or document.get("swagger") == "2.0"
        ):
            raise flow.DeployError(f"Expected OpenAPI 3.x or Swagger 2.0: {path}")
        if not isinstance(document.get("paths"), dict) or not document["paths"]:
            raise flow.DeployError(f"Specification has no paths: {path}")
        file_context = context.replace(
            "{file}", re.sub(r"[^A-Za-z0-9_]", "_", path.stem)
        )
        if not any(
            file_context == root or file_context.startswith(root + ".")
            for root in namespaces
        ):
            raise flow.DeployError("Training context must start with POLYAPI_NAMESPACE")
        if not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", file_context, flags=re.ASCII
        ):
            raise flow.DeployError("Training context must contain dotted identifiers")
        specs.append((path, file_context, text))
    if not specs:
        raise flow.DeployError("No OpenAPI JSON/YAML specifications found")
    return specs


def order_schemas(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {f"{item.get('context', '')}.{item['name']}": item for item in schemas}
    ordered: list[dict[str, Any]] = []
    active: set[str] = set()
    visited: set[str] = set()

    def references(value: Any):
        if isinstance(value, dict):
            ref = value.get("x-poly-ref")
            if isinstance(ref, dict) and not ref.get("publicNamespace"):
                yield ref.get("path")
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)

    def visit(key: str) -> None:
        if key in active:
            raise flow.DeployError(
                "Model schemas have a dependency cycle; no resources were trained"
            )
        if key in visited:
            return
        active.add(key)
        for ref in references(by_key[key].get("definition", {})):
            if ref in by_key:
                visit(ref)
        active.remove(key)
        visited.add(key)
        ordered.append(by_key[key])

    for key in by_key:
        visit(key)
    return ordered


def validate_model(
    model: Any, config: flow.DeployConfig
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(model, dict) or not any(kind in model for kind in MODEL_TYPES):
        raise flow.DeployError(
            "Expected a Poly model with functions, schemas, or webhooks"
        )
    result = {}
    for kind, (_, schema) in MODEL_TYPES.items():
        items = model.get(kind, [])
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise flow.DeployError(f"Invalid model {kind}")
        seen = set()
        for item in items:
            flow.require_owned_context(item.get("context"), config)
            flow.validate_endpoint_payload(schema, item)
            identity = (item.get("context"), item.get("name"))
            if identity in seen:
                raise flow.DeployError(f"Duplicate model identity in {kind}")
            seen.add(identity)
        result[kind] = items
    result["schemas"] = order_schemas(result["schemas"])
    return result


def train_model(
    client: flow.PolyApiClient | None,
    model: dict[str, list[dict[str, Any]]],
    *,
    dry_run: bool,
) -> None:
    for kind, (endpoint, _) in MODEL_TYPES.items():
        for item in model[kind]:
            print(
                f"{'Would train' if dry_run else 'Training'} {kind}: {item['context']}.{item['name']}"
            )
            if not dry_run:
                if client is None:
                    raise flow.DeployError("Training requires a verified API client")
                client.request("PUT", endpoint, payload=item)


def train_openapi(
    client: flow.PolyApiClient | None,
    specs: list[tuple[Path, str, str]],
    config: flow.DeployConfig,
    *,
    dry_run: bool,
    generate_only: bool,
    overwrite_model: bool,
    disable_ai: bool = False,
    renames: list[list[str]] | None = None,
) -> None:
    rename_model({}, renames or [])
    # Validate all input/output paths before any remote operation.
    outputs: set[Path] = set()
    for path, _, _ in specs:
        output = path.with_suffix(".poly.json").resolve()
        if output in outputs:
            raise flow.DeployError(
                "Multiple specifications would overwrite the same model file"
            )
        outputs.add(output)
        if (
            not dry_run
            and path.with_suffix(".poly.json").exists()
            and not overwrite_model
        ):
            raise flow.DeployError(
                f"Model already exists for {path.name}; use train-model or --overwrite-model"
            )
    models = []
    for path, context, text in specs:
        stages = "translate/train" if disable_ai else "translate/enrich/train"
        print(
            f"{'Would ' + stages if dry_run else 'Translating'} {path} in {context} with hostUrl as an argument"
        )
        if dry_run:
            continue
        if client is None:
            raise flow.DeployError("Translation requires a verified API client")
        translated = client.request(
            "POST",
            "/specification-input/oas",
            params={"context": context, "hostUrlAsArgument": "hostUrl"},
            raw_body=text,
            headers={"Content-Type": "text/plain"},
        )
        model = (
            validate_model(translated, config)
            if disable_ai
            else enrich_model(client, translated, config, context)
        )
        model = validate_model(rename_model(model, renames or []), config)
        output = path.with_suffix(".poly.json")
        output.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
        print(f"Saved model: {output}")
        models.append(model)
    # Cross-file collisions must fail before the first resource upsert.
    combined = validate_model(
        {
            kind: [item for model in models for item in model[kind]]
            for kind in MODEL_TYPES
        },
        config,
    )
    if not generate_only:
        train_model(client, combined, dry_run=dry_run)


def deletion_plan(
    client: flow.PolyApiClient,
    kinds: list[str],
    context: str,
    *,
    include_children: bool,
) -> list[tuple[str, dict[str, Any]]]:
    def matches(value: Any) -> bool:
        return isinstance(value, str) and (
            value == context or include_children and value.startswith(context + ".")
        )

    functions = (
        flow.list_resources(client, "/functions/server")
        if set(kinds) & {"jobs", "triggers"}
        else []
    )
    by_id = {item.get("id"): item for item in functions}
    selected = []
    for kind in kinds:
        for item in flow.list_resources(client, ENDPOINTS[kind]):
            owned = matches(item.get("context"))
            if kind in {"jobs", "triggers"}:
                detail = client.request(
                    "GET", f"{ENDPOINTS[kind]}/{flow.resource_id(item)}"
                )
                if not isinstance(detail, dict):
                    raise flow.DeployError("Invalid job/trigger detail")
                identifiers = (
                    [entry.get("id") for entry in detail.get("functions", [])]
                    if kind == "jobs"
                    else [detail.get("serverFunctionId")]
                )
                owned = bool(identifiers) and all(
                    identifier in by_id and matches(by_id[identifier].get("context"))
                    for identifier in identifiers
                )
            if owned:
                selected.append((f"{ENDPOINTS[kind]}/{flow.resource_id(item)}", item))
    # Remove dependants before their functions or schemas.
    selected.sort(
        key=lambda entry: (
            not entry[0].startswith(("/triggers/", "/jobs/", "/subscriptions/")),
            entry[0],
        )
    )
    return selected


def delete_content(
    client: flow.PolyApiClient, plan: list[tuple[str, dict[str, Any]]], *, apply: bool
) -> None:
    for endpoint, item in plan:
        print(f"DELETE {endpoint} ({item.get('context', '')}.{item.get('name', '')})")
    print(f"{len(plan)} resource(s) selected")
    if not apply or not plan:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise flow.DeployError(
            "Deletion requires an interactive terminal and exact DELETE confirmation"
        )
    if input("Type DELETE to remove exactly these resources: ").strip() != "DELETE":
        raise flow.DeployError("Deletion cancelled")
    for endpoint, _ in plan:
        client.request("DELETE", endpoint)
        try:
            client.request("GET", endpoint, expected=(200,))
        except flow.ApiError as error:
            if error.status == 404:
                continue
            raise
        raise flow.DeployError(
            f"Deletion could not be verified for {endpoint}; inspect before retrying"
        )


def function_logs(
    client: flow.PolyApiClient,
    identifier: str,
    *,
    page: int,
    page_size: int,
    keyword: str | None,
    limit: int,
) -> Any:
    params: dict[str, Any] = {
        "logsPage": page,
        "logsPageSize": page_size,
        "limit": limit,
    }
    if keyword:
        params["keyword"] = keyword
    return client.request(
        "GET",
        f"/functions/server/{flow.resource_id({'id': identifier})}/logs",
        params=params,
    )


def format_logs(value: Any, *, raw: bool = False) -> str:
    if raw:
        return value if isinstance(value, str) else json.dumps(value, indent=2)

    def find_logs(item: Any) -> tuple[list[Any], dict[str, Any]] | None:
        if isinstance(item, list):
            return item, {}
        if isinstance(item, dict):
            for key in ("logs", "items", "data", "results"):
                if isinstance(item.get(key), list):
                    return item[key], {
                        name: nested for name, nested in item.items() if name != key
                    }
            for nested in item.values():
                found = find_logs(nested)
                if found is not None:
                    return found
        return None

    found = find_logs(value)
    if found is None:
        return value if isinstance(value, str) else json.dumps(value, indent=2)
    entries, metadata = found
    if not entries:
        return "No logs found."
    lines = [f"Logs ({len(entries)})"]
    if metadata:
        lines.extend(("Metadata", json.dumps(metadata, indent=2)))
    groups = (
        ("timestamp", "time", "createdAt", "created_at", "date"),
        ("level", "severity", "status"),
        ("message", "msg", "text", "body", "error"),
    )
    known = {key for group in groups for key in group}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            lines.append(entry if isinstance(entry, str) else json.dumps(entry))
            continue
        parts = []
        for group_index, keys in enumerate(groups):
            part = next(
                (
                    entry[key]
                    for key in keys
                    if key in entry and entry[key] is not None and entry[key] != ""
                ),
                None,
            )
            if part is not None:
                text = part if isinstance(part, str) else json.dumps(part)
                parts.append(text.upper() if group_index == 1 else text)
        lines.append(" | ".join(parts) if parts else f"Log {index + 1}")
        details = {key: nested for key, nested in entry.items() if key not in known}
        if details:
            lines[-1] += "\n" + "\n".join(
                "  " + line for line in json.dumps(details, indent=2).splitlines()
            )
    return "\n\n".join(lines)


def prompt_value(value: Any, label: str) -> Any:
    if value is not None:
        return value
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise flow.DeployError(f"{label} is required in non-interactive mode")
    try:
        answer = input(f"{label}: ").strip()
    except EOFError:
        raise flow.DeployError(f"{label} prompt cancelled") from None
    if not answer:
        raise flow.DeployError(f"{label} is required")
    return answer


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    training = sub.add_parser(
        "train-openapi",
        help="Translate and train OpenAPI JSON/YAML with hostUrl as an argument",
    )
    training.add_argument("source", type=Path, nargs="?")
    training.add_argument(
        "--context", help="Exact context, optionally containing {file}"
    )
    training.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate local inputs only; no provider calls",
    )
    training.add_argument(
        "--generate-only",
        action="store_true",
        help="Save translated models without training resources",
    )
    training.add_argument("--overwrite-model", action="store_true")
    training.add_argument("--skip-generate", action="store_true")
    training.add_argument(
        "--disable-ai",
        action="store_true",
        help="Keep specification names and descriptions without AI enrichment",
    )
    training.add_argument(
        "--rename",
        nargs=2,
        action="append",
        default=[],
        metavar=("FROM", "TO"),
        help="Rename a generated identifier and its model references",
    )
    model = sub.add_parser(
        "train-model", help="Train a previously translated Poly model"
    )
    model.add_argument("source", type=Path)
    model.add_argument("--dry-run", action="store_true")
    model.add_argument("--skip-generate", action="store_true")
    deletion = sub.add_parser(
        "delete-content",
        help="Preview exact-context cleanup; --apply requires DELETE confirmation",
    )
    deletion.add_argument("--context")
    deletion.add_argument("--types", help=",".join(ENDPOINTS))
    deletion.add_argument("--include-children", action="store_true")
    deletion.add_argument("--apply", action="store_true")
    logs = sub.add_parser("logs", help="Fetch one page of server-function logs")
    logs.add_argument("function_id", nargs="?")
    logs.add_argument("--page", "--logs-page", type=int, default=1)
    logs.add_argument("--page-size", "--logs-page-size", type=int, default=100)
    logs.add_argument("--limit", type=int)
    logs.add_argument("--keyword")
    logs.add_argument(
        "--raw",
        action="store_true",
        help="Print the original JSON response instead of formatted logs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    if args.command == "train-openapi":
        args.source = Path(prompt_value(args.source, "OpenAPI file or directory"))
        args.context = prompt_value(args.context, "PolyAPI context (supports {file})")
    elif args.command == "delete-content":
        args.context = prompt_value(args.context, "Context to delete")
        args.types = prompt_value(
            args.types, "Resource types (" + ",".join(ENDPOINTS) + ")"
        )
    elif args.command == "logs":
        args.function_id = prompt_value(args.function_id, "Function ID")
        if args.limit is None:
            args.limit = args.page_size
    repo = flow.find_repo_root()
    logger = flow.Logger(False)
    dry_run = (
        getattr(args, "dry_run", False)
        or args.command == "logs"
        or args.command == "delete-content"
        and not args.apply
    )
    cli_args = flow.CliArgs(dry_run, False, False, False, (), None, False)
    env_file = flow.load_env(repo)
    config = flow.build_config(repo, cli_args)
    specs, model, kinds = [], None, []
    if args.command == "train-openapi":
        specs = load_specs(
            args.source, args.context, config.namespaces or (config.namespace,)
        )
        if args.dry_run:
            # This preview does not need credentials or an authenticated provider.
            train_openapi(
                None,
                specs,
                config,
                dry_run=True,
                generate_only=False,
                overwrite_model=False,
                disable_ai=args.disable_ai,
                renames=args.rename,
            )
            return 0
    elif args.command == "train-model":
        model = validate_model(json.loads(args.source.read_text()), config)
        if args.dry_run:
            train_model(None, model, dry_run=True)
            return 0
    elif args.command == "delete-content":
        flow.require_owned_context(args.context, config)
        kinds = list(dict.fromkeys(part.strip() for part in args.types.split(",")))
        if not kinds or any(kind not in ENDPOINTS for kind in kinds):
            raise flow.DeployError("Choose valid resource types for deletion")
    elif min(args.page, args.page_size, args.limit) < 1:
        raise flow.DeployError("Log page, page size, and limit must be positive")

    target = flow.get_target_env_id(env_file)
    preflight = flow.build_deployment_preflight(repo, env_file)
    flow.print_deployment_visibility(logger, preflight, cli_args, target)
    flow.maybe_confirm_branch_env_file_match(repo, preflight, logger)
    flow.enforce_production_safeguards(
        preflight, cli_args, flow.get_execution_api_key()
    )
    api_key, base_url = flow.get_api_key_and_url()
    if not api_key or not base_url:
        raise flow.DeployError(
            "Set POLY_API_KEY and POLY_API_BASE_URL for the selected target"
        )
    client = flow.PolyApiClient(
        base_url,
        api_key,
        timeout_seconds=config.http_timeout_seconds,
        otp_manager=flow.OtpManager(config.otp_enabled),
    )
    flow.enforce_target_environment(client, target, env_file, logger)
    if args.command == "train-openapi":
        train_openapi(
            client,
            specs,
            config,
            dry_run=False,
            generate_only=args.generate_only,
            overwrite_model=args.overwrite_model,
            disable_ai=args.disable_ai,
            renames=args.rename,
        )
    elif args.command == "train-model":
        train_model(client, model, dry_run=False)
    elif args.command == "delete-content":
        delete_content(
            client,
            deletion_plan(
                client, kinds, args.context, include_children=args.include_children
            ),
            apply=args.apply,
        )
    else:
        print(
            format_logs(
                function_logs(
                    client,
                    args.function_id,
                    page=args.page,
                    page_size=args.page_size,
                    keyword=args.keyword,
                    limit=args.limit,
                ),
                raw=args.raw,
            )
        )
    if (
        args.command in {"train-openapi", "train-model"}
        and not args.skip_generate
        and not getattr(args, "generate_only", False)
    ):
        flow.run_poly_generate(config, logger, "after OpenAPI training", cli_args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (flow.DeployError, OSError, ValueError) as error:
        # Parser/IO exceptions may include source content; print only our bounded errors.
        message = (
            str(error) if isinstance(error, flow.DeployError) else type(error).__name__
        )
        print(f"ERROR: {message}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
