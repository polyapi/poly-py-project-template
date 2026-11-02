#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import getpass
import glob
import hashlib
import itertools
import keyword
import time
from urllib.parse import quote
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import traceback
import types
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, cast

from dotenv import dotenv_values
from jsonschema import Draft7Validator
from polyapi.deployables import DeployableRecord
from polyapi.parser import (
    _get_req_name_if_not_in_base,
    get_jsonschema_type,
    parse_function_code,
)
from polyapi.utils import get_auth_headers
import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.function_graph import (
    FunctionGraph,
    GraphError,
    extract_references,
    match_reference,
)
from cli.environment import (
    ConnectionError,
    get_api_key_and_url as resolve_api_connection,
    normalize_connection_settings,
)

SKIP_VALIDATION_ACKNOWLEDGEMENT = "I know what am doing"

ArtifactType = Literal[
    "functions",
    "variables",
    "webhooks",
    "jobs",
    "tables",
    "triggers",
    "schemas",
    "ai-functions",
    "canopy",
    "subscriptions",
]
FunctionType = Literal["client", "server"]
TypeSelectionToken = Literal[
    "functions",
    "client-functions",
    "server-functions",
    "variables",
    "webhooks",
    "jobs",
    "tables",
    "triggers",
    "schemas",
    "ai-functions",
    "canopy",
    "subscriptions",
]

DEFAULT_FUNCTION_DISCOVERY_GLOBS = (
    "workflows/src/**/deployment/*.sh",
    "workflows/src/**/artefacts/functions/*.json",
    "workflows/src/**/client/*.py",
    "workflows/src/**/server/*.py",
)
DEFAULT_VARIABLE_GLOBS = (
    "workflows/src/**/artefacts/vari/**/*.json",
    "workflows/src/**/artefacts/vari/**/*.jsonc",
)
DEFAULT_WEBHOOK_GLOBS = ("workflows/src/**/artefacts/webhooks/*.json",)
DEFAULT_JOB_GLOBS = ("workflows/src/**/artefacts/jobs/*.json",)
DEFAULT_TABLE_GLOBS = ("workflows/src/**/artefacts/tabi/*.json",)
DEFAULT_SCHEMA_GLOBS = (
    "workflows/src/**/artefacts/schemas/*.json",
    "workflows/src/**/artefacts/schemas/*.jsonc",
)
DEFAULT_TRIGGER_GLOBS = ("workflows/src/**/artefacts/triggers/*.json",)
MANDATORY_ORPHAN_EXCLUDED_CONTEXTS: tuple[str, ...] = ()
LOCAL_ORPHAN_ARTIFACT_SUBDIRS: dict[ArtifactType, str] = {
    "functions": "artefacts/functions",
    "variables": "artefacts/vari",
    "webhooks": "artefacts/webhooks",
    "jobs": "artefacts/jobs",
    "tables": "artefacts/tabi",
    "triggers": "artefacts/triggers",
    "schemas": "artefacts/schemas",
    "ai-functions": "artefacts/ai-functions",
    "canopy": "artefacts/canopy",
    "subscriptions": "artefacts/subscriptions",
}
DEFAULT_ENABLED_TYPES: tuple[ArtifactType, ...] = (
    "functions",
    "variables",
    "webhooks",
    "jobs",
    "tables",
    "triggers",
    "schemas",
    "ai-functions",
    "canopy",
    "subscriptions",
)
FUNCTION_TYPE_ALIASES: dict[str, FunctionType] = {
    "client-functions": "client",
    "server-functions": "server",
}
SUPPORTED_TYPE_SELECTIONS: tuple[TypeSelectionToken, ...] = (
    "functions",
    "client-functions",
    "server-functions",
    "variables",
    "webhooks",
    "jobs",
    "tables",
    "triggers",
    "schemas",
    "ai-functions",
    "canopy",
    "subscriptions",
)
# --- Context auto-detection helpers (mirrors finding_gen_context.py logic) ---
GENERATED_POLYAPI_NAMESPACES = (
    "poly",
    "vari",
    "tabi",
    "schemas",
    "webhook",
)
STATIC_POLYAPI_IMPORTS = (
    "polyCustom",
    "typedefs",
    "config",
    "exceptions",
    "execute",
)
KNOWN_POLYAPI_IMPORTS = frozenset(
    (*GENERATED_POLYAPI_NAMESPACES, *STATIC_POLYAPI_IMPORTS)
)
VARI_CONTEXT_METHODS = frozenset(
    (
        "get",
        "get_async",
        "inject",
        "inject_async",
        "update",
        "update_async",
        "onUpdate",
    )
)
TABI_CONTEXT_MEMBERS = frozenset(
    (
        "count",
        "select_many",
        "select_one",
        "insert_many",
        "insert_one",
        "upsert_many",
        "upsert_one",
        "update_many",
        "update_one",
        "delete_many",
        "delete_one",
        "table_id",
    )
)
_POLY_SCAN_PATTERN = re.compile(r"poly\.[a-zA-Z_][a-zA-Z0-9_.]*")
_POLY_ALIAS_ASSIGN_PATTERN = re.compile(
    r"(?m)^\s*(?P<alias>[a-zA-Z_][a-zA-Z0-9_]*)\s*(?::[^=\n]+)?=\s*"
    r"(?:(?:cast)\([^,\n]+,\s*poly\s*\)|poly)"
    r"(?:\.(?P<path>[a-zA-Z_][a-zA-Z0-9_.]*))?"
    r"(?![a-zA-Z0-9_.]|\s*\()"
)
_DOCSTRING_PATTERN = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_COMMENT_PATTERN = re.compile(r"#.*?$", re.MULTILINE)


def _strip_non_code(source: str) -> str:
    source = _DOCSTRING_PATTERN.sub("", source)
    source = _COMMENT_PATTERN.sub("", source)
    return source


def _expand_namespace_aliases(
    source: str,
    namespace: str,
    pattern: re.Pattern[str],
) -> str:
    aliases = {
        match.group("alias"): (
            f"{namespace}.{match.group('path')}" if match.group("path") else namespace
        )
        for match in pattern.finditer(source)
        if match.group("alias") not in KNOWN_POLYAPI_IMPORTS
    }
    expanded = source
    for alias, target in aliases.items():
        expanded = re.sub(
            rf"(?<![a-zA-Z0-9_.]){re.escape(alias)}\.",
            f"{target}.",
            expanded,
        )
    return expanded


def _expand_poly_aliases(source: str) -> str:
    return _expand_namespace_aliases(source, "poly", _POLY_ALIAS_ASSIGN_PATTERN)


def _is_schema_ref(fn: str) -> bool:
    return any(seg[0].isupper() and not seg.isupper() for seg in fn.split(".") if seg)


def _context_is_same_or_child(context: str | None, prefix: str) -> bool:
    return bool(context) and (context == prefix or context.startswith(f"{prefix}."))


def _context_has_excluded_descendant(
    context: str, excluded_contexts: tuple[str, ...]
) -> bool:
    return any(
        excluded == context or excluded.startswith(f"{context}.")
        for excluded in excluded_contexts
    )


def _owner_context_from_reference(thing: str) -> str:
    segments = [segment for segment in thing.split(".") if segment]
    if len(segments) <= 1:
        return thing
    return ".".join(segments[:-1])


def _dedupe_contexts(contexts: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    for context in contexts:
        if context and context not in deduped:
            deduped.append(context)
    return deduped


def compact_contexts_against_safe_parents(
    contexts: Iterable[str], excluded_contexts: tuple[str, ...]
) -> list[str]:
    deduped = _dedupe_contexts(contexts)
    if not excluded_contexts:
        return deduped

    promoted = promote_contexts_to_safe_ancestors(deduped, excluded_contexts)
    deduped = _dedupe_contexts(promoted)

    compacted: list[str] = []
    for context in deduped:
        covered_by_safe_parent = any(
            parent != context
            and _context_is_same_or_child(context, parent)
            and not _context_has_excluded_descendant(parent, excluded_contexts)
            for parent in deduped
        )
        if not covered_by_safe_parent:
            compacted.append(context)
    return compacted


def promote_contexts_to_safe_ancestors(
    contexts: list[str], excluded_contexts: tuple[str, ...]
) -> list[str]:
    promoted = list(contexts)
    while True:
        candidates: dict[str, list[str]] = {}
        for context in promoted:
            segments = context.split(".")
            for depth in range(2, len(segments)):
                candidate = ".".join(segments[:depth])
                if _context_has_excluded_descendant(candidate, excluded_contexts):
                    continue
                candidates.setdefault(candidate, []).append(context)

        best_candidate = ""
        best_children: list[str] = []
        best_savings = 0
        for candidate, children in candidates.items():
            unique_children = _dedupe_contexts(children)
            if len(unique_children) < 2:
                continue
            original_size = sum(len(child) for child in unique_children)
            savings = original_size - len(candidate)
            if savings > best_savings:
                best_candidate = candidate
                best_children = unique_children
                best_savings = savings

        if not best_candidate:
            return promoted

        next_promoted: list[str] = []
        inserted = False
        for context in promoted:
            if context in best_children:
                if not inserted:
                    next_promoted.append(best_candidate)
                    inserted = True
                continue
            next_promoted.append(context)
        promoted = _dedupe_contexts(next_promoted)


def _get_context_ns(thing: str, excluded_contexts: tuple[str, ...] = ()) -> str:
    if excluded_contexts:
        return _owner_context_from_reference(thing)

    segments = thing.split(".")
    if segments[0] == "OOB" and len(segments) > 1:
        namespace = f"OOB.{segments[1]}"
    else:
        namespace = segments[0]
    return namespace


def infer_generate_contexts(
    source_code: str, excluded_contexts: tuple[str, ...] = ()
) -> list[str]:
    dependencies = scan_function_dependencies("", source_code, excluded_contexts)
    return list(dependencies["contexts"])


def _attribute_chain(node: ast.Attribute) -> list[str] | None:
    segments: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        segments.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    segments.append(current.id)
    segments.reverse()
    return segments


@dataclass
class _PolyApiReferenceScan:
    references: dict[str, list[str]] = field(
        default_factory=lambda: {
            namespace: [] for namespace in GENERATED_POLYAPI_NAMESPACES
        }
    )
    bindings: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    package_aliases: set[str] = field(default_factory=set)
    unknown_imports: set[str] = field(default_factory=set)
    dynamic_vari_contexts: list[str] = field(default_factory=list)

    def add_reference(self, namespace: str, segments: Iterable[str]) -> None:
        reference = ".".join(segment for segment in segments if segment)
        if reference and reference not in self.references[namespace]:
            self.references[namespace].append(reference)

    def generated_namespace(self, module_name: str) -> str | None:
        if not module_name.startswith("polyapi."):
            return None
        namespace = module_name.split(".")[1]
        if namespace not in KNOWN_POLYAPI_IMPORTS:
            self.unknown_imports.add(module_name)
            return None
        if namespace not in GENERATED_POLYAPI_NAMESPACES:
            return None
        return namespace

    def register_root_import(self, alias: ast.alias) -> None:
        imported_name = alias.name
        if imported_name not in KNOWN_POLYAPI_IMPORTS:
            self.unknown_imports.add(f"polyapi.{imported_name}")
            return
        if imported_name in GENERATED_POLYAPI_NAMESPACES:
            self.bindings[alias.asname or imported_name] = (imported_name, ())

    def register_generated_from_import(
        self,
        module_name: str,
        namespace: str,
        alias: ast.alias,
    ) -> None:
        if alias.name == "*":
            self.unknown_imports.add(f"{module_name}.*")
            return
        prefix = tuple(module_name.split(".")[2:])
        imported_prefix = (*prefix, alias.name)
        self.bindings[alias.asname or alias.name] = (namespace, imported_prefix)
        if namespace in {"poly", "schemas", "webhook"}:
            self.add_reference(namespace, imported_prefix)

    def register_from_import(self, node: ast.ImportFrom) -> None:
        if node.level != 0:
            return
        module_name = node.module or ""
        if module_name == "polyapi":
            for alias in node.names:
                self.register_root_import(alias)
            return
        namespace = self.generated_namespace(module_name)
        if namespace is None:
            return
        for alias in node.names:
            self.register_generated_from_import(module_name, namespace, alias)

    def register_import_alias(self, alias: ast.alias) -> None:
        imported_name = alias.name
        if imported_name == "polyapi":
            self.package_aliases.add(alias.asname or "polyapi")
            return
        namespace = self.generated_namespace(imported_name)
        if namespace is None:
            return
        if alias.asname:
            prefix = tuple(imported_name.split(".")[2:])
            self.bindings[alias.asname] = (namespace, prefix)
            return
        self.package_aliases.add("polyapi")

    def register_import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.register_import_alias(alias)

    def resolve_attribute_chain(
        self,
        chain: list[str],
    ) -> tuple[str, tuple[str, ...]] | None:
        binding = self.bindings.get(chain[0])
        if binding is not None:
            namespace, prefix = binding
            return namespace, (*prefix, *chain[1:])
        if chain[0] not in self.package_aliases or len(chain) < 2:
            return None
        namespace = chain[1]
        if namespace in GENERATED_POLYAPI_NAMESPACES:
            return namespace, tuple(chain[2:])
        if namespace not in STATIC_POLYAPI_IMPORTS:
            self.unknown_imports.add(f"polyapi.{namespace}")
        return None

    def add_attribute_chain(self, chain: list[str]) -> None:
        resolved = self.resolve_attribute_chain(chain)
        if resolved is None:
            return
        namespace, segments = resolved
        self.add_reference(namespace, segments)

    def raise_for_unknown_imports(self) -> None:
        if not self.unknown_imports:
            return
        names = ", ".join(sorted(self.unknown_imports))
        raise DeployError(
            f"Unsupported PolyAPI import(s): {names}. Define context inference "
            "semantics in Flow before using a new PolyAPI namespace."
        )


class _PolyApiReferenceVisitor(ast.NodeVisitor):
    def __init__(self, scan: _PolyApiReferenceScan) -> None:
        self.scan = scan

    def visit_Attribute(self, node: ast.Attribute) -> None:
        chain = _attribute_chain(node)
        if chain is None:
            self.generic_visit(node)
            return
        self.scan.add_attribute_chain(chain)


def _parse_polyapi_source(source_code: str) -> ast.Module:
    try:
        return ast.parse(source_code)
    except SyntaxError as error:
        raise DeployError(
            "Unable to inspect PolyAPI imports because the function source is "
            f"not valid Python: line {error.lineno or 'unknown'}"
        ) from error


def _inspect_polyapi_source(
    source_code: str,
    table_exports: dict[str, str] | None = None,
    available_table_modules: Iterable[str] = (),
    *,
    variable_exports: dict[str, str] | None = None,
    available_variable_modules: Iterable[str] = (),
) -> _PolyApiReferenceScan:
    module = _parse_polyapi_source(source_code)
    scan = _PolyApiReferenceScan()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            scan.register_from_import(node)
        elif isinstance(node, ast.Import):
            scan.register_import(node)
    _PolyApiReferenceVisitor(scan).visit(module)
    scan.raise_for_unknown_imports()
    # Resources share lexical alias, tuple and helper-argument resolution.
    for namespace in ("tabi", "vari"):
        scan.references[namespace] = sorted(
            extract_references(
                source_code,
                namespace=namespace,
                known_exports=(
                    (table_exports or {})
                    if namespace == "tabi"
                    else (variable_exports or {})
                ),
                available_modules=(
                    available_table_modules
                    if namespace == "tabi"
                    else available_variable_modules
                ),
            )
        )
    scan.dynamic_vari_contexts = sorted(
        {
            reference.split("*")[0].rstrip(".")
            for reference in scan.references["vari"]
            if "*" in reference
        }
    )
    return scan


def inspect_polyapi_references(source_code: str) -> dict[str, list[str]]:
    """Validate PolyAPI imports and return generated namespace references."""
    scan = _inspect_polyapi_source(source_code)
    return scan.references


def context_from_generated_reference(
    namespace: str,
    reference: str,
    excluded_contexts: tuple[str, ...],
) -> str | None:
    segments = [segment for segment in reference.split(".") if segment]
    if namespace in {"vari", "tabi"}:
        if "*" in segments or len(segments) < 2:
            return None
        members = VARI_CONTEXT_METHODS if namespace == "vari" else TABI_CONTEXT_MEMBERS
        if segments[-1] in members:
            if len(segments) < 3:
                return None  # Root resource members are handled by the scope guard.
            resource_reference = ".".join(segments[:-1])
        else:
            # A new resource may not be in the live catalog until the resource
            # sync step, after the dynamic function plan is prepared.
            resource_reference = reference
        return _get_context_ns(resource_reference, excluded_contexts)
    if namespace in {"schemas", "webhook"}:
        if len(segments) < 2:
            return None
        return _get_context_ns(".".join(segments), excluded_contexts)
    return None


def add_poly_dependency(
    *,
    reference: str,
    func_name: str,
    calls: list[str],
    contexts: list[str],
    excluded_contexts: tuple[str, ...],
) -> None:
    # Client functions retain their declared name, including a literal _async.
    # Keep exact SDK paths when following bundled clients; server wrapper aliases
    # are resolved separately by FunctionGraph for deployment ordering.
    normalized = reference.removeprefix("poly.")
    if (
        normalized == func_name
        or _is_schema_ref(normalized)
        or context_is_orphan_excluded(normalized, excluded_contexts)
    ):
        return
    if normalized not in calls:
        calls.append(normalized)
    context = _get_context_ns(normalized, excluded_contexts)
    if context not in contexts:
        contexts.append(context)


def add_generated_context(
    *,
    namespace: str,
    reference: str,
    contexts: list[str],
    excluded_contexts: tuple[str, ...],
) -> None:
    context = context_from_generated_reference(
        namespace,
        reference,
        excluded_contexts,
    )
    if (
        context
        and not context_is_orphan_excluded(context, excluded_contexts)
        and context not in contexts
    ):
        contexts.append(context)


def add_dynamic_vari_context(
    *,
    context: str,
    contexts: list[str],
    excluded_contexts: tuple[str, ...],
) -> None:
    if not context:
        raise DeployError(
            "Cannot safely infer dynamic Vari access without a static context base"
        )
    if context_is_orphan_excluded(context, excluded_contexts):
        return
    if _context_has_excluded_descendant(context, excluded_contexts):
        raise DeployError(
            f"Cannot safely infer dynamic Vari access from broad context {context!r}: "
            "it contains an excluded descendant"
        )
    resolved_context = context if excluded_contexts else _get_context_ns(context)
    if resolved_context not in contexts:
        contexts.append(resolved_context)


def scan_function_dependencies(
    func_name: str,
    source_code: str,
    excluded_contexts: tuple[str, ...] = (),
    known_functions: dict[str, str] | None = None,
    table_exports: dict[str, str] | None = None,
    variable_exports: dict[str, str] | None = None,
    *,
    available_table_modules: Iterable[str] = (),
    available_variable_modules: Iterable[str] = (),
) -> dict[str, list[str]]:
    inspection = _inspect_polyapi_source(
        source_code,
        table_exports,
        available_table_modules,
        variable_exports=variable_exports,
        available_variable_modules=available_variable_modules,
    )
    namespace_references = inspection.references
    clean = _strip_non_code(source_code)
    clean = _expand_poly_aliases(clean)
    calls: list[str] = []
    contexts: list[str] = []
    # Use parsed references: quoted examples must never widen generation scope.
    scoped_variables = {
        reference.rsplit(".", 1)[0]
        for reference in namespace_references["vari"]
        if len(reference.split(".")) > 2
        and reference.split(".")[-1] in VARI_CONTEXT_METHODS
    }
    variable_dependencies = {
        reference: match_reference(reference, variable_exports or {})
        for reference in namespace_references["vari"]
    }
    root_variables = sorted(
        {
            reference.split(".")[0]
            for reference in namespace_references["vari"]
            if len(reference.split(".")) == 2
            and reference.split(".")[-1] in VARI_CONTEXT_METHODS
            and reference not in scoped_variables
            and not variable_dependencies[reference]
        }
        | {
            variable
            for variables in variable_dependencies.values()
            for variable in variables
            if "." not in variable
        }
    )
    # An alias to a scoped table named count can resemble a root count method.
    # Its longer table.member reference establishes the scoped table identity.
    scoped_tables = {
        reference.rsplit(".", 1)[0]
        for reference in namespace_references["tabi"]
        if len(reference.split(".")) > 2
        and reference.split(".")[-1] in TABI_CONTEXT_MEMBERS
    }
    table_dependencies = {
        reference: match_reference(reference, table_exports or {})
        for reference in namespace_references["tabi"]
    }
    root_tables = sorted(
        {
            reference.split(".")[0]
            for reference in namespace_references["tabi"]
            if len(reference.split(".")) == 2
            and reference.split(".")[-1] in TABI_CONTEXT_MEMBERS
            and reference not in scoped_tables
            and not table_dependencies[reference]
        }
        | {
            table
            for tables in table_dependencies.values()
            for table in tables
            if "." not in table
        }
    )

    for reference in _POLY_SCAN_PATTERN.findall(clean):
        add_poly_dependency(
            reference=reference,
            func_name=func_name,
            calls=calls,
            contexts=contexts,
            excluded_contexts=excluded_contexts,
        )

    for reference in namespace_references["poly"]:
        add_poly_dependency(
            reference=reference,
            func_name=func_name,
            calls=calls,
            contexts=contexts,
            excluded_contexts=excluded_contexts,
        )

    aliases = {key: key for key in (known_functions or {})}
    aliases.update(
        {
            f"{key}_async": key
            for key, kind in (known_functions or {}).items()
            if kind == "server"
        }
    )
    for reference in sorted(extract_references(source_code, known_exports=aliases)):
        matches = match_reference(reference, aliases)
        for resolved in sorted(
            matches or ({reference} if "*" not in reference else set())
        ):
            if resolved in aliases and "." not in resolved:
                # A known root callable has an empty context, even when its
                # name matches an excluded context. Keep it for the root guard.
                if resolved not in calls:
                    calls.append(resolved)
                continue
            add_poly_dependency(
                reference=resolved,
                func_name=func_name,
                calls=calls,
                contexts=contexts,
                excluded_contexts=excluded_contexts,
            )

    resource_dependencies = {"vari": variable_dependencies, "tabi": table_dependencies}
    for namespace in ("vari", "tabi", "schemas", "webhook"):
        for reference in namespace_references[namespace]:
            matches = resource_dependencies.get(namespace, {}).get(reference, set())
            if matches:
                for resource in sorted(matches):
                    if "." in resource:
                        context = _get_context_ns(resource, excluded_contexts)
                        if not context_is_orphan_excluded(context, excluded_contexts):
                            contexts.append(context)
                continue
            # A bare import/alias prefix must not add a broader context than its
            # resolved member when no catalog is available (e.g. offline checks).
            if namespace in {"tabi", "vari"} and any(
                other.startswith(f"{reference}.")
                for other in namespace_references[namespace]
            ):
                continue
            add_generated_context(
                namespace=namespace,
                reference=reference,
                contexts=contexts,
                excluded_contexts=excluded_contexts,
            )

    for context in inspection.dynamic_vari_contexts:
        add_dynamic_vari_context(
            context=context,
            contexts=contexts,
            excluded_contexts=excluded_contexts,
        )

    return {
        "calls": calls,
        "contexts": _dedupe_contexts(contexts),
        "root_variables": root_variables,
        "root_tables": root_tables,
    }


# ------------------------------------------------------------------------------

UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
FUNCTION_REF_PATTERN = re.compile(r"^\{\{(?P<context>.+)\.(?P<name>[^.]+)\}\}$")
ENV_TOKEN_PATTERN = re.compile(r"\{\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}\}")
ENV_ALERT_PATTERN = re.compile(r"\{\{(?P<name>[A-Z_][A-Z0-9_]*)\}\}")
WEBHOOK_TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{[^{}]+\}\}|\{[^{}]+\}")
FLOW_LOGO = r"""
__/\\\\\\\\\\\\\\\__/\\\___________________/\\\\\_______/\\\______________/\\\_
 _\/\\\///////////__\/\\\_________________/\\\///\\\____\/\\\_____________\/\\\_
  _\/\\\_____________\/\\\_______________/\\\/__\///\\\__\/\\\_____________\/\\\_
   _\/\\\\\\\\\\\_____\/\\\______________/\\\______\//\\\_\//\\\____/\\\____/\\\__
    _\/\\\///////______\/\\\_____________\/\\\_______\/\\\__\//\\\__/\\\\\__/\\\___
     _\/\\\_____________\/\\\_____________\//\\\______/\\\____\//\\\/\\\/\\\/\\\____
      _\/\\\_____________\/\\\______________\///\\\__/\\\_______\//\\\\\\//\\\\\_____
       _\/\\\_____________\/\\\\\\\\\\\\\\\____\///\\\\\/_________\//\\\__\//\\\______
        _\///______________\///////////////_______\/////____________\///____\///_______
""".strip("\n")
FLOW_LOGO_WIDTH = max(len(line) for line in FLOW_LOGO.splitlines())
# Each line of the Slant Relief logo can split a single letter into multiple slash groups.
# These indices map each visible group back to F, L, O, or W so every letter keeps one color.
FLOW_LOGO_CLUSTER_COLOR_SEQUENCE = (
    (0, 1, 2, 3, 3),
    (0, 1, 2, 3, 3),
    (0, 1, 2, 2, 3, 3),
    (0, 1, 2, 2, 3, 3, 3),
    (0, 1, 2, 2, 3, 3, 3),
    (0, 1, 2, 2, 3),
    (0, 1, 2, 2, 3),
    (0, 1, 2, 3, 3),
    (0, 1, 2, 3, 3),
)
POLYAPI_BRAND_COLORS = (
    (66, 133, 244),
    (234, 67, 53),
    (251, 188, 5),
    (52, 168, 83),
)
BLUE = POLYAPI_BRAND_COLORS[0]
RED = POLYAPI_BRAND_COLORS[1]
YELLOW = POLYAPI_BRAND_COLORS[2]
GREEN = POLYAPI_BRAND_COLORS[3]
WHITE = (255, 255, 255)
CYAN = (64, 196, 255)
GRAY = (150, 150, 150)
ROOT_HELP_DESCRIPTION = textwrap.dedent("""
    Flow is the PolyAPI deployment CLI for this repository.

    Use the `push` command to sync local artefacts to PolyAPI.
    Running `flow.py` without a command shows this help.
    """).strip()
ROOT_HELP_EPILOG = textwrap.dedent("""
    Examples:
      python cli/flow.py
      python cli/flow.py push --dry-run
      python cli/flow.py push --paths sample_workflow
      python cli/flow.py push --paths utilities/common
      python cli/flow.py push --adopt-orphans
      python cli/flow.py push --delete-orphans
      python cli/flow.py push --help
      python cli/flow.py train-openapi --help
      python cli/flow.py train-model --help
      python cli/flow.py delete-content --help

    Notes:
      - Functions are discovered from inline polyConfig in Python source; no peer JSON is required.
      - Scope shortcuts like `sample_workflow` and `utilities/common` resolve under `workflows/src/`.
    """).strip()
PUSH_HELP_DESCRIPTION = textwrap.dedent("""
    Deploy PolyAPI artefacts from local files with env-driven behavior.

    Deploy order:
      1. Variables
      2. Canopy
      3. Tables and schemas
      4. Client and server functions
      5. AI functions
      6. Webhooks and triggers
      7. Jobs
      8. GraphQL subscriptions

    Full syncs respect POLY_DEPLOY_PRIORITY_PATHS. In this repo that means
    `workflows/src/utilities` can be scanned before the rest of `workflows/src`.

    When you run interactively without --paths, the CLI asks whether to sync
    the full repo or a specific scope such as `sample_workflow` or
    `utilities/common`.
    """).strip()
PUSH_HELP_EPILOG = textwrap.dedent("""
    Examples:
      python cli/flow.py push --dry-run
      python cli/flow.py push --paths sample_workflow --types functions,jobs
      python cli/flow.py push --paths sample_workflow --types server-functions,jobs
      python cli/flow.py push --paths utilities/common
      python cli/flow.py push --paths workflows/src/sample_workflow --types functions,variables,jobs
      python cli/flow.py push --adopt-orphans
      python cli/flow.py push --delete-orphans
      python cli/flow.py push --show-config

    Supported --types values:
      functions, client-functions, server-functions, variables,
            webhooks, jobs, tables, triggers, schemas, ai-functions, canopy, subscriptions

    Notes:
      - Functions are discovered from inline polyConfig in Python source; no peer JSON is required.
      - {{ENV_NAME}} tokens in deployed JSON/JSONC files are substituted from the selected env file when defined.
      - Missing env-style placeholders trigger a preflight warning and confirmation prompt before sync.
      - Orphans are reported by default; pass --adopt-orphans to recreate supported remote artefacts locally or --delete-orphans to remove them.
      - If OTP_ENABLED=true, all writes send an interactively supplied x-otp.
      - Function refs like {{example.sample_workflow.motd_server}} resolve to Poly function IDs.
      - Existing GraphQL subscriptions require --force and an explicit recreation confirmation.
      - Existing secret/obscured variables are skipped by default unless enabled in the selected env file.
    """).strip()
DEFAULT_ENV_FILE = ".env"
DEFAULT_BRANCH_ENV_DIRECTORY = "env"
DEFAULT_GIT_PROD_BRANCH = "main"
DEFAULT_GIT_PROD_SKIP_BRANCHES = ("develop",)
DEFAULT_HTTP_TIMEOUT_SECONDS = 900
PRODUCTION_CONFIRMATION_VALUE = "PROD"

# Every env file must pin the environment it is allowed to deploy to, so
# a dev env file can never be used against production and vice versa.
TARGET_ENV_ID_VAR = "TARGET_ENV_ID"
# Reports the authenticated principal, including the environment the API key is
# bound to. Takes no parameters and returns a required `environmentId`.
WHOAMI_PATH = "/whoami"
ENV_ID_HINT = (
    "The ID is shown in the Poly UI, and is returned as `environmentId` by "
    f"`GET {WHOAMI_PATH}` for the API key you have configured"
)


class DeployError(RuntimeError):
    pass


class ApiError(DeployError):
    def __init__(
        self,
        method: str,
        path: str,
        status: int,
        *,
        secrecy_change_rejected: bool = False,
    ) -> None:
        super().__init__(f"{method} {path} failed with status {status}")
        self.status = status
        self.secrecy_change_rejected = secrecy_change_rejected


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    context: str
    function_type: FunctionType
    source_path: Path
    discovery_path: Path
    description: str | None = None
    description_explicit: bool = False
    visibility: str | None = None
    logs_enabled: bool | None = None
    generate_contexts: tuple[str, ...] = ()
    execution_api_key: str | None = None
    line_number: int | None = None

    @property
    def local_timestamp(self) -> float:
        discovery_mtime = self.discovery_path.stat().st_mtime
        source_mtime = self.source_path.stat().st_mtime
        return max(discovery_mtime, source_mtime)

    @property
    def identity(self) -> tuple[FunctionType, str, str]:
        return (self.function_type, self.context, self.name)


@dataclass(frozen=True)
class ArtifactFile:
    path: Path
    data: dict[str, Any]

    @property
    def local_timestamp(self) -> float:
        return self.path.stat().st_mtime


@dataclass(frozen=True)
class MissingEnvTokenRef:
    env_name: str
    path: Path
    line_number: int | None = None


@dataclass(frozen=True)
class ContextPathMapping:
    context_prefix: str
    root_path: Path
    target_filename: str | None = None


@dataclass(frozen=True)
class EnvLoadResult:
    display_path: str
    resolved_path: Path
    exists: bool
    from_envpath: bool


@dataclass(frozen=True)
class BranchEnvBinding:
    branch: str
    env_file: str | None = None


@dataclass(frozen=True)
class DeploymentPreflight:
    env_file: EnvLoadResult
    production_branch: str
    current_branch: str
    production_sensitive: bool
    current_branch_parent: str | None = None
    sensitive_reason: str | None = None
    safeguard_exemption_reason: str | None = None
    expected_env_branch: str | None = None
    expected_env_file: str | None = None


@dataclass(frozen=True)
class OrphanedResource:
    artifact_type: ArtifactType
    name: str
    context: str
    resource_id: str
    delete_path: str
    detail_path: str = ""
    function_type: FunctionType | None = None
    requires_otp: bool = False
    contexts: tuple[str, ...] = ()


@dataclass
class ArtifactStats:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: int = 0


@dataclass
class Summary:
    functions: ArtifactStats = field(default_factory=ArtifactStats)
    variables: ArtifactStats = field(default_factory=ArtifactStats)
    webhooks: ArtifactStats = field(default_factory=ArtifactStats)
    jobs: ArtifactStats = field(default_factory=ArtifactStats)
    tables: ArtifactStats = field(default_factory=ArtifactStats)
    triggers: ArtifactStats = field(default_factory=ArtifactStats)
    schemas: ArtifactStats = field(default_factory=ArtifactStats)
    ai_functions: ArtifactStats = field(default_factory=ArtifactStats)
    canopy: ArtifactStats = field(default_factory=ArtifactStats)
    subscriptions: ArtifactStats = field(default_factory=ArtifactStats)
    orphans: list[OrphanedResource] = field(default_factory=list)

    def get(self, artifact_type: ArtifactType) -> ArtifactStats:
        return getattr(self, artifact_type.replace("-", "_"))


@dataclass(frozen=True)
class DeployConfig:
    repo_root: Path
    enabled_types: tuple[ArtifactType, ...]
    skip_paths: tuple[Path, ...]
    priority_paths: tuple[Path, ...]
    function_discovery_globs: tuple[str, ...]
    variable_globs: tuple[str, ...]
    webhook_globs: tuple[str, ...]
    job_globs: tuple[str, ...]
    table_globs: tuple[str, ...]
    schema_globs: tuple[str, ...]
    trigger_globs: tuple[str, ...]
    use_mtime_guard: bool
    remote_timestamp_fields: tuple[str, ...]
    update_existing_secret_variables: bool
    recreate_variables_on_secrecy_change: bool
    generate_after_function_sync: bool
    generate_contexts: tuple[str, ...]
    generate_names: tuple[str, ...]
    generate_ids: tuple[str, ...]
    orphan_excluded_contexts: tuple[str, ...]
    initial_scope_prompt_enabled: bool
    initial_scope_state_file: Path
    namespace: str = "example"
    namespaces: tuple[str, ...] = ()
    http_timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS
    context_path_mappings: tuple[ContextPathMapping, ...] = ()
    otp_enabled: bool = False
    server_function_images: dict[str, str] = field(default_factory=dict)
    server_function_scripts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CliArgs:
    dry_run: bool
    force: bool
    verbose: bool
    delete_orphans: bool
    path_filters: tuple[Path, ...]
    enabled_types: tuple[ArtifactType, ...] | None
    show_config: bool
    adopt_orphans: bool = False
    function_types: tuple[FunctionType, ...] | None = None
    skip_generate: bool = False
    resume: bool = False
    build_schema: bool = False
    skip_invalid: bool = False
    skip_validation_acknowledged: bool = False
    invalid_paths: tuple[Path, ...] = ()
    dynamic: bool = False
    seeds: tuple[str, ...] = ()
    skip_unchanged_cfx: bool = False
    yes: bool = False
    max_leaves: int = 10
    seed_from: Literal["remote", "specs", "none"] | None = None
    json_output: bool = False
    why: str | None = None
    hubs: bool = False
    through_sfx: bool = False


class ResumeState:
    """Tracks successfully synced artefacts so a failed run can be resumed."""

    FILENAME = ".flow_resume.json"

    def __init__(
        self,
        repo_root: Path,
        load: bool,
        *,
        target: tuple[str, str] | None = None,
        read_only: bool = False,
    ) -> None:
        self._read_only = read_only
        self._path = repo_root / self.FILENAME
        self._target = (
            {"baseUrl": target[0].rstrip("/"), "environmentId": target[1]}
            if target
            else None
        )
        self.pending_functions: set[str] = set()
        self._done: set[str] = set()
        self._fingerprints: dict[str, str] = {}
        if self._target:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Expected a resume-state object")
                done = data.get("done")
                pending = data.get("pending_functions", [])
                fingerprints = data.get("fingerprints", {})
                saved_target = data.get("target")
                if (
                    not isinstance(done, list)
                    or not all(isinstance(key, str) for key in done)
                    or not isinstance(pending, list)
                    or not all(isinstance(key, str) for key in pending)
                    or not isinstance(fingerprints, dict)
                    or not all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in fingerprints.items()
                    )
                    or (
                        saved_target is not None
                        and (
                            not isinstance(saved_target, dict)
                            or not all(
                                isinstance(saved_target.get(key), str)
                                and saved_target[key]
                                for key in ("baseUrl", "environmentId")
                            )
                        )
                    )
                ):
                    raise ValueError("Invalid resume-state fields")
            except FileNotFoundError:
                return
            except (OSError, ValueError):
                raise DeployError(
                    "Cannot read valid .flow_resume.json; restore a valid snapshot before continuing. "
                    "If recovery is impossible, explicitly rebuild the affected dependency tree before discarding the state."
                ) from None
            if saved_target != self._target:
                if pending:
                    raise DeployError(
                        "Unfinished dependency deployment belongs to another target; resume it with its original environment file"
                    )
                print("[resume] Ignoring state for a different or unverified target")
                return
            if not load:
                if pending:
                    raise DeployError(
                        "Unfinished dependency deployment; rerun push-dynamic with --resume and the same target"
                    )
                return
            self.pending_functions = set(pending)
            self._done = set(done)
            self._fingerprints = fingerprints
            print(
                f"[resume] Loaded {len(self._done)} already-synced artefact(s) from {self._path.name}"
            )

    def is_done(self, key: str, fingerprint: str | None = None) -> bool:
        return (
            fingerprint is not None
            and key in self._done
            and self._fingerprints.get(key) == fingerprint
        )

    def mark_done(self, key: str, fingerprint: str | None = None) -> None:
        self._done.add(key)
        if fingerprint is not None:
            self._fingerprints[key] = fingerprint
        else:
            self._fingerprints.pop(key, None)
        self.pending_functions.discard(key)
        self.save()

    def plan_functions(self, keys: set[str]) -> None:
        self.pending_functions = set(keys)
        self.save()

    def save(self) -> None:
        if self._read_only:
            return
        snapshot = json.dumps(
            {
                "target": self._target,
                **(
                    {"pending_functions": sorted(self.pending_functions)}
                    if self.pending_functions
                    else {}
                ),
                "done": sorted(self._done),
                "fingerprints": {
                    key: self._fingerprints[key]
                    for key in sorted(self._fingerprints)
                    if key in self._done
                },
            },
            indent=2,
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=".flow_resume.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                output.write(snapshot)
                output.flush()
                os.fsync(output.fileno())
            # The destination remains a complete snapshot even if saving fails.
            os.replace(temporary_path, self._path)
        except OSError:
            raise DeployError(
                "Could not save resume state; deployment stopped. Restore write access and rerun with --resume."
            ) from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> None:
        if self._read_only:
            return
        self.pending_functions.clear()
        self._done.clear()
        self._fingerprints.clear()
        if self._path.exists():
            self._path.unlink()


def add_push_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--build-schema",
        action="store_true",
        help="Refresh Python JSON schemas from the platform OpenAPI contract before sync",
    )
    parser.add_argument(
        "--skip-invalid",
        action="store_true",
        help="Skip invalid local artefacts after reporting validation failures",
    )
    parser.add_argument(
        "--skip-validation",
        metavar="ACKNOWLEDGEMENT",
        help=f'Bypass scope, branch-env and PROD prompts only with "{SKIP_VALIDATION_ACKNOWLEDGEMENT}"',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned actions without changing anything remotely.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Update existing artefacts even when unchanged and bypass the "
            "local-newer-than-remote guard. Existing secret/obscured variables are "
            "still protected unless secret updates are explicitly enabled. "
            "For GraphQL subscriptions, request confirmed DELETE + CREATE."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print more detail while scanning and diffing.",
    )
    parser.add_argument(
        "--delete-orphans",
        action="store_true",
        help="Delete managed remote artefacts that are not present locally.",
    )
    parser.add_argument(
        "--adopt-orphans",
        action="store_true",
        help="Recreate supported orphaned remote artefacts into local JSON files.",
    )
    parser.add_argument(
        "--types",
        help=(
            "Comma-separated artefact types to deploy. "
            "Supported values: functions,client-functions,server-functions,"
            "variables,webhooks,jobs,tables,triggers,schemas,ai-functions,canopy,subscriptions."
        ),
    )
    parser.add_argument(
        "--paths",
        help=(
            "Comma-separated scope filters. Accepts repo paths like "
            "`workflows/src/sample_workflow` or shortcuts like `sample_workflow` and "
            "`utilities/common`."
        ),
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print resolved config and exit.",
    )
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="Skip the poly generate step after function sync.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from a previous interrupted run, skipping already-synced artefacts.",
    )


class Logger:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.machine_readable = False
        self.colors_enabled = terminal_supports_color()

    def info(self, message: str) -> None:
        print(message)

    def detail(self, message: str) -> None:
        if self.verbose:
            print(style_text(message, rgb=GRAY, enabled=self.colors_enabled))

    def muted(self, message: str) -> None:
        self.info(style_text(message, rgb=GRAY, enabled=self.colors_enabled))

    def section(self, label: str) -> None:
        self.info(create_header(label, enabled=self.colors_enabled))

    def action(self, message: str) -> None:
        self.info(style_text(message, rgb=BLUE, enabled=self.colors_enabled))

    def item(
        self,
        label: str,
        *,
        file_path: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        line = style_text(f"  → {label}", dim=True, enabled=self.colors_enabled)
        if file_path and repo_root:
            line += " " + format_file_ref(
                file_path, repo_root, enabled=self.colors_enabled
            )
        self.info(line)

    def ok(self, message: str) -> None:
        rgb = YELLOW if message.startswith("Skipped") else GREEN
        self._status_line("✓", "OK", message, rgb)

    def fail(self, message: str) -> None:
        self._status_line("✗", "FAIL", message, RED)

    def blocked(self, message: str) -> None:
        self._status_line("!", "BLOCK", message, YELLOW)

    def planned(self, message: str) -> None:
        self._status_line("~", "PLAN", message, BLUE)

    def skip(self, message: str) -> None:
        self._status_line("!", "SKIP", message, YELLOW)

    def warn(self, message: str) -> None:
        print(
            style_text(
                f"WARN: {message}", rgb=YELLOW, bold=True, enabled=self.colors_enabled
            )
        )

    def error(self, message: str) -> None:
        print(
            style_text(
                f"ERROR: {message}", rgb=RED, bold=True, enabled=self.colors_enabled
            ),
            file=sys.stderr,
        )

    def _status_line(
        self,
        symbol: str,
        tag: str,
        message: str,
        rgb: tuple[int, int, int],
    ) -> None:
        symbol_text = style_text(
            symbol, rgb=rgb, bold=True, enabled=self.colors_enabled
        )
        tag_text = style_text(
            f"[{tag}]", rgb=rgb, bold=True, enabled=self.colors_enabled
        )
        message_text = style_text(message, rgb=rgb, enabled=self.colors_enabled)
        self.info(f"    {symbol_text} {tag_text} {message_text}")


class HelpFormatter(
    argparse.RawDescriptionHelpFormatter,
    argparse.ArgumentDefaultsHelpFormatter,
):
    pass


class PolyApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
        otp_manager: OtpManager | None = None,
    ) -> None:
        self.otp_manager = otp_manager
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(get_auth_headers(api_key))
        self.session.headers["Content-Type"] = "application/json"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: Any | None = None,
        raw_body: str | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        if raw_body is not None and payload is not None:
            raise DeployError("Choose a JSON payload or raw request body, not both")
        body = {"data": raw_body} if raw_body is not None else {"json": payload}
        try:
            response = self.session.request(
                method=method,
                url=f"{self.base_url}{path}",
                params=params,
                **body,
                headers={
                    **(
                        self.otp_manager.delete_headers()
                        if self.otp_manager is not None
                        and method.upper() not in {"GET", "HEAD", "OPTIONS"}
                        else {}
                    ),
                    **(headers or {}),
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.exceptions.Timeout as exc:
            raise DeployError(
                f"{method} {path} timed out after {self.timeout_seconds}s"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise DeployError(f"{method} {path} request failed") from exc
        if response.status_code not in expected:
            # Keep the known recovery signal without exposing the provider's body.
            secrecy_change_rejected = (
                method.upper() == "PATCH"
                and path.startswith("/variables/")
                and response.status_code == 400
                and "Cannot change secrecy level" in response.text
            )
            raise ApiError(
                method,
                path,
                response.status_code,
                secrecy_change_rejected=secrecy_change_rejected,
            )
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text


class OtpManager:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._otp: str | None = None

    def delete_headers(self) -> dict[str, str]:
        if not self.enabled:
            return {}
        if self._otp is None:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise DeployError(
                    "OTP is required to write resources, but no interactive terminal is available"
                )
            try:
                otp = getpass.getpass(
                    "OTP is required to write resources. Enter OTP: "
                ).strip()
            except EOFError as exc:
                raise DeployError("OTP prompt cancelled") from exc
            except KeyboardInterrupt as exc:
                print()
                raise DeployError("OTP prompt cancelled") from exc
            if not otp:
                raise DeployError("OTP is required to write resources")
            self._otp = otp
        return {"x-otp": self._otp}


class FunctionRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[FunctionType, str, str], dict[str, Any]] = {}

    def load(self, function_type: FunctionType, items: list[dict[str, Any]]) -> None:
        for item in items:
            key = (function_type, item["context"], item["name"])
            self._items[key] = item

    def get(
        self, function_type: FunctionType, context: str, name: str
    ) -> dict[str, Any] | None:
        return self._items.get((function_type, context, name))

    def require_id(self, context: str, name: str) -> str:
        server_match = self.get("server", context, name)
        if server_match:
            return server_match["id"]
        client_match = self.get("client", context, name)
        if client_match:
            return client_match["id"]
        raise DeployError(f"Unable to resolve function reference {context}.{name}")

    def require_kind_id(
        self,
        function_type: FunctionType,
        context: str,
        name: str,
    ) -> str:
        match = self.get(function_type, context, name)
        if match:
            return match["id"]
        raise DeployError(
            f"Unable to resolve {function_type} function reference {context}.{name}"
        )

    def upsert(self, function_type: FunctionType, item: dict[str, Any]) -> None:
        self._items[(function_type, item["context"], item["name"])] = item

    def find_by_id(
        self,
        function_id: str,
        *,
        function_type: FunctionType | None = None,
    ) -> tuple[FunctionType, dict[str, Any]] | None:
        for item_key, item in self._items.items():
            item_type, _context, _name = item_key
            if function_type is not None and item_type != function_type:
                continue
            if item.get("id") == function_id:
                return item_type, item
        return None


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=ROOT_HELP_DESCRIPTION,
        epilog=ROOT_HELP_EPILOG,
        formatter_class=HelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.add_parser(
        "setup", help="Configure the SDK, project target, and editor."
    )
    push_parser = subparsers.add_parser(
        "push",
        help="Sync local artefacts to PolyAPI.",
        description=PUSH_HELP_DESCRIPTION,
        epilog=PUSH_HELP_EPILOG,
        formatter_class=HelpFormatter,
    )
    add_push_arguments(push_parser)
    dynamic_parser = subparsers.add_parser(
        "push-dynamic",
        help="Sync changed functions and all local consumers through nested clients.",
        description="Plan nested Python SDK dependencies, then deploy dependencies before consumers.",
        formatter_class=HelpFormatter,
    )
    add_push_arguments(dynamic_parser)
    dynamic_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Accept the dependency plan; production and OTP safeguards still apply",
    )
    dynamic_parser.add_argument(
        "--max-leaves",
        type=int,
        default=10,
        help="Require typing the server count above this threshold (default: 10)",
    )
    dynamic_parser.add_argument(
        "seeds",
        nargs="*",
        help="Force named context.name, source file, or directory trees",
    )
    dynamic_parser.add_argument(
        "--seeds", dest="seed_list", help="Comma-separated forced function seeds"
    )
    dynamic_parser.add_argument(
        "--skip-unchanged-cfx",
        action="store_true",
        help="Skip unchanged intermediate clients; always rebuild affected servers",
    )
    dynamic_parser.add_argument(
        "--seed-from",
        choices=("remote", "specs", "none"),
        help="Detect changed seeds from live payloads, native SDK cached code, or forced seeds only",
    )
    dynamic_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Preview only: implies --dry-run; JSON goes to stdout and progress to stderr",
    )
    dynamic_parser.add_argument(
        "--why",
        metavar="CONTEXT.NAME",
        help="Explain seed-to-caller paths and exit without deploying",
    )
    dynamic_parser.add_argument(
        "--hubs",
        action="store_true",
        help="List shared clients and their consumer counts offline",
    )
    dynamic_parser.add_argument(
        "--through-sfx",
        action="store_true",
        help="Show callers beyond server functions in reports only",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> CliArgs | None:
    parser = create_parser()
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if not effective_argv:
        parser.print_help()
        return None

    parsed = parser.parse_args(effective_argv)
    if getattr(parsed, "command", None) not in {"push", "push-dynamic"}:
        parser.print_help()
        return None
    if parsed.delete_orphans and parsed.adopt_orphans:
        raise DeployError("Choose either --delete-orphans or --adopt-orphans, not both")
    if (
        parsed.skip_validation is not None
        and parsed.skip_validation != SKIP_VALIDATION_ACKNOWLEDGEMENT
    ):
        raise DeployError(
            f'--skip-validation must exactly equal "{SKIP_VALIDATION_ACKNOWLEDGEMENT}"'
        )

    enabled_types, function_types = parse_type_selection(
        parse_csv_env_value(parsed.types)
    )
    dynamic = parsed.command == "push-dynamic"
    json_output = bool(getattr(parsed, "json_output", False))
    dry_run = parsed.dry_run or json_output
    if json_output and parsed.build_schema:
        raise DeployError(
            "--json cannot be combined with --build-schema; reports never refresh local schemas"
        )
    if getattr(parsed, "max_leaves", 10) < 0:
        raise DeployError("--max-leaves must be nonnegative")
    seeds = tuple(getattr(parsed, "seeds", ())) + parse_csv_env_value(
        getattr(parsed, "seed_list", None)
    )
    if dynamic and parsed.adopt_orphans:
        raise DeployError("Use push for orphan adoption")
    if dynamic and parsed.skip_invalid:
        raise DeployError(
            "push-dynamic requires a complete valid function graph; --skip-invalid is not supported"
        )
    if dynamic and enabled_types is not None and "functions" not in enabled_types:
        raise DeployError("push-dynamic requires functions in --types")
    if (
        dynamic
        and seeds
        and not parsed.paths
        and enabled_types is None
        and parsed.seed_from in {None, "none"}
    ):
        enabled_types = ("functions",)
    if dynamic and seeds and parsed.delete_orphans:
        raise DeployError("Named function trees cannot delete orphans")
    report_only = bool(getattr(parsed, "why", None) or getattr(parsed, "hubs", False))
    offline_preview = (
        dynamic and dry_run and getattr(parsed, "seed_from", None) in {"specs", "none"}
    )
    if getattr(parsed, "why", None) and getattr(parsed, "hubs", False):
        raise DeployError("Choose either --why or --hubs")
    if report_only or offline_preview:
        if (
            parsed.build_schema
            or parsed.delete_orphans
            or parsed.resume
            or parsed.show_config
        ):
            raise DeployError(
                "Graph reports and offline previews cannot refresh schemas, delete orphans, resume deployments, or show target config"
            )
        if enabled_types is not None and enabled_types != ("functions",):
            raise DeployError(
                "Graph reports and offline previews support functions only"
            )
        enabled_types = ("functions",)
    return CliArgs(
        dynamic=dynamic,
        seed_from=getattr(parsed, "seed_from", None),
        json_output=json_output,
        why=getattr(parsed, "why", None),
        hubs=getattr(parsed, "hubs", False),
        through_sfx=getattr(parsed, "through_sfx", False),
        yes=getattr(parsed, "yes", False),
        max_leaves=getattr(parsed, "max_leaves", 10),
        seeds=seeds,
        skip_unchanged_cfx=getattr(parsed, "skip_unchanged_cfx", False),
        dry_run=dry_run,
        force=parsed.force,
        verbose=parsed.verbose,
        delete_orphans=parsed.delete_orphans,
        path_filters=tuple(Path(item) for item in parse_csv_env_value(parsed.paths)),
        enabled_types=enabled_types,
        show_config=parsed.show_config,
        adopt_orphans=parsed.adopt_orphans,
        function_types=function_types,
        skip_generate=parsed.skip_generate,
        resume=parsed.resume,
        build_schema=parsed.build_schema,
        skip_invalid=parsed.skip_invalid,
        skip_validation_acknowledged=parsed.skip_validation is not None,
    )


def find_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def terminal_supports_color() -> bool:
    has_tty = sys.stdout.isatty()
    no_color_disabled = os.getenv("NO_COLOR") is None
    term_supported = os.getenv("TERM", "").lower() != "dumb"
    return has_tty and no_color_disabled and term_supported


def style_text(
    text: str,
    *,
    rgb: tuple[int, int, int] | None = None,
    bold: bool = False,
    dim: bool = False,
    enabled: bool | None = None,
) -> str:
    if enabled is None:
        enabled = terminal_supports_color()
    if not enabled:
        return text

    prefix = ""
    if bold:
        prefix += "\033[1m"
    if dim:
        prefix += "\033[2m"
    if rgb is not None:
        prefix += f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if not prefix:
        return text
    return f"{prefix}{text}\033[0m"


def multicolor_rule(
    length: int = 60,
    *,
    char: str = "═",
    enabled: bool | None = None,
) -> str:
    if enabled is None:
        enabled = terminal_supports_color()
    base_length, remainder = divmod(length, len(POLYAPI_BRAND_COLORS))
    segments = []
    for index, color in enumerate(POLYAPI_BRAND_COLORS):
        segment_length = base_length + (1 if index < remainder else 0)
        if segment_length == 0:
            continue
        segments.append(
            style_text(char * segment_length, rgb=color, bold=True, enabled=enabled)
        )
    return "".join(segments)


def visible_logo_cluster_spans(line: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, character in enumerate(line):
        is_visible = character not in {" ", "_"}
        if is_visible and start is None:
            start = index
            continue
        if not is_visible and start is not None:
            spans.append((start, index - 1))
            start = None
    if start is not None:
        spans.append((start, len(line) - 1))
    return spans


def fill_rule_color_gaps(
    color_indices: list[int | None],
    *,
    extend_trailing: bool,
    bias_left_even_gap: bool,
) -> list[int]:
    known_indices = [
        index for index, value in enumerate(color_indices) if value is not None
    ]
    if not known_indices:
        return [0 for _ in color_indices]

    filled = color_indices[:]
    first = known_indices[0]
    for index in range(first):
        filled[index] = filled[first]

    last = known_indices[-1]
    if extend_trailing:
        for index in range(last + 1, len(filled)):
            filled[index] = filled[last]

    previous = first
    for current in known_indices[1:]:
        if current - previous > 1:
            left = filled[previous]
            right = filled[current]
            gap = current - previous - 1
            left_count = gap // 2 if gap % 2 == 0 else (gap + 1) // 2
            if bias_left_even_gap and gap % 2 == 0 and left != right:
                left_count += 1
            left_count = min(left_count, gap)
            for offset, index in enumerate(range(previous + 1, current), start=1):
                if left == right:
                    filled[index] = left
                else:
                    filled[index] = left if offset <= left_count else right
        previous = current

    end = len(filled) if extend_trailing else last + 1
    return [cast(int, value) for value in filled[:end]]


def build_flow_rule_color_indices(position: Literal["top", "bottom"]) -> list[int]:
    logo_lines = FLOW_LOGO.splitlines()
    cluster_spans = [visible_logo_cluster_spans(line) for line in logo_lines]
    color_indices: list[int | None] = [None] * FLOW_LOGO_WIDTH
    row_range = (
        range(len(logo_lines))
        if position == "top"
        else range(len(logo_lines) - 1, -1, -1)
    )

    for column in range(FLOW_LOGO_WIDTH):
        for row in row_range:
            for cluster_index, (start, end) in enumerate(cluster_spans[row]):
                if start <= column <= end:
                    color_indices[column] = FLOW_LOGO_CLUSTER_COLOR_SEQUENCE[row][
                        cluster_index
                    ]
                    break
            if color_indices[column] is not None:
                break

    filled = fill_rule_color_gaps(
        color_indices,
        extend_trailing=position == "bottom",
        bias_left_even_gap=position == "bottom",
    )

    if position == "bottom":
        # Nudge the lower rule slightly toward the O span so the colored line tracks
        # the lower silhouette more closely than the strict raw cluster boundary.
        for index in range(1, len(filled)):
            if filled[index - 1] == 1 and filled[index] == 2:
                filled[index - 1] = 2
                break

    return filled


def render_flow_rule(
    position: Literal["top", "bottom"] = "top",
    *,
    enabled: bool | None = None,
) -> str:
    if enabled is None:
        enabled = terminal_supports_color()
    color_indices = build_flow_rule_color_indices(position)
    return "".join(
        style_text(
            "═" * sum(1 for _ in group),
            rgb=POLYAPI_BRAND_COLORS[color_index],
            bold=True,
            enabled=enabled,
        )
        for color_index, group in itertools.groupby(color_indices)
    )


def create_header(label: str, *, enabled: bool | None = None) -> str:
    if enabled is None:
        enabled = terminal_supports_color()
    line = multicolor_rule(40, char="━", enabled=enabled)
    return f"\n{style_text(f'[{label}]', rgb=CYAN, bold=True, enabled=enabled)} {line}"


def relative_label(path: Path, repo_root: Path) -> str:
    if is_relative_to(path, repo_root):
        return path.relative_to(repo_root).as_posix()
    return str(path)


def format_file_ref(path: Path, repo_root: Path, *, enabled: bool | None = None) -> str:
    return style_text(relative_label(path, repo_root), rgb=CYAN, enabled=enabled)


def render_flow_logo(*, enabled: bool | None = None) -> str:
    if enabled is None:
        enabled = terminal_supports_color()
    if not enabled:
        return FLOW_LOGO

    colors = (BLUE, RED, YELLOW, GREEN)
    logo_lines = FLOW_LOGO.splitlines()
    rendered_lines: list[str] = []
    for line_index, line in enumerate(logo_lines):
        cluster_colors = FLOW_LOGO_CLUSTER_COLOR_SEQUENCE[line_index]
        cluster_index = -1
        in_cluster = False
        rendered_line: list[str] = []
        for character in line:
            if character == " ":
                rendered_line.append(character)
                in_cluster = False
                continue
            if character == "_":
                rgb = WHITE
                in_cluster = False
            else:
                if not in_cluster:
                    cluster_index += 1
                    in_cluster = True
                color_index = (
                    cluster_colors[cluster_index]
                    if cluster_index < len(cluster_colors)
                    else min(cluster_index, len(colors) - 1)
                )
                rgb = colors[color_index]
            rendered_line.append(style_text(character, rgb=rgb, enabled=enabled))
        rendered_lines.append("".join(rendered_line))
    return "\n".join(rendered_lines)


def format_scope_label(cli_args: CliArgs, config: DeployConfig) -> str:
    if not cli_args.path_filters:
        interactive_prompt_enabled = all(
            (
                config.initial_scope_prompt_enabled,
                sys.stdin.isatty(),
                sys.stdout.isatty(),
            )
        )
        if interactive_prompt_enabled:
            return "interactive selection"
        return "full repo"
    labels: list[str] = []
    for path in cli_args.path_filters:
        if is_relative_to(path, config.repo_root):
            labels.append(str(path.relative_to(config.repo_root)))
        else:
            labels.append(str(path))
    return ", ".join(labels)


def print_startup_banner(
    *,
    logger: Logger,
    config: DeployConfig,
    cli_args: CliArgs,
) -> None:
    if not sys.stdout.isatty():
        return

    colors_enabled = terminal_supports_color()
    logger.info("")
    logger.info(render_flow_rule("top", enabled=colors_enabled))
    logger.info(render_flow_logo(enabled=colors_enabled))
    logger.info(render_flow_rule("bottom", enabled=colors_enabled))
    logger.info(
        style_text(
            "flow",
            rgb=POLYAPI_BRAND_COLORS[0],
            bold=True,
            enabled=colors_enabled,
        )
        + " "
        + style_text(
            f"[{'dry-run' if cli_args.dry_run else 'apply'}]",
            rgb=POLYAPI_BRAND_COLORS[1],
            bold=True,
            enabled=colors_enabled,
        )
    )
    if cli_args.adopt_orphans:
        mode_label = "adopt-orphans"
    elif cli_args.delete_orphans:
        mode_label = "delete-orphans"
    else:
        mode_label = "safe-sync"
    logger.info(f"mode: {mode_label}")
    logger.info(f"types: {', '.join(config.enabled_types)}")
    if "functions" in config.enabled_types and cli_args.function_types:
        logger.info(f"function types: {', '.join(cli_args.function_types)}")
    logger.info(f"scope: {format_scope_label(cli_args, config)}")
    if config.priority_paths:
        priority_labels = [
            (
                str(path.relative_to(config.repo_root))
                if is_relative_to(path, config.repo_root)
                else str(path)
            )
            for path in config.priority_paths
        ]
        logger.info(f"priority: {', '.join(priority_labels)}")
    if config.server_function_images:
        logger.info(
            "server function images: "
            + format_server_function_images(config.server_function_images)
        )
    if config.server_function_scripts:
        logger.info(
            "server function scripts: "
            + format_server_function_scripts(config.server_function_scripts)
        )
    logger.info("")


def get_api_key_and_url() -> tuple[str | None, str | None]:
    try:
        return resolve_api_connection()
    except ConnectionError as error:
        raise DeployError(str(error)) from None


def load_env(repo_root: Path) -> EnvLoadResult:
    os.environ.update(normalize_connection_settings(os.environ))
    raw_envpath = os.environ.get("ENVPATH")
    if raw_envpath is not None:
        env_label = raw_envpath
        if not env_label.strip():
            raise DeployError(
                "ENVPATH is set but empty. Set it to a valid env file path."
            )
        env_path = Path(env_label)
        if not env_path.is_absolute():
            env_path = repo_root / env_path
        env_path = env_path.resolve(strict=False)
        if not env_path.is_file():
            raise DeployError(
                f"ENVPATH points to `{env_label}`, but that file does not exist."
            )
        os.environ.update(normalize_connection_settings(dotenv_values(env_path)))
        return EnvLoadResult(
            display_path=env_label,
            resolved_path=env_path,
            exists=True,
            from_envpath=True,
        )

    env_path = (repo_root / DEFAULT_ENV_FILE).resolve(strict=False)
    if env_path.is_file():
        os.environ.update(normalize_connection_settings(dotenv_values(env_path)))
    return EnvLoadResult(
        display_path=DEFAULT_ENV_FILE,
        resolved_path=env_path,
        exists=env_path.is_file(),
        from_envpath=False,
    )


def parse_branch_env_binding(raw_value: str, *, env_name: str) -> BranchEnvBinding:
    normalized = raw_value.strip()
    branch_part, separator, env_part = normalized.partition(":")
    branch = branch_part.strip()
    if not branch:
        raise DeployError(f"{env_name} contains an empty branch entry.")
    if separator and not env_part.strip():
        raise DeployError(
            f"{env_name} entry `{normalized}` must include an env file after `:`."
        )
    env_file = env_part.strip() or None
    return BranchEnvBinding(branch=branch, env_file=env_file)


def configured_production_branch_config() -> BranchEnvBinding:
    raw_value = os.getenv("GIT_PROD_BRANCH")
    if raw_value is None or not raw_value.strip():
        return BranchEnvBinding(branch=DEFAULT_GIT_PROD_BRANCH)
    return parse_branch_env_binding(raw_value, env_name="GIT_PROD_BRANCH")


def configured_production_branch() -> str:
    return configured_production_branch_config().branch


def configured_production_skip_branch_configs(
    production_branch: str,
) -> tuple[BranchEnvBinding, ...]:
    configured = env_csv("GIT_PROD_SKIP_BRANCHES", DEFAULT_GIT_PROD_SKIP_BRANCHES)
    unique_bindings: list[BranchEnvBinding] = []
    seen: dict[str, BranchEnvBinding] = {}
    for raw_value in configured:
        binding = parse_branch_env_binding(raw_value, env_name="GIT_PROD_SKIP_BRANCHES")
        branch = binding.branch
        if branch == production_branch:
            raise DeployError(
                "GIT_PROD_SKIP_BRANCHES cannot include the configured production branch "
                f"`{production_branch}`."
            )
        existing = seen.get(branch)
        if existing is not None:
            if existing.env_file != binding.env_file:
                raise DeployError(
                    "GIT_PROD_SKIP_BRANCHES configures branch "
                    f"`{branch}` more than once with different env files."
                )
            continue
        unique_bindings.append(binding)
        seen[branch] = binding
    return tuple(unique_bindings)


def configured_production_skip_branches(production_branch: str) -> tuple[str, ...]:
    return tuple(
        binding.branch
        for binding in configured_production_skip_branch_configs(production_branch)
    )


def resolve_branch_env_file(repo_root: Path, env_file: str) -> Path:
    candidate = Path(env_file)
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    uses_explicit_path = any(
        separator in env_file for separator in ("/", "\\")
    ) or env_file.startswith(".")
    if uses_explicit_path:
        return (repo_root / candidate).resolve(strict=False)
    return (repo_root / DEFAULT_BRANCH_ENV_DIRECTORY / candidate).resolve(strict=False)


def format_expected_branch_env_file(repo_root: Path, env_file: str) -> str:
    return relative_label(resolve_branch_env_file(repo_root, env_file), repo_root)


def get_execution_api_key() -> str | None:
    if "POLY_EXEC_API_KEY" not in os.environ:
        return None
    raw_value = os.environ["POLY_EXEC_API_KEY"].strip()
    if not raw_value:
        raise DeployError(
            "POLY_EXEC_API_KEY is set but empty. Provide a non-empty execution key."
        )
    return raw_value


def get_target_env_id(env_file: EnvLoadResult) -> str:
    """Return the PolyAPI environment ID the selected env file is pinned to."""
    if not env_file.exists:
        raise DeployError(
            f"`{env_file.display_path}` does not exist, so no "
            f"{TARGET_ENV_ID_VAR} pin could be read. Point ENVPATH at the env "
            "file for the environment you are deploying to."
        )

    declared = dotenv_values(env_file.resolved_path)
    raw_value = declared.get(TARGET_ENV_ID_VAR)
    if raw_value is None or not raw_value.strip():
        raise DeployError(
            f"{TARGET_ENV_ID_VAR} is not set in `{env_file.display_path}`. "
            "Every env file must pin the PolyAPI environment ID it is allowed to "
            "deploy to, so that it cannot be used against another environment. "
            f"{ENV_ID_HINT}. Add `{TARGET_ENV_ID_VAR}=<id>` to the env file "
            "itself, then re-run. Exporting it in your shell does not count: the "
            "pin must be declared by the file."
        )
    return raw_value.strip()


def run_git_command(
    repo_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        error_text = (
            completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        )
        raise DeployError(f"`git {' '.join(args)}` failed: {error_text}")
    return completed


def resolve_git_production_ref(repo_root: Path, production_branch: str) -> str:
    for candidate in (production_branch, f"origin/{production_branch}"):
        completed = run_git_command(
            repo_root, "rev-parse", "--verify", candidate, check=False
        )
        if completed.returncode == 0:
            return candidate
    raise DeployError(
        "Configured production branch "
        f"`{production_branch}` was not found locally. Set GIT_PROD_BRANCH to a valid branch name."
    )


def current_git_branch(repo_root: Path) -> str:
    branch = run_git_command(repo_root, "branch", "--show-current").stdout.strip()
    if branch:
        return branch
    head = run_git_command(repo_root, "rev-parse", "--short", "HEAD").stdout.strip()
    if head:
        return f"DETACHED@{head}"
    raise DeployError("Could not determine the current Git branch.")


def list_local_git_branches(repo_root: Path) -> tuple[str, ...]:
    completed = run_git_command(
        repo_root,
        "for-each-ref",
        "refs/heads",
        "--format=%(refname:short)",
    )
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def resolve_branch_base_commit(
    repo_root: Path,
    branch_ref: str,
    *,
    head_ref: str = "HEAD",
) -> str | None:
    fork_point = run_git_command(
        repo_root,
        "merge-base",
        "--fork-point",
        branch_ref,
        head_ref,
        check=False,
    )
    if fork_point.returncode == 0 and fork_point.stdout.strip():
        return fork_point.stdout.strip()

    merge_base = run_git_command(
        repo_root,
        "merge-base",
        branch_ref,
        head_ref,
        check=False,
    )
    if merge_base.returncode == 0 and merge_base.stdout.strip():
        return merge_base.stdout.strip()
    return None


def commits_since_base(
    repo_root: Path,
    base_commit: str,
    *,
    head_ref: str = "HEAD",
) -> int:
    completed = run_git_command(
        repo_root,
        "rev-list",
        "--count",
        f"{base_commit}..{head_ref}",
    )
    raw_value = completed.stdout.strip()
    if not raw_value.isdigit():
        raise DeployError(f"Unexpected `git rev-list --count` output: {raw_value!r}")
    return int(raw_value)


def nearest_ancestor_branch(
    repo_root: Path,
    *,
    current_branch: str,
    excluded_branches: tuple[str, ...] = (),
    max_distance: int | None = None,
    head_ref: str = "HEAD",
) -> str | None:
    nearest_branch: str | None = None
    nearest_distance: int | None = None
    for candidate in list_local_git_branches(repo_root):
        if candidate == current_branch or candidate in excluded_branches:
            continue
        candidate_base = resolve_branch_base_commit(
            repo_root, candidate, head_ref=head_ref
        )
        if candidate_base is None:
            continue
        candidate_distance = commits_since_base(
            repo_root, candidate_base, head_ref=head_ref
        )
        if max_distance is not None and candidate_distance >= max_distance:
            continue
        if nearest_distance is None or candidate_distance < nearest_distance:
            nearest_branch = candidate
            nearest_distance = candidate_distance
    return nearest_branch


def branch_ancestor_chain(
    repo_root: Path,
    *,
    current_branch: str,
    excluded_branches: tuple[str, ...] = (),
) -> tuple[str, ...]:
    ancestors: list[str] = []
    seen = {current_branch}
    branch_ref = current_branch
    while True:
        parent = nearest_ancestor_branch(
            repo_root,
            current_branch=branch_ref,
            excluded_branches=excluded_branches,
            head_ref=branch_ref,
        )
        if parent is None or parent in seen:
            return tuple(ancestors)
        ancestors.append(parent)
        seen.add(parent)
        branch_ref = parent


def is_branch_directly_derived_from_production(
    repo_root: Path,
    *,
    current_branch: str,
    production_branch: str,
    production_ref: str,
) -> bool:
    production_base = resolve_branch_base_commit(
        repo_root,
        production_ref,
        head_ref=current_branch,
    )
    if production_base is None:
        return False
    production_distance = commits_since_base(
        repo_root,
        production_base,
        head_ref=current_branch,
    )
    closer_branch = nearest_ancestor_branch(
        repo_root,
        current_branch=current_branch,
        excluded_branches=(production_branch,),
        max_distance=production_distance,
        head_ref=current_branch,
    )
    return closer_branch is None


def build_deployment_preflight(
    repo_root: Path,
    env_file: EnvLoadResult,
) -> DeploymentPreflight:
    production_branch_config = configured_production_branch_config()
    production_branch = production_branch_config.branch
    production_skip_branch_configs = configured_production_skip_branch_configs(
        production_branch
    )
    current_branch_name = current_git_branch(repo_root)
    production_ref = resolve_git_production_ref(repo_root, production_branch)
    current_branch_parent = None
    current_branch_ancestors: tuple[str, ...] = ()
    if not current_branch_name.startswith("DETACHED@"):
        current_branch_ancestors = branch_ancestor_chain(
            repo_root,
            current_branch=current_branch_name,
        )
        if current_branch_ancestors:
            current_branch_parent = current_branch_ancestors[0]

    if current_branch_name == production_branch:
        return DeploymentPreflight(
            env_file=env_file,
            production_branch=production_branch,
            current_branch=current_branch_name,
            production_sensitive=True,
            current_branch_parent=current_branch_parent,
            sensitive_reason="current branch matches the configured production branch",
            expected_env_branch=production_branch_config.branch,
            expected_env_file=production_branch_config.env_file,
        )

    exempt_binding = next(
        (
            binding
            for binding in production_skip_branch_configs
            if binding.branch in (current_branch_name, *current_branch_ancestors)
        ),
        None,
    )
    if exempt_binding is not None:
        exempt_branch = exempt_binding.branch
        if exempt_branch == current_branch_name:
            exemption_reason = f"exempt via GIT_PROD_SKIP_BRANCHES ({exempt_branch})"
        else:
            exemption_reason = f"derived from exempt branch {exempt_branch}"
        return DeploymentPreflight(
            env_file=env_file,
            production_branch=production_branch,
            current_branch=current_branch_name,
            production_sensitive=False,
            current_branch_parent=current_branch_parent,
            safeguard_exemption_reason=exemption_reason,
            expected_env_branch=exempt_binding.branch,
            expected_env_file=exempt_binding.env_file,
        )

    if is_branch_directly_derived_from_production(
        repo_root,
        current_branch=current_branch_name,
        production_branch=production_branch,
        production_ref=production_ref,
    ):
        return DeploymentPreflight(
            env_file=env_file,
            production_branch=production_branch,
            current_branch=current_branch_name,
            production_sensitive=True,
            current_branch_parent=current_branch_parent,
            sensitive_reason="current branch is directly derived from the configured production branch",
            expected_env_branch=production_branch_config.branch,
            expected_env_file=production_branch_config.env_file,
        )

    return DeploymentPreflight(
        env_file=env_file,
        production_branch=production_branch,
        current_branch=current_branch_name,
        production_sensitive=False,
        current_branch_parent=current_branch_parent,
    )


def maybe_confirm_branch_env_file_match(
    repo_root: Path,
    preflight: DeploymentPreflight,
    logger: Logger,
    cli_args: CliArgs | None = None,
) -> None:
    branch_label = preflight.expected_env_branch or preflight.current_branch
    selected_label = preflight.env_file.display_path

    if preflight.expected_env_branch is None:
        return

    if preflight.expected_env_file is None:
        warning_message = (
            f"Branch `{branch_label}` does not pin an env file in "
            "`GIT_PROD_BRANCH`/`GIT_PROD_SKIP_BRANCHES`. "
            f"Flow loaded `{selected_label}`. Confirm that this is the correct env file."
        )
    else:
        expected_env_path = resolve_branch_env_file(
            repo_root, preflight.expected_env_file
        )
        if preflight.env_file.resolved_path == expected_env_path:
            return
        expected_label = format_expected_branch_env_file(
            repo_root, preflight.expected_env_file
        )
        warning_message = (
            f"Branch `{branch_label}` expects env file `{expected_label}`, "
            f"but Flow loaded `{selected_label}`."
        )

    logger.warn(warning_message)

    if cli_args and cli_args.skip_validation_acknowledged:
        logger.warn("Branch environment-file mismatch acknowledged non-interactively.")
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DeployError(
            f"{warning_message} Run interactively to confirm or update ENVPATH."
        )

    try:
        answer = input("Continue with this env file anyway? [y/N]: ").strip().lower()
    except EOFError as exc:
        raise DeployError("Env file confirmation prompt cancelled") from exc
    except KeyboardInterrupt as exc:
        print()
        raise DeployError("Env file confirmation prompt cancelled") from exc

    if answer not in {"y", "yes"}:
        raise DeployError(
            "Deployment cancelled because the selected env file was not confirmed for the "
            "current branch."
        )


def warn_for_missing_default_env_file(
    env_file: EnvLoadResult,
    logger: Logger,
) -> None:
    if env_file.exists or env_file.from_envpath:
        return
    logger.warn(
        "No `.env` file was found. Create `.env` or set `ENVPATH` to point to a specific env file."
    )


def print_deployment_visibility(
    logger: Logger,
    preflight: DeploymentPreflight,
    cli_args: CliArgs,
    target_env_id: str,
) -> None:
    logger.info(
        render_visibility_line(
            "env file", format_env_file_visibility(preflight.env_file), CYAN
        )
    )
    logger.info(render_visibility_line("target environment", target_env_id, CYAN))
    logger.info(
        render_visibility_line(
            "current git branch",
            format_current_branch_visibility(preflight),
            BLUE,
        )
    )
    logger.info(
        render_visibility_line("production branch", preflight.production_branch, YELLOW)
    )
    logger.info(
        render_visibility_line(
            "production safeguards",
            describe_production_safeguards(preflight, cli_args),
            GREEN if not production_safeguards_triggered(preflight, cli_args) else RED,
        )
    )
    logger.info("")


def prompt_for_production_confirmation(preflight: DeploymentPreflight) -> None:
    prompt = (
        "Production safeguards are active for "
        f"`{preflight.current_branch}`. Type `{PRODUCTION_CONFIRMATION_VALUE}` to continue: "
    )
    try:
        answer = input(prompt).strip()
    except EOFError as exc:
        raise DeployError("Production confirmation prompt cancelled") from exc
    except KeyboardInterrupt as exc:
        print()
        raise DeployError("Production confirmation prompt cancelled") from exc

    if answer != PRODUCTION_CONFIRMATION_VALUE:
        raise DeployError(
            "Production-sensitive deployment cancelled. "
            f"Type `{PRODUCTION_CONFIRMATION_VALUE}` exactly to continue."
        )


def enforce_production_safeguards(
    preflight: DeploymentPreflight,
    cli_args: CliArgs,
    execution_api_key: str | None,
) -> None:
    if not production_safeguards_triggered(preflight, cli_args):
        return
    if execution_api_key is None:
        raise DeployError(
            "Production safeguards were triggered but POLY_EXEC_API_KEY is not set. "
            "Set it before deploying."
        )
    if cli_args.skip_validation_acknowledged:
        print("Production confirmation acknowledged non-interactively.")
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DeployError(
            "Production-sensitive deployment requires an interactive terminal so you can type "
            f"`{PRODUCTION_CONFIRMATION_VALUE}`."
        )
    prompt_for_production_confirmation(preflight)


def production_safeguards_triggered(
    preflight: DeploymentPreflight,
    cli_args: CliArgs,
) -> bool:
    if not preflight.production_sensitive:
        return False
    if cli_args.dry_run:
        return False
    if cli_args.adopt_orphans:
        return False
    return True


def format_env_file_visibility(env_file: EnvLoadResult) -> str:
    if env_file.exists:
        return env_file.display_path
    return f"{env_file.display_path} (missing, not loaded)"


def format_current_branch_visibility(preflight: DeploymentPreflight) -> str:
    is_named_branch = not preflight.current_branch.startswith("DETACHED@")
    is_non_production_branch = preflight.current_branch != preflight.production_branch
    if preflight.current_branch_parent and is_named_branch and is_non_production_branch:
        return (
            f"{preflight.current_branch} (branch of {preflight.current_branch_parent})"
        )
    return preflight.current_branch


def describe_production_safeguards(
    preflight: DeploymentPreflight,
    cli_args: CliArgs,
) -> str:
    if production_safeguards_triggered(preflight, cli_args):
        return "triggered"
    if preflight.production_sensitive and cli_args.dry_run:
        return "not triggered (dry-run)"
    if preflight.production_sensitive and cli_args.adopt_orphans:
        return "not triggered (adopt-orphans mode)"
    if preflight.safeguard_exemption_reason is not None:
        return f"not triggered ({preflight.safeguard_exemption_reason})"
    return "not triggered"


def render_visibility_line(
    label: str,
    value: str,
    rgb: tuple[int, int, int],
) -> str:
    colors_enabled = terminal_supports_color()
    return (
        style_text(f"{label}:", bold=True, enabled=colors_enabled)
        + " "
        + style_text(
            value,
            rgb=rgb,
            enabled=colors_enabled,
        )
    )


def env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def parse_csv_env_value(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DeployError(f"Invalid boolean value for {name}: {raw}")


def env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError as exc:
        raise DeployError(f"Invalid integer value for {name}: {raw}") from exc
    if min_value is not None and parsed < min_value:
        raise DeployError(f"{name} must be >= {min_value}: {raw}")
    return parsed


def parse_server_function_images(raw: str | None) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}

    mappings: dict[str, str] = {}
    for entry in (item.strip() for item in raw.split(",") if item.strip()):
        if ":" not in entry:
            raise DeployError(
                "POLY_DEPLOY_SERVER_FUNCTION_IMAGES entries must use the format <context>.<name>:<image>"
            )
        function_key, image_name = (part.strip() for part in entry.split(":", 1))
        if not function_key or not image_name:
            raise DeployError(
                "POLY_DEPLOY_SERVER_FUNCTION_IMAGES entries must use the format <context>.<name>:<image>"
            )
        key_parts = function_key.split(".")
        if len(key_parts) < 2 or any(not part.strip() for part in key_parts):
            raise DeployError(
                "POLY_DEPLOY_SERVER_FUNCTION_IMAGES function key must include both "
                f"context and function name: {function_key}"
            )
        if function_key in mappings:
            raise DeployError(
                f"Duplicate function key in POLY_DEPLOY_SERVER_FUNCTION_IMAGES: {function_key}"
            )
        mappings[function_key] = image_name
    return mappings


def format_server_function_images(server_function_images: dict[str, str]) -> str:
    return ", ".join(
        f"{function_key} -> {server_function_images[function_key]}"
        for function_key in sorted(server_function_images)
    )


def parse_server_function_scripts(raw: str | None) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}

    mappings: dict[str, str] = {}
    for entry in (item.strip() for item in raw.split(",") if item.strip()):
        if ":" not in entry:
            raise DeployError(
                "POLY_DEPLOY_SERVER_FUNCTION_SCRIPT entries must use the format "
                "<context>.<name>:<command>"
            )
        function_key, command = (part.strip() for part in entry.split(":", 1))
        if not function_key or not command:
            raise DeployError(
                "POLY_DEPLOY_SERVER_FUNCTION_SCRIPT entries must use the format "
                "<context>.<name>:<command>"
            )
        key_parts = function_key.split(".")
        if len(key_parts) < 2 or any(not part.strip() for part in key_parts):
            raise DeployError(
                "POLY_DEPLOY_SERVER_FUNCTION_SCRIPT function key must include both "
                f"context and function name: {function_key}"
            )
        if function_key in mappings:
            raise DeployError(
                f"Duplicate function key in POLY_DEPLOY_SERVER_FUNCTION_SCRIPT: {function_key}"
            )
        mappings[function_key] = command
    return mappings


def format_server_function_scripts(server_function_scripts: dict[str, str]) -> str:
    return ", ".join(
        f"{function_key} -> {server_function_scripts[function_key]}"
        for function_key in sorted(server_function_scripts)
    )


def parse_context_path_map(
    repo_root: Path, raw: str | None
) -> tuple[ContextPathMapping, ...]:
    if raw is None or not raw.strip():
        return ()
    mappings: list[ContextPathMapping] = []
    seen_prefixes: set[str] = set()
    for entry in (item.strip() for item in raw.split(",") if item.strip()):
        if "=" not in entry:
            raise DeployError(
                "STORK_CONTEXT_PATH_MAP entries must use the format <context_prefix>=<repo_path>"
            )
        context_prefix, raw_path = (part.strip() for part in entry.split("=", 1))
        if not context_prefix or not raw_path:
            raise DeployError(
                "STORK_CONTEXT_PATH_MAP entries must use the format <context_prefix>=<repo_path>"
            )
        target_filename: str | None = None
        if context_prefix.endswith((".json", ".jsonc")):
            target_filename = context_prefix
            context_prefix = re.sub(r"\.jsonc?$", "", context_prefix)
        if context_prefix in seen_prefixes:
            raise DeployError(
                f"Duplicate context prefix in STORK_CONTEXT_PATH_MAP: {context_prefix}"
            )
        root_path = resolve_path(repo_root, raw_path)
        if not is_relative_to(root_path, repo_root):
            raise DeployError(
                f"Context path mapping for {context_prefix} must resolve inside the repo: {raw_path}"
            )
        mappings.append(
            ContextPathMapping(
                context_prefix=context_prefix,
                root_path=root_path,
                target_filename=target_filename,
            )
        )
        seen_prefixes.add(context_prefix)
    return tuple(mappings)


def cast_artifact_types(items: tuple[str, ...]) -> list[ArtifactType]:
    valid = set(DEFAULT_ENABLED_TYPES)
    invalid = [item for item in items if item not in valid]
    if invalid:
        raise DeployError(
            f"Unsupported artefact type(s): {', '.join(invalid)}. Expected one of: {', '.join(sorted(valid))}"
        )
    return [cast(ArtifactType, item) for item in items if item in valid]


def parse_type_selection(
    items: tuple[str, ...],
) -> tuple[tuple[ArtifactType, ...] | None, tuple[FunctionType, ...] | None]:
    if not items:
        return None, None

    invalid = [item for item in items if item not in SUPPORTED_TYPE_SELECTIONS]
    if invalid:
        raise DeployError(
            "Unsupported --types value(s): "
            f"{', '.join(invalid)}. Expected one of: {', '.join(SUPPORTED_TYPE_SELECTIONS)}"
        )

    enabled_types: list[ArtifactType] = []
    seen_artifact_types: set[ArtifactType] = set()
    selected_function_types: list[FunctionType] = []
    seen_function_types: set[FunctionType] = set()

    for item in items:
        if item in FUNCTION_TYPE_ALIASES:
            if "functions" not in seen_artifact_types:
                enabled_types.append("functions")
                seen_artifact_types.add("functions")
            function_type = FUNCTION_TYPE_ALIASES[item]
            if function_type not in seen_function_types:
                selected_function_types.append(function_type)
                seen_function_types.add(function_type)
            continue

        artifact_type = cast(ArtifactType, item)
        if artifact_type not in seen_artifact_types:
            enabled_types.append(artifact_type)
            seen_artifact_types.add(artifact_type)

    function_types: tuple[FunctionType, ...] | None = (
        tuple(selected_function_types) if selected_function_types else None
    )
    return tuple(enabled_types), function_types


def build_config(repo_root: Path, cli_args: CliArgs) -> DeployConfig:
    enabled_types = cli_args.enabled_types or tuple(
        cast_artifact_types(env_csv("POLY_DEPLOY_ENABLED_TYPES", DEFAULT_ENABLED_TYPES))
    )
    if cli_args.dynamic and "functions" not in enabled_types:
        raise DeployError(
            "push-dynamic requires functions in the effective resource selection; "
            "include functions in --types or POLY_DEPLOY_ENABLED_TYPES"
        )
    configured_orphan_excluded_contexts = env_csv(
        "POLY_DEPLOY_EXCLUDE_ORPHAN_CONTEXTS",
        env_csv("EXCLUDE_ORPHANS", ()),
    )
    orphan_excluded_contexts = tuple(
        _dedupe_contexts(
            (*MANDATORY_ORPHAN_EXCLUDED_CONTEXTS, *configured_orphan_excluded_contexts)
        )
    )
    remote_timestamp_fields = tuple(
        field
        for field in env_csv(
            "POLY_DEPLOY_REMOTE_TIMESTAMP_FIELDS",
            ("lastUpdatedAt", "updatedAt"),
        )
        if field != "createdAt"
    )
    return DeployConfig(
        repo_root=repo_root,
        namespace=required_namespace(),
        namespaces=required_namespaces(),
        enabled_types=enabled_types,
        skip_paths=tuple(
            resolve_path(repo_root, item)
            for item in env_csv("POLY_DEPLOY_SKIP_PATHS", ())
        ),
        priority_paths=tuple(
            resolve_path(repo_root, item)
            for item in env_csv("POLY_DEPLOY_PRIORITY_PATHS", ())
        ),
        function_discovery_globs=env_csv(
            "POLY_DEPLOY_FUNCTION_DISCOVERY_GLOBS", DEFAULT_FUNCTION_DISCOVERY_GLOBS
        ),
        variable_globs=env_csv("POLY_DEPLOY_VARIABLE_GLOBS", DEFAULT_VARIABLE_GLOBS),
        webhook_globs=env_csv("POLY_DEPLOY_WEBHOOK_GLOBS", DEFAULT_WEBHOOK_GLOBS),
        job_globs=env_csv("POLY_DEPLOY_JOB_GLOBS", DEFAULT_JOB_GLOBS),
        table_globs=env_csv("POLY_DEPLOY_TABLE_GLOBS", DEFAULT_TABLE_GLOBS),
        schema_globs=env_csv("POLY_DEPLOY_SCHEMA_GLOBS", DEFAULT_SCHEMA_GLOBS),
        trigger_globs=env_csv("POLY_DEPLOY_TRIGGER_GLOBS", DEFAULT_TRIGGER_GLOBS),
        use_mtime_guard=env_bool("POLY_DEPLOY_USE_MTIME_GUARD", True),
        remote_timestamp_fields=remote_timestamp_fields,
        update_existing_secret_variables=env_bool(
            "POLY_DEPLOY_UPDATE_EXISTING_SECRET_VARIABLES", False
        ),
        recreate_variables_on_secrecy_change=env_bool(
            "POLY_DEPLOY_RECREATE_VARIABLE_ON_SECRECY_CHANGE", True
        ),
        generate_after_function_sync=env_bool(
            "POLY_DEPLOY_GENERATE_AFTER_FUNCTION_SYNC", True
        ),
        generate_contexts=env_csv("POLY_DEPLOY_GENERATE_CONTEXTS", ()),
        generate_names=env_csv("POLY_DEPLOY_GENERATE_NAMES", ()),
        generate_ids=env_csv("POLY_DEPLOY_GENERATE_IDS", ()),
        orphan_excluded_contexts=orphan_excluded_contexts,
        initial_scope_prompt_enabled=env_bool("POLY_DEPLOY_INITIAL_SCOPE_PROMPT", True),
        initial_scope_state_file=resolve_path(
            repo_root,
            os.getenv(
                "POLY_DEPLOY_INITIAL_SCOPE_STATE_FILE", ".poly_deploy.state.json"
            ),
        ),
        http_timeout_seconds=env_int(
            "POLY_DEPLOY_HTTP_TIMEOUT_SECONDS",
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            min_value=1,
        ),
        context_path_mappings=parse_context_path_map(
            repo_root,
            os.getenv("STORK_CONTEXT_PATH_MAP"),
        ),
        otp_enabled=env_bool("OTP_ENABLED", False),
        server_function_images=parse_server_function_images(
            os.getenv("POLY_DEPLOY_SERVER_FUNCTION_IMAGES")
        ),
        server_function_scripts=parse_server_function_scripts(
            os.getenv("POLY_DEPLOY_SERVER_FUNCTION_SCRIPT")
        ),
    )


def resolve_path(repo_root: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve(strict=False)


def resolve_scope_filter(repo_root: Path, raw_scope: str) -> Path:
    normalized = raw_scope.strip()
    if not normalized:
        raise DeployError("Scope cannot be empty")

    if normalized.startswith("./"):
        normalized = normalized[2:]

    raw_path = Path(normalized)
    candidates: list[Path] = []
    if raw_path.is_absolute():
        candidates.append(raw_path.resolve(strict=False))
    else:
        candidates.append(resolve_path(repo_root, normalized))
        if not normalized.startswith("workflows/src/"):
            candidates.append(
                resolve_path(repo_root, Path("workflows/src") / normalized)
            )

    attempted: list[str] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        attempted.append(
            str(candidate.relative_to(repo_root))
            if is_relative_to(candidate, repo_root)
            else str(candidate)
        )
        if candidate.exists() and is_relative_to(candidate, repo_root):
            return candidate

    raise DeployError(
        f"Could not resolve scope {raw_scope!r}. Tried: {', '.join(attempted)}"
    )


def prompt_for_initial_scope(repo_root: Path) -> Path | None:
    prompt = (
        "First run: press Enter to sync the entire repo, "
        "or type a path like `sample_workflow` or `utilities/common`: "
    )
    while True:
        try:
            raw_scope = input(prompt).strip()
        except EOFError as exc:
            raise DeployError("Initial scope prompt cancelled") from exc
        except KeyboardInterrupt as exc:
            print()
            raise DeployError("Initial scope prompt cancelled") from exc

        if not raw_scope:
            return None

        try:
            return resolve_scope_filter(repo_root, raw_scope)
        except DeployError as exc:
            print(f"Invalid scope: {exc}")


def write_initial_scope_state(state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {"promptedAt": datetime.now().isoformat(timespec="seconds")},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def maybe_prompt_for_initial_scope(
    cli_args: CliArgs,
    config: DeployConfig,
    logger: Logger,
) -> CliArgs:
    if cli_args.skip_validation_acknowledged:
        logger.muted("Scope acknowledged non-interactively")
        return cli_args
    if cli_args.path_filters or (
        cli_args.seeds and config.enabled_types == ("functions",)
    ):
        return cli_args
    if not config.initial_scope_prompt_enabled:
        return cli_args
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return cli_args

    selected_scope = prompt_for_initial_scope(config.repo_root)
    if selected_scope is None:
        logger.muted("Initial scope set to full repository sync")
        return cli_args

    scoped_args = replace(cli_args, path_filters=(selected_scope,))
    logger.muted(
        "Initial scope set to " f"{selected_scope.relative_to(config.repo_root)}"
    )
    return scoped_args


def iter_preflight_json_patterns(config: DeployConfig) -> tuple[str, ...]:
    patterns: list[str] = []
    if "functions" in config.enabled_types:
        patterns.extend(
            pattern
            for pattern in config.function_discovery_globs
            if pattern.endswith(".json") or pattern.endswith(".jsonc")
        )
    if "variables" in config.enabled_types:
        patterns.extend(config.variable_globs)
    if "webhooks" in config.enabled_types:
        patterns.extend(config.webhook_globs)
    if "jobs" in config.enabled_types:
        patterns.extend(config.job_globs)
    if "tables" in config.enabled_types:
        patterns.extend(config.table_globs)
    if "schemas" in config.enabled_types:
        patterns.extend(config.schema_globs)
    if "triggers" in config.enabled_types:
        patterns.extend(config.trigger_globs)
    for kind in ("ai-functions", "canopy", "subscriptions"):
        if kind in config.enabled_types:
            patterns.extend(extra_resource_globs(kind))
    return tuple(dict.fromkeys(patterns))


def find_unresolved_env_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            tokens.update(find_unresolved_env_tokens(key))
            tokens.update(find_unresolved_env_tokens(item))
        return tokens
    if isinstance(value, list):
        for item in value:
            tokens.update(find_unresolved_env_tokens(item))
        return tokens
    if isinstance(value, str):
        tokens.update(
            match.group("name") for match in ENV_ALERT_PATTERN.finditer(value)
        )
    return tokens


def find_env_token_line_number(path: Path, env_name: str) -> int | None:
    token = f"{{{{{env_name}}}}}"
    try:
        for index, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if token in line:
                return index
    except OSError:
        return None
    return None


def collect_missing_env_token_refs(
    config: DeployConfig,
    cli_args: CliArgs,
) -> list[MissingEnvTokenRef]:
    refs: list[MissingEnvTokenRef] = []
    seen: set[tuple[str, Path]] = set()
    patterns = iter_preflight_json_patterns(config)
    if not patterns:
        return refs

    for path in discover_paths(
        config.repo_root,
        patterns,
        config.skip_paths,
        config.priority_paths,
    ):
        if path in cli_args.invalid_paths:
            continue
        if not matches_path_filters([path], cli_args.path_filters, config.repo_root):
            continue

        data = load_json_document(path)
        for env_name in sorted(find_unresolved_env_tokens(data)):
            if os.getenv(env_name) is not None:
                continue
            key = (env_name, path)
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                MissingEnvTokenRef(
                    env_name=env_name,
                    path=path,
                    line_number=find_env_token_line_number(path, env_name),
                )
            )
    return refs


def prompt_for_missing_env_tokens() -> bool:
    prompt = "Continue anyway and leave unresolved placeholders as-is? [y/N]: "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError as exc:
            raise DeployError("Missing environment variable prompt cancelled") from exc
        except KeyboardInterrupt as exc:
            print()
            raise DeployError("Missing environment variable prompt cancelled") from exc

        if answer in {"", "n", "no", "stop"}:
            return False
        if answer in {"y", "yes", "c", "continue"}:
            return True
        print("Please answer `y` to continue or `n` to stop.")


def maybe_prompt_for_missing_env_tokens(
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
) -> None:
    refs = collect_missing_env_token_refs(config, cli_args)
    if not refs:
        return

    grouped: dict[str, list[MissingEnvTokenRef]] = {}
    for ref in refs:
        grouped.setdefault(ref.env_name, []).append(ref)

    logger.info("")
    logger.info(multicolor_rule(enabled=logger.colors_enabled))
    logger.warn("Missing env-file values for deploy placeholders")
    logger.muted(
        "These {{ENV_NAME}} tokens appear in deployable JSON/JSONC artefacts but are not defined."
    )
    logger.muted(
        "Set them in the selected env file first, or continue and leave those placeholders unchanged."
    )
    logger.info("")

    for env_name in sorted(grouped):
        logger.info(
            style_text(
                f"  • {env_name}",
                rgb=YELLOW,
                bold=True,
                enabled=logger.colors_enabled,
            )
        )
        for ref in grouped[env_name]:
            file_ref = format_file_ref(
                ref.path, config.repo_root, enabled=logger.colors_enabled
            )
            if ref.line_number is None:
                logger.info(f"    {file_ref}")
            else:
                logger.info(f"    {file_ref}:{ref.line_number}")

    logger.info(multicolor_rule(enabled=logger.colors_enabled))

    if (
        cli_args.skip_validation_acknowledged
        or not sys.stdin.isatty()
        or not sys.stdout.isatty()
    ):
        missing = ", ".join(sorted(grouped))
        raise DeployError(
            "Missing env-file values for deploy placeholders in non-interactive mode: "
            f"{missing}"
        )

    if prompt_for_missing_env_tokens():
        logger.warn("Continuing with unresolved placeholders left as-is")
        return

    raise DeployError(
        "Stopped so you can configure the missing env-file values and try again"
    )


def normalize_cli_path_filters(repo_root: Path, cli_args: CliArgs) -> CliArgs:
    if not cli_args.path_filters:
        return cli_args
    normalized = tuple(
        resolve_scope_filter(repo_root, str(path_filter))
        for path_filter in cli_args.path_filters
    )
    return replace(cli_args, path_filters=normalized)


def path_is_excluded(path: Path, skip_paths: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(is_relative_to(resolved, skip_path) for skip_path in skip_paths)


def matches_path_filters(
    paths: list[Path], filters: tuple[Path, ...], repo_root: Path
) -> bool:
    if not filters:
        return True
    resolved_filters = [resolve_path(repo_root, item) for item in filters]
    for candidate in paths:
        resolved = candidate.resolve(strict=False)
        if any(
            is_relative_to(resolved, filter_path) for filter_path in resolved_filters
        ):
            return True
    return False


def is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def discover_paths(
    repo_root: Path,
    patterns: tuple[str, ...],
    skip_paths: tuple[Path, ...],
    priority_paths: tuple[Path, ...],
) -> list[Path]:
    discovered: set[Path] = set()
    expanded_patterns = set(patterns)
    expanded_patterns.update(
        pattern.replace("/artefacts/", "/artifacts/") for pattern in patterns
    )
    for pattern in sorted(expanded_patterns):
        expanded_pattern = (
            pattern if os.path.isabs(pattern) else str(repo_root / pattern)
        )
        for match in glob.glob(expanded_pattern, recursive=True):
            path = Path(match)
            if not path.is_file():
                continue
            if path_is_excluded(path, skip_paths):
                continue
            discovered.add(path.resolve())
    return sorted(
        discovered,
        key=lambda path: (path_priority_rank(path, priority_paths), str(path)),
    )


def path_priority_rank(path: Path, priority_paths: tuple[Path, ...]) -> int:
    resolved = path.resolve(strict=False)
    for index, priority_path in enumerate(priority_paths):
        if is_relative_to(resolved, priority_path):
            return index
    return len(priority_paths)


def load_json_document(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonc":
        raw = strip_json_comments(raw)
    data = json.loads(raw)
    data = substitute_env_tokens(data)
    if not isinstance(data, dict):
        raise DeployError(f"Expected a JSON object in {path}")
    return data


def strip_json_comments(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    string_delimiter = '"'
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == string_delimiter:
                in_string = False
            index += 1
            continue

        if char in {'"', "'"}:
            in_string = True
            string_delimiter = char
            result.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] != "\n":
                index += 1
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and not (
                text[index] == "*" and text[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue

        result.append(char)
        index += 1

    return "".join(result)


_NO_STRUCTURED_ENV_VALUE = object()


def substitute_env_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            substitute_env_tokens(key): substitute_env_tokens(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [substitute_env_tokens(item) for item in value]
    if isinstance(value, str):
        hydrated = maybe_hydrate_structured_env_token(value)
        if hydrated is not _NO_STRUCTURED_ENV_VALUE:
            return hydrated
        return ENV_TOKEN_PATTERN.sub(replace_env_token, value)
    return value


def maybe_hydrate_structured_env_token(value: str) -> Any:
    """When a string is exactly one `{{TOKEN}}` and the env value is a JSON array
    or object, return the parsed structure instead of embedding it as a quoted
    (double-encoded) string. Scalar env values (plain strings, UUIDs, numbers)
    are left untouched so they stay strings."""
    match = ENV_TOKEN_PATTERN.fullmatch(value.strip())
    if match is None:
        return _NO_STRUCTURED_ENV_VALUE
    raw = os.getenv(match.group("name"))
    if raw is None:
        return _NO_STRUCTURED_ENV_VALUE
    stripped = raw.strip()
    if not stripped or stripped[0] not in "[{":
        return _NO_STRUCTURED_ENV_VALUE
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return _NO_STRUCTURED_ENV_VALUE


def replace_env_token(match: re.Match[str]) -> str:
    env_name = match.group("name")
    return os.getenv(env_name, match.group(0))


def canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, str):
        return value.replace("\r\n", "\n").rstrip()
    return value


def project_remote_like_template(remote: Any, template: Any) -> Any:
    if isinstance(template, dict):
        if not isinstance(remote, dict):
            return remote
        projected: dict[str, Any] = {}
        for key, template_value in template.items():
            if key in remote:
                projected[key] = project_remote_like_template(
                    remote[key], template_value
                )
        return projected
    if isinstance(template, list):
        if not isinstance(remote, list):
            return remote
        if template and isinstance(template[0], dict):
            return [project_remote_like_template(item, template[0]) for item in remote]
        return remote
    return remote


def parse_iso_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).timestamp()


def pick_remote_timestamp(
    remote: dict[str, Any], fields: tuple[str, ...]
) -> tuple[float | None, str | None]:
    for field_name in fields:
        timestamp = parse_iso_timestamp(remote.get(field_name))
        if timestamp is not None:
            return timestamp, field_name
    return None, None


def can_update(
    *,
    local_timestamp: float,
    remote: dict[str, Any],
    config: DeployConfig,
    cli_args: CliArgs,
) -> tuple[bool, str | None]:
    if cli_args.force or not config.use_mtime_guard:
        return True, None
    remote_timestamp, field_name = pick_remote_timestamp(
        remote, config.remote_timestamp_fields
    )
    if remote_timestamp is None:
        return True, None
    if local_timestamp > remote_timestamp:
        return True, None
    local_dt = datetime.fromtimestamp(local_timestamp).isoformat(timespec="seconds")
    remote_dt = datetime.fromtimestamp(remote_timestamp).isoformat(timespec="seconds")
    return (
        False,
        f"local file is not newer than remote ({local_dt} <= {remote_dt} via {field_name})",
    )


_DEPLOY_RECEIPT_RE = re.compile(
    r"^# Poly deployed @ (?P<timestamp>\S+) - (?P<context_name>\S+) - (?P<url>\S+) - (?P<file_rev>[a-f0-9]{7})(?:\s+-\s+(?P<git_rev>[a-f0-9]{7}))?$"
)


def strip_deploy_receipt(code: str) -> str:
    lines = code.split("\n")
    if lines and _DEPLOY_RECEIPT_RE.match(lines[0].strip()):
        return "\n".join(lines[1:])
    return code


def load_function_specs(
    config: DeployConfig, cli_args: CliArgs, logger: Logger
) -> list[FunctionSpec]:
    specs: list[FunctionSpec] = []
    for path in discover_paths(
        config.repo_root,
        config.function_discovery_globs,
        config.skip_paths,
        config.priority_paths,
    ):
        if path in cli_args.invalid_paths:
            continue
        if not matches_path_filters([path], cli_args.path_filters, config.repo_root):
            continue
        if path.suffix in {".json", ".jsonc"}:
            specs.extend(load_function_specs_from_manifest(path))
            continue
        if path.suffix == ".py":
            specs.extend(
                load_function_specs_from_python(path, config.orphan_excluded_contexts)
            )
            continue
        specs.extend(load_function_specs_from_shell(path))

    deduped: dict[tuple[FunctionType, str, str], FunctionSpec] = {}
    for spec in specs:
        if spec.source_path in cli_args.invalid_paths:
            continue
        if path_is_excluded(spec.source_path, config.skip_paths):
            logger.detail(
                f"Skipping function source under excluded path: {spec.source_path}"
            )
            continue
        if context_is_orphan_excluded(spec.context, config.orphan_excluded_contexts):
            logger.detail(
                f"Skipping function in excluded context {spec.context}: {spec.source_path}"
            )
            continue
        if (
            cli_args.function_types
            and spec.function_type not in cli_args.function_types
        ):
            continue
        if not matches_path_filters(
            [spec.discovery_path, spec.source_path],
            cli_args.path_filters,
            config.repo_root,
        ):
            continue
        existing = deduped.get(spec.identity)
        if existing:
            raise DeployError(
                f"Duplicate function identity discovered for {spec.context}.{spec.name}: "
                f"{existing.discovery_path} and {spec.discovery_path}"
            )
        deduped[spec.identity] = spec
    return list(deduped.values())


def load_function_specs_from_manifest(path: Path) -> list[FunctionSpec]:
    data = load_json_document(path)
    items = data.get("functions", data)
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise DeployError(f"Expected a function manifest list in {path}")
    specs: list[FunctionSpec] = []
    for item in items:
        if not isinstance(item, dict):
            raise DeployError(f"Invalid function manifest entry in {path}")
        function_type = item.get("type")
        if function_type not in {"client", "server"}:
            raise DeployError(
                f"Function manifest entry in {path} needs type=client|server"
            )
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise DeployError(
                f"Function manifest entry in {path} needs a non-empty name"
            )
        context = item.get("context")
        if not isinstance(context, str) or not context.strip():
            raise DeployError(
                f"Function manifest entry in {path} needs a non-empty context"
            )
        raw_file = item.get("file")
        if not isinstance(raw_file, str) or not raw_file.strip():
            raise DeployError(
                f"Function manifest entry in {path} needs a non-empty file"
            )
        source_path = resolve_manifest_source(path, raw_file)
        specs.append(
            FunctionSpec(
                name=name,
                context=context,
                function_type=function_type,
                source_path=source_path,
                discovery_path=path,
                description=item.get("description") if "description" in item else None,
                description_explicit="description" in item,
                visibility=item.get("visibility"),
                logs_enabled=item.get("logsEnabled"),
                generate_contexts=tuple(
                    item.get("generateContexts", [])
                    if isinstance(item.get("generateContexts"), list)
                    else []
                ),
                execution_api_key=item.get("executionApiKey"),
            )
        )
    return specs


def function_config_generate_contexts(config: dict[str, Any]) -> tuple[str, ...]:
    raw_contexts = config.get("generate_contexts")
    if raw_contexts is None:
        raw_contexts = config.get("generateContexts")

    if isinstance(raw_contexts, str):
        values = raw_contexts.split(",")
    elif isinstance(raw_contexts, (list, tuple)):
        values = raw_contexts
    else:
        return ()

    return tuple(
        context for context in (str(value).strip() for value in values) if context
    )


def resolve_manifest_source(manifest_path: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (manifest_path.parent / path).resolve()
    return path


def trailing_backslash_count(value: str) -> int:
    count = 0
    for char in reversed(value):
        if char != "\\":
            break
        count += 1
    return count


def iter_shell_commands(path: Path) -> Iterator[tuple[int, str]]:
    buffer: list[str] = []
    start_line_number = 0

    for index, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw_line.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue

        line = raw_line.rstrip()
        continued = trailing_backslash_count(line) % 2 == 1
        if continued:
            line = line[:-1]

        segment = line.strip() if not buffer else line.lstrip()
        if segment:
            if not buffer:
                start_line_number = index
            buffer.append(segment)

        if continued:
            continue

        if buffer:
            yield start_line_number, " ".join(buffer)
            buffer = []
            start_line_number = 0

    if buffer:
        yield start_line_number, " ".join(buffer)


def load_function_specs_from_shell(path: Path) -> list[FunctionSpec]:
    specs: list[FunctionSpec] = []
    for index, command in iter_shell_commands(path):
        if "function add" not in command:
            continue
        spec = parse_function_spec_from_shell_line(path, command, index)
        if spec:
            specs.append(spec)
    return specs


def ast_annotation_name(annotation: ast.AST | None) -> str:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return ast_annotation_name(annotation.value)
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.rsplit(".", 1)[-1]
    return ""


def infer_function_type_from_path(path: Path) -> FunctionType | None:
    if path.name.endswith("_sfx.py"):
        return "server"
    if path.name.endswith("_cfx.py"):
        return "client"
    return None


def function_type_from_poly_config_annotation(
    annotation: ast.AST | None, path: Path
) -> FunctionType | None:
    annotation_name = ast_annotation_name(annotation)
    if annotation_name == "PolyServerFunction":
        return "server"
    if annotation_name == "PolyClientFunction":
        return "client"
    return infer_function_type_from_path(path)


def extract_poly_config_from_python(
    code: str, path: Path
) -> tuple[dict[str, Any], FunctionType] | None:
    try:
        module = ast.parse(code)
    except SyntaxError as exc:
        raise DeployError(f"Invalid Python function source: {path}") from exc

    for node in module.body:
        value: ast.AST | None = None
        annotation: ast.AST | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "polyConfig"
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "polyConfig"
        ):
            value = node.value
            annotation = node.annotation
        else:
            continue
        if value is None:
            raise DeployError(
                f"polyConfig must have a literal dictionary value: {path}"
            )

        try:
            config_value = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise DeployError(
                f"polyConfig must be a literal dictionary: {path}"
            ) from exc
        if not isinstance(config_value, dict):
            raise DeployError(f"polyConfig must be a dictionary: {path}")

        function_type = function_type_from_poly_config_annotation(annotation, path)
        if function_type is None:
            raise DeployError(f"Unable to determine polyConfig function type: {path}")

        return config_value, function_type
    return None


def load_function_specs_from_python(
    path: Path, excluded_contexts: tuple[str, ...] = ()
) -> list[FunctionSpec]:
    code = path.read_text(encoding="utf-8")
    if "polyConfig" not in code:
        return []
    metadata = extract_poly_config_from_python(code, path)
    if metadata is None:
        return []
    config, function_type = metadata
    name = config.get("name", "")
    context = config.get("context", "")
    if not isinstance(name, str) or not name.strip():
        raise DeployError(f"polyConfig must declare a non-empty function name: {path}")
    if not isinstance(context, str) or not context.strip():
        raise DeployError(
            f"polyConfig must declare a non-empty function context: {path}"
        )
    if context_is_orphan_excluded(context, excluded_contexts):
        return []
    description_explicit = "description" in config

    return [
        FunctionSpec(
            name=name,
            context=context,
            function_type=function_type,
            source_path=path,
            discovery_path=path,
            description=config.get("description") if description_explicit else None,
            description_explicit=description_explicit,
            visibility=config.get("visibility"),
            logs_enabled=config.get("logs_enabled"),
            generate_contexts=function_config_generate_contexts(config),
        )
    ]


def parse_function_spec_from_shell_line(
    script_path: Path,
    raw_line: str,
    line_number: int,
) -> FunctionSpec | None:
    line = raw_line
    for separator in ("|", ";", "&&"):
        if separator in line:
            line = line.split(separator, 1)[0].strip()
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        raise DeployError(
            f"Malformed shell syntax in {script_path}:{line_number}: {exc}"
        ) from exc
    command_prefixes = (
        ["python", "-m", "polyapi", "function", "add"],
        ["python3", "-m", "polyapi", "function", "add"],
    )
    if any(tokens[:5] == prefix for prefix in command_prefixes) and len(tokens) < 7:
        raise DeployError(
            f"Malformed function command in {script_path}:{line_number}. "
            "Expected `python -m polyapi function add <name> <file> --context <context> --client|--server`"
        )
    if len(tokens) < 7:
        return None
    prefix = tokens[:5]
    if prefix[0] not in {"python", "python3"} or prefix[1:5] != [
        "-m",
        "polyapi",
        "function",
        "add",
    ]:
        return None

    name = tokens[5]
    file_token = tokens[6]
    source_path = resolve_shell_source_path(script_path, file_token)
    context: str | None = None
    description: str | None = None
    description_explicit = False
    function_type: FunctionType | None = None
    logs_enabled: bool | None = None
    generate_contexts: tuple[str, ...] = ()
    visibility: str | None = None
    execution_api_key: str | None = None

    index = 7
    while index < len(tokens):
        token = tokens[index]
        value: str | None = None
        if "=" in token and token.startswith("--"):
            token, value = token.split("=", 1)
        elif token.startswith("--"):
            next_value = tokens[index + 1] if index + 1 < len(tokens) else None
            if next_value and not next_value.startswith("--"):
                value = next_value
                index += 1

        if token == "--context" and value:
            context = value
        elif token == "--description":
            description = value or ""
            description_explicit = True
        elif token == "--client":
            function_type = "client"
        elif token == "--server":
            function_type = "server"
        elif token == "--logs":
            logs_enabled = (value or "").lower() == "enabled"
        elif token == "--generate-contexts":
            generate_contexts = tuple(
                part.strip() for part in (value or "").split(",") if part.strip()
            )
        elif token == "--visibility" and value:
            visibility = value.upper()
        elif token == "--execution-api-key":
            execution_api_key = value
        index += 1

    if not context:
        raise DeployError(
            f"Missing --context for function command in {script_path}:{line_number}"
        )
    if function_type is None:
        raise DeployError(
            f"Missing --client/--server for function command in {script_path}:{line_number}"
        )
    if not source_path.exists():
        raise DeployError(
            f"Function source {source_path} referenced from {script_path}:{line_number} does not exist"
        )

    return FunctionSpec(
        name=name,
        context=context,
        function_type=function_type,
        source_path=source_path,
        discovery_path=script_path,
        description=description,
        description_explicit=description_explicit,
        visibility=visibility,
        logs_enabled=logs_enabled,
        generate_contexts=generate_contexts,
        execution_api_key=execution_api_key,
        line_number=line_number,
    )


def resolve_shell_source_path(script_path: Path, raw_path: str) -> Path:
    raw = Path(raw_path)
    if raw.is_absolute():
        return raw.resolve()

    candidates = [
        (script_path.parent / raw).resolve(),
        (script_path.parent.parent / raw).resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_artifact_files(
    config: DeployConfig,
    cli_args: CliArgs,
    patterns: tuple[str, ...],
) -> list[ArtifactFile]:
    items: list[ArtifactFile] = []
    for path in discover_paths(
        config.repo_root,
        patterns,
        config.skip_paths,
        config.priority_paths,
    ):
        if path in cli_args.invalid_paths:
            continue
        if not matches_path_filters([path], cli_args.path_filters, config.repo_root):
            continue
        items.append(ArtifactFile(path=path, data=load_json_document(path)))
    return items


def ensure_mapping(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeployError(f"Expected a JSON object in {path}")
    return value


def describe_environment(env_id: str, env_name: str) -> str:
    return f"{env_name} ({env_id})" if env_name else env_id


def fetch_remote_environment(client: PolyApiClient) -> tuple[str, str]:
    """Return the (id, name) of the environment the configured API key is bound to."""
    payload = client.request("GET", WHOAMI_PATH, expected=(200,))
    if not isinstance(payload, dict):
        raise DeployError(
            f"`GET {WHOAMI_PATH}` did not return an object, so the target "
            "environment could not be verified. Check that the configured "
            "PolyAPI host serves the API and not a web app."
        )

    env_id = payload.get("environmentId")
    if not isinstance(env_id, str) or not env_id:
        # An unauthenticated caller gets the empty whoami shape instead.
        raise DeployError(
            f"`GET {WHOAMI_PATH}` reported no environment, which means the "
            "configured PolyAPI key is not valid for this host. Re-run "
            "`python -m polyapi setup`."
        )

    env_name = payload.get("environmentName")
    return env_id, env_name if isinstance(env_name, str) else ""


def enforce_target_environment(
    client: PolyApiClient,
    target_env_id: str,
    env_file: EnvLoadResult,
    logger: Logger,
) -> None:
    """Refuse to deploy unless the env file is pinned to the live environment."""
    actual_env_id, actual_env_name = fetch_remote_environment(client)
    actual_label = describe_environment(actual_env_id, actual_env_name)
    if actual_env_id == target_env_id:
        logger.muted(f"target environment verified: {actual_label}")
        return
    raise DeployError(
        f"`{env_file.display_path}` is pinned to {TARGET_ENV_ID_VAR}="
        f"{target_env_id}, but the configured PolyAPI key points at "
        f"{actual_label}. Refusing to deploy. Either load the env file for "
        f"{actual_label}, or point the PolyAPI key at {target_env_id} "
        "(`python -m polyapi setup`)."
    )


def fetch_function_registry(client: PolyApiClient) -> FunctionRegistry:
    registry = FunctionRegistry()
    registry.load("server", client.request("GET", "/functions/server"))
    registry.load("client", client.request("GET", "/functions/client"))
    return registry


def reference_registry(
    client: PolyApiClient, owned: FunctionRegistry
) -> FunctionRegistry:
    registry = copy.deepcopy(owned)
    for suffix in ("tenant", "public"):
        for item in list_optional_scope_resources(
            client, f"/functions/server/{suffix}"
        ):
            existing = registry.get("server", item["context"], item["name"])
            if existing and existing["id"] != item["id"]:
                raise DeployError("Ambiguous accessible server-function reference")
            registry.upsert("server", item)
    return registry


def reference_webhooks(
    client: PolyApiClient, owned: dict[tuple[str, str], dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    index = dict(owned)
    for suffix in ("tenant", "public"):
        for item in list_optional_scope_resources(client, f"/webhooks/{suffix}"):
            key = (item["context"], item["name"])
            if key in index and index[key]["id"] != item["id"]:
                raise DeployError("Ambiguous accessible webhook reference")
            index[key] = item
    return index


def fetch_function_detail(
    client: PolyApiClient,
    function_type: FunctionType,
    function_id: str,
    cache: dict[tuple[FunctionType, str], dict[str, Any]],
) -> dict[str, Any]:
    key = (function_type, function_id)
    if key not in cache:
        cache[key] = client.request("GET", f"/functions/{function_type}/{function_id}")
    return cache[key]


class ImportStub:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> "ImportStub":
        return ImportStub(f"{self._name}.{attr}")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._name.endswith((".get", ".get_async")):
            return JsonValueStub(f"{self._name}()")
        if self._name.endswith((".inject", ".inject_async")):
            return "__FLOW_PLACEHOLDER__"
        if self._name.endswith((".items", ".keys", ".values")):
            return []
        return self

    def __getitem__(self, _: Any) -> "ImportStub":
        return self

    def __or__(self, _: Any) -> "ImportStub":
        return self

    def __ror__(self, _: Any) -> "ImportStub":
        return self

    def __iter__(self) -> Any:
        return iter(())

    def __bool__(self) -> bool:
        return False

    def __mro_entries__(self, bases: tuple[type, ...]) -> tuple[type, ...]:
        return (object,)

    def __repr__(self) -> str:
        return f"<ImportStub {self._name}>"


class JsonValueStub:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        if attr in {"items", "keys", "values"}:
            return lambda: []
        return JsonValueStub(f"{self._name}.{attr}")

    def __getitem__(self, key: Any) -> "JsonValueStub":
        return JsonValueStub(f"{self._name}[{key!r}]")

    def get(self, _key: Any, default: Any = None) -> Any:
        if default is not None:
            return default
        return JsonValueStub(f"{self._name}.get")

    def items(self) -> list[tuple[Any, Any]]:
        return []

    def keys(self) -> list[Any]:
        return []

    def values(self) -> list[Any]:
        return []

    def __iter__(self) -> Any:
        return iter(("__FLOW_PLACEHOLDER__",))

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 1

    def __repr__(self) -> str:
        return f"<JsonValueStub {self._name}>"


class PlaceholderModule(types.ModuleType):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.__dict__["__path__"] = []

    def __getattr__(self, attr: str) -> ImportStub:
        return ImportStub(f"{self.__name__}.{attr}")


PARSER_STUBBED_POLYAPI_IMPORTS = (
    *GENERATED_POLYAPI_NAMESPACES,
    "polyCustom",
)
_POLYAPI_IMPORT_ERROR_PATTERN = re.compile(
    r"cannot import name ['\"](?P<name>[a-zA-Z_][a-zA-Z0-9_]*)['\"] "
    r"from ['\"]polyapi['\"]"
)


def is_generated_polyapi_import(import_name: str) -> bool:
    return any(
        import_name == f"polyapi.{namespace}"
        or import_name.startswith(f"polyapi.{namespace}.")
        for namespace in PARSER_STUBBED_POLYAPI_IMPORTS
    )


def ensure_placeholder_module(
    module_name: str,
    created_modules: dict[str, types.ModuleType],
) -> types.ModuleType:
    parent: types.ModuleType | None = None
    qualified_name = ""
    for part in module_name.split("."):
        qualified_name = part if not qualified_name else f"{qualified_name}.{part}"
        module = sys.modules.get(qualified_name)
        if module is None:
            module = PlaceholderModule(qualified_name)
            sys.modules[qualified_name] = module
            created_modules[qualified_name] = module
        if parent is not None and part not in parent.__dict__:
            setattr(parent, part, module)
        parent = module
    assert parent is not None
    return parent


def should_retry_function_parse(exc: BaseException) -> bool:
    if isinstance(exc, ModuleNotFoundError):
        if exc.name:
            return is_generated_polyapi_import(exc.name)
        # The pinned SDK's fresh-clone loader raises without a module name.
        frames = traceback.extract_tb(exc.__traceback__)
        return bool(frames) and (
            frames[-1].filename == sys.modules["polyapi"].__file__
            and frames[-1].name == "_load_generated_poly"
        )
    if isinstance(exc, AttributeError):
        module = getattr(exc, "obj", None)
        return (
            isinstance(module, types.ModuleType)
            and (
                is_generated_polyapi_import(module.__name__)
                or module.__name__ == "polyapi"
                and exc.name in PARSER_STUBBED_POLYAPI_IMPORTS
            )
        ) or _traceback_contains_generated_polyapi_module(exc)
    if isinstance(exc, NameError):
        return _traceback_contains_generated_polyapi_module(exc)
    if not isinstance(exc, ImportError):
        return False

    match = _POLYAPI_IMPORT_ERROR_PATTERN.search(str(exc))
    return bool(match and match.group("name") in PARSER_STUBBED_POLYAPI_IMPORTS)


def _traceback_contains_generated_polyapi_module(exc: BaseException) -> bool:
    return any(
        "/polyapi/poly/" in frame.filename.replace("\\", "/")
        for frame in traceback.extract_tb(exc.__traceback__)
    )


def parse_function_code_with_import_fallback(
    code: str, name: str, context: str
) -> DeployableRecord:
    try:
        return parse_function_code(code, name, context)
    except BaseException as exc:
        if not should_retry_function_parse(exc):
            raise

        created_modules: dict[str, types.ModuleType] = {}
        builtins_module: Any = builtins
        json_module: Any = json
        original_import = builtins_module.__import__
        original_json_loads = json_module.loads
        try:
            real_polyapi = original_import("polyapi")
        except ModuleNotFoundError:
            real_polyapi = None

        polyapi_proxy = types.ModuleType("polyapi")
        if real_polyapi is not None:
            polyapi_proxy.__dict__.update(real_polyapi.__dict__)
        for namespace in PARSER_STUBBED_POLYAPI_IMPORTS:
            polyapi_proxy.__dict__[namespace] = ImportStub(f"polyapi.{namespace}")

        def guarded_import(
            import_name: str,
            globals_dict: dict[str, Any] | None = None,
            locals_dict: dict[str, Any] | None = None,
            fromlist: tuple[str, ...] | list[str] = (),
            level: int = 0,
        ) -> Any:
            if import_name == "polyapi":
                return polyapi_proxy
            if is_generated_polyapi_import(import_name):
                return ensure_placeholder_module(import_name, created_modules)
            return original_import(
                import_name, globals_dict, locals_dict, fromlist, level
            )

        def guarded_json_loads(value: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(value, (ImportStub, JsonValueStub)):
                return JsonValueStub("json.loads")
            if value == "__FLOW_PLACEHOLDER__":
                return JsonValueStub("json.loads")
            return original_json_loads(value, *args, **kwargs)

        builtins_module.__import__ = guarded_import
        json_module.loads = guarded_json_loads
        try:
            return parse_function_code(code, name, context)
        finally:
            builtins_module.__import__ = original_import
            json_module.loads = original_json_loads
            for module_name, module in reversed(list(created_modules.items())):
                if sys.modules.get(module_name) is module:
                    sys.modules.pop(module_name, None)


def collect_client_requirements(code: str) -> set[str]:
    """Resolve client imports without executing source or importing its modules."""
    tree = ast.parse(code)
    pip_name_lookup = packages_distributions()
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str | None]
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module]
        else:
            continue
        for module in modules:
            # Use the pinned SDK's base-image exclusions and distribution names.
            requirement = _get_req_name_if_not_in_base(module, pip_name_lookup)
            if requirement:
                dependencies.add(requirement)
    return dependencies


def build_function_payload(
    spec: FunctionSpec,
    remote_func_context: dict | None = None,
    excluded_contexts: tuple[str, ...] = (),
) -> dict[str, Any]:
    code = strip_deploy_receipt(spec.source_path.read_text(encoding="utf-8"))
    parsed = parse_function_code_with_import_fallback(code, spec.name, spec.context)
    return_type = parsed["types"]["returns"]["type"]
    if not return_type:
        raise DeployError(f"Could not determine return type for {spec.source_path}")
    return_type_schema = parsed["types"]["returns"].get("typeSchema")

    if spec.description_explicit:
        description = spec.description if spec.description is not None else ""
    else:
        description = parsed["types"]["description"] or parsed["description"]

    payload: dict[str, Any] = {
        "context": spec.context or parsed["context"],
        "name": spec.name,
        "description": description,
        "code": code,
        "language": "python",
        "visibility": spec.visibility
        or parsed["config"].get("visibility")
        or "ENVIRONMENT",
        "returnType": get_jsonschema_type(return_type),
        "arguments": [
            {
                **argument,
                "key": argument["name"],
                "type": get_jsonschema_type(argument["type"]),
            }
            for argument in parsed["types"]["params"]
        ],
    }
    if return_type_schema is not None:
        payload["returnTypeSchema"] = return_type_schema

    logs_enabled = spec.logs_enabled
    if logs_enabled is None:
        logs_enabled = parsed["config"].get("logs_enabled")
    if logs_enabled is not None and spec.function_type == "server":
        payload["logsEnabled"] = logs_enabled

    if spec.function_type == "server":
        generate_contexts = resolve_server_generate_contexts(
            spec,
            code,
            remote_func_context,
            excluded_contexts,
        )
        if generate_contexts is None or generate_contexts:
            # An explicit empty list clears an existing context filter when a
            # root-level SDK function requires unfiltered generation.
            payload["generateContexts"] = generate_contexts or []

    if spec.execution_api_key and spec.function_type == "server":
        payload["executionApiKey"] = spec.execution_api_key

    if spec.function_type == "server":
        dependencies = set(parsed.get("dependencies", []))
        current_context = current_function_context(
            spec, code, remote_func_context, excluded_contexts
        )
        for _, node in reachable_client_contexts(
            f"{spec.context}.{spec.name}", current_context, excluded_contexts
        ):
            if node.get("kind") != "client" or "code" not in node:
                continue
            dependencies.update(collect_client_requirements(node["code"]))
        if dependencies:
            payload["requirements"] = sorted(dependencies)

    return payload


def apply_server_function_image_override(
    payload: dict[str, Any],
    spec: FunctionSpec,
    config: DeployConfig,
    logger: Logger,
) -> dict[str, Any]:
    if spec.function_type != "server":
        return payload

    function_key = f"{spec.context}.{spec.name}"
    image_name = config.server_function_images.get(function_key)
    if image_name is None:
        return payload

    logger.info(f"image override: {function_key} -> {image_name}")
    overridden_payload = dict(payload)
    overridden_payload["image"] = image_name
    return overridden_payload


def run_server_function_post_deploy_script(
    *,
    function_id: str,
    spec: FunctionSpec,
    config: DeployConfig,
    logger: Logger,
) -> None:
    if spec.function_type != "server":
        return

    function_key = f"{spec.context}.{spec.name}"
    command = config.server_function_scripts.get(function_key)
    if command is None:
        return

    service_name = f"function-{function_id}"
    rendered_command = command.replace("<id>", function_id)
    logger.info(f"post-deploy script: {function_key} -> {rendered_command}")

    env = os.environ.copy()
    env["FUNCTION_ID"] = function_id
    env["SERVICE_NAME"] = service_name

    interactive_terminal = (
        sys.stdin.isatty() and sys.stderr.isatty() and not logger.machine_readable
    )
    run_kwargs: dict[str, Any] = {
        "cwd": config.repo_root,
        "env": env,
        "text": True,
        "check": False,
    }
    if not interactive_terminal:
        run_kwargs["capture_output"] = True

    completed = subprocess.run(
        ["/bin/bash", "-lc", rendered_command],
        **run_kwargs,
    )
    if not interactive_terminal:
        if completed.stdout:
            for line in completed.stdout.splitlines():
                logger.detail(f"script stdout: {line}")
        if completed.stderr:
            for line in completed.stderr.splitlines():
                logger.detail(f"script stderr: {line}")
    if completed.returncode != 0:
        raise DeployError(
            "Post-deploy script failed for "
            f"{function_key} with exit code {completed.returncode}: {rendered_command}"
        )


def normalize_function_payload(
    payload: dict[str, Any], *, include_description: bool = True
) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    normalized.pop("executionApiKey", None)
    if not include_description:
        normalized.pop("description", None)
    if "arguments" in normalized:
        normalized["arguments"] = normalize_function_arguments(normalized["arguments"])
    if "generateContexts" in normalized:
        normalized["generateContexts"] = sorted(normalized["generateContexts"] or [])
    if "requirements" in normalized:
        normalized["requirements"] = sorted(normalized["requirements"] or [])
    return canonicalize(normalized)


def payload_resume_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_function_subset(
    payload: dict[str, Any], remote: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_normalized = normalize_function_payload(payload)
    remote_normalized = normalize_function_payload(
        project_remote_like_template(remote, payload)
    )
    local_normalized, remote_normalized = normalize_description_fields_for_compare(
        local_normalized, remote_normalized
    )
    normalize_unreported_function_fields_in_place(local_normalized, remote_normalized)
    return local_normalized, remote_normalized


def normalize_unreported_function_fields_in_place(
    local: dict[str, Any], remote: dict[str, Any]
) -> None:
    if local.get("requirements") and remote.get("requirements") in (None, []):
        local.pop("requirements", None)
        remote.pop("requirements", None)


def normalize_function_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, list):
        return arguments
    return [
        (
            normalize_function_argument(argument)
            if isinstance(argument, dict)
            else argument
        )
        for argument in arguments
    ]


def normalize_function_argument(argument: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(argument)
    if normalized.get("description") in (None, ""):
        normalized.pop("description", None)
    if normalized.get("typeSchema") is None:
        normalized.pop("typeSchema", None)
    return normalized


def normalize_description_fields_for_compare(
    local: Any, remote: Any
) -> tuple[Any, Any]:
    local_normalized = copy.deepcopy(local)
    remote_normalized = copy.deepcopy(remote)
    normalize_description_fields_in_place(local_normalized, remote_normalized)
    return local_normalized, remote_normalized


def normalize_description_fields_in_place(local: Any, remote: Any) -> None:
    if isinstance(local, dict) and isinstance(remote, dict):
        if local.get("description") in (None, ""):
            local.pop("description", None)
            remote.pop("description", None)
        for key in sorted(set(local) & set(remote)):
            if key == "value":
                continue
            normalize_description_fields_in_place(local[key], remote[key])
        return
    if isinstance(local, list) and isinstance(remote, list):
        for local_item, remote_item in zip(local, remote):
            normalize_description_fields_in_place(local_item, remote_item)


def summarize_function_payload_diff(
    local: dict[str, Any], remote: dict[str, Any]
) -> list[str]:
    differences: list[str] = []
    for key in sorted(set(local) | set(remote)):
        if key not in local:
            differences.append(f"{key}: remote-only")
            continue
        if key not in remote:
            differences.append(f"{key}: local-only")
            continue
        if local[key] == remote[key]:
            continue
        differences.append(f"{key}: {summarize_value_diff(local[key], remote[key])}")
    return differences


def summarize_value_diff(local: Any, remote: Any) -> str:
    if isinstance(local, str) and isinstance(remote, str):
        local_lines = local.splitlines()
        remote_lines = remote.splitlines()
        first_diff_line = next(
            (
                index
                for index, pair in enumerate(
                    itertools.zip_longest(local_lines, remote_lines), start=1
                )
                if pair[0] != pair[1]
            ),
            None,
        )
        line_part = (
            f", first differing line {first_diff_line}" if first_diff_line else ""
        )
        return (
            f"string changed "
            f"(local {len(local)} chars/{len(local_lines)} lines, "
            f"remote {len(remote)} chars/{len(remote_lines)} lines{line_part})"
        )
    if isinstance(local, list) and isinstance(remote, list):
        return summarize_list_diff(local, remote)
    if isinstance(local, dict) and isinstance(remote, dict):
        changed_keys = sorted(
            key for key in set(local) | set(remote) if local.get(key) != remote.get(key)
        )
        return f"object keys changed: {', '.join(changed_keys[:8])}"
    if isinstance(local, (bool, int, float)) and isinstance(remote, (bool, int, float)):
        return f"value changed (local {local!r}, remote {remote!r})"
    return f"{type(remote).__name__} -> {type(local).__name__}"


def summarize_list_diff(local: list[Any], remote: list[Any]) -> str:
    if all(isinstance(item, dict) for item in local + remote):
        local_names = [
            str(item.get("name") or item.get("key") or "?") for item in local
        ]
        remote_names = [
            str(item.get("name") or item.get("key") or "?") for item in remote
        ]
        if local_names != remote_names:
            return f"list entries changed: local={local_names}, remote={remote_names}"
        changed_entries = [
            summarize_named_dict_diff(name, local_item, remote_item)
            for name, local_item, remote_item in zip(local_names, local, remote)
            if local_item != remote_item
        ]
        if changed_entries:
            return f"list item fields changed: {'; '.join(changed_entries[:8])}"
    return f"list changed (local {len(local)} items, remote {len(remote)} items)"


def summarize_named_dict_diff(
    name: str, local: dict[str, Any], remote: dict[str, Any]
) -> str:
    field_summaries: list[str] = []
    for key in sorted(set(local) | set(remote)):
        if key not in local:
            field_summaries.append(f"{key}=remote-only")
            continue
        if key not in remote:
            field_summaries.append(f"{key}=local-only")
            continue
        if local[key] == remote[key]:
            continue
        field_summaries.append(f"{key}={summarize_value_diff(local[key], remote[key])}")
    return f"{name}({', '.join(field_summaries[:8])})"


def should_skip_unchanged(
    cli_args: CliArgs,
    local_normalized: Any,
    remote_normalized: Any,
) -> bool:
    return not cli_args.force and local_normalized == remote_normalized


def log_forced_unchanged_update(
    logger: Logger,
    cli_args: CliArgs,
    local_normalized: Any,
    remote_normalized: Any,
) -> None:
    if cli_args.force and local_normalized == remote_normalized:
        logger.detail("diff: none (forced update)")


@dataclass
class DynamicFunctionPlan:
    specs: dict[str, FunctionSpec]
    payloads: dict[str, dict[str, Any]]
    registry: FunctionRegistry
    order: list[str]
    seeds: set[str]
    unchanged: int
    graph: FunctionGraph
    cycles: list[list[str]]
    seed_from: str
    offline: bool


SDK_ASYNC_FUNCTION_TYPES = {"serverFunction", "apiFunction", "aiFunction"}
SDK_EXTERNAL_FUNCTION_TYPES = SDK_ASYNC_FUNCTION_TYPES | {
    "authFunction",
    "webhookHandle",
}
SDK_AUTH_ASYNC_NAMES = {"introspectToken", "refreshToken", "revokeToken"}


def index_sdk_function_specs(catalog: list, *, source: str) -> dict[str, dict]:
    """Map effective SDK callable exports to their declaring specifications."""
    from polyapi.generate import parse_function_specs
    from polyapi.typedefs import SpecificationDto

    if any(
        not isinstance(item, dict) or not isinstance(item.get("type"), str)
        for item in catalog
    ):
        raise DeployError(f"Invalid {source} entry")
    try:
        functions = parse_function_specs(cast(list[SpecificationDto], catalog))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DeployError(f"Invalid {source} function specification") from None
    result: dict[str, dict] = {}
    for item in functions:
        if (
            not isinstance(item.get("context"), str)
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise DeployError(f"Invalid function identity in {source}")
        # Native generation emits definitions in catalog order. getToken is
        # already async with no suffixed alias; other auth helpers export both.
        # Clients and webhook listeners retain their literal declared names.
        key = f"{item['context']}.{item['name']}" if item["context"] else item["name"]
        result[key] = dict(item)
        if item["type"] in SDK_ASYNC_FUNCTION_TYPES or (
            item["type"] == "authFunction" and item["name"] in SDK_AUTH_ASYNC_NAMES
        ):
            result[f"{key}_async"] = result[key]
    return result


def read_cached_function_specs() -> dict[tuple[str, str], dict]:
    # SDK 0.3.20 uses context/name/code, unlike other SDKs' cache formats.
    from polyapi.generate import read_cached_specs

    try:
        cached = read_cached_specs()
    except (OSError, ValueError):
        raise DeployError(
            "No readable native SDK specs cache; run python -m polyapi generate or use --seed-from remote"
        ) from None
    if not isinstance(cached, list):
        raise DeployError("Invalid native SDK specs cache: expected a list")
    result = {}
    for key, item in index_sdk_function_specs(
        cached, source="native SDK specs cache"
    ).items():
        kind = {"customFunction": "client", "serverFunction": "server"}.get(
            item["type"]
        )
        declared_key = (
            f"{item['context']}.{item['name']}" if item["context"] else item["name"]
        )
        if kind is None or key != declared_key:
            continue
        result[(kind, key)] = item
    return result


def cached_function_changed(spec: FunctionSpec, code: str, cached: dict) -> bool:
    item = cached.get((spec.function_type, f"{spec.context}.{spec.name}"), {})
    cached_code = item.get("code")
    if not isinstance(cached_code, str) or not cached_code.strip():
        return True  # Filtered caches and server specs may omit implementation code.

    def normalize(value: str) -> str:
        return strip_deploy_receipt(value).replace("\r\n", "\n").rstrip()

    return normalize(code) != normalize(cached_code)


def load_function_graph(
    config: DeployConfig, cli_args: CliArgs, logger: Logger
) -> tuple[dict[str, FunctionSpec], dict[str, tuple[FunctionType, str]], FunctionGraph]:
    all_args = replace(cli_args, path_filters=(), function_types=None, invalid_paths=())
    all_config = replace(config, skip_paths=(), orphan_excluded_contexts=())
    specs = load_function_specs(all_config, all_args, logger)
    by_key: dict[str, FunctionSpec] = {}
    sources: dict[str, tuple[FunctionType, str]] = {}
    for spec in specs:
        key = f"{spec.context}.{spec.name}"
        if key in by_key:
            raise DeployError(f"Ambiguous client/server identity: {key}")
        by_key[key] = spec
        sources[key] = (
            spec.function_type,
            strip_deploy_receipt(spec.source_path.read_text(encoding="utf-8")),
        )
    try:
        graph = FunctionGraph.build(sources)
    except (GraphError, SyntaxError) as error:
        raise DeployError(
            f"Cannot build Python dependency graph ({type(error).__name__})"
        ) from None
    return by_key, sources, graph


def dynamic_plan_report(
    plan: DynamicFunctionPlan, config: DeployConfig, cli_args: CliArgs
) -> dict[str, Any]:
    graph = plan.graph
    reached = graph.closure(plan.seeds, through_servers=cli_args.through_sfx)
    report: dict[str, Any] = {
        "seedFrom": plan.seed_from,
        "offline": plan.offline,
        "comparison": (
            "cached-code"
            if plan.seed_from == "specs"
            else "live-payload" if not plan.offline else "forced-only"
        ),
        "seeds": sorted(plan.seeds),
        "order": plan.order,
        "cycles": plan.cycles,
        "graph": {
            "functions": len(graph.kinds),
            "edges": sum(map(len, graph.dependencies.values())),
        },
        "nodes": [
            {
                "key": key,
                "type": graph.kinds[key],
                "deploy": key in plan.order,
                "reason": (
                    "seed"
                    if key in plan.seeds
                    else "consumer" if key in plan.order else "report-only"
                ),
                "via": sorted(graph.dependencies[key] & reached),
                "file": relative_label(plan.specs[key].source_path, config.repo_root),
            }
            for key in sorted(reached)
        ],
    }
    if plan.seed_from == "specs":
        report["cacheEnvironment"] = "unknown"
        report["notes"] = [
            "Native SDK cache compares code only; missing functions or source code are treated as changed. Live target and payload checks still apply before deployment."
        ]
    if cli_args.why:
        if cli_args.why not in graph.kinds:
            raise DeployError(f"Unknown local function: {cli_args.why}")
        chains, truncated = graph.explain(
            plan.seeds, cli_args.why, through_servers=cli_args.through_sfx
        )
        report.update(target=cli_args.why, chains=chains, truncated=truncated)
    return report


def print_dynamic_report(
    report: dict[str, Any], cli_args: CliArgs, logger: Logger
) -> None:
    if "hubs" in report:
        for hub in report["hubs"]:
            logger.info(
                f"{hub['key']}: {hub['clientCallers']} client callers, {hub['serverLeaves']} server leaves"
            )
    elif cli_args.why:
        for chain in report["chains"]:
            logger.info(" -> ".join(chain))
        if not report["chains"]:
            logger.info(f"{cli_args.why}: no path from the selected seeds")
        if report["truncated"]:
            logger.warn("Additional paths omitted (limit 25)")
    elif cli_args.through_sfx:
        for node in report["nodes"]:
            if not node["deploy"]:
                logger.info(
                    f"{node['key']} [{node['type']}] report-only; via {', '.join(node['via'])}"
                )


def function_resume_key(spec: FunctionSpec) -> str:
    return f"fn:{spec.function_type}:{spec.context}:{spec.name}"


def plan_dynamic_functions(
    client: PolyApiClient | None,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    resume_state: ResumeState,
) -> DynamicFunctionPlan:
    """Prepare the complete function tree before any deployment write."""
    by_key, sources, graph = load_function_graph(config, cli_args, logger)
    specs = list(by_key.values())
    seed_from = cli_args.seed_from or (
        "remote" if not cli_args.seeds or cli_args.path_filters else "none"
    )
    if client is None and seed_from == "remote":
        raise DeployError("Live comparison requires a verified target")
    cached = read_cached_function_specs() if seed_from == "specs" else {}
    if seed_from == "specs":
        logger.warn(
            "Native SDK specs cache environment is unknown; comparing code only. Missing functions or source code are treated as changed."
        )

    eligible = {
        key
        for key, spec in by_key.items()
        if not path_is_excluded(spec.source_path, config.skip_paths)
        and not path_is_excluded(spec.discovery_path, config.skip_paths)
        and not context_is_orphan_excluded(
            spec.context, config.orphan_excluded_contexts
        )
        and spec.source_path not in cli_args.invalid_paths
        and spec.discovery_path not in cli_args.invalid_paths
    }
    selected = {
        f"{spec.context}.{spec.name}"
        for spec in load_function_specs(config, cli_args, logger)
    }
    forced: set[str] = set()
    for raw in cli_args.seeds:
        if raw in by_key:
            matches = {raw}
        else:
            scope = resolve_scope_filter(config.repo_root, raw)
            matches = {
                key
                for key, spec in by_key.items()
                if matches_path_filters(
                    [spec.source_path, spec.discovery_path], (scope,), config.repo_root
                )
            }
        if not matches:
            raise DeployError(f"No local function matches seed: {raw}")
        forced.update(matches)
    pending_keys = {function_resume_key(spec): key for key, spec in by_key.items()}
    if resume_state.pending_functions - pending_keys.keys():
        raise DeployError(
            "Pending function identities are no longer present; restore them before resuming"
        )
    forced.update(pending_keys[key] for key in resume_state.pending_functions)
    detection = selected if seed_from != "none" else set()
    if cli_args.force:
        forced.update(detection)

    registry = (
        fetch_function_registry(client) if client is not None else FunctionRegistry()
    )
    detail_cache: dict[tuple[FunctionType, str], dict[str, Any]] = {}
    contexts = build_local_func_context(specs, config.orphan_excluded_contexts)
    if client is not None:
        planned_exports: dict[str, dict[str, str]] = {"tabi": {}, "vari": {}}
        for kind, namespace, patterns in (
            ("tables", "tabi", config.table_globs),
            ("variables", "vari", config.variable_globs),
        ):
            if kind not in config.enabled_types:
                continue
            for artifact in load_artifact_files(config, cli_args, patterns):
                data = ensure_mapping(artifact.data, artifact.path)
                context = data.get("context") or ""
                if context_is_orphan_excluded(context, config.orphan_excluded_contexts):
                    continue
                require_owned_context(context, config)
                owner = f"{context}.{data['name']}"
                planned_exports[namespace][owner] = owner
        contexts = build_remote_func_context(
            client,
            config.orphan_excluded_contexts,
            local_func_context=contexts,
            planned_resource_exports=planned_exports,
        )
    payloads: dict[str, dict[str, Any]] = {}
    changed: set[str] = set()
    execution_key = get_execution_api_key() if client is not None else None
    compared: set[str] = set()

    def compare(key: str) -> bool:
        if key in compared:
            return key in changed
        spec = by_key[key]
        if key not in eligible:
            raise DeployError(
                f"Dependency plan requires excluded function {key}; no functions deployed"
            )
        require_owned_context(spec.context, config)
        if client is None and seed_from != "specs":
            return False
        compared.add(key)
        if client is None:
            if seed_from == "specs" and cached_function_changed(
                spec, sources[key][1], cached
            ):
                changed.add(key)
            return key in changed
        if execution_key and spec.function_type == "server":
            spec = replace(spec, execution_api_key=execution_key)
        try:
            payload = apply_server_function_image_override(
                build_function_payload(spec, contexts, config.orphan_excluded_contexts),
                spec,
                config,
                logger,
            )
            validate_endpoint_payload(
                (
                    "CreateServerCustomFunctionDto"
                    if spec.function_type == "server"
                    else "CreateClientCustomFunctionDto"
                ),
                payload,
            )
        except (
            DeployError,
            ImportError,
            ValueError,
            TypeError,
            KeyError,
            OSError,
            SyntaxError,
        ) as error:
            raise DeployError(
                f"Invalid dependency function {key} ({type(error).__name__})"
            ) from None
        payloads[key] = payload
        existing = registry.get(spec.function_type, spec.context, spec.name)
        if existing:
            detail = fetch_function_detail(
                client, spec.function_type, existing["id"], detail_cache
            )
            local, remote = normalize_function_subset(payload, detail)
            differs = local != remote
            if differs:
                allowed, _ = can_update(
                    local_timestamp=spec.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    raise DeployError(
                        f"Dependency plan blocked by newer remote function: {key}"
                    )
        else:
            differs = True
        seed_changed = (
            cached_function_changed(spec, sources[key][1], cached)
            if seed_from == "specs"
            else differs
        )
        if seed_changed:
            changed.add(key)
        return seed_changed

    for key in sorted(detection | forced):
        compare(key)
    seeds = changed | forced
    # An out-of-scope intermediate may have its own edits and other consumers.
    # Promote it to a seed and repeat until every newly affected branch is planned.
    while True:
        closure = graph.closure(seeds)
        for key in sorted(closure):
            compare(key)
        promoted = {key for key in closure & changed if graph.kinds[key] == "client"}
        if promoted <= seeds:
            break
        seeds |= promoted

    order, cycles = graph.order(closure)
    if cli_args.skip_unchanged_cfx:
        if any(key not in compared and graph.kinds[key] == "client" for key in order):
            logger.warn(
                "Client functions were not compared; retaining them in the offline plan."
            )
        order = [
            key
            for key in order
            if key in seeds or graph.kinds[key] == "server" or key not in compared
        ]
    logger.section("FUNCTION DEPENDENCY PLAN")
    logger.info(
        f"{len(sources)} local functions; {len(seeds)} seeds; {len(order)} planned deployments"
    )
    for group in cycles:
        logger.warn("Dependency cycle (stable order within cycle): " + ", ".join(group))
    for key in sorted(graph.dynamic & closure):
        logger.warn(
            f"{key}: namespace/dynamic access includes all matching local functions"
        )
    for index, key in enumerate(order, 1):
        reason = (
            "seed"
            if key in seeds
            else "consumer of " + ", ".join(sorted(graph.dependencies[key] & closure))
        )
        scope_note = (
            " (outside --paths)"
            if cli_args.path_filters and key not in selected
            else ""
        )
        logger.info(f"{index}. {key} [{graph.kinds[key]}] {reason}{scope_note}")
    return DynamicFunctionPlan(
        by_key,
        payloads,
        registry,
        order,
        seeds,
        len(detection - set(order)),
        graph,
        cycles,
        seed_from,
        client is None,
    )


def confirm_dynamic_plan(
    plan: DynamicFunctionPlan, config: DeployConfig, cli_args: CliArgs
) -> None:
    if cli_args.dry_run or cli_args.yes or cli_args.skip_validation_acknowledged:
        return
    if (
        not plan.order
        and config.enabled_types == ("functions",)
        and not cli_args.delete_orphans
    ):
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DeployError(
            "Review --dry-run, then use --yes to accept the dependency plan non-interactively"
        )
    try:
        answer = (
            input(
                f"Deploy {len(plan.order)} planned function(s) and sync other selected resources? [y/N]: "
            )
            .strip()
            .lower()
        )
        if answer not in {"y", "yes"}:
            raise DeployError("Dependency deployment cancelled; nothing was deployed")
        leaves = sum(plan.specs[key].function_type == "server" for key in plan.order)
        if leaves > cli_args.max_leaves:
            if input(
                f"Type {leaves} to confirm rebuilding {leaves} server functions: "
            ).strip() != str(leaves):
                raise DeployError(
                    "Dependency deployment cancelled; nothing was deployed"
                )
    except EOFError:
        raise DeployError(
            "Dependency deployment cancelled; nothing was deployed"
        ) from None


def execute_dynamic_functions(
    plan: DynamicFunctionPlan,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    resume_state: ResumeState,
) -> tuple[FunctionRegistry, bool]:
    if plan.offline:
        raise DeployError(
            "Offline reports cannot be deployed; rerun against a verified target"
        )
    stats = summary.functions
    stats.skipped += plan.unchanged
    if cli_args.dry_run:
        for key in plan.order:
            spec = plan.specs[key]
            existing = plan.registry.get(spec.function_type, spec.context, spec.name)
            if existing:
                stats.updated += 1
            else:
                stats.created += 1
                plan.registry.upsert(
                    spec.function_type,
                    {
                        "id": f"planned:{spec.function_type}:{key}",
                        "context": spec.context,
                        "name": spec.name,
                    },
                )
        return plan.registry, bool(plan.order)
    if "tables" in config.enabled_types and any(
        plan.specs[key].function_type == "server" for key in plan.order
    ):
        # Table synchronization can create or change native generated schemas
        # after planning. Recheck the prepared scopes before any function writes.
        contexts = build_remote_func_context(
            client,
            config.orphan_excluded_contexts,
            local_func_context=build_local_func_context(
                list(plan.specs.values()), config.orphan_excluded_contexts
            ),
        )
        for key in plan.order:
            if plan.specs[key].function_type == "server":
                validate_table_generation(
                    [
                        node
                        for _, node in reachable_client_contexts(
                            key, contexts, config.orphan_excluded_contexts
                        )
                    ],
                    plan.payloads[key].get("generateContexts"),
                )
    resume_state.plan_functions(
        {function_resume_key(plan.specs[key]) for key in plan.order}
    )
    for index, key in enumerate(plan.order):
        spec = plan.specs[key]
        payload = plan.payloads[key]
        existing = plan.registry.get(spec.function_type, spec.context, spec.name)
        try:
            result = client.request(
                "POST",
                f"/functions/{spec.function_type}",
                payload=payload,
                expected=(200, 201),
            )
            plan.registry.upsert(
                spec.function_type,
                {"id": result["id"], "context": spec.context, "name": spec.name},
            )
            run_server_function_post_deploy_script(
                function_id=result["id"], spec=spec, config=config, logger=logger
            )
            resume_state.mark_done(
                function_resume_key(spec),
                payload_resume_fingerprint(normalize_function_payload(payload)),
            )
        except (DeployError, OSError, KeyError, TypeError):
            stats.failed += 1
            logger.fail(
                f"Deployment stopped at {key}; remaining consumers were not deployed. Retry push-dynamic with --resume and the same target."
            )
            logger.info(f"{len(plan.order) - index - 1} remaining function(s) not run")
            return plan.registry, index > 0
        if existing:
            stats.updated += 1
        else:
            stats.created += 1
        logger.ok(f"Deployed {key}")
    return plan.registry, bool(plan.order)


def sync_functions(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    resume_state: ResumeState,
    dynamic_plan: DynamicFunctionPlan | None = None,
) -> tuple[FunctionRegistry, bool]:
    if resume_state.pending_functions and not cli_args.dynamic:
        raise DeployError(
            "Resume the unfinished function tree with push-dynamic --resume"
        )
    if cli_args.dynamic:
        plan = dynamic_plan or plan_dynamic_functions(
            client, config, cli_args, logger, resume_state
        )
        return execute_dynamic_functions(
            plan, client, config, cli_args, logger, summary, resume_state
        )
    stats = summary.get("functions")
    registry = fetch_function_registry(client)
    detail_cache: dict[tuple[FunctionType, str], dict[str, Any]] = {}
    specs = load_function_specs(config, cli_args, logger)
    dependency_specs = specs
    if config.orphan_excluded_contexts:
        dependency_specs = load_function_specs(
            config,
            replace(cli_args, path_filters=(), function_types=None),
            logger,
        )
    local_func_context = build_local_func_context(
        dependency_specs, config.orphan_excluded_contexts
    )

    logger.section("FUNCTIONS")
    if not specs:
        logger.muted("No local function declarations found")
        return registry, False

    client_specs = [spec for spec in specs if spec.function_type == "client"]
    server_specs = [spec for spec in specs if spec.function_type == "server"]

    logger.muted(f"Found {len(specs)} local function declaration(s)")
    logger.action("Syncing functions...")

    changed_clients = sync_function_group(
        specs=client_specs,
        client=client,
        registry=registry,
        detail_cache=detail_cache,
        config=config,
        cli_args=cli_args,
        logger=logger,
        stats=stats,
        resume_state=resume_state,
        local_func_context=local_func_context,
    )

    changed_servers = sync_function_group(
        specs=server_specs,
        client=client,
        registry=registry,
        detail_cache=detail_cache,
        config=config,
        cli_args=cli_args,
        logger=logger,
        stats=stats,
        resume_state=resume_state,
        local_func_context=local_func_context,
    )

    return registry, changed_clients or changed_servers


def build_local_func_context(
    specs: list[FunctionSpec], excluded_contexts: tuple[str, ...] = ()
) -> dict[str, dict]:
    func_context: dict[str, dict] = {}
    known = {f"{spec.context}.{spec.name}": spec.function_type for spec in specs}
    for spec in specs:
        if spec.source_path.suffix != ".py":
            continue
        if context_is_orphan_excluded(spec.context, excluded_contexts):
            continue
        func_name = f"{spec.context}.{spec.name}"
        code = strip_deploy_receipt(spec.source_path.read_text(encoding="utf-8"))
        func_context[func_name] = {
            **scan_function_dependencies(func_name, code, excluded_contexts, known),
            "table_exports": {},
            "table_failures": {},
            "variable_exports": {},
            "kind": spec.function_type,
            "code": code,
        }
    return func_context


def index_sdk_table_exports(catalog: list) -> tuple[dict[str, str], dict[str, str]]:
    """Associate native table classes and flattened query/row types with owners.

    Parse SDK-rendered definitions without executing them or shared client source.
    This also covers schema-dependent nested row types without guessing suffixes.
    """
    from polyapi.poly_tables import _render_table
    from polyapi.typedefs import TableSpecDto

    exports: dict[str, str] = {}
    failures: dict[str, str] = {}
    for table in catalog:
        if not isinstance(table, dict) or table.get("type") != "table":
            continue
        context = table.get("context") or ""
        prefix = f"{context}." if context else ""
        owner = f"{prefix}{table['name']}"
        try:
            module = ast.parse(_render_table(cast(TableSpecDto, table)))
        except (
            KeyError,
            TypeError,
            ValueError,
            SyntaxError,
            IndexError,
            AttributeError,
        ):
            # A visible table may never enter this server's generated SDK. Keep
            # its identity and defer failure until the effective scope is known.
            exports[owner] = owner
            failures[owner] = context
            continue
        for node in module.body:
            if isinstance(node, ast.ClassDef):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [
                    target.id for target in node.targets if isinstance(target, ast.Name)
                ]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            exports.update({f"{prefix}{name}": owner for name in names})
    return exports, failures


def build_remote_func_context(
    client: "PolyApiClient",
    excluded_contexts: tuple[str, ...] = (),
    *,
    local_func_context: dict[str, dict],
    planned_resource_exports: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict]:
    # Use the same live catalog and language filter as SDK generation. /specs
    # applies shared visibility and public namespaces; its order sets precedence.
    # Keep this read on the already verified target, without loading SDK credentials.
    catalog = resource_list(client.request("GET", "/specs", params={"noTypes": "true"}))
    table_exports, table_failures = index_sdk_table_exports(catalog)
    variable_exports = {
        ".".join(filter(None, (item.get("context"), item["name"]))): ".".join(
            filter(None, (item.get("context"), item["name"]))
        )
        for item in catalog
        if isinstance(item, dict) and item.get("type") == "serverVariable"
    }
    # Dynamic plans precede resource synchronization. Selected local identities
    # disambiguate new resources named like SDK methods without inventing types
    # or replacing the live SDK's export ownership and rendering failures.
    for namespace, exports in (("tabi", table_exports), ("vari", variable_exports)):
        for name, owner in (planned_resource_exports or {}).get(namespace, {}).items():
            exports.setdefault(name, owner)
    functions = {
        key: item
        for key, item in index_sdk_function_specs(
            catalog, source="live SDK catalog"
        ).items()
        if not context_is_orphan_excluded(item["context"], excluded_contexts)
    }
    # A local declaration may replace its own stale source, but cannot claim an
    # export owned by another declaration (including generated HTTP aliases).
    for key, node in local_func_context.items():
        kind = node.get("kind", "client")
        expected_type = "serverFunction" if kind == "server" else "customFunction"
        exports = (key, f"{key}_async") if kind == "server" else (key,)
        for export in exports:
            function = functions.get(export)
            if function is None:
                continue
            declared_key = (
                f"{function['context']}.{function['name']}"
                if function["context"]
                else function["name"]
            )
            if declared_key != key or function["type"] != expected_type:
                raise DeployError(
                    f"Ambiguous generated SDK name: {export}; local {kind} {key} "
                    f"conflicts with {function['type']} {declared_key} in the live "
                    "SDK catalog. Rename the conflicting function before deployment."
                )
    # Shared clients supply dependency information only; they never enter the
    # deployment registry.
    func_context = dict(local_func_context)
    known = {
        key: "client" if item["type"] == "customFunction" else "external"
        for key, item in functions.items()
        if item["type"] == "customFunction"
        or item["type"] in SDK_EXTERNAL_FUNCTION_TYPES
    }
    known.update(
        {key: node.get("kind", "client") for key, node in func_context.items()}
    )
    for key, node in list(func_context.items()):
        if "code" in node:
            func_context[key] = {
                **node,
                **scan_function_dependencies(
                    key,
                    node["code"],
                    excluded_contexts,
                    known,
                    table_exports,
                    variable_exports,
                ),
                "table_exports": table_exports,
                "table_failures": table_failures,
                "variable_exports": variable_exports,
            }
    pending = [call for node in func_context.values() for call in node["calls"]]
    visited: set[str] = set()
    while pending:
        func_name = pending.pop()
        if func_name in visited:
            continue
        visited.add(func_name)
        if func_name in func_context:
            pending.extend(func_context[func_name]["calls"])
            continue
        function = functions.get(func_name)
        if function is None:
            continue
        if function["type"] != "customFunction":
            if (
                function["type"] in SDK_EXTERNAL_FUNCTION_TYPES
                and not function["context"]
            ):
                # Root wrappers and listeners require unfiltered SDK generation.
                # Retain only that dependency, never their implementation.
                func_context[func_name] = {
                    "kind": "external",
                    "calls": [],
                    "contexts": [],
                }
            continue
        code = function.get("code")
        if not isinstance(code, str) or not code.strip():
            raise DeployError(f"Missing Python source for SDK client: {func_name}")
        func_context[func_name] = {
            **scan_function_dependencies(
                func_name,
                code,
                excluded_contexts,
                known,
                table_exports,
                variable_exports,
            ),
            "table_exports": table_exports,
            "table_failures": table_failures,
            "variable_exports": variable_exports,
            "kind": "client",
            "code": code,
        }
        pending.extend(func_context[func_name]["calls"])
    return func_context


def current_function_context(
    spec: FunctionSpec,
    code: str,
    func_context: dict | None,
    excluded_contexts: tuple[str, ...],
) -> dict[str, dict]:
    contexts = dict(func_context or {})
    key = f"{spec.context}.{spec.name}"
    known = {name: node.get("kind", "client") for name, node in contexts.items()}
    known[key] = spec.function_type
    # Re-scan current source, never inherit stale remote self-dependencies.
    table_exports = contexts.get(key, {}).get("table_exports", {})
    variable_exports = contexts.get(key, {}).get("variable_exports", {})
    table_failures = contexts.get(key, {}).get("table_failures", {})
    contexts[key] = {
        **scan_function_dependencies(
            key, code, excluded_contexts, known, table_exports, variable_exports
        ),
        "table_exports": table_exports,
        "table_failures": table_failures,
        "variable_exports": variable_exports,
        "kind": spec.function_type,
        "code": code,
    }
    return contexts


def reachable_client_contexts(
    func_name: str,
    func_context: dict[str, dict],
    excluded_contexts: tuple[str, ...] = (),
) -> Iterator[tuple[str, dict]]:
    from collections import deque

    visited: set[str] = set()
    queue = deque([func_name])
    while queue:
        key = queue.popleft()
        if key in visited:
            continue
        visited.add(key)
        node = func_context.get(key)
        if node is None or context_is_orphan_excluded(
            key.rpartition(".")[0], excluded_contexts
        ):
            continue
        if key != func_name and node.get("kind") == "server":
            continue  # Its implementation and packages run across an HTTP boundary.
        yield key, node
        queue.extend(node["calls"])


def get_transitive_contexts(
    func_name: str,
    func_context: dict[str, dict],
    excluded_contexts: tuple[str, ...] = (),
) -> list[str]:
    all_contexts: list[str] = []
    for _, node in reachable_client_contexts(
        func_name, func_context, excluded_contexts
    ):
        for context in node["contexts"]:
            if (
                not context_is_orphan_excluded(context, excluded_contexts)
                and context not in all_contexts
            ):
                all_contexts.append(context)
    return all_contexts


def resolve_contexts_against_exclusions(
    contexts: Iterable[str],
    replacement_candidates: Iterable[str],
    excluded_contexts: tuple[str, ...],
) -> list[str]:
    if not excluded_contexts:
        return _dedupe_contexts(contexts)

    requested_contexts = _dedupe_contexts(contexts)
    candidates = [
        context
        for context in _dedupe_contexts(replacement_candidates)
        if not context_is_orphan_excluded(context, excluded_contexts)
        and not _context_has_excluded_descendant(context, excluded_contexts)
    ]
    resolved: list[str] = []
    for context in requested_contexts:
        if context_is_orphan_excluded(context, excluded_contexts):
            continue
        if _context_has_excluded_descendant(context, excluded_contexts):
            replacements = [
                candidate
                for candidate in candidates
                if _context_is_same_or_child(candidate, context)
            ]
            if not replacements:
                excluded_descendants = ", ".join(
                    repr(excluded)
                    for excluded in excluded_contexts
                    if _context_is_same_or_child(excluded, context)
                )
                raise DeployError(
                    f"Cannot safely resolve broad context {context!r}: it contains "
                    f"excluded context(s) {excluded_descendants} and no concrete "
                    "non-excluded replacement was found"
                )
            resolved.extend(replacements)
            continue
        resolved.append(context)
    return compact_contexts_against_safe_parents(resolved, excluded_contexts)


def generated_resource_modules(
    exports: dict[str, str], generate_contexts: Iterable[str] | None
) -> set[str]:
    contexts = tuple(generate_contexts or ())
    modules: set[str] = set()
    for owner in set(exports.values()):
        parts = owner.split(".")[:-1]
        if contexts and not any(
            _context_is_same_or_child(".".join(parts), selected)
            for selected in contexts
        ):
            continue
        modules.update(".".join(parts[:length]) for length in range(1, len(parts) + 1))
    return modules


def validate_table_generation(
    nodes: list[dict], generate_contexts: list[str] | None
) -> None:
    failures = {
        owner: context
        for node in nodes
        for owner, context in node.get("table_failures", {}).items()
    }
    if not failures:
        return
    # The SDK renders every table in the selected contexts, even unused ones.
    required = {
        owner
        for owner, context in failures.items()
        if not generate_contexts
        or any(
            _context_is_same_or_child(context, selected)
            for selected in generate_contexts
        )
    }
    exports = {
        name: owner
        for node in nodes
        for name, owner in node.get("table_exports", {}).items()
    }
    namespaces = {
        context: context for context in generated_resource_modules(exports, None)
    }
    generated_modules = generated_resource_modules(exports, generate_contexts)
    for node in nodes:
        for reference in extract_references(
            node.get("code", ""),
            namespace="tabi",
            known_exports=exports,
            available_modules=generated_modules,
        ):
            matches = match_reference(reference, exports)
            required.update(matches & failures.keys())
            if matches:
                continue
            # Failed rendering leaves flattened row/query exports unknown. Use
            # their module scope, not guessed type suffixes, and retain root
            # references even when explicit filters would otherwise omit them.
            contexts = match_reference(reference, namespaces) or {""}
            required.update(
                owner
                for owner, context in failures.items()
                if context in contexts
                or any(
                    selected and _context_is_same_or_child(context, selected)
                    for selected in contexts
                )
            )
    if required:
        raise DeployError(
            "Cannot generate SDK table exports required by this server: "
            + ", ".join(sorted(required))
        )


def resolve_server_generate_contexts(
    spec: FunctionSpec,
    code: str,
    func_context: dict | None,
    excluded_contexts: tuple[str, ...],
) -> list[str] | None:
    func_name = f"{spec.context}.{spec.name}"
    current_context = current_function_context(
        spec, code, func_context, excluded_contexts
    )
    local_contexts = current_context[func_name]["contexts"]
    root_dependencies: set[str] = set()
    reachable = list(
        reachable_client_contexts(func_name, current_context, excluded_contexts)
    )
    nodes = [node for _, node in reachable]
    remote_contexts = get_transitive_contexts(
        func_name, current_context, excluded_contexts
    )
    inferred_contexts = _dedupe_contexts([*local_contexts, *remote_contexts])
    replacement_candidates = [spec.context, *local_contexts, *remote_contexts]
    for key, node in reachable:
        if "." not in key and node.get("kind") in {"client", "external"}:
            root_dependencies.add(f"poly.{key}")
        root_variables = node.get("root_variables", [])
        root_tables = node.get("root_tables", [])
        if (
            (root_tables or root_variables)
            and spec.generate_contexts
            and (node.get("table_exports") or node.get("variable_exports"))
        ):
            # An explicit child scope can supply an otherwise unused module import.
            # Re-scan without altering shared nodes; actual table uses stay strong.
            selected_contexts = resolve_contexts_against_exclusions(
                spec.generate_contexts, replacement_candidates, excluded_contexts
            )
            rescanned = scan_function_dependencies(
                key,
                node["code"],
                excluded_contexts,
                table_exports=node.get("table_exports", {}),
                variable_exports=node.get("variable_exports", {}),
                available_table_modules=(
                    generated_resource_modules(
                        node.get("table_exports", {}), selected_contexts
                    )
                    if selected_contexts
                    else ()
                ),
                available_variable_modules=(
                    generated_resource_modules(
                        node.get("variable_exports", {}), selected_contexts
                    )
                    if selected_contexts
                    else ()
                ),
            )
            root_tables = rescanned["root_tables"]
            root_variables = rescanned["root_variables"]
        root_dependencies.update(f"vari.{name}" for name in root_variables)
        root_dependencies.update(f"tabi.{name}" for name in root_tables)
    if root_dependencies:
        # The platform ignores empty context selectors. SDK 0.3.20 can include
        # root dependencies only through unfiltered generation on this payload path.
        if excluded_contexts or spec.generate_contexts:
            raise DeployError(
                "Root-level SDK functions, Vari or Tabi require unfiltered generation; cannot "
                "preserve explicit generate_contexts or context exclusions: "
                + ", ".join(sorted(root_dependencies))
            )
        validate_table_generation(nodes, None)
        return None
    resolved = resolve_contexts_against_exclusions(
        spec.generate_contexts or inferred_contexts,
        replacement_candidates,
        excluded_contexts,
    )
    validate_table_generation(nodes, resolved)
    return resolved


def sync_function_group(
    *,
    specs: list[FunctionSpec],
    client: PolyApiClient,
    registry: FunctionRegistry,
    detail_cache: dict[tuple[FunctionType, str], dict[str, Any]],
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    stats: ArtifactStats,
    resume_state: ResumeState,
    local_func_context: dict[str, dict] | None = None,
) -> bool:
    any_changed = False
    execution_api_key = get_execution_api_key()
    func_context = {
        **(local_func_context or {}),
        **build_local_func_context(specs, config.orphan_excluded_contexts),
    }
    if any(spec.function_type == "server" for spec in specs):
        func_context = build_remote_func_context(
            client, config.orphan_excluded_contexts, local_func_context=func_context
        )
    for spec in specs:
        resume_key = f"fn:{spec.function_type}:{spec.context}:{spec.name}"
        logger.item(
            f"{spec.name} [{spec.function_type}] ({spec.context})",
            file_path=spec.source_path,
            repo_root=config.repo_root,
        )
        if spec.function_type == "server" and spec.source_path.suffix == ".py":
            code = strip_deploy_receipt(spec.source_path.read_text(encoding="utf-8"))
            _gc = resolve_server_generate_contexts(
                spec,
                code,
                func_context,
                config.orphan_excluded_contexts,
            )
            logger.info(f"generateContexts: {_gc}")
        try:
            if execution_api_key and spec.function_type == "server":
                spec = replace(spec, execution_api_key=execution_api_key)
            payload = build_function_payload(
                spec, func_context, config.orphan_excluded_contexts
            )
            payload = apply_server_function_image_override(
                payload, spec, config, logger
            )
            payload_fingerprint = payload_resume_fingerprint(
                normalize_function_payload(payload)
            )
            if not cli_args.force and resume_state.is_done(
                resume_key, payload_fingerprint
            ):
                logger.ok("Skipped (resumed)")
                stats.skipped += 1
                continue
            existing = registry.get(spec.function_type, spec.context, spec.name)

            if existing:
                detail = fetch_function_detail(
                    client, spec.function_type, existing["id"], detail_cache
                )
                local_normalized, remote_normalized = normalize_function_subset(
                    payload, detail
                )
                if should_skip_unchanged(cli_args, local_normalized, remote_normalized):
                    logger.ok("Skipped")
                    stats.skipped += 1
                    resume_state.mark_done(resume_key, payload_fingerprint)
                    continue
                if local_normalized == remote_normalized:
                    logger.detail("force: redeploying unchanged function")
                else:
                    for diff_line in summarize_function_payload_diff(
                        local_normalized, remote_normalized
                    ):
                        logger.detail(f"diff: {diff_line}")
                log_forced_unchanged_update(
                    logger, cli_args, local_normalized, remote_normalized
                )
                for diff_line in summarize_function_payload_diff(
                    local_normalized, remote_normalized
                ):
                    logger.detail(f"diff: {diff_line}")
                allowed, reason = can_update(
                    local_timestamp=spec.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    logger.blocked(reason or "Update blocked")
                    stats.blocked += 1
                    continue
                if not cli_args.dry_run:
                    result = client.request(
                        "POST",
                        f"/functions/{spec.function_type}",
                        payload=payload,
                        expected=(200, 201),
                    )
                    registry.upsert(
                        spec.function_type,
                        {
                            "id": result["id"],
                            "context": spec.context,
                            "name": spec.name,
                        },
                    )
                    detail_cache[(spec.function_type, result["id"])] = {
                        **project_remote_like_template(payload, payload),
                        "id": result["id"],
                    }
                    run_server_function_post_deploy_script(
                        function_id=result["id"],
                        spec=spec,
                        config=config,
                        logger=logger,
                    )
                    resume_state.mark_done(resume_key, payload_fingerprint)
                    logger.ok("Updated")
                else:
                    gc = payload.get("generateContexts")
                    logger.planned(
                        f"Would update (generateContexts: {gc})"
                        if gc
                        else "Would update"
                    )
                stats.updated += 1
                any_changed = True
                continue

            if not cli_args.dry_run:
                result = client.request(
                    "POST",
                    f"/functions/{spec.function_type}",
                    payload=payload,
                    expected=(200, 201),
                )
                registry.upsert(
                    spec.function_type,
                    {"id": result["id"], "context": spec.context, "name": spec.name},
                )
                detail_cache[(spec.function_type, result["id"])] = {
                    **project_remote_like_template(payload, payload),
                    "id": result["id"],
                }
                run_server_function_post_deploy_script(
                    function_id=result["id"],
                    spec=spec,
                    config=config,
                    logger=logger,
                )
                resume_state.mark_done(resume_key, payload_fingerprint)
                logger.ok("Created")
            else:
                gc = payload.get("generateContexts")
                registry.upsert(
                    spec.function_type,
                    {
                        "id": f"planned:{spec.function_type}:{spec.context}.{spec.name}",
                        "context": spec.context,
                        "name": spec.name,
                    },
                )
                logger.planned(
                    f"Would create (generateContexts: {gc})" if gc else "Would create"
                )
            stats.created += 1
            any_changed = True
        except DeployError as exc:
            logger.fail(str(exc))
            stats.failed += 1
    return any_changed


def run_poly_sync(config: DeployConfig, logger: Logger, reason: str) -> None:
    command = [sys.executable, "-m", "polyapi", "sync"]
    logger.action(f"Running {' '.join(command)} ({reason})")
    completed = subprocess.run(command, cwd=config.repo_root, check=False)
    if completed.returncode != 0:
        raise DeployError(
            f"`{' '.join(command)}` failed with exit code {completed.returncode}"
        )


def resolve_poly_generate_contexts(
    config: DeployConfig,
    cli_args: CliArgs | None,
    logger: Logger,
) -> tuple[str, ...]:
    if not config.orphan_excluded_contexts:
        return config.generate_contexts

    replacement_candidates = (
        collect_local_context_prefixes(config, cli_args, logger) if cli_args else ()
    )
    if config.generate_contexts:
        contexts = resolve_contexts_against_exclusions(
            config.generate_contexts,
            replacement_candidates,
            config.orphan_excluded_contexts,
        )
    else:
        contexts = [
            context
            for context in replacement_candidates
            if not context_is_orphan_excluded(context, config.orphan_excluded_contexts)
            and not _context_has_excluded_descendant(
                context, config.orphan_excluded_contexts
            )
        ]

    if not contexts:
        raise DeployError(
            "Unable to derive safe `polyapi generate --contexts` values while "
            "POLY_DEPLOY_EXCLUDE_ORPHAN_CONTEXTS is set. Configure "
            "POLY_DEPLOY_GENERATE_CONTEXTS with non-excluded concrete contexts "
            "or run with --skip-generate."
        )
    return tuple(contexts)


def run_poly_generate(
    config: DeployConfig,
    logger: Logger,
    reason: str,
    cli_args: CliArgs | None = None,
) -> None:
    command = [sys.executable, "-m", "polyapi", "generate"]
    generate_contexts = resolve_poly_generate_contexts(config, cli_args, logger)
    if generate_contexts:
        command.extend(["--contexts", ",".join(generate_contexts)])
    if config.generate_names:
        command.extend(["--names", ",".join(config.generate_names)])
    if config.generate_ids:
        command.extend(["--ids", ",".join(config.generate_ids)])
    logger.action(f"Running {' '.join(command)} ({reason})")
    if cli_args and cli_args.json_output:
        completed = subprocess.run(
            command, cwd=config.repo_root, check=False, stdout=sys.stderr
        )
    else:
        completed = subprocess.run(command, cwd=config.repo_root, check=False)
    if completed.returncode != 0:
        raise DeployError(
            f"`{' '.join(command)}` failed with exit code {completed.returncode}"
        )


def normalize_dict_subset(
    payload: dict[str, Any], remote: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_normalized = canonicalize(payload)
    remote_subset = canonicalize(project_remote_like_template(remote, payload))
    return normalize_description_fields_for_compare(local_normalized, remote_subset)


def normalize_variable_payload_for_compare(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    for key in ("description", "expiresAt", "ownerUserId"):
        if normalized.get(key) in (None, ""):
            normalized.pop(key, None)
    return canonicalize(normalized)


def normalize_variable_subset(
    payload: dict[str, Any], remote: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    remote_subset = project_remote_like_template(remote, payload)
    local_normalized, remote_normalized = (
        normalize_variable_payload_for_compare(payload),
        normalize_variable_payload_for_compare(remote_subset),
    )
    if is_empty_variable_value(local_normalized.get("value")):
        local_normalized.pop("value", None)
        remote_normalized.pop("value", None)
    return normalize_description_fields_for_compare(local_normalized, remote_normalized)


def build_variable_update_payload(
    payload: dict[str, Any],
    remote: dict[str, Any] | None = None,
    *,
    update_secret_values: bool = False,
) -> dict[str, Any]:
    update_payload = copy.deepcopy(payload)
    protected = is_protected_variable_payload(payload) or is_protected_variable_payload(
        remote or {}
    )
    if (protected and not update_secret_values) or is_empty_variable_value(
        update_payload.get("value")
    ):
        update_payload.pop("value", None)
    return update_payload


def is_empty_variable_value(value: Any) -> bool:
    return value in (None, "", {}, [])


def sync_variables(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    otp_manager: OtpManager,
    resume_state: ResumeState,
) -> None:
    stats = summary.get("variables")
    files = [
        item
        for item in load_artifact_files(config, cli_args, config.variable_globs)
        if not context_is_orphan_excluded(
            cast(str | None, item.data.get("context")),
            config.orphan_excluded_contexts,
        )
    ]
    logger.section("VARIABLES")
    if not files:
        logger.muted("No local variable files found")
        return

    existing_items = client.request("GET", "/variables")
    existing_index = {(item["context"], item["name"]): item for item in existing_items}
    detail_cache: dict[str, dict[str, Any]] = {}

    logger.muted(f"Found {len(files)} local variable file(s)")
    logger.action("Syncing variables...")
    for item in files:
        try:
            data = build_variable_payload(item.path, item.data)
            validation = dict(data)
            if "value" in validation and not isinstance(validation["value"], dict):
                # Same documented OpenAPI drift as TS: Vari supports scalars/arrays.
                validation["value"] = {}
            schema = item.data.get("schema")
            if (
                isinstance(schema, dict)
                and "value" in data
                and not is_secret_variable_payload(data)
            ):
                if not Draft7Validator(schema).is_valid(data["value"]):
                    raise DeployError(
                        "Variable value does not match its declared schema"
                    )
            validate_endpoint_payload(
                (
                    "UpdateVariableDto"
                    if (data["context"], data["name"]) in existing_index
                    else "CreateVariableDto"
                ),
                validation,
            )
            context = data["context"]
            name = data["name"]
            resume_key = f"var:{context}:{name}"
            logger.item(
                f"{name} ({context})", file_path=item.path, repo_root=config.repo_root
            )
            # Sensitive Vari values must not leave a persisted digest either.
            fingerprint = (
                payload_resume_fingerprint(data)
                if str(data.get("secrecy", "NONE")).upper() == "NONE"
                and not data.get("secret")
                else None
            )
            if not cli_args.force and resume_state.is_done(resume_key, fingerprint):
                logger.ok("Skipped (resumed)")
                stats.skipped += 1
                continue
            existing = existing_index.get((context, name))

            if existing:
                detail = detail_cache.get(existing["id"])
                if detail is None:
                    detail = client.request("GET", f"/variables/{existing['id']}")
                    detail_cache[existing["id"]] = detail

                data = build_variable_update_payload(
                    data,
                    detail,
                    update_secret_values=config.update_existing_secret_variables,
                )
                force_update_obscured = (
                    is_obscured_variable_payload(data) and cli_args.force
                )

                if is_obscured_variable_payload(data) and not cli_args.force:
                    logger.skip("Existing obscured variable requires --force to update")
                    stats.skipped += 1
                    continue

                if is_secret_variable_payload(data) and (
                    not is_obscured_variable_payload(data)
                    and not config.update_existing_secret_variables
                ):
                    logger.skip("Existing secret updates disabled")
                    stats.skipped += 1
                    continue

                if force_update_obscured:
                    logger.detail("force: updating obscured variable")
                else:
                    local_normalized, remote_normalized = normalize_variable_subset(
                        data, detail
                    )
                    if local_normalized == remote_normalized and not cli_args.force:
                        logger.ok("Skipped")
                        stats.skipped += 1
                        continue
                    if local_normalized == remote_normalized:
                        logger.detail("force: updating unchanged variable")
                    else:
                        for diff_line in summarize_function_payload_diff(
                            local_normalized, remote_normalized
                        ):
                            logger.detail(f"diff: {diff_line}")
                local_normalized, remote_normalized = normalize_variable_subset(
                    data, detail
                )
                if should_skip_unchanged(cli_args, local_normalized, remote_normalized):
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                log_forced_unchanged_update(
                    logger, cli_args, local_normalized, remote_normalized
                )
                for diff_line in summarize_function_payload_diff(
                    local_normalized, remote_normalized
                ):
                    logger.detail(f"diff: {diff_line}")

                allowed, reason = can_update(
                    local_timestamp=item.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    logger.blocked(reason or "Update blocked")
                    stats.blocked += 1
                    continue

                if not cli_args.dry_run:
                    update_payload = data
                    try:
                        client.request(
                            "PATCH",
                            f"/variables/{existing['id']}",
                            payload=update_payload,
                            expected=(200,),
                        )
                    except DeployError as exc:
                        cannot_change_secrecy = (
                            isinstance(exc, ApiError) and exc.secrecy_change_rejected
                        )
                        if (
                            config.recreate_variables_on_secrecy_change
                            and cannot_change_secrecy
                            and (
                                not is_protected_variable_payload(detail)
                                or config.update_existing_secret_variables
                            )
                        ):
                            client.request(
                                "DELETE",
                                f"/variables/{existing['id']}",
                                expected=(200, 204),
                                headers=otp_manager.delete_headers(),
                            )
                            created = client.request(
                                "POST", "/variables", payload=data, expected=(201,)
                            )
                            existing_index[(context, name)] = created
                        else:
                            raise
                    resume_state.mark_done(resume_key, fingerprint)
                    logger.ok("Updated")
                else:
                    logger.planned("Would update")
                stats.updated += 1
                continue

            if not cli_args.dry_run:
                created = client.request(
                    "POST", "/variables", payload=data, expected=(201,)
                )
                existing_index[(context, name)] = created
                resume_state.mark_done(resume_key, fingerprint)
                logger.ok("Created")
            else:
                logger.planned("Would create")
            stats.created += 1
        except DeployError as exc:
            logger.fail(str(exc))
            stats.failed += 1


def build_variable_payload(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: data[key]
        for key in (
            "expiresAt",
            "context",
            "name",
            "description",
            "value",
            "secret",
            "secrecy",
            "visibility",
            "state",
            "ownerUserId",
        )
        if key in data
    }
    if "context" not in payload or "name" not in payload:
        raise DeployError(f"Variable file {path} must include context and name")
    if is_secret_variable_payload(payload):
        # Never deploy SECRET values from source files.
        # Poly rejects empty-string secret values on create, and real secret values should be
        # set in Canopy rather than stored in JSON. OBSCURED values are readable at runtime;
        # when a real checked-in value is present, keep it so first-time variable creation can
        # seed environment-substituted placeholders. Empty obscured values are omitted.
        if is_obscured_variable_payload(payload):
            if is_empty_variable_value(payload.get("value")):
                payload.pop("value", None)
            return payload
        payload["value"] = {}
    return payload


def is_protected_variable_payload(payload: dict[str, Any]) -> bool:
    # Existing PARTIAL values still require protection despite deprecated creation.
    return is_secret_variable_payload(payload) or (
        str(payload.get("secrecy", "")).upper() == "PARTIAL"
    )


def is_secret_variable_payload(payload: dict[str, Any]) -> bool:
    secrecy = str(payload.get("secrecy", "")).upper()
    return bool(payload.get("secret")) or secrecy == "SECRET" or secrecy == "OBSCURED"


def is_obscured_variable_payload(payload: dict[str, Any]) -> bool:
    return str(payload.get("secrecy", "")).upper() == "OBSCURED"


POLY_MANAGED_TABLE_COLUMN_NAMES = frozenset({"id", "createdAt", "updatedAt"})


def strip_poly_managed_table_columns(data: dict[str, Any]) -> dict[str, Any]:
    """Keep generated columns visible in source without sending them to Tabi."""
    payload = copy.deepcopy(data)
    columns = payload.get("columns")
    if isinstance(columns, list):
        payload["columns"] = [
            column
            for column in columns
            if not (
                isinstance(column, dict)
                and column.get("name") in POLY_MANAGED_TABLE_COLUMN_NAMES
            )
        ]
    return payload


def sync_tables(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    resume_state: ResumeState,
) -> None:
    stats = summary.get("tables")
    files = [
        item
        for item in load_artifact_files(config, cli_args, config.table_globs)
        if not context_is_orphan_excluded(
            cast(str | None, item.data.get("context") or ""),
            config.orphan_excluded_contexts,
        )
    ]
    logger.section("TABLES")
    if not files:
        logger.muted("No local table files found")
        return

    existing_tables = fetch_all_tables(client)
    existing_index = {
        ((item.get("context") or ""), item["name"]): item for item in existing_tables
    }
    detail_cache: dict[str, dict[str, Any]] = {}

    logger.muted(f"Found {len(files)} local table file(s)")
    logger.action("Syncing tables...")
    for item in files:
        try:
            data = ensure_mapping(item.data, item.path)
            payload = strip_poly_managed_table_columns(data)
            validate_endpoint_payload("CreateTableDto", payload)
            name = data["name"]
            context = data.get("context") or ""
            resume_key = f"table:{context}:{name}"
            label = f"{name} ({context})" if context else name
            logger.item(label, file_path=item.path, repo_root=config.repo_root)
            fingerprint = payload_resume_fingerprint(payload)
            if not cli_args.force and resume_state.is_done(resume_key, fingerprint):
                logger.ok("Skipped (resumed)")
                stats.skipped += 1
                continue
            existing = existing_index.get((context, name))

            if existing:
                detail = detail_cache.get(existing["id"])
                if detail is None:
                    detail = client.request("GET", f"/tables/{existing['id']}")
                    detail_cache[existing["id"]] = detail
                local_normalized, remote_normalized = normalize_dict_subset(
                    payload, strip_poly_managed_table_columns(detail)
                )
                if local_normalized == remote_normalized and not cli_args.force:
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                if local_normalized == remote_normalized:
                    logger.detail("force: updating unchanged table")
                else:
                    for diff_line in summarize_function_payload_diff(
                        local_normalized, remote_normalized
                    ):
                        logger.detail(f"diff: {diff_line}")
                if should_skip_unchanged(cli_args, local_normalized, remote_normalized):
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                log_forced_unchanged_update(
                    logger, cli_args, local_normalized, remote_normalized
                )
                for diff_line in summarize_function_payload_diff(
                    local_normalized, remote_normalized
                ):
                    logger.detail(f"diff: {diff_line}")
                allowed, reason = can_update(
                    local_timestamp=item.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    logger.blocked(reason or "Update blocked")
                    stats.blocked += 1
                    continue
                if not cli_args.dry_run:
                    client.request(
                        "PATCH",
                        f"/tables/{existing['id']}",
                        payload=payload,
                        expected=(200,),
                    )
                    resume_state.mark_done(resume_key, fingerprint)
                    logger.ok("Updated")
                else:
                    logger.planned("Would update")
                stats.updated += 1
                continue

            if not cli_args.dry_run:
                created = client.request(
                    "POST", "/tables", payload=payload, expected=(201,)
                )
                existing_index[(context, name)] = created
                resume_state.mark_done(resume_key, fingerprint)
                logger.ok("Created")
            else:
                logger.planned("Would create")
            stats.created += 1
        except DeployError as exc:
            logger.fail(str(exc))
            stats.failed += 1


def fetch_all_tables(client: PolyApiClient) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        response = client.request(
            "GET", "/tables", params={"page": page}, expected=(200,)
        )
        results.extend(response.get("results", []))
        pagination = response.get("pagination") or {}
        pages = pagination.get("pages", 0)
        if page >= pages or pages == 0:
            break
        page += 1
    return results


def sync_schemas(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    resume_state: ResumeState,
) -> None:
    stats = summary.get("schemas")
    files = [
        item
        for item in load_artifact_files(config, cli_args, config.schema_globs)
        if not context_is_orphan_excluded(
            cast(str | None, item.data.get("context")),
            config.orphan_excluded_contexts,
        )
    ]
    logger.section("SCHEMAS")
    if not files:
        logger.muted("No local schema files found")
        return

    existing_items = client.request("GET", "/schemas")
    existing_index = {(item["context"], item["name"]): item for item in existing_items}
    detail_cache: dict[str, dict[str, Any]] = {}

    logger.muted(f"Found {len(files)} local schema file(s)")
    logger.action("Syncing schemas...")
    for item in files:
        try:
            payload = build_schema_payload(item.path, item.data)
            validate_endpoint_payload("CreateSchemaDto", payload)
            context = payload["context"]
            name = payload["name"]
            resume_key = f"schema:{context}:{name}"
            logger.item(
                f"{name} ({context})", file_path=item.path, repo_root=config.repo_root
            )
            fingerprint = payload_resume_fingerprint(payload)
            if not cli_args.force and resume_state.is_done(resume_key, fingerprint):
                logger.ok("Skipped (resumed)")
                stats.skipped += 1
                continue
            existing = existing_index.get((context, name))

            if existing:
                detail = detail_cache.get(existing["id"])
                if detail is None:
                    detail = client.request("GET", f"/schemas/{existing['id']}")
                    detail_cache[existing["id"]] = detail
                local_normalized, remote_normalized = normalize_dict_subset(
                    payload, detail
                )
                if local_normalized == remote_normalized and not cli_args.force:
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                if local_normalized == remote_normalized:
                    logger.detail("force: updating unchanged schema")
                else:
                    for diff_line in summarize_function_payload_diff(
                        local_normalized, remote_normalized
                    ):
                        logger.detail(f"diff: {diff_line}")
                if should_skip_unchanged(cli_args, local_normalized, remote_normalized):
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                log_forced_unchanged_update(
                    logger, cli_args, local_normalized, remote_normalized
                )
                for diff_line in summarize_function_payload_diff(
                    local_normalized, remote_normalized
                ):
                    logger.detail(f"diff: {diff_line}")
                allowed, reason = can_update(
                    local_timestamp=item.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    logger.blocked(reason or "Update blocked")
                    stats.blocked += 1
                    continue
                if not cli_args.dry_run:
                    client.request(
                        "PATCH",
                        f"/schemas/{existing['id']}",
                        payload=payload,
                        expected=(200,),
                    )
                    resume_state.mark_done(resume_key, fingerprint)
                    logger.ok("Updated")
                else:
                    logger.planned("Would update")
                stats.updated += 1
                continue

            if not cli_args.dry_run:
                created = client.request(
                    "POST", "/schemas", payload=payload, expected=(201,)
                )
                existing_index[(context, name)] = created
                resume_state.mark_done(resume_key, fingerprint)
                logger.ok("Created")
            else:
                logger.planned("Would create")
            stats.created += 1
        except DeployError as exc:
            logger.fail(str(exc))
            stats.failed += 1


def build_schema_payload(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: copy.deepcopy(value)
        for key, value in data.items()
        if key
        in {
            "name",
            "context",
            "definition",
            "description",
            "visibility",
            "state",
            "ownerUserId",
        }
    }
    if "name" not in payload or "context" not in payload:
        raise DeployError(f"Schema file {path} must include name and context")
    definition = payload.get("definition")
    if not isinstance(definition, dict):
        raise DeployError(f"Schema file {path} must include a definition object")
    return payload


def sync_webhooks(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    function_registry: FunctionRegistry,
    resume_state: ResumeState,
) -> dict[tuple[str, str], dict[str, Any]]:
    stats = summary.get("webhooks")
    files = [
        item
        for item in load_artifact_files(config, cli_args, config.webhook_globs)
        if not should_skip_excluded_artifact_contexts(
            webhook_contexts_from_raw(item.data), config.orphan_excluded_contexts
        )
    ]
    logger.section("WEBHOOKS")
    existing_items = client.request("GET", "/webhooks")
    existing_index = {(item["context"], item["name"]): item for item in existing_items}
    detail_cache: dict[str, dict[str, Any]] = {}
    existing_route_index = build_remote_webhook_route_index(
        client, existing_items, detail_cache
    )

    if not files:
        logger.muted("No local webhook files found")
        return existing_index

    logger.muted(f"Found {len(files)} local webhook file(s)")
    logger.action("Syncing webhooks...")
    for item in files:
        try:
            payload = build_webhook_payload(item.path, item.data, function_registry)
            context = payload["context"]
            name = payload["name"]
            resume_key = f"webhook:{context}:{name}"
            route_key = webhook_route_key_from_mapping(payload)
            logger.item(
                f"{name} ({context})", file_path=item.path, repo_root=config.repo_root
            )
            fingerprint = payload_resume_fingerprint(payload)
            if not cli_args.force and resume_state.is_done(resume_key, fingerprint):
                logger.ok("Skipped (resumed)")
                stats.skipped += 1
                continue
            existing = existing_index.get((context, name))
            if existing is None:
                existing = existing_route_index.get(route_key)
                if existing is None:
                    existing = find_existing_webhook_by_route(
                        client,
                        payload,
                        existing_items,
                        detail_cache,
                    )
                if existing is not None:
                    existing_index[(context, name)] = existing
                    if webhook_route_key_is_complete(route_key):
                        existing_route_index[route_key] = existing

            validate_endpoint_payload(
                "UpdateWebhookHandleDto" if existing else "CreateWebhookHandleDto",
                payload,
            )
            if existing:
                detail = detail_cache.get(existing["id"])
                if detail is None:
                    detail = client.request("GET", f"/webhooks/{existing['id']}")
                    detail_cache[existing["id"]] = detail
                require_owned_context(detail.get("context"), config)
                local_normalized, remote_normalized = normalize_webhook_subset(
                    payload, detail
                )
                if local_normalized == remote_normalized and not cli_args.force:
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                if local_normalized == remote_normalized:
                    logger.detail("force: updating unchanged webhook")
                else:
                    for diff_line in summarize_function_payload_diff(
                        local_normalized, remote_normalized
                    ):
                        logger.detail(f"diff: {diff_line}")
                if should_skip_unchanged(cli_args, local_normalized, remote_normalized):
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                log_forced_unchanged_update(
                    logger, cli_args, local_normalized, remote_normalized
                )
                for diff_line in summarize_function_payload_diff(
                    local_normalized, remote_normalized
                ):
                    logger.detail(f"diff: {diff_line}")
                allowed, reason = can_update(
                    local_timestamp=item.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    logger.blocked(reason or "Update blocked")
                    stats.blocked += 1
                    continue
                if not cli_args.dry_run:
                    updated = client.request(
                        "PATCH",
                        f"/webhooks/{existing['id']}",
                        payload=payload,
                        expected=(200,),
                    )
                    existing_index[(context, name)] = updated
                    if webhook_route_key_is_complete(route_key):
                        existing_route_index[route_key] = updated
                    resume_state.mark_done(resume_key, fingerprint)
                    logger.ok("Updated")
                else:
                    logger.planned("Would update")
                stats.updated += 1
                continue

            if not cli_args.dry_run:
                try:
                    created = client.request(
                        "POST", "/webhooks", payload=payload, expected=(201,)
                    )
                except DeployError as exc:
                    if not isinstance(exc, ApiError) or exc.status != 409:
                        raise
                    existing_items = client.request("GET", "/webhooks")
                    detail_cache.clear()
                    existing = existing_index.get((context, name))
                    if existing is None:
                        existing = find_existing_webhook_by_route(
                            client,
                            payload,
                            existing_items,
                            detail_cache,
                        )
                    if webhook_route_key_is_complete(route_key):
                        existing_route_index = build_remote_webhook_route_index(
                            client,
                            existing_items,
                            detail_cache,
                        )
                        if existing is None:
                            existing = existing_route_index.get(route_key)
                    if existing is None:
                        raise
                    require_owned_context(existing.get("context"), config)
                    updated = client.request(
                        "PATCH",
                        f"/webhooks/{existing['id']}",
                        payload=payload,
                        expected=(200,),
                    )
                    existing_index[(context, name)] = updated
                    if webhook_route_key_is_complete(route_key):
                        existing_route_index[route_key] = updated
                    resume_state.mark_done(resume_key, fingerprint)
                    logger.ok("Updated")
                    stats.updated += 1
                    continue
                existing_index[(context, name)] = created
                if webhook_route_key_is_complete(route_key):
                    existing_route_index[route_key] = created
                resume_state.mark_done(resume_key, fingerprint)
                logger.ok("Created")
            else:
                existing_index[(context, name)] = {
                    **payload,
                    "id": f"planned:webhook:{context}.{name}",
                }
                logger.planned("Would create")
            stats.created += 1
        except DeployError as exc:
            logger.fail(str(exc))
            stats.failed += 1

    return existing_index


def build_webhook_payload(
    path: Path,
    data: dict[str, Any],
    function_registry: FunctionRegistry,
) -> dict[str, Any]:
    payload = {
        key: copy.deepcopy(value)
        for key, value in data.items()
        if key
        in {
            "name",
            "context",
            "eventPayload",
            "templateBody",
            "eventPayloadType",
            "eventPayloadTypeSchema",
            "description",
            "visibility",
            "state",
            "responsePayload",
            "responseHeaders",
            "responseStatus",
            "slug",
            "subpath",
            "method",
            "requirePolyApiKey",
            "securityFunctions",
            "xmlParserOptions",
            "enabled",
            "ownerUserId",
        }
    }
    if "name" not in payload or "context" not in payload:
        raise DeployError(f"Webhook file {path} must include name and context")

    security_functions = payload.get("securityFunctions")
    if isinstance(security_functions, list):
        resolved_security_functions: list[dict[str, Any]] = []
        for entry in security_functions:
            if not isinstance(entry, dict) or "id" not in entry:
                raise DeployError(f"Invalid security function entry in {path}")
            resolved_security_functions.append(
                {
                    "id": resolve_function_id(
                        entry["id"],
                        function_registry,
                        required_type="server",
                    ),
                    **({"message": entry["message"]} if "message" in entry else {}),
                }
            )
        payload["securityFunctions"] = resolved_security_functions

    if "method" in payload:
        payload["method"] = normalize_webhook_method_component(payload["method"])
    event_payload_type = str(payload.get("eventPayloadType") or "").lower()
    if event_payload_type and event_payload_type != "object":
        payload.pop("eventPayloadTypeSchema", None)
    elif (
        payload.get("eventPayload") is not None and "eventPayloadTypeSchema" in payload
    ):
        payload.pop("eventPayloadTypeSchema", None)

    return payload


def normalize_webhook_route_component(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().strip("/")
    normalized = WEBHOOK_TEMPLATE_TOKEN_PATTERN.sub("{}", normalized)
    normalized = re.sub(r"/{2,}", "/", normalized)
    if "?" in normalized:
        path, query = normalized.split("?", 1)
        query_parts = [part for part in query.split("&") if part]
        normalized = f"{path}?{'&'.join(sorted(query_parts))}"
    return normalized


def normalize_webhook_method_component(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().upper()


def webhook_route_key_from_mapping(data: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_webhook_method_component(data.get("method")),
        normalize_webhook_route_component(data.get("slug")),
        normalize_webhook_route_component(data.get("subpath")),
    )


def webhook_route_key_is_complete(route_key: tuple[str, str, str]) -> bool:
    return bool(route_key[0] and route_key[1] and route_key[2])


def normalize_webhook_for_compare(value: Any) -> Any:
    normalized = canonicalize(value)
    if not isinstance(normalized, dict):
        return normalized
    webhook = copy.deepcopy(normalized)
    if "slug" in webhook:
        webhook["slug"] = normalize_webhook_route_component(webhook["slug"])
    if "subpath" in webhook:
        webhook["subpath"] = normalize_webhook_route_component(webhook["subpath"])
    if isinstance(webhook.get("method"), str):
        webhook["method"] = webhook["method"].upper()
    if isinstance(webhook.get("eventPayloadType"), str):
        webhook["eventPayloadType"] = webhook["eventPayloadType"].lower()
    return webhook


def normalize_webhook_subset(
    payload: dict[str, Any], remote: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_normalized, remote_normalized = normalize_dict_subset(payload, remote)
    return normalize_webhook_for_compare(
        local_normalized
    ), normalize_webhook_for_compare(remote_normalized)


def build_remote_webhook_route_index(
    client: PolyApiClient,
    existing_items: list[dict[str, Any]],
    detail_cache: dict[str, dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    route_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in existing_items:
        route_key = webhook_route_key_from_mapping(item)
        if not webhook_route_key_is_complete(route_key):
            detail = detail_cache.get(item["id"])
            if detail is None:
                detail = client.request("GET", f"/webhooks/{item['id']}")
                detail_cache[item["id"]] = detail
            route_key = webhook_route_key_from_mapping(detail)
        if webhook_route_key_is_complete(route_key):
            route_index[route_key] = item
    return route_index


def find_existing_webhook_by_route(
    client: PolyApiClient,
    payload: dict[str, Any],
    existing_items: list[dict[str, Any]],
    detail_cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    route_key = webhook_route_key_from_mapping(payload)
    if not webhook_route_key_is_complete(route_key):
        return None
    route_index = build_remote_webhook_route_index(client, existing_items, detail_cache)
    return route_index.get(route_key)


def resolve_function_id(
    raw_value: Any,
    registry: FunctionRegistry,
    *,
    required_type: FunctionType | None = None,
) -> str:
    if not isinstance(raw_value, str):
        raise DeployError(
            f"Expected a string function reference, got {type(raw_value).__name__}"
        )
    if UUID_PATTERN.match(raw_value):
        return raw_value
    match = FUNCTION_REF_PATTERN.match(raw_value)
    if not match:
        raise DeployError(
            f"Function reference {raw_value!r} must be a UUID or use the format {{context.name}}"
        )
    context = match.group("context")
    name = match.group("name")
    if required_type is not None:
        return registry.require_kind_id(required_type, context, name)
    return registry.require_id(context, name)


def sync_jobs(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    function_registry: FunctionRegistry,
    resume_state: ResumeState,
) -> None:
    stats = summary.get("jobs")
    files = [
        item
        for item in load_artifact_files(config, cli_args, config.job_globs)
        if not should_skip_excluded_artifact_contexts(
            job_contexts_from_raw(item.data), config.orphan_excluded_contexts
        )
    ]
    logger.section("JOBS")
    if not files:
        logger.muted("No local job files found")
        return

    existing_items = client.request("GET", "/jobs")
    existing_index = {item["name"]: item for item in existing_items}
    if len(existing_index) != len(existing_items):
        raise DeployError("Duplicate remote job names; cannot select an update target")
    detail_cache: dict[str, dict[str, Any]] = {}

    logger.muted(f"Found {len(files)} local job file(s)")
    logger.action("Syncing jobs...")
    for item in files:
        try:
            payload = build_job_payload(item.path, item.data, function_registry)
            validate_endpoint_payload(
                "UpdateJobDto" if payload["name"] in existing_index else "CreateJobDto",
                payload,
            )
            name = payload["name"]
            resume_key = f"job:{name}"
            logger.item(name, file_path=item.path, repo_root=config.repo_root)
            fingerprint = payload_resume_fingerprint(payload)
            if not cli_args.force and resume_state.is_done(resume_key, fingerprint):
                logger.ok("Skipped (resumed)")
                stats.skipped += 1
                continue
            existing = existing_index.get(name)

            if existing:
                detail = detail_cache.get(existing["id"])
                if detail is None:
                    detail = client.request("GET", f"/jobs/{existing['id']}")
                    detail_cache[existing["id"]] = detail
                require_owned_function_ids(
                    [entry.get("id") for entry in detail.get("functions", [])],
                    function_registry,
                    config,
                    resource_name=name,
                )
                local_normalized, remote_normalized = normalize_job_subset(
                    payload, detail
                )
                if local_normalized == remote_normalized and not cli_args.force:
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                if local_normalized == remote_normalized:
                    logger.detail("force: updating unchanged job")
                else:
                    for diff_line in summarize_function_payload_diff(
                        local_normalized, remote_normalized
                    ):
                        logger.detail(f"diff: {diff_line}")
                if should_skip_unchanged(cli_args, local_normalized, remote_normalized):
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                log_forced_unchanged_update(
                    logger, cli_args, local_normalized, remote_normalized
                )
                for diff_line in summarize_function_payload_diff(
                    local_normalized, remote_normalized
                ):
                    logger.detail(f"diff: {diff_line}")
                allowed, reason = can_update(
                    local_timestamp=item.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    logger.blocked(reason or "Update blocked")
                    stats.blocked += 1
                    continue
                if not cli_args.dry_run:
                    client.request(
                        "PATCH",
                        f"/jobs/{existing['id']}",
                        payload=payload,
                        expected=(200,),
                    )
                    resume_state.mark_done(resume_key, fingerprint)
                    logger.ok("Updated")
                else:
                    logger.planned("Would update")
                stats.updated += 1
                continue

            if not cli_args.dry_run:
                created = client.request(
                    "POST", "/jobs", payload=payload, expected=(201,)
                )
                existing_index[name] = created
                resume_state.mark_done(resume_key, fingerprint)
                logger.ok("Created")
            else:
                logger.planned("Would create")
            stats.created += 1
        except DeployError as exc:
            logger.fail(str(exc))
            stats.failed += 1


def build_job_payload(
    path: Path,
    data: dict[str, Any],
    function_registry: FunctionRegistry,
) -> dict[str, Any]:
    payload = copy.deepcopy(ensure_mapping(data, path))
    functions = payload.get("functions")
    if not isinstance(functions, list) or not functions:
        raise DeployError(f"Job file {path} must include a non-empty functions list")
    resolved_functions: list[dict[str, Any]] = []
    for function_entry in functions:
        if not isinstance(function_entry, dict):
            raise DeployError(f"Invalid job function entry in {path}")
        resolved_entry = copy.deepcopy(function_entry)
        if "id" in resolved_entry:
            resolved_entry["id"] = resolve_function_id(
                resolved_entry["id"],
                function_registry,
                required_type="server",
            )
        elif "functionName" in resolved_entry and "functionContext" in resolved_entry:
            resolved_entry["id"] = function_registry.require_kind_id(
                "server",
                resolved_entry["functionContext"],
                resolved_entry["functionName"],
            )
            resolved_entry.pop("functionName", None)
            resolved_entry.pop("functionContext", None)
        else:
            raise DeployError(
                f"Job file {path} needs either id or functionName/functionContext in each functions entry"
            )
        resolved_functions.append(resolved_entry)
    payload["functions"] = resolved_functions
    if "name" not in payload:
        raise DeployError(f"Job file {path} must include name")
    return payload


def normalize_job_subset(
    payload: dict[str, Any], remote: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_normalized, remote_normalized = normalize_dict_subset(payload, remote)
    normalize_unreported_job_limits_in_place(local_normalized, remote_normalized)
    normalize_unreported_job_defaults_in_place(local_normalized, remote_normalized)
    return local_normalized, remote_normalized


def normalize_unreported_job_limits_in_place(
    local: dict[str, Any], remote: dict[str, Any]
) -> None:
    for key in ("maxConcurrentJobs", "maxExecutionTime"):
        local.pop(key, None)
        remote.pop(key, None)


def normalize_unreported_job_defaults_in_place(local: Any, remote: Any) -> None:
    if isinstance(local, dict) and isinstance(remote, dict):
        default_values = {
            "eventPayload": {},
            "paramsPayload": {},
            "headersPayload": {},
            "enabled": False,
            "acceptableDelayTime": 30,
        }
        for key, default_value in default_values.items():
            if local.get(key) != default_value:
                continue
            if key in remote and remote.get(key) not in (None, default_value):
                continue
            local.pop(key, None)
            remote.pop(key, None)
        for key in sorted(set(local) & set(remote)):
            normalize_unreported_job_defaults_in_place(local[key], remote[key])
        return
    if isinstance(local, list) and isinstance(remote, list):
        for local_item, remote_item in zip(local, remote):
            normalize_unreported_job_defaults_in_place(local_item, remote_item)


def sync_triggers(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
    otp_manager: OtpManager,
    resume_state: ResumeState,
) -> None:
    stats = summary.get("triggers")
    files = [
        item
        for item in load_artifact_files(config, cli_args, config.trigger_globs)
        if not should_skip_excluded_artifact_contexts(
            trigger_contexts_from_raw(item.data), config.orphan_excluded_contexts
        )
    ]
    logger.section("TRIGGERS")
    if not files:
        logger.muted("No local trigger files found")
        return

    existing_items = client.request("GET", "/triggers")
    detail_cache: dict[str, dict[str, Any]] = {}

    logger.muted(f"Found {len(files)} local trigger file(s)")
    logger.action("Syncing triggers...")
    for item in files:
        try:
            resolved = build_trigger_spec(
                item.path, item.data, function_registry, webhook_index
            )
            payload = resolved["create_payload"]
            validate_endpoint_payload("CreateTriggerDto", payload)
            semantic = resolved["semantic"]
            label = resolved["label"]
            resume_key = f"trigger:{item.path.relative_to(config.repo_root).as_posix()}"
            fingerprint = payload_resume_fingerprint(
                normalize_function_payload(payload)
            )
            logger.item(label, file_path=item.path, repo_root=config.repo_root)
            if not cli_args.force and resume_state.is_done(resume_key, fingerprint):
                logger.ok("Skipped (resumed)")
                stats.skipped += 1
                continue
            existing = find_existing_trigger(existing_items, semantic)

            if existing:
                detail = detail_cache.get(existing["id"])
                if detail is None:
                    detail = client.request("GET", f"/triggers/{existing['id']}")
                    detail_cache[existing["id"]] = detail
                existing_semantic = trigger_semantic_from_remote(detail)
                if "ownerUserId" not in semantic:
                    owner = existing_semantic.pop("ownerUserId", None)
                    if owner is not None:
                        payload["ownerUserId"] = owner
                require_owned_function_ids(
                    [existing_semantic.get("serverFunctionId")],
                    function_registry,
                    config,
                    resource_name=semantic.get("name"),
                )
                same_trigger = canonicalize(semantic) == canonicalize(existing_semantic)
                if same_trigger and not cli_args.force:
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                if same_trigger:
                    logger.detail("force: updating unchanged trigger")
                semantic_normalized = canonicalize(semantic)
                existing_semantic_normalized = canonicalize(existing_semantic)
                if should_skip_unchanged(
                    cli_args, semantic_normalized, existing_semantic_normalized
                ):
                    logger.ok("Skipped")
                    stats.skipped += 1
                    continue
                log_forced_unchanged_update(
                    logger, cli_args, semantic_normalized, existing_semantic_normalized
                )
                allowed, reason = can_update(
                    local_timestamp=item.local_timestamp,
                    remote=detail,
                    config=config,
                    cli_args=cli_args,
                )
                if not allowed:
                    logger.blocked(reason or "Update blocked")
                    stats.blocked += 1
                    continue

                patch_payload = build_trigger_patch_payload(semantic, existing_semantic)
                if patch_payload == {} and cli_args.force:
                    patch_payload = build_force_trigger_patch_payload(semantic)
                if patch_payload is not None:
                    if not cli_args.dry_run:
                        if patch_payload:
                            client.request(
                                "PATCH",
                                f"/triggers/{existing['id']}",
                                payload=patch_payload,
                                expected=(200,),
                            )
                        resume_state.mark_done(resume_key, fingerprint)
                        logger.ok("Updated")
                    else:
                        logger.planned("Would update")
                    stats.updated += 1
                    continue

                if not cli_args.dry_run:
                    client.request(
                        "DELETE",
                        f"/triggers/{existing['id']}",
                        expected=(200, 204),
                        headers=otp_manager.delete_headers(),
                    )
                    created = client.request(
                        "POST", "/triggers", payload=payload, expected=(201,)
                    )
                    existing_items = [
                        item for item in existing_items if item["id"] != existing["id"]
                    ]
                    existing_items.append(created)
                    resume_state.mark_done(resume_key, fingerprint)
                    logger.ok("Recreated")
                else:
                    logger.planned("Would recreate")
                stats.updated += 1
                continue

            if not cli_args.dry_run:
                created = client.request(
                    "POST", "/triggers", payload=payload, expected=(201,)
                )
                existing_items.append(created)
                resume_state.mark_done(resume_key, fingerprint)
                logger.ok("Created")
            else:
                logger.planned("Would create")
            stats.created += 1
        except DeployError as exc:
            logger.fail(str(exc))
            stats.failed += 1


def build_missing_trigger_webhook_message(
    *,
    path: Path,
    source: dict[str, Any],
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> str:
    context = source["webhookContext"]
    name = source["webhookName"]
    available_names = sorted(
        webhook_name
        for webhook_context, webhook_name in webhook_index
        if webhook_context == context
    )
    if available_names:
        known = ", ".join(available_names)
        return (
            f"Unable to resolve trigger webhook {context}.{name} from {path}. "
            f"Known webhook names in {context}: {known}"
        )
    return f"Unable to resolve trigger webhook {context}.{name} from {path}"


def build_trigger_spec(
    path: Path,
    data: dict[str, Any],
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    destination = ensure_mapping(data.get("destination"), path)
    function_id = function_registry.require_kind_id(
        "server",
        destination["functionContext"],
        destination["functionName"],
    )
    name = data.get("name")
    enabled = data.get("enabled", True)
    owner = {"ownerUserId": data["ownerUserId"]} if "ownerUserId" in data else {}

    if "source" in data:
        source = ensure_mapping(data["source"], path)
        webhook = webhook_index.get((source["webhookContext"], source["webhookName"]))
        if not webhook:
            raise DeployError(
                build_missing_trigger_webhook_message(
                    path=path,
                    source=source,
                    webhook_index=webhook_index,
                )
            )
        wait_for_response = data.get("waitForResponse", True)
        create_payload = {
            **({"name": name} if name else {}),
            **owner,
            "source": {"webhookHandleId": webhook["id"]},
            "destination": {"serverFunctionId": function_id},
            "waitForResponse": wait_for_response,
            "enabled": enabled,
        }
        semantic = {
            "name": name,
            **owner,
            "webhookHandleId": webhook["id"],
            "serverFunctionId": function_id,
            "waitForResponse": wait_for_response,
            "enabled": enabled,
        }
        label = (
            f"trigger {name}"
            if name
            else f"trigger webhook:{source['webhookContext']}.{source['webhookName']} -> {destination['functionContext']}.{destination['functionName']}"
        )
        return {
            "create_payload": create_payload,
            "semantic": semantic,
            "label": label,
        }

    error_handler = ensure_mapping(data.get("errorHandler"), path)
    create_payload = {
        **({"name": name} if name else {}),
        **owner,
        "source": {"errorHandler": copy.deepcopy(error_handler)},
        "destination": {"serverFunctionId": function_id},
        "enabled": enabled,
    }
    semantic = {
        "name": name,
        **owner,
        "errorHandler": copy.deepcopy(error_handler),
        "serverFunctionId": function_id,
        "enabled": enabled,
    }
    label = (
        f"trigger {name}"
        if name
        else f"trigger error:{error_handler['path']} -> {destination['functionContext']}.{destination['functionName']}"
    )
    return {
        "create_payload": create_payload,
        "semantic": semantic,
        "label": label,
    }


def find_existing_trigger(
    existing_items: list[dict[str, Any]],
    semantic: dict[str, Any],
) -> dict[str, Any] | None:
    name = semantic.get("name")
    if name:
        matches = [item for item in existing_items if item.get("name") == name]
        if len(matches) > 1:
            raise DeployError("Duplicate remote trigger names")
        if matches:
            return matches[0]
    webhook_handle_id = semantic.get("webhookHandleId")
    if webhook_handle_id:
        for item in existing_items:
            same_webhook = item.get("webhookHandleId") == webhook_handle_id
            same_function = item.get("serverFunctionId") == semantic.get(
                "serverFunctionId"
            )
            if same_webhook and same_function:
                return item
        return None
    error_handler = semantic.get("errorHandler") or {}
    for item in existing_items:
        remote_error = item.get("errorHandler") or {}
        same_error_path = remote_error.get("path") == error_handler.get("path")
        same_function = item.get("serverFunctionId") == semantic.get("serverFunctionId")
        if same_error_path and same_function:
            return item
    return None


def trigger_semantic_from_remote(detail: dict[str, Any]) -> dict[str, Any]:
    owner = {"ownerUserId": detail["ownerUserId"]} if "ownerUserId" in detail else {}
    if detail.get("webhookHandleId"):
        return {
            **owner,
            "name": detail.get("name"),
            "webhookHandleId": detail.get("webhookHandleId"),
            "serverFunctionId": detail.get("serverFunctionId"),
            "waitForResponse": detail.get("waitForResponse", True),
            "enabled": detail.get("enabled", True),
        }
    return {
        **owner,
        "name": detail.get("name"),
        "errorHandler": copy.deepcopy(detail.get("errorHandler") or {}),
        "serverFunctionId": detail.get("serverFunctionId"),
        "enabled": detail.get("enabled", True),
    }


def build_trigger_patch_payload(
    desired: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any] | None:
    if desired.get("webhookHandleId") != current.get("webhookHandleId"):
        return None
    if desired.get("serverFunctionId") != current.get("serverFunctionId"):
        return None
    if canonicalize(desired.get("errorHandler")) != canonicalize(
        current.get("errorHandler")
    ):
        return None
    payload: dict[str, Any] = {}
    for key in ("name", "waitForResponse", "enabled", "ownerUserId"):
        if key in desired and desired.get(key) != current.get(key):
            payload[key] = desired.get(key)
    return payload if payload else {}


def build_force_trigger_patch_payload(semantic: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("name", "waitForResponse", "enabled", "ownerUserId"):
        if key in semantic:
            payload[key] = semantic.get(key)
    return payload


def collapse_context_prefixes(
    contexts: set[str], excluded_contexts: tuple[str, ...] = ()
) -> tuple[str, ...]:
    ordered_contexts = sorted(
        {item.strip() for item in contexts if item and item.strip()},
        key=lambda item: (item.count("."), len(item), item),
    )
    if excluded_contexts:
        resolved_contexts = resolve_contexts_against_exclusions(
            ordered_contexts,
            ordered_contexts,
            excluded_contexts,
        )
        return tuple(
            sorted(
                resolved_contexts,
                key=lambda item: (item.count("."), len(item), item),
            )
        )

    collapsed: list[str] = []
    for context in ordered_contexts:
        if any(
            context == prefix or context.startswith(f"{prefix}.")
            for prefix in collapsed
        ):
            continue
        collapsed.append(context)
    return tuple(collapsed)


def context_matches_prefixes(context: str | None, prefixes: tuple[str, ...]) -> bool:
    if not context or not prefixes:
        return False
    return any(
        context == prefix or context.startswith(f"{prefix}.") for prefix in prefixes
    )


def context_is_orphan_excluded(
    context: str | None, excluded_prefixes: tuple[str, ...]
) -> bool:
    return context_matches_prefixes(context, excluded_prefixes)


def context_is_orphan_skipped(context: str | None, config: DeployConfig) -> bool:
    return not owns_context(context, config) or context_is_orphan_excluded(
        context, config.orphan_excluded_contexts
    )


def any_context_is_orphan_excluded(
    contexts: set[str] | tuple[str, ...] | list[str],
    excluded_prefixes: tuple[str, ...],
) -> bool:
    return any(
        context_is_orphan_excluded(context, excluded_prefixes) for context in contexts
    )


def should_skip_excluded_artifact_contexts(
    contexts: set[str] | tuple[str, ...] | list[str],
    excluded_prefixes: tuple[str, ...],
) -> bool:
    return bool(contexts) and any_context_is_orphan_excluded(
        contexts, excluded_prefixes
    )


def extract_function_ref_context(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    match = FUNCTION_REF_PATTERN.match(raw_value)
    if not match:
        return None
    return match.group("context")


def job_contexts_from_raw(data: dict[str, Any]) -> set[str]:
    contexts: set[str] = set()
    functions = data.get("functions")
    if not isinstance(functions, list):
        return contexts
    for entry in functions:
        if not isinstance(entry, dict):
            continue
        function_context = entry.get("functionContext")
        if isinstance(function_context, str) and function_context.strip():
            contexts.add(function_context)
            continue
        ref_context = extract_function_ref_context(entry.get("id"))
        if ref_context:
            contexts.add(ref_context)
    return contexts


def trigger_contexts_from_raw(data: dict[str, Any]) -> set[str]:
    contexts: set[str] = set()
    source = data.get("source")
    if isinstance(source, dict):
        webhook_context = source.get("webhookContext")
        if isinstance(webhook_context, str) and webhook_context.strip():
            contexts.add(webhook_context)
    destination = data.get("destination")
    if isinstance(destination, dict):
        function_context = destination.get("functionContext")
        if isinstance(function_context, str) and function_context.strip():
            contexts.add(function_context)
    return contexts


def webhook_contexts_from_raw(data: dict[str, Any]) -> set[str]:
    contexts: set[str] = set()
    context = data.get("context")
    if isinstance(context, str) and context.strip():
        contexts.add(context)
    security_functions = data.get("securityFunctions")
    if not isinstance(security_functions, list):
        return contexts
    for entry in security_functions:
        if not isinstance(entry, dict):
            continue
        ref_context = extract_function_ref_context(entry.get("id"))
        if ref_context:
            contexts.add(ref_context)
    return contexts


def collect_local_context_prefixes(
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
) -> tuple[str, ...]:
    contexts: set[str] = set()

    if "functions" in config.enabled_types:
        for spec in load_function_specs(config, cli_args, logger):
            if context_is_orphan_excluded(
                spec.context, config.orphan_excluded_contexts
            ):
                continue
            contexts.add(spec.context)
            if spec.source_path.suffix != ".py":
                continue
            try:
                code = strip_deploy_receipt(
                    spec.source_path.read_text(encoding="utf-8")
                )
            except OSError as error:
                raise DeployError(
                    "Unable to read function source while resolving local SDK "
                    f"contexts: {spec.source_path}"
                ) from error
            contexts.update(
                infer_generate_contexts(code, config.orphan_excluded_contexts)
            )

    if "variables" in config.enabled_types:
        for item in load_artifact_files(config, cli_args, config.variable_globs):
            context = item.data.get("context")
            if isinstance(context, str) and not context_is_orphan_excluded(
                context, config.orphan_excluded_contexts
            ):
                contexts.add(context)

    if "webhooks" in config.enabled_types:
        for item in load_artifact_files(config, cli_args, config.webhook_globs):
            contexts.update(
                context
                for context in webhook_contexts_from_raw(item.data)
                if not context_is_orphan_excluded(
                    context, config.orphan_excluded_contexts
                )
            )

    if "tables" in config.enabled_types:
        for item in load_artifact_files(config, cli_args, config.table_globs):
            context = item.data.get("context")
            if (
                isinstance(context, str)
                and context.strip()
                and not context_is_orphan_excluded(
                    context, config.orphan_excluded_contexts
                )
            ):
                contexts.add(context)

    if "schemas" in config.enabled_types:
        for item in load_artifact_files(config, cli_args, config.schema_globs):
            context = item.data.get("context")
            if (
                isinstance(context, str)
                and context.strip()
                and not context_is_orphan_excluded(
                    context, config.orphan_excluded_contexts
                )
            ):
                contexts.add(context)

    if "jobs" in config.enabled_types:
        for item in load_artifact_files(config, cli_args, config.job_globs):
            contexts.update(
                context
                for context in job_contexts_from_raw(item.data)
                if not context_is_orphan_excluded(
                    context, config.orphan_excluded_contexts
                )
            )

    if "triggers" in config.enabled_types:
        for item in load_artifact_files(config, cli_args, config.trigger_globs):
            contexts.update(
                context
                for context in trigger_contexts_from_raw(item.data)
                if not context_is_orphan_excluded(
                    context, config.orphan_excluded_contexts
                )
            )

    return collapse_context_prefixes(contexts, config.orphan_excluded_contexts)


def build_local_variable_keys(
    config: DeployConfig,
    cli_args: CliArgs,
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in load_artifact_files(config, cli_args, config.variable_globs):
        payload = build_variable_payload(item.path, item.data)
        if context_is_orphan_excluded(
            payload["context"], config.orphan_excluded_contexts
        ):
            continue
        keys.add((payload["context"], payload["name"]))
    return keys


def build_local_table_keys(
    config: DeployConfig,
    cli_args: CliArgs,
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in load_artifact_files(config, cli_args, config.table_globs):
        data = ensure_mapping(item.data, item.path)
        context = data.get("context") or ""
        if context_is_orphan_excluded(context, config.orphan_excluded_contexts):
            continue
        keys.add((context, data["name"]))
    return keys


def build_local_schema_keys(
    config: DeployConfig,
    cli_args: CliArgs,
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in load_artifact_files(config, cli_args, config.schema_globs):
        data = ensure_mapping(item.data, item.path)
        if context_is_orphan_excluded(data["context"], config.orphan_excluded_contexts):
            continue
        keys.add((data["context"], data["name"]))
    return keys


def build_local_webhook_keys(
    config: DeployConfig,
    cli_args: CliArgs,
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in load_artifact_files(config, cli_args, config.webhook_globs):
        data = ensure_mapping(item.data, item.path)
        if should_skip_excluded_artifact_contexts(
            webhook_contexts_from_raw(data), config.orphan_excluded_contexts
        ):
            continue
        keys.add((data["context"], data["name"]))
    return keys


def build_local_job_names(
    config: DeployConfig,
    cli_args: CliArgs,
) -> set[str]:
    names: set[str] = set()
    for item in load_artifact_files(config, cli_args, config.job_globs):
        data = ensure_mapping(item.data, item.path)
        if should_skip_excluded_artifact_contexts(
            job_contexts_from_raw(data), config.orphan_excluded_contexts
        ):
            continue
        names.add(data["name"])
    return names


def trigger_key_from_semantic(semantic: dict[str, Any]) -> str:
    webhook_handle_id = semantic.get("webhookHandleId")
    server_function_id = semantic.get("serverFunctionId")
    if webhook_handle_id:
        return f"webhook:{webhook_handle_id}:{server_function_id}"
    error_handler = semantic.get("errorHandler") or {}
    return f"error:{error_handler.get('path')}:{server_function_id}"


def build_local_trigger_names_and_keys(
    config: DeployConfig,
    cli_args: CliArgs,
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    keys: set[str] = set()
    for item in load_artifact_files(config, cli_args, config.trigger_globs):
        if should_skip_excluded_artifact_contexts(
            trigger_contexts_from_raw(item.data), config.orphan_excluded_contexts
        ):
            continue
        resolved = build_trigger_spec(
            item.path, item.data, function_registry, webhook_index
        )
        semantic = resolved["semantic"]
        name = semantic.get("name")
        if isinstance(name, str) and name:
            names.add(name)
            continue
        keys.add(trigger_key_from_semantic(semantic))
    return names, keys


def trigger_key_from_remote(trigger: dict[str, Any]) -> str:
    webhook_handle_id = trigger.get("webhookHandleId")
    server_function_id = trigger.get("serverFunctionId")
    if webhook_handle_id:
        return f"webhook:{webhook_handle_id}:{server_function_id}"
    error_handler = trigger.get("errorHandler") or {}
    return f"error:{error_handler.get('path')}:{server_function_id}"


def remote_contexts_for_job(
    detail: dict[str, Any],
    function_context_by_id: dict[str, str],
) -> set[str]:
    contexts: set[str] = set()
    for entry in detail.get("functions") or []:
        if not isinstance(entry, dict):
            continue
        function_id = entry.get("id")
        if isinstance(function_id, str):
            context = function_context_by_id.get(function_id)
            if context:
                contexts.add(context)
    return contexts


def remote_contexts_for_trigger(
    trigger: dict[str, Any],
    function_context_by_id: dict[str, str],
    webhook_context_by_id: dict[str, str],
) -> set[str]:
    contexts: set[str] = set()
    webhook_handle_id = trigger.get("webhookHandleId")
    if isinstance(webhook_handle_id, str):
        webhook_context = webhook_context_by_id.get(webhook_handle_id)
        if webhook_context:
            contexts.add(webhook_context)
    server_function_id = trigger.get("serverFunctionId")
    if isinstance(server_function_id, str):
        function_context = function_context_by_id.get(server_function_id)
        if function_context:
            contexts.add(function_context)
    return contexts


def format_orphan_context(contexts: set[str]) -> str:
    return ", ".join(sorted(contexts))


def context_matches_prefix(context: str, prefix: str) -> bool:
    return context == prefix or context.startswith(f"{prefix}.")


def find_context_mapping(
    context: str,
    mappings: tuple[ContextPathMapping, ...],
) -> ContextPathMapping | None:
    matches = [
        item
        for item in mappings
        if context_matches_prefix(context, item.context_prefix)
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (item.context_prefix.count("."), len(item.context_prefix)),
    )


def find_artifact_default_mapping(
    artifact_type: ArtifactType,
    mappings: tuple[ContextPathMapping, ...],
) -> ContextPathMapping | None:
    for mapping in mappings:
        if mapping.context_prefix == artifact_type:
            return mapping
    return None


def orphan_exact_mapping_keys(orphan: OrphanedResource) -> tuple[str, ...]:
    if (
        orphan.artifact_type
        in {"variables", "webhooks", "tables", "functions", "schemas"}
        and orphan.context
    ):
        return (f"{orphan.context}.{orphan.name}",)
    return ()


def find_exact_orphan_mapping(
    orphan: OrphanedResource,
    mappings: tuple[ContextPathMapping, ...],
) -> ContextPathMapping | None:
    lookup_keys = orphan_exact_mapping_keys(orphan)
    for key in lookup_keys:
        for mapping in mappings:
            if mapping.context_prefix == key:
                return mapping
    return None


def prompt_for_context_path_mapping(
    context: str, repo_root: Path
) -> ContextPathMapping:
    prompt = textwrap.dedent(f"""
        No STORK_CONTEXT_PATH_MAP match for context `{context}`.
        Enter a mapping in the form <context_prefix>=<repo_path>
        Example: {context}=workflows/src/utilities/example
        Mapping: """).replace("\n", " ")
    while True:
        try:
            raw_value = input(prompt).strip()
        except EOFError as exc:
            raise DeployError("Context path mapping prompt cancelled") from exc
        except KeyboardInterrupt as exc:
            print()
            raise DeployError("Context path mapping prompt cancelled") from exc
        if not raw_value:
            raise DeployError(
                "Context path mapping is required to adopt orphaned artefacts"
            )
        mappings = parse_context_path_map(repo_root, raw_value)
        if len(mappings) != 1:
            print(
                "Please provide exactly one mapping in the form <context_prefix>=<repo_path>."
            )
            continue
        mapping = mappings[0]
        if not context_matches_prefix(context, mapping.context_prefix):
            print(
                f"Context prefix {mapping.context_prefix!r} does not match {context!r}."
            )
            continue
        return mapping


def prompt_for_orphan_root_path(orphan: OrphanedResource, repo_root: Path) -> Path:
    context_label = orphan.context or ", ".join(orphan.contexts) or "(no context)"
    prompt = (
        f"Choose a local repo path for orphaned {orphan.artifact_type} {orphan.name!r} "
        f"({context_label}): "
    )
    while True:
        try:
            raw_value = input(prompt).strip()
        except EOFError as exc:
            raise DeployError("Orphan adoption prompt cancelled") from exc
        except KeyboardInterrupt as exc:
            print()
            raise DeployError("Orphan adoption prompt cancelled") from exc
        if not raw_value:
            raise DeployError(
                "A local repo path is required to adopt orphaned artefacts"
            )
        root_path = resolve_path(repo_root, raw_value)
        if not is_relative_to(root_path, repo_root):
            print("The local repo path must be inside this repository.")
            continue
        return root_path


def resolve_orphan_root_path(
    orphan: OrphanedResource,
    *,
    config: DeployConfig,
    runtime_mappings: dict[str, ContextPathMapping],
) -> Path:
    all_mappings = (
        *config.context_path_mappings,
        *runtime_mappings.values(),
    )
    exact_mapping = find_exact_orphan_mapping(orphan, tuple(all_mappings))
    if exact_mapping is not None:
        return exact_mapping.root_path
    default_mapping = find_artifact_default_mapping(
        orphan.artifact_type, tuple(all_mappings)
    )
    if orphan.artifact_type == "triggers" and default_mapping is not None:
        return default_mapping.root_path
    contexts = tuple(dict.fromkeys(context for context in orphan.contexts if context))
    if not contexts:
        if default_mapping is not None:
            return default_mapping.root_path
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise DeployError(
                f"Cannot adopt {orphan.artifact_type} {orphan.name!r} without an interactive terminal"
            )
        mapping = prompt_for_context_path_mapping(
            orphan.artifact_type, config.repo_root
        )
        runtime_mappings[mapping.context_prefix] = mapping
        return mapping.root_path

    resolved_roots: dict[Path, set[str]] = {}
    unresolved_contexts: list[str] = []

    for context in contexts:
        matched_mapping = find_context_mapping(context, tuple(all_mappings))
        if matched_mapping is None:
            unresolved_contexts.append(context)
            continue
        resolved_roots.setdefault(matched_mapping.root_path, set()).add(context)

    for context in unresolved_contexts:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise DeployError(
                f"Missing STORK_CONTEXT_PATH_MAP entry for context {context!r} in non-interactive mode"
            )
        mapping = prompt_for_context_path_mapping(context, config.repo_root)
        runtime_mappings[mapping.context_prefix] = mapping
        resolved_roots.setdefault(mapping.root_path, set()).add(context)

    if len(resolved_roots) == 1:
        return next(iter(resolved_roots))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        roots = ", ".join(
            str(path.relative_to(config.repo_root)) for path in resolved_roots
        )
        raise DeployError(
            f"Conflicting local roots for orphaned {orphan.artifact_type} {orphan.name!r}: {roots}"
        )
    return prompt_for_orphan_root_path(orphan, config.repo_root)


def resolve_orphan_target_filename(
    orphan: OrphanedResource,
    *,
    config: DeployConfig,
    runtime_mappings: dict[str, ContextPathMapping],
) -> str | None:
    all_mappings = (
        *config.context_path_mappings,
        *runtime_mappings.values(),
    )
    exact_mapping = find_exact_orphan_mapping(orphan, tuple(all_mappings))
    if exact_mapping is not None and exact_mapping.target_filename is not None:
        return exact_mapping.target_filename
    default_mapping = find_artifact_default_mapping(
        orphan.artifact_type, tuple(all_mappings)
    )
    if orphan.artifact_type == "triggers" and default_mapping is not None:
        return default_mapping.target_filename
    contexts = tuple(dict.fromkeys(context for context in orphan.contexts if context))
    if not contexts:
        return default_mapping.target_filename if default_mapping is not None else None

    filenames: set[str] = set()
    for context in contexts:
        mapping = find_context_mapping(context, tuple(all_mappings))
        if mapping is None or mapping.target_filename is None:
            continue
        filenames.add(mapping.target_filename)
    if len(filenames) == 1:
        return next(iter(filenames))
    return None


def sanitize_filename_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized.strip("._") or "artifact"


def build_orphan_target_path(
    orphan: OrphanedResource,
    *,
    root_path: Path,
    target_filename: str | None = None,
) -> Path:
    subdir = LOCAL_ORPHAN_ARTIFACT_SUBDIRS.get(orphan.artifact_type)
    if not subdir or orphan.artifact_type == "functions":
        raise DeployError(f"Local adoption is not supported for {orphan.artifact_type}")
    subdir_path = Path(subdir)
    if tuple(root_path.parts[-len(subdir_path.parts) :]) == subdir_path.parts:
        base_dir = root_path
    else:
        base_dir = root_path / subdir
    if target_filename:
        target = base_dir / Path(target_filename).name
    else:
        filename = sanitize_filename_component(orphan.name)
        target = base_dir / f"{filename}.json"
    if not target.exists():
        return target
    fallback = sanitize_filename_component(
        f"{orphan.context}.{orphan.name}" if orphan.context else orphan.name
    )
    return base_dir / f"{fallback}.json"


def select_dict_fields(
    data: dict[str, Any], allowed_keys: tuple[str, ...]
) -> dict[str, Any]:
    return {key: copy.deepcopy(data[key]) for key in allowed_keys if key in data}


def build_local_variable_document(detail: dict[str, Any]) -> dict[str, Any]:
    document = select_dict_fields(
        detail,
        (
            "expiresAt",
            "context",
            "name",
            "description",
            "value",
            "secret",
            "secrecy",
            "visibility",
            "state",
        ),
    )
    if is_secret_variable_payload(document):
        document["value"] = ""
    return document


def build_local_webhook_document(
    detail: dict[str, Any],
    function_registry: FunctionRegistry,
) -> dict[str, Any]:
    document = select_dict_fields(
        detail,
        (
            "name",
            "context",
            "eventPayload",
            "eventPayloadType",
            "eventPayloadTypeSchema",
            "description",
            "visibility",
            "state",
            "responsePayload",
            "responseHeaders",
            "responseStatus",
            "slug",
            "subpath",
            "method",
            "requirePolyApiKey",
            "securityFunctions",
            "xmlParserOptions",
            "enabled",
        ),
    )
    security_functions = document.get("securityFunctions")
    if isinstance(security_functions, list):
        rewritten: list[dict[str, Any]] = []
        for entry in security_functions:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            rewritten_entry = copy.deepcopy(entry)
            if isinstance(entry_id, str):
                match = function_registry.find_by_id(entry_id, function_type="server")
                if match is not None:
                    _kind, function_item = match
                    rewritten_entry["id"] = (
                        f"{{{{{function_item['context']}.{function_item['name']}}}}}"
                    )
            rewritten.append(rewritten_entry)
        document["securityFunctions"] = rewritten
    return document


def strip_read_only_keys(data: dict[str, Any]) -> dict[str, Any]:
    ignored_keys = {
        "id",
        "createdAt",
        "createdBy",
        "updatedAt",
        "updatedBy",
        "lastUpdatedAt",
        "lastUpdatedBy",
        "ownerUserId",
        "_id",
        "__v",
    }
    return {
        key: copy.deepcopy(value)
        for key, value in data.items()
        if key not in ignored_keys
    }


def build_local_table_document(detail: dict[str, Any]) -> dict[str, Any]:
    return strip_read_only_keys(detail)


def build_local_schema_document(detail: dict[str, Any]) -> dict[str, Any]:
    return select_dict_fields(
        detail,
        (
            "name",
            "context",
            "definition",
            "description",
            "visibility",
            "state",
            "ownerUserId",
        ),
    )


def build_local_job_document(
    detail: dict[str, Any],
    function_registry: FunctionRegistry,
) -> dict[str, Any]:
    document = strip_read_only_keys(detail)
    functions = document.get("functions")
    if isinstance(functions, list):
        rewritten_functions: list[dict[str, Any]] = []
        for entry in functions:
            if not isinstance(entry, dict):
                continue
            rewritten_entry = copy.deepcopy(entry)
            entry_id = rewritten_entry.get("id")
            if isinstance(entry_id, str):
                match = function_registry.find_by_id(entry_id, function_type="server")
                if match is not None:
                    _kind, function_item = match
                    rewritten_entry.pop("id", None)
                    rewritten_entry["functionContext"] = function_item["context"]
                    rewritten_entry["functionName"] = function_item["name"]
            rewritten_functions.append(rewritten_entry)
        document["functions"] = rewritten_functions
    return document


def build_webhook_id_index(
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in webhook_index.values()
        if isinstance(item.get("id"), str)
    }


def get_trigger_webhook_id(detail: dict[str, Any]) -> str | None:
    source = detail.get("source") or {}
    source_webhook = source.get("webhook") if isinstance(source, dict) else {}
    webhook = detail.get("webhook") or {}
    candidates = (
        detail.get("webhookHandleId"),
        source.get("webhookHandleId") if isinstance(source, dict) else None,
        source_webhook.get("id") if isinstance(source_webhook, dict) else None,
        webhook.get("id") if isinstance(webhook, dict) else None,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def get_trigger_server_function_id(detail: dict[str, Any]) -> str | None:
    destination = detail.get("destination") or {}
    server_function = (
        destination.get("serverFunction") if isinstance(destination, dict) else {}
    )
    function = detail.get("serverFunction") or {}
    candidates = (
        detail.get("serverFunctionId"),
        destination.get("serverFunctionId") if isinstance(destination, dict) else None,
        server_function.get("id") if isinstance(server_function, dict) else None,
        function.get("id") if isinstance(function, dict) else None,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def build_local_trigger_document(
    detail: dict[str, Any],
    *,
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    name = detail.get("name")
    if isinstance(name, str) and name:
        document["name"] = name
    document["enabled"] = detail.get("enabled", True)
    webhook_handle_id = get_trigger_webhook_id(detail)
    if webhook_handle_id:
        webhook_by_id = build_webhook_id_index(webhook_index)
        webhook = webhook_by_id.get(webhook_handle_id)
        if not webhook:
            raise DeployError(
                f"Unable to resolve trigger webhook id {webhook_handle_id} while adopting orphan"
            )
        document["source"] = {
            "webhookContext": webhook["context"],
            "webhookName": webhook["name"],
        }
        document["waitForResponse"] = detail.get("waitForResponse", True)
    else:
        document["errorHandler"] = copy.deepcopy(detail.get("errorHandler") or {})

    function_id = get_trigger_server_function_id(detail)
    if function_id is None:
        raise DeployError("Trigger orphan is missing serverFunctionId")
    match = function_registry.find_by_id(function_id, function_type="server")
    if match is None:
        raise DeployError(
            f"Unable to resolve server function id {function_id} while adopting orphan"
        )
    _kind, function_item = match
    document["destination"] = {
        "functionContext": function_item["context"],
        "functionName": function_item["name"],
    }
    return document


def build_local_orphan_document(
    orphan: OrphanedResource,
    detail: dict[str, Any],
    *,
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    if orphan.artifact_type == "variables":
        return build_local_variable_document(detail)
    if orphan.artifact_type == "webhooks":
        return build_local_webhook_document(detail, function_registry)
    if orphan.artifact_type == "jobs":
        return build_local_job_document(detail, function_registry)
    if orphan.artifact_type == "tables":
        return build_local_table_document(detail)
    if orphan.artifact_type == "schemas":
        return build_local_schema_document(detail)
    if orphan.artifact_type == "triggers":
        return build_local_trigger_document(
            detail,
            function_registry=function_registry,
            webhook_index=webhook_index,
        )
    raise DeployError(f"Local adoption is not supported for {orphan.artifact_type}")


def write_local_orphan_file(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def adopt_orphans_locally(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    orphans: list[OrphanedResource],
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> None:
    runtime_mappings: dict[str, ContextPathMapping] = {}
    remaining_orphans: list[OrphanedResource] = []
    by_type: dict[ArtifactType, list[OrphanedResource]] = {
        artifact_type: [] for artifact_type in DEFAULT_ENABLED_TYPES
    }
    for orphan in orphans:
        by_type[orphan.artifact_type].append(orphan)

    for artifact_type in DEFAULT_ENABLED_TYPES:
        items = by_type[artifact_type]
        if not items:
            continue
        if artifact_type == "functions":
            logger.muted(
                f"Found {len(items)} orphaned {artifact_type} resource(s); local adoption is not supported"
            )
            remaining_orphans.extend(items)
            continue
        action_text = "Would adopt" if cli_args.dry_run else "Adopting"
        logger.action(
            f"{action_text} {len(items)} orphaned {artifact_type} resource(s) into local files..."
        )
        for orphan in items:
            label = (
                f"{orphan.name} ({orphan.context})" if orphan.context else orphan.name
            )
            logger.item(label)
            try:
                root_path = resolve_orphan_root_path(
                    orphan,
                    config=config,
                    runtime_mappings=runtime_mappings,
                )
                target_filename = resolve_orphan_target_filename(
                    orphan,
                    config=config,
                    runtime_mappings=runtime_mappings,
                )
                target_path = build_orphan_target_path(
                    orphan,
                    root_path=root_path,
                    target_filename=target_filename,
                )
                if target_path.exists():
                    raise DeployError(f"Target file already exists: {target_path}")
                detail = client.request(
                    "GET", orphan.detail_path or orphan.delete_path, expected=(200,)
                )
                document = build_local_orphan_document(
                    orphan,
                    ensure_mapping(
                        detail, Path(orphan.detail_path or orphan.delete_path)
                    ),
                    function_registry=function_registry,
                    webhook_index=webhook_index,
                )
                if not cli_args.dry_run:
                    write_local_orphan_file(target_path, document)
                    logger.ok(
                        f"Created {relative_label(target_path, config.repo_root)}"
                    )
                else:
                    logger.planned(
                        f"Would create {relative_label(target_path, config.repo_root)}"
                    )
            except DeployError as exc:
                logger.fail(str(exc))
                summary.get(orphan.artifact_type).failed += 1
                remaining_orphans.append(orphan)
    summary.orphans.extend(remaining_orphans)


def collect_orphans(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[OrphanedResource], dict[str, int]]:
    orphan_config = replace(config, skip_paths=())
    repo_cli_args = replace(cli_args, path_filters=())
    logger.detail(
        "Orphan detection compares all remote resources against all local repo artefacts"
    )

    orphans: list[OrphanedResource] = []
    excluded_counts: dict[str, int] = {}
    server_functions = client.request("GET", "/functions/server")
    client_functions = client.request("GET", "/functions/client")
    function_context_by_id = {
        item["id"]: item["context"]
        for item in [*server_functions, *client_functions]
        if isinstance(item.get("context"), str)
    }
    webhook_context_by_id = {
        item["id"]: item["context"]
        for item in webhook_index.values()
        if isinstance(item.get("context"), str)
    }

    if "functions" in config.enabled_types:
        repo_function_specs = load_function_specs(orphan_config, repo_cli_args, logger)
        local_function_keys = {spec.identity for spec in repo_function_specs}
        function_groups: tuple[tuple[FunctionType, list[dict[str, Any]]], ...] = (
            ("client", client_functions),
            ("server", server_functions),
        )
        for function_type, items in function_groups:
            if cli_args.function_types and function_type not in cli_args.function_types:
                continue
            for item in items:
                context = item.get("context")
                if context_is_orphan_skipped(context, config):
                    excluded_counts["functions"] = (
                        excluded_counts.get("functions", 0) + 1
                    )
                    continue
                identity = (function_type, item["context"], item["name"])
                if identity in local_function_keys:
                    continue
                orphans.append(
                    OrphanedResource(
                        artifact_type="functions",
                        name=item["name"],
                        context=item["context"],
                        resource_id=item["id"],
                        delete_path=f"/functions/{function_type}/{item['id']}",
                        detail_path=f"/functions/{function_type}/{item['id']}",
                        function_type=function_type,
                        contexts=(item["context"],),
                    )
                )

    if "variables" in config.enabled_types:
        local_variable_keys = build_local_variable_keys(orphan_config, repo_cli_args)
        for item in client.request("GET", "/variables"):
            context = item.get("context")
            if context_is_orphan_skipped(context, config):
                excluded_counts["variables"] = excluded_counts.get("variables", 0) + 1
                continue
            variable_identity: tuple[str, str] = (item["context"], item["name"])
            if variable_identity in local_variable_keys:
                continue
            orphans.append(
                OrphanedResource(
                    artifact_type="variables",
                    name=item["name"],
                    context=item["context"],
                    resource_id=item["id"],
                    delete_path=f"/variables/{item['id']}",
                    detail_path=f"/variables/{item['id']}",
                    requires_otp=is_secret_variable_payload(item),
                    contexts=(item["context"],),
                )
            )

    if "tables" in config.enabled_types:
        local_table_keys = build_local_table_keys(orphan_config, repo_cli_args)
        for item in fetch_all_tables(client):
            context = item.get("context") or ""
            if context_is_orphan_skipped(context, config):
                excluded_counts["tables"] = excluded_counts.get("tables", 0) + 1
                continue
            table_identity: tuple[str, str] = (context, item["name"])
            if table_identity in local_table_keys:
                continue
            orphans.append(
                OrphanedResource(
                    artifact_type="tables",
                    name=item["name"],
                    context=context,
                    resource_id=item["id"],
                    delete_path=f"/tables/{item['id']}",
                    detail_path=f"/tables/{item['id']}",
                    contexts=((context,) if context else ()),
                )
            )

    if "schemas" in config.enabled_types:
        local_schema_keys = build_local_schema_keys(orphan_config, repo_cli_args)
        for item in client.request("GET", "/schemas"):
            context = item.get("context")
            if context_is_orphan_skipped(context, config):
                excluded_counts["schemas"] = excluded_counts.get("schemas", 0) + 1
                continue
            schema_identity: tuple[str, str] = (item["context"], item["name"])
            if schema_identity in local_schema_keys:
                continue
            orphans.append(
                OrphanedResource(
                    artifact_type="schemas",
                    name=item["name"],
                    context=item["context"],
                    resource_id=item["id"],
                    delete_path=f"/schemas/{item['id']}",
                    detail_path=f"/schemas/{item['id']}",
                    contexts=(item["context"],),
                )
            )

    if "webhooks" in config.enabled_types:
        local_webhook_keys = build_local_webhook_keys(orphan_config, repo_cli_args)
        for item in webhook_index.values():
            context = item.get("context")
            if context_is_orphan_skipped(context, config):
                excluded_counts["webhooks"] = excluded_counts.get("webhooks", 0) + 1
                continue
            webhook_identity: tuple[str, str] = (item["context"], item["name"])
            if webhook_identity in local_webhook_keys:
                continue
            orphans.append(
                OrphanedResource(
                    artifact_type="webhooks",
                    name=item["name"],
                    context=item["context"],
                    resource_id=item["id"],
                    delete_path=f"/webhooks/{item['id']}",
                    detail_path=f"/webhooks/{item['id']}",
                    contexts=(item["context"],),
                )
            )

    if "jobs" in config.enabled_types:
        local_job_names = build_local_job_names(orphan_config, repo_cli_args)
        job_detail_cache: dict[str, dict[str, Any]] = {}
        for item in client.request("GET", "/jobs"):
            detail = job_detail_cache.get(item["id"])
            if detail is None:
                detail = client.request("GET", f"/jobs/{item['id']}")
                job_detail_cache[item["id"]] = detail
            entries = detail.get("functions")
            if (
                not isinstance(entries, list)
                or not entries
                or any(
                    not isinstance(entry, dict)
                    or entry.get("id") not in function_context_by_id
                    for entry in entries
                )
            ):
                continue
            contexts = remote_contexts_for_job(detail, function_context_by_id)
            if contexts and any_context_is_orphan_excluded(
                contexts, config.orphan_excluded_contexts
            ):
                excluded_counts["jobs"] = excluded_counts.get("jobs", 0) + 1
                continue
            if item["name"] in local_job_names:
                continue
            orphans.append(
                OrphanedResource(
                    artifact_type="jobs",
                    name=item["name"],
                    context=format_orphan_context(contexts),
                    resource_id=item["id"],
                    delete_path=f"/jobs/{item['id']}",
                    detail_path=f"/jobs/{item['id']}",
                    contexts=tuple(sorted(contexts)),
                )
            )

    if "triggers" in config.enabled_types:
        local_trigger_names, local_trigger_keys = build_local_trigger_names_and_keys(
            orphan_config,
            repo_cli_args,
            function_registry,
            webhook_index,
        )
        trigger_detail_cache: dict[str, dict[str, Any]] = {}
        for item in client.request("GET", "/triggers"):
            detail = trigger_detail_cache.get(item["id"])
            if detail is None:
                detail = client.request("GET", f"/triggers/{item['id']}")
                trigger_detail_cache[item["id"]] = detail
            if detail.get("serverFunctionId") not in function_context_by_id:
                continue
            if (
                detail.get("webhookHandleId") is not None
                and detail["webhookHandleId"] not in webhook_context_by_id
            ):
                continue
            contexts = remote_contexts_for_trigger(
                detail, function_context_by_id, webhook_context_by_id
            )
            if contexts and any_context_is_orphan_excluded(
                contexts, config.orphan_excluded_contexts
            ):
                excluded_counts["triggers"] = excluded_counts.get("triggers", 0) + 1
                continue
            name = detail.get("name")
            if isinstance(name, str) and name and name in local_trigger_names:
                continue
            if not name and trigger_key_from_remote(detail) in local_trigger_keys:
                continue
            orphans.append(
                OrphanedResource(
                    artifact_type="triggers",
                    name=name or trigger_key_from_remote(detail),
                    context=format_orphan_context(contexts),
                    resource_id=detail["id"],
                    delete_path=f"/triggers/{detail['id']}",
                    detail_path=f"/triggers/{detail['id']}",
                    contexts=tuple(sorted(contexts)),
                )
            )

    # Jobs and triggers have no own context: every referenced resource must be owned.
    orphans = [
        item
        for item in orphans
        if item.contexts
        and all(
            not context_is_orphan_skipped(context, config) for context in item.contexts
        )
    ]
    return orphans, excluded_counts


def process_orphans(
    *,
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    function_registry: FunctionRegistry,
    webhook_index: dict[tuple[str, str], dict[str, Any]],
    otp_manager: OtpManager,
) -> bool:
    if cli_args.invalid_paths:
        logger.warn(
            "Orphan detection disabled because local artefacts failed validation."
        )
        return False
    if cli_args.path_filters or cli_args.seeds:
        logger.info("Scoped push: orphan detection is disabled.")
        return False
    orphans, excluded_counts = collect_orphans(
        client=client,
        config=config,
        cli_args=cli_args,
        logger=logger,
        function_registry=function_registry,
        webhook_index=webhook_index,
    )

    logger.section("ORPHANS")
    if not orphans:
        if excluded_counts:
            for artifact_type, count in sorted(excluded_counts.items()):
                logger.muted(
                    f"Skipped {count} {artifact_type} orphan(s) excluded by POLY_DEPLOY_EXCLUDE_ORPHAN_CONTEXTS"
                )
        logger.muted("No orphaned remote resources detected")
        return False

    by_type: dict[ArtifactType, list[OrphanedResource]] = {
        artifact_type: [] for artifact_type in DEFAULT_ENABLED_TYPES
    }
    for orphan in orphans:
        by_type[orphan.artifact_type].append(orphan)

    for artifact_type in DEFAULT_ENABLED_TYPES:
        if excluded_counts.get(artifact_type):
            logger.muted(
                f"Skipped {excluded_counts[artifact_type]} {artifact_type} orphan(s) excluded by POLY_DEPLOY_EXCLUDE_ORPHAN_CONTEXTS"
            )

    if cli_args.adopt_orphans:
        adopt_orphans_locally(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            orphans=orphans,
            function_registry=function_registry,
            webhook_index=webhook_index,
        )
        return False

    if not cli_args.delete_orphans:
        for artifact_type in DEFAULT_ENABLED_TYPES:
            items = by_type[artifact_type]
            if not items:
                continue
            logger.muted(
                f"Found {len(items)} orphaned {artifact_type} resource(s) (skipping deletion)"
            )
        summary.orphans.extend(orphans)
        return False

    functions_deleted = False
    deletion_order: tuple[ArtifactType, ...] = (
        "triggers",
        "jobs",
        "webhooks",
        "functions",
        "variables",
        "tables",
        "schemas",
    )
    for artifact_type in deletion_order:
        items = by_type[artifact_type]
        if not items:
            continue
        action_text = "Would delete" if cli_args.dry_run else "Deleting"
        logger.action(
            f"{action_text} {len(items)} orphaned {artifact_type} resource(s)..."
        )
        for orphan in items:
            label = (
                f"{orphan.name} ({orphan.context})" if orphan.context else orphan.name
            )
            logger.item(label)
            try:
                if not cli_args.dry_run:
                    client.request(
                        "DELETE",
                        orphan.delete_path,
                        expected=(200, 204),
                        headers=otp_manager.delete_headers(),
                    )
                    logger.ok("Deleted")
                else:
                    logger.planned("Would delete")
                summary.get(orphan.artifact_type).deleted += 1
                if orphan.artifact_type == "functions":
                    functions_deleted = True
            except DeployError as exc:
                logger.fail(str(exc))
                summary.get(orphan.artifact_type).failed += 1
    return functions_deleted


def print_summary(summary: Summary, logger: Logger) -> None:
    logger.info("")
    logger.info(multicolor_rule(enabled=logger.colors_enabled))
    logger.info(
        style_text(
            "SYNCHRONIZATION SUMMARY",
            rgb=CYAN,
            bold=True,
            enabled=logger.colors_enabled,
        )
    )
    logger.info(multicolor_rule(enabled=logger.colors_enabled))

    total = ArtifactStats()
    for artifact_type in DEFAULT_ENABLED_TYPES:
        stats = summary.get(artifact_type)
        total.created += stats.created
        total.updated += stats.updated
        total.deleted += stats.deleted
        total.skipped += stats.skipped
        total.blocked += stats.blocked
        total.failed += stats.failed

        if (
            sum(
                (
                    stats.created,
                    stats.updated,
                    stats.deleted,
                    stats.skipped,
                    stats.blocked,
                    stats.failed,
                )
            )
            == 0
        ):
            continue

        logger.info(
            style_text(
                artifact_type.capitalize() + ":",
                bold=True,
                enabled=logger.colors_enabled,
            )
        )
        if stats.created:
            logger.ok(f"Created: {stats.created}")
        if stats.updated:
            logger.ok(f"Updated: {stats.updated}")
        if stats.deleted:
            logger.ok(f"Deleted: {stats.deleted}")
        if stats.skipped:
            logger.ok(f"Skipped: {stats.skipped}")
        if stats.blocked:
            logger.blocked(f"Blocked: {stats.blocked}")
        if stats.failed:
            logger.fail(f"Failed: {stats.failed}")
        logger.info("")

    logger.info(style_text("Total:", bold=True, enabled=logger.colors_enabled))
    if total.created:
        logger.ok(f"Created: {total.created}")
    if total.updated:
        logger.ok(f"Updated: {total.updated}")
    if total.deleted:
        logger.ok(f"Deleted: {total.deleted}")
    if total.skipped:
        logger.ok(f"Skipped: {total.skipped}")
    if total.blocked:
        logger.blocked(f"Blocked: {total.blocked}")
    if total.failed:
        logger.fail(f"Failed: {total.failed}")

    logger.info(multicolor_rule(enabled=logger.colors_enabled))
    if total.failed or total.blocked:
        logger.info(
            style_text(
                "[WARNING] Completed with errors",
                rgb=RED,
                bold=True,
                enabled=logger.colors_enabled,
            )
        )
    else:
        logger.info(
            style_text(
                "[SUCCESS] Synchronization completed successfully",
                rgb=GREEN,
                bold=True,
                enabled=logger.colors_enabled,
            )
        )
    logger.info(multicolor_rule(enabled=logger.colors_enabled))

    if summary.orphans:
        logger.info("")
        logger.info(
            style_text(
                "[ORPHANED RESOURCES DETECTED]",
                rgb=YELLOW,
                bold=True,
                enabled=logger.colors_enabled,
            )
        )
        logger.muted("The following resources exist remotely but not locally:")
        logger.info("")
        for artifact_type in DEFAULT_ENABLED_TYPES:
            items = [
                orphan
                for orphan in summary.orphans
                if orphan.artifact_type == artifact_type
            ]
            if not items:
                continue
            logger.info(
                style_text(
                    f"{artifact_type.capitalize()} ({len(items)}):",
                    bold=True,
                    enabled=logger.colors_enabled,
                )
            )
            for orphan in items:
                label = (
                    f"{orphan.name} ({orphan.context})"
                    if orphan.context
                    else orphan.name
                )
                logger.info(
                    style_text(
                        f"    - {label}", dim=True, enabled=logger.colors_enabled
                    )
                )
            logger.info("")
        logger.warn(
            "To delete these orphaned resources, rerun with `push --delete-orphans`"
        )
        logger.warn(
            "To recreate supported orphaned resources locally, rerun with `push --adopt-orphans`"
        )
        logger.info(multicolor_rule(enabled=logger.colors_enabled))


def summary_has_incomplete_work(summary: Summary) -> bool:
    for artifact_type in DEFAULT_ENABLED_TYPES:
        stats = summary.get(artifact_type)
        if stats.failed or stats.blocked:
            return True
    return False


def print_config(
    config: DeployConfig,
    cli_args: CliArgs,
    *,
    env_file: EnvLoadResult | None = None,
    preflight: DeploymentPreflight | None = None,
) -> None:
    print("Resolved config:")
    print(
        json.dumps(
            {
                "repo_root": str(config.repo_root),
                "env_file": env_file.display_path if env_file is not None else None,
                "production_branch": (
                    preflight.production_branch if preflight is not None else None
                ),
                "current_git_branch": (
                    preflight.current_branch if preflight is not None else None
                ),
                "production_sensitive": (
                    preflight.production_sensitive if preflight is not None else None
                ),
                "enabled_types": list(config.enabled_types),
                "build_schema": cli_args.build_schema,
                "skip_invalid": cli_args.skip_invalid,
                "skip_validation_acknowledged": cli_args.skip_validation_acknowledged,
                "function_types": (
                    list(cli_args.function_types) if cli_args.function_types else None
                ),
                "skip_paths": [str(path) for path in config.skip_paths],
                "priority_paths": [str(path) for path in config.priority_paths],
                "function_discovery_globs": list(config.function_discovery_globs),
                "variable_globs": list(config.variable_globs),
                "webhook_globs": list(config.webhook_globs),
                "job_globs": list(config.job_globs),
                "table_globs": list(config.table_globs),
                "schema_globs": list(config.schema_globs),
                "trigger_globs": list(config.trigger_globs),
                "use_mtime_guard": config.use_mtime_guard,
                "remote_timestamp_fields": list(config.remote_timestamp_fields),
                "server_function_images": {
                    key: config.server_function_images[key]
                    for key in sorted(config.server_function_images)
                },
                "server_function_scripts": {
                    key: config.server_function_scripts[key]
                    for key in sorted(config.server_function_scripts)
                },
                "update_existing_secret_variables": config.update_existing_secret_variables,
                "recreate_variables_on_secrecy_change": config.recreate_variables_on_secrecy_change,
                "generate_after_function_sync": config.generate_after_function_sync,
                "generate_contexts": list(config.generate_contexts),
                "generate_names": list(config.generate_names),
                "generate_ids": list(config.generate_ids),
                "orphan_excluded_contexts": list(config.orphan_excluded_contexts),
                "context_path_mappings": {
                    (
                        item.target_filename
                        if item.target_filename is not None
                        else item.context_prefix
                    ): str(item.root_path)
                    for item in config.context_path_mappings
                },
                "initial_scope_prompt_enabled": config.initial_scope_prompt_enabled,
                "initial_scope_state_file": str(config.initial_scope_state_file),
                "http_timeout_seconds": config.http_timeout_seconds,
                "otp_enabled": config.otp_enabled,
            },
            indent=2,
        )
    )


def build_remote_webhook_index(
    client: PolyApiClient,
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (item["context"], item["name"]): item
        for item in client.request("GET", "/webhooks")
    }


def required_namespaces() -> tuple[str, ...]:
    raw = os.getenv("POLYAPI_NAMESPACES") or os.getenv("POLYAPI_NAMESPACE", "")
    namespaces = tuple(
        dict.fromkeys(part.strip() for part in raw.split(",") if part.strip())
    )
    if not namespaces or any(
        not all(
            part.isidentifier() and not keyword.iskeyword(part)
            for part in namespace.split(".")
        )
        for namespace in namespaces
    ):
        raise DeployError(
            "POLYAPI_NAMESPACE(S) must contain non-empty Python context paths"
        )
    return namespaces


def required_namespace() -> str:
    return required_namespaces()[0]


def owns_context(context: Any, config: DeployConfig) -> bool:
    return isinstance(context, str) and any(
        _context_is_same_or_child(context, namespace)
        for namespace in config.namespaces or (config.namespace,)
    )


def require_owned_context(context: Any, config: DeployConfig) -> None:
    if not owns_context(context, config):
        raise DeployError("Local resource context must start with POLYAPI_NAMESPACE(S)")


def require_owned_function_ids(
    identifiers: list[Any],
    registry: FunctionRegistry,
    config: DeployConfig,
    *,
    resource_name: str | None = None,
) -> None:
    if not identifiers:
        raise DeployError("Cannot establish ownership of the existing resource")
    for identifier in identifiers:
        match = registry.find_by_id(identifier, function_type="server")
        if not match or (
            context_is_orphan_skipped(match[1].get("context"), config)
            and not namespaced_resource_name(resource_name, config)
        ):
            raise DeployError("Existing resource references an unmanaged function")


def namespaced_resource_name(name: Any, config: DeployConfig) -> bool:
    return isinstance(name, str) and any(
        name.startswith(namespace + separator)
        for namespace in config.namespaces or (config.namespace,)
        for separator in (".", "_", "-")
    )


def extra_resource_globs(kind: str) -> tuple[str, ...]:
    return tuple(
        f"workflows/src/**/artefacts/{kind}/*.{extension}"
        for extension in ("json", "jsonc")
    )


def extra_resource_files(
    config: DeployConfig, cli_args: CliArgs, kind: str
) -> list[ArtifactFile]:
    files = load_artifact_files(config, cli_args, extra_resource_globs(kind))
    seen: set[tuple[Any, Any]] = set()
    selected = []
    for item in files:
        ensure_mapping(item.data, item.path)
        identity = (item.data.get("context"), item.data.get("name"))
        if kind != "canopy":
            require_owned_context(identity[0], config)
            if not isinstance(identity[1], str) or not identity[1]:
                raise DeployError("Resource name is required")
            if context_is_orphan_excluded(identity[0], config.orphan_excluded_contexts):
                continue
            if identity in seen:
                raise DeployError("Duplicate local resource identity")
            seen.add(identity)
        selected.append(item)
    return selected


def preflight_reference_records(
    data: Any,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]
]:
    """Validate portable reference shapes without resolving remote resources."""
    placeholder_id = "00000000-0000-4000-8000-000000000001"
    functions: dict[tuple[str, str], dict[str, Any]] = {}
    variables: dict[tuple[str, str], dict[str, Any]] = {}
    webhooks: dict[tuple[str, str], dict[str, Any]] = {}

    def add(target: dict, context: Any, name: Any) -> None:
        if not all(
            isinstance(value, str) and value.strip() for value in (context, name)
        ):
            raise DeployError("Portable references require a context and name")
        target[(context, name)] = {
            "context": context,
            "name": name,
            "id": placeholder_id,
        }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for prefix, target in (
                ("function", functions),
                ("variable", variables),
                ("webhook", webhooks),
            ):
                if prefix + "Context" in value or prefix + "Name" in value:
                    add(
                        target,
                        value.get(prefix + "Context"),
                        value.get(prefix + "Name"),
                    )
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str):
            match = FUNCTION_REF_PATTERN.fullmatch(value)
            if match:
                add(functions, match.group("context"), match.group("name"))

    visit(data)
    return list(functions.values()), list(variables.values()), webhooks


def validate_local_artifact(
    kind: str, path: Path, data: dict[str, Any], config: DeployConfig
) -> None:
    references = []
    if kind == "webhooks":
        references = [
            {"id": item.get("id")}
            for item in data.get("securityFunctions", [])
            if isinstance(item, dict)
        ]
    elif kind == "jobs":
        references = [
            {
                key: item[key]
                for key in ("functionName", "functionContext", "id")
                if key in item
            }
            for item in data.get("functions", [])
            if isinstance(item, dict)
        ]
    elif kind in {"triggers", "subscriptions"}:
        references = [data.get("source", {}), data.get("destination", {})]
    elif kind == "ai-functions":
        references = [data.get("systemPromptVariable", {})]
        for tool in data.get("toolFunctions", []):
            if isinstance(tool, dict):
                references.append(
                    {
                        key: tool[key]
                        for key in ("functionName", "functionContext")
                        if key in tool
                    }
                )
                references.extend(
                    {
                        key: argument[key]
                        for key in ("variableName", "variableContext")
                        if key in argument
                    }
                    for argument in tool.get("argumentConfig", [])
                    if isinstance(argument, dict)
                    and argument.get("source") == "variable"
                )
    if kind == "subscriptions":

        def collect_variables(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    collect_variables(item)
            elif isinstance(value, dict):
                if value.get("type") == "PolyVariable":
                    references.append(
                        {
                            key: value[key]
                            for key in ("variableName", "variableContext")
                            if key in value
                        }
                    )
                else:
                    for item in value.values():
                        collect_variables(item)

        collect_variables(data.get("paramsObject"))
    functions, variables, webhooks = preflight_reference_records(references)
    registry = FunctionRegistry()
    registry.load("server", functions)
    registry.load("client", functions)
    if kind == "variables":
        validate_endpoint_payload(
            "CreateVariableDto", build_variable_payload(path, data)
        )
    elif kind == "tables":
        validate_endpoint_payload(
            "CreateTableDto", strip_poly_managed_table_columns(data)
        )
    elif kind == "schemas":
        validate_endpoint_payload("CreateSchemaDto", build_schema_payload(path, data))
    elif kind == "webhooks":
        validate_endpoint_payload(
            "CreateWebhookHandleDto", build_webhook_payload(path, data, registry)
        )
    elif kind == "jobs":
        validate_endpoint_payload(
            "CreateJobDto", build_job_payload(path, data, registry)
        )
    elif kind == "triggers":
        validate_endpoint_payload(
            "CreateTriggerDto",
            build_trigger_spec(path, data, registry, webhooks)["create_payload"],
        )
    elif kind == "ai-functions":
        resolve_ai_payload(data, functions, variables, config)
    elif kind == "subscriptions":
        resolve_subscription_payload(data, functions, variables, config)
    elif kind == "canopy":
        validate_canopy(data, config)


def validate_local_artifacts(
    config: DeployConfig, cli_args: CliArgs, logger: Logger
) -> CliArgs:
    """Validate every selected local input before the first deployment write."""
    invalid = set(cli_args.invalid_paths)
    patterns = {
        "functions": config.function_discovery_globs,
        "variables": config.variable_globs,
        "webhooks": config.webhook_globs,
        "jobs": config.job_globs,
        "tables": config.table_globs,
        "schemas": config.schema_globs,
        "triggers": config.trigger_globs,
        **{
            kind: extra_resource_globs(kind)
            for kind in ("ai-functions", "canopy", "subscriptions")
        },
    }
    for kind in config.enabled_types:
        for path in discover_paths(
            config.repo_root, patterns[kind], config.skip_paths, config.priority_paths
        ):
            if not matches_path_filters(
                [path], cli_args.path_filters, config.repo_root
            ):
                continue
            try:
                if kind == "functions":
                    if path.suffix == ".py":
                        specs = load_function_specs_from_python(
                            path, config.orphan_excluded_contexts
                        )
                    elif path.suffix in {".json", ".jsonc"}:
                        specs = load_function_specs_from_manifest(path)
                    else:
                        specs = load_function_specs_from_shell(path)
                    for spec in specs:
                        if (
                            cli_args.function_types
                            and spec.function_type not in cli_args.function_types
                        ):
                            continue
                        if path_is_excluded(spec.source_path, config.skip_paths):
                            continue
                        payload = build_function_payload(
                            spec, excluded_contexts=config.orphan_excluded_contexts
                        )
                        schema = (
                            "CreateServerCustomFunctionDto"
                            if spec.function_type == "server"
                            else "CreateClientCustomFunctionDto"
                        )
                        validate_endpoint_payload(schema, payload)
                else:
                    data = load_json_document(path)
                    if context_is_orphan_excluded(
                        data.get("context"), config.orphan_excluded_contexts
                    ):
                        continue
                    validate_local_artifact(kind, path, data, config)
            except (
                DeployError,
                ImportError,
                ValueError,
                TypeError,
                KeyError,
                OSError,
                SyntaxError,
            ) as error:
                # Parser/provider diagnostics can contain payloads or credential
                # placeholders. Report only the source identity and error class.
                message = f"Local validation failed: {path.relative_to(config.repo_root)} ({type(error).__name__})"
                if not cli_args.skip_invalid:
                    raise DeployError(
                        message + "; fix the artefact or use --skip-invalid"
                    ) from None
                logger.warn(message + "; skipped")
                invalid.add(path)
    return replace(cli_args, invalid_paths=tuple(sorted(invalid)))


def validate_local_namespace(
    config: DeployConfig, cli_args: CliArgs, logger: Logger
) -> None:
    """Reject cross-project writes before the first provider request."""
    if "functions" in config.enabled_types:
        for spec in load_function_specs(config, cli_args, logger):
            require_owned_context(spec.context, config)
    patterns = {
        "variables": config.variable_globs,
        "webhooks": config.webhook_globs,
        "jobs": config.job_globs,
        "tables": config.table_globs,
        "schemas": config.schema_globs,
        "triggers": config.trigger_globs,
        **{
            kind: extra_resource_globs(kind)
            for kind in ("ai-functions", "canopy", "subscriptions")
        },
    }
    for kind in config.enabled_types:
        for item in load_artifact_files(config, cli_args, patterns.get(kind, ())):
            data = ensure_mapping(item.data, item.path)
            if kind == "canopy":
                validate_canopy(data, config)
            elif kind == "triggers":
                destination = data.get("destination", {})
                if (
                    not isinstance(destination, dict)
                    or not destination.get("functionContext")
                    or not destination.get("functionName")
                ):
                    raise DeployError(
                        "Triggers require a portable destination reference"
                    )
                if not namespaced_resource_name(data.get("name"), config):
                    require_owned_context(destination["functionContext"], config)
            elif kind == "jobs":
                entries = data.get("functions")
                if not isinstance(entries, list) or not entries:
                    raise DeployError("Local jobs require portable function references")
                contexts = [
                    job_contexts_from_raw({"functions": [entry]}) for entry in entries
                ]
                if any(
                    not group
                    or (
                        not namespaced_resource_name(data.get("name"), config)
                        and any(not owns_context(context, config) for context in group)
                    )
                    for group in contexts
                ):
                    raise DeployError(
                        "Local jobs must reference namespace-owned functions"
                    )
            else:
                require_owned_context(data.get("context"), config)


def resource_list(value: Any) -> list[dict[str, Any]]:
    """Accept the list envelopes used by the platform; malformed reads fail closed."""
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    if isinstance(value, dict):
        for key in ("results", "items", "functions", "subscriptions", "data"):
            if key in value:
                return resource_list(value[key])
    raise DeployError("Provider returned an invalid resource list")


def list_resources(client: PolyApiClient, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        data = client.request("GET", path, params={"page": page} if page > 1 else None)
        items.extend(resource_list(data))
        pagination = data.get("pagination") if isinstance(data, dict) else None
        if not isinstance(pagination, dict):
            return items
        pages = pagination.get("pages")
        if type(pages) is not int or not 0 <= pages <= 1000:
            raise DeployError("Provider returned invalid pagination")
        if page >= pages:
            return items
        page += 1


def resource_detail(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeployError("Provider returned an invalid resource detail")
    for key in ("data", "result", "function"):
        if key in value and isinstance(value[key], dict):
            return value[key]
    return value


def list_optional_scope_resources(
    client: PolyApiClient, path: str
) -> list[dict[str, Any]]:
    try:
        return list_resources(client, path)
    except ApiError as error:
        if error.status in {403, 404}:
            return []
        raise


def resource_id(item: dict[str, Any]) -> str:
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise DeployError("Provider resource is missing its ID")
    return quote(identifier, safe="")


def exact_reference(records: list[dict[str, Any]], context: Any, name: Any) -> str:
    if (
        not isinstance(context, str)
        or not isinstance(name, str)
        or not context
        or not name
    ):
        raise DeployError("Portable references require an exact context and name")
    matches = {
        item["id"]
        for item in records
        if item.get("context") == context
        and item.get("name") == name
        and isinstance(item.get("id"), str)
        and item["id"]
    }
    if len(matches) != 1:
        raise DeployError(f"Reference {context}.{name} has {len(matches)} matches")
    return matches.pop()


def referenceable_functions(
    client: PolyApiClient, kinds: tuple[str, ...]
) -> list[dict[str, Any]]:
    functions: list[dict[str, Any]] = []
    for kind in kinds:
        for suffix in ("", "/tenant", "/public"):
            reader = list_optional_scope_resources if suffix else list_resources
            functions.extend(reader(client, f"/functions/{kind}{suffix}"))
    return functions


def variable_reference(
    reference: dict[str, Any], variables: list[dict[str, Any]], config: DeployConfig
) -> str:
    require_owned_context(reference.get("variableContext"), config)
    return exact_reference(
        variables, reference.get("variableContext"), reference.get("variableName")
    )


def validate_endpoint_payload(name: str, payload: dict[str, Any]) -> None:
    schema_path = Path(__file__).parent / "schemas" / f"{name}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def normalize_nullable(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("nullable") and isinstance(value.get("type"), str):
                value["type"] = [value["type"], "null"]
            for nested in value.values():
                normalize_nullable(nested)
        elif isinstance(value, list):
            for nested in value:
                normalize_nullable(nested)

    normalize_nullable(schema)
    if name in {
        "CreateServerCustomFunctionDto",
        "CreateClientCustomFunctionDto",
        "CreateApiFunctionDto",
    }:
        # The Python SDK emits typeSchema as JSON text or None. The platform's
        # argument resolver accepts both; validate the decoded schema without
        # altering the SDK-shaped deployment payload.
        payload = copy.deepcopy(payload)
        for argument in (
            payload.get("arguments", [])
            if isinstance(payload.get("arguments", []), list)
            else []
        ):
            if isinstance(argument, dict) and "typeSchema" in argument:
                if argument["typeSchema"] is None:
                    argument.pop("typeSchema")
                elif isinstance(argument["typeSchema"], str):
                    try:
                        argument["typeSchema"] = json.loads(argument["typeSchema"])
                    except ValueError:
                        raise DeployError(
                            "Function argument typeSchema must contain valid JSON"
                        ) from None
    errors = list(Draft7Validator(schema).iter_errors(payload))
    if errors:
        # Validation messages may contain values, including credentials.
        path = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise DeployError(f"{name} validation failed at {path} ({errors[0].validator})")


def matching_remote(
    records: list[dict[str, Any]], data: dict[str, Any]
) -> dict[str, Any] | None:
    matches = [
        item
        for item in records
        if (item.get("context"), item.get("name"))
        == (data.get("context"), data.get("name"))
    ]
    if len(matches) > 1:
        raise DeployError("Duplicate remote resource identity")
    return matches[0] if matches else None


def project_remote(local: Any, remote: Any) -> Any:
    if isinstance(local, dict) and isinstance(remote, dict):
        return {
            key: project_remote(value, remote.get(key)) for key, value in local.items()
        }
    if isinstance(local, list) and isinstance(remote, list):
        # Keep length changes visible; removing a tool or UI row must cause an update.
        if len(local) != len(remote):
            return remote
        return [project_remote(left, right) for left, right in zip(local, remote)]
    return remote


def resolve_ai_payload(
    data: dict[str, Any],
    functions: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    config: DeployConfig,
) -> dict[str, Any]:
    require_owned_context(data.get("context"), config)
    if any(key in data for key in ("id", "systemPromptVariableId")):
        raise DeployError("AI function artefacts must use portable references")
    payload = copy.deepcopy(data)
    prompt = payload.pop("systemPromptVariable", None)
    if prompt is not None:
        if not isinstance(prompt, dict) or "systemPrompt" in payload:
            raise DeployError("Choose one inline systemPrompt or systemPromptVariable")
        payload["systemPromptVariableId"] = variable_reference(
            prompt, variables, config
        )
    if "toolFunctions" in payload:
        if not isinstance(payload["toolFunctions"], list):
            raise DeployError("toolFunctions must be an array")
        resolved_tools = []
        for tool in payload["toolFunctions"]:
            if not isinstance(tool, dict) or "id" in tool:
                raise DeployError("Tool functions must use portable references")
            resolved: dict[str, Any] = {
                "id": exact_reference(
                    functions, tool.get("functionContext"), tool.get("functionName")
                )
            }
            if "argumentConfig" in tool:
                if not isinstance(tool["argumentConfig"], list):
                    raise DeployError("argumentConfig must be an array")
                arguments = []
                for argument in tool["argumentConfig"]:
                    if not isinstance(argument, dict) or "variableId" in argument:
                        raise DeployError("Tool arguments must use portable references")
                    argument = dict(argument)
                    if argument.get("source") == "variable":
                        argument["variableId"] = variable_reference(
                            argument, variables, config
                        )
                        argument.pop("variableName")
                        argument.pop("variableContext")
                    arguments.append(argument)
                resolved["argumentConfig"] = arguments
            resolved_tools.append(resolved)
        payload["toolFunctions"] = resolved_tools
    validate_endpoint_payload("CreateAiFunctionV2Dto", payload)
    return payload


def extra_orphans(
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
    kind: ArtifactType,
    endpoint: str,
    files: list[ArtifactFile],
    records: list[dict[str, Any]],
) -> bool:
    if cli_args.path_filters or cli_args.invalid_paths or cli_args.seeds:
        return False
    # Skipped paths still own resources; a skip must never become a deletion.
    all_files = load_artifact_files(
        replace(config, skip_paths=()), cli_args, extra_resource_globs(kind)
    )
    identities = {(item.data["context"], item.data["name"]) for item in all_files}
    changed = False
    for item in records:
        context = item.get("context")
        if not isinstance(context, str) or context_is_orphan_skipped(context, config):
            continue
        if (context, item.get("name")) in identities:
            continue
        identifier = resource_id(item)
        orphan = OrphanedResource(
            artifact_type=kind,
            name=item["name"],
            context=context,
            resource_id=identifier,
            delete_path=f"{endpoint}/{identifier}",
            contexts=(context,),
        )
        if not cli_args.delete_orphans:
            summary.orphans.append(orphan)
            continue
        logger.info(
            f"{'Would delete' if cli_args.dry_run else 'Deleting'} {kind}: {context}.{item['name']}"
        )
        if not cli_args.dry_run:
            client.request("DELETE", orphan.delete_path)
        summary.get(kind).deleted += 1
        changed = True
    return changed


def include_planned_references(
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    functions: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    kinds: tuple[FunctionType, ...],
) -> None:
    if not cli_args.dry_run:
        return

    def append_missing(
        records: list[dict[str, Any]], context: str, name: str, kind: str
    ) -> None:
        if not any(
            (item.get("context"), item.get("name")) == (context, name)
            for item in records
        ):
            records.append(
                {
                    "id": f"planned:{kind}:{context}.{name}",
                    "context": context,
                    "name": name,
                }
            )

    if "functions" in config.enabled_types:
        for spec in load_function_specs(config, cli_args, logger):
            if spec.function_type in kinds:
                build_function_payload(spec)
                append_missing(functions, spec.context, spec.name, spec.function_type)
    if "variables" in config.enabled_types:
        for item in load_artifact_files(config, cli_args, config.variable_globs):
            payload = build_variable_payload(item.path, item.data)
            append_missing(variables, payload["context"], payload["name"], "variable")


def sync_ai_functions(
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
) -> bool:
    files = extra_resource_files(config, cli_args, "ai-functions")
    if not files and not cli_args.delete_orphans:
        return False
    records = list_resources(client, "/functions/ai")
    functions = (
        referenceable_functions(client, ("api", "server", "client"))
        if any(item.data.get("toolFunctions") for item in files)
        else []
    )
    variables = list_resources(client, "/variables") if files else []
    include_planned_references(
        config, cli_args, logger, functions, variables, ("server", "client")
    )
    # Resolve and validate the whole selected group before its first write.
    plans = []
    for item in files:
        payload = resolve_ai_payload(item.data, functions, variables, config)
        remote = matching_remote(records, item.data)
        if remote is None:
            plans.append(("POST", "/functions/ai", payload))
            continue
        path = f"/functions/ai/{resource_id(remote)}"
        detail = resource_detail(client.request("GET", path))
        changes = {
            key: value
            for key, value in payload.items()
            if value
            != (
                detail.get(key)
                if key in {"outputSchema", "returnTypeSchema"}
                else project_remote(value, detail.get(key))
            )
        }
        if any(key in changes for key in ("context", "provider")):
            raise DeployError("AI context/provider changes require explicit recreation")
        if not changes:
            summary.ai_functions.skipped += 1
            continue
        validate_endpoint_payload("UpdateAiFunctionV2Dto", changes)
        plans.append(("PATCH", path, changes))
    for method, path, payload in plans:
        logger.info(
            f"{'Would apply' if cli_args.dry_run else 'Applying'} {method} {path}"
        )
        if not cli_args.dry_run:
            client.request(method, path, payload=payload)
        stats = summary.ai_functions
        if method == "POST":
            stats.created += 1
        else:
            stats.updated += 1
    deleted = extra_orphans(
        client,
        config,
        cli_args,
        logger,
        summary,
        "ai-functions",
        "/functions/ai",
        files,
        records,
    )
    return bool(plans) or deleted


def validate_canopy(data: dict[str, Any], config: DeployConfig) -> None:
    if not any(
        isinstance(data.get(key), str) and data[key].strip()
        for key in ("name", "subpath", "id")
    ):
        raise DeployError("Canopy requires a name, subpath, or id")
    for key in ("name", "subpath", "id", "description"):
        if key in data and not isinstance(data[key], str):
            raise DeployError(f"Canopy {key} must be a string")
    if data.get("subpath") and not re.fullmatch(r"[A-Za-z0-9_.-]+", data["subpath"]):
        raise DeployError("Canopy subpath must be a single URL segment")
    if "visibility" in data and data["visibility"] not in {
        "ENVIRONMENT",
        "TENANT",
        "PUBLIC",
    }:
        raise DeployError("Invalid Canopy visibility")


def canopy_overrides() -> dict[str, str]:
    raw = os.getenv("CANOPY_APP_IDS", "").strip()
    if not raw:
        return {}
    try:
        pairs = (
            list(json.loads(raw).items())
            if raw.startswith("{")
            else [pair.split("=", 1) for pair in raw.split(",")]
        )
        result = {}
        for key, value in pairs:
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key.strip()
                or not value.strip()
                or key.strip() in result
            ):
                raise ValueError()
            result[key.strip()] = value.strip()
        return result
    except (ValueError, TypeError, AttributeError):
        raise DeployError(
            "CANOPY_APP_IDS must be a JSON object or subpath=id pairs"
        ) from None


def select_canopy_application(
    data: dict[str, Any], applications: list[dict[str, Any]], overrides: dict[str, str]
) -> dict[str, Any] | None:
    override = overrides.get(data.get("subpath", ""))
    if override:
        matches = [app for app in applications if app.get("id") == override]
        if len(matches) != 1:
            raise DeployError(
                "Canopy override must identify exactly one accessible application"
            )
        return matches[0]
    for key in ("id", "subpath", "name"):
        if not data.get(key):
            continue
        matches = [app for app in applications if app.get(key) == data[key]]
        if len(matches) > 1:
            raise DeployError(f"Ambiguous Canopy {key}")
        if matches:
            return matches[0]
    return None


def write_canopy_id(path: Path, identifier: str) -> None:
    # Write the unresolved source, never environment-substituted config values.
    raw = path.read_text(encoding="utf-8")
    document = json.loads(strip_json_comments(raw) if path.suffix == ".jsonc" else raw)
    document["id"] = identifier
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def sync_canopy(
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
) -> None:
    files = extra_resource_files(config, cli_args, "canopy")
    if not files:
        return
    applications = list_resources(client, "/applications")
    overrides = canopy_overrides()
    plans = []
    seen = set()
    for item in files:
        data = item.data
        validate_canopy(data, config)
        remote = select_canopy_application(data, applications, overrides)
        identity = (
            ("id", resource_id(remote))
            if remote
            else (
                ("subpath", data["subpath"])
                if data.get("subpath")
                else ("name", data.get("name"))
            )
        )
        if identity in seen:
            raise DeployError("Duplicate local Canopy target")
        seen.add(identity)
        app_config = {
            key: value
            for key, value in data.items()
            if key not in {"id", "description", "visibility"}
        }
        if remote is None:
            if not data.get("name"):
                raise DeployError("A new Canopy application requires a name")
            payload = {"name": data["name"], "config": app_config}
            for key in ("description", "visibility"):
                if key in data:
                    payload[key] = data[key]
            plans.append(("POST", "/applications", payload, item.path))
            continue
        path = f"/applications/{resource_id(remote)}"
        missing_config = False
        try:
            detail = client.request("GET", f"{path}/config")
        except ApiError as exc:
            if exc.status != 404:
                raise
            detail = {}
            missing_config = True
        description_changed = "description" in data and data[
            "description"
        ] != remote.get("description")
        if (
            not cli_args.force
            and not description_changed
            and app_config == project_remote(app_config, detail)
        ):
            summary.canopy.skipped += 1
            continue
        if description_changed or missing_config:
            update = {"config": app_config}
            if "description" in data:
                update["description"] = data["description"]
            plans.append(("PATCH", path, update, item.path))
        else:
            plans.append(("PUT", f"{path}/config", app_config, item.path))
    for method, path, payload, source in plans:
        logger.info(
            f"{'Would apply' if cli_args.dry_run else 'Applying'} {method} {path}"
        )
        if not cli_args.dry_run:
            result = client.request(method, path, payload=payload)
            if method == "POST":
                resource_id(result)
                write_canopy_id(source, result["id"])
        if method == "POST":
            summary.canopy.created += 1
        else:
            summary.canopy.updated += 1
    # Applications have no resource context. As in TS Flow, never infer Canopy orphans.


def resolve_subscription_payload(
    data: dict[str, Any],
    functions: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    config: DeployConfig,
) -> dict[str, Any]:
    require_owned_context(data.get("context"), config)
    if any(
        key in data for key in ("id", "functionId", "paramsVariableId", "paramsSfxId")
    ):
        raise DeployError("Subscription artefacts must use portable references")
    payload = copy.deepcopy(data)
    destination = payload.pop("destination", None)
    if not isinstance(destination, dict):
        raise DeployError("Subscription requires a destination function reference")
    payload["functionId"] = exact_reference(
        functions, destination.get("functionContext"), destination.get("functionName")
    )

    def resolve_params(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve_params(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") == "PolyVariable":
            if "id" in value:
                raise DeployError(
                    "PolyVariable must use variableName and variableContext"
                )
            identifier = variable_reference(value, variables, config)
            resolved = {
                key: nested
                for key, nested in value.items()
                if key not in {"variableName", "variableContext"}
            }
            resolved["id"] = identifier
            return resolved
        return {key: resolve_params(nested) for key, nested in value.items()}

    if "paramsObject" in payload:
        payload["paramsObject"] = resolve_params(payload["paramsObject"])
    validate_endpoint_payload("GraphQLSubscriptionCreateDto", payload)
    return payload


def confirm_subscription_recreate(identity: str) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DeployError("Subscription recreation requires an interactive terminal")
    return (
        input(
            f"Type YES to delete and recreate {identity}; its stream offset may reset: "
        ).strip()
        == "YES"
    )


def sync_subscriptions(
    client: PolyApiClient,
    config: DeployConfig,
    cli_args: CliArgs,
    logger: Logger,
    summary: Summary,
) -> None:
    files = extra_resource_files(config, cli_args, "subscriptions")
    if not files and not cli_args.delete_orphans:
        return
    records = list_resources(client, "/subscriptions/graphql")
    functions = referenceable_functions(client, ("server",)) if files else []
    variables = list_resources(client, "/variables") if files else []
    include_planned_references(
        config, cli_args, logger, functions, variables, ("server",)
    )
    plans = [
        (
            item,
            resolve_subscription_payload(item.data, functions, variables, config),
            matching_remote(records, item.data),
        )
        for item in files
    ]
    for item, payload, remote in plans:
        identity = f"{item.data['context']}.{item.data['name']}"
        if remote is not None and not cli_args.force:
            logger.warn(
                f"{identity}: updates are unsupported; --force requests confirmed recreation"
            )
            summary.subscriptions.blocked += 1
            continue
        if cli_args.dry_run:
            logger.info(
                f"Would {'recreate' if remote else 'create'} subscription {identity}"
            )
        else:
            if remote is not None:
                if not confirm_subscription_recreate(identity):
                    summary.subscriptions.blocked += 1
                    continue
                client.request(
                    "DELETE", f"/subscriptions/graphql/{resource_id(remote)}"
                )
                logger.info("Waiting 30 seconds for the streaming connection to close")
                time.sleep(30)
            # Do not retry uncertain creates or a partially completed recreation.
            client.request("POST", "/subscriptions/graphql", payload=payload)
        if remote is None:
            summary.subscriptions.created += 1
        else:
            summary.subscriptions.updated += 1
    extra_orphans(
        client,
        config,
        cli_args,
        logger,
        summary,
        "subscriptions",
        "/subscriptions/graphql",
        files,
        records,
    )


def main() -> int:
    if sys.argv[1:2] and sys.argv[1] in {
        "train-openapi",
        "train-model",
        "delete-content",
        "logs",
    }:
        from cli import operations

        try:
            return operations.main(sys.argv[1:])
        except (operations.flow.DeployError, OSError, ValueError) as error:
            message = (
                str(error)
                if isinstance(error, operations.flow.DeployError)
                else type(error).__name__
            )
            raise DeployError(message) from None
    if sys.argv[1:2] == ["setup"]:
        from cli.setup import main as setup_main

        return setup_main(sys.argv[2:])
    cli_args = parse_args()
    if cli_args is None:
        return 0
    if not cli_args.json_output:
        return run_flow(cli_args, {})
    report: dict[str, Any] = {}
    output = sys.stdout
    try:
        with redirect_stdout(sys.stderr):
            status = run_flow(cli_args, report)
        report["status"] = (
            "failed"
            if status
            else (
                "report"
                if cli_args.why or cli_args.hubs
                else "preview" if cli_args.dry_run else "completed"
            )
        )
        return status
    except (Exception, KeyboardInterrupt):
        report["status"] = "failed"
        raise
    finally:
        print(json.dumps(report, indent=2, sort_keys=True), file=output)


def run_flow(cli_args: CliArgs, report: dict[str, Any]) -> int:
    repo_root = find_repo_root()
    logger = Logger(verbose=cli_args.verbose)
    logger.machine_readable = cli_args.json_output
    offline = cli_args.dynamic and (
        cli_args.hubs
        or (
            cli_args.seed_from in {"specs", "none"}
            and (cli_args.dry_run or cli_args.why)
        )
    )
    if offline:
        cli_args = normalize_cli_path_filters(repo_root, cli_args)
        config = build_config(repo_root, cli_args)
        if cli_args.hubs:
            _, _, graph = load_function_graph(config, cli_args, logger)
            report.update(offline=True, hubs=graph.hubs())
        else:
            plan = plan_dynamic_functions(
                None,
                config,
                cli_args,
                logger,
                ResumeState(repo_root, False, read_only=True),
            )
            report.update(dynamic_plan_report(plan, config, cli_args))
        print_dynamic_report(report, cli_args, logger)
        logger.info(
            "Offline report only; target, native deployment payloads and remote-only dependencies are not verified."
        )
        return 0
    if cli_args.why:
        cli_args = replace(cli_args, dry_run=True)
    env_file = load_env(repo_root)
    warn_for_missing_default_env_file(env_file, logger)
    # Mandatory for every command
    target_env_id = get_target_env_id(env_file)
    cli_args = normalize_cli_path_filters(repo_root, cli_args)
    config = build_config(repo_root, cli_args)
    preflight = build_deployment_preflight(repo_root, env_file)
    if cli_args.show_config:
        print_config(config, cli_args, env_file=env_file, preflight=preflight)
        return 0

    print_startup_banner(logger=logger, config=config, cli_args=cli_args)
    print_deployment_visibility(logger, preflight, cli_args, target_env_id)
    if not cli_args.why:
        maybe_confirm_branch_env_file_match(repo_root, preflight, logger, cli_args)
        cli_args = maybe_prompt_for_initial_scope(cli_args, config, logger)
        execution_api_key = get_execution_api_key()
        enforce_production_safeguards(preflight, cli_args, execution_api_key)
        if cli_args.build_schema:
            from cli.schema_refresh import refresh, SchemaRefreshError

            try:
                refresh(repo_root)
            except (ValueError, OSError) as error:
                message = (
                    str(error)
                    if isinstance(error, SchemaRefreshError)
                    else type(error).__name__
                )
                raise DeployError(f"Schema refresh failed: {message}") from None
        if not cli_args.adopt_orphans:
            cli_args = validate_local_artifacts(config, cli_args, logger)
        validate_local_namespace(config, cli_args, logger)
        maybe_prompt_for_missing_env_tokens(config, cli_args, logger)
    otp_manager = OtpManager(config.otp_enabled)
    api_key, base_url = get_api_key_and_url()
    if not api_key or not base_url:
        raise DeployError(
            "Missing PolyAPI config. Set POLY_API_KEY and POLY_API_BASE_URL or run `python -m polyapi setup`."
        )

    summary = Summary()
    client = PolyApiClient(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=config.http_timeout_seconds,
        otp_manager=otp_manager,
    )

    # Verify the env file's pin against the live environment before so a mismatched env file never touches anything
    enforce_target_environment(client, target_env_id, env_file, logger)
    resume_state = ResumeState(
        repo_root,
        load=cli_args.resume,
        target=None if cli_args.why else (base_url, target_env_id),
        read_only=cli_args.dry_run,
    )

    if resume_state.pending_functions and not cli_args.dynamic:
        raise DeployError(
            "Resume the unfinished function tree with push-dynamic --resume"
        )
    function_registry = fetch_function_registry(client)
    webhook_index: dict[tuple[str, str], dict[str, Any]] = {}

    if cli_args.adopt_orphans:
        if any(
            kind in config.enabled_types
            for kind in ("ai-functions", "canopy", "subscriptions")
        ):
            raise DeployError(
                "Orphan adoption supports core resources only; select explicit --types."
            )
        webhook_index = build_remote_webhook_index(client)
        process_orphans(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            function_registry=function_registry,
            webhook_index=webhook_index,
            otp_manager=otp_manager,
        )
        print_summary(summary, logger)
        return 0

    dynamic_plan = None
    if cli_args.dynamic:
        dynamic_plan = plan_dynamic_functions(
            client, config, cli_args, logger, resume_state
        )
        report.update(dynamic_plan_report(dynamic_plan, config, cli_args))
        print_dynamic_report(report, cli_args, logger)
        if cli_args.why:
            return 0
        confirm_dynamic_plan(dynamic_plan, config, cli_args)

    if "variables" in config.enabled_types:
        sync_variables(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            otp_manager=otp_manager,
            resume_state=resume_state,
        )

    if "canopy" in config.enabled_types:
        sync_canopy(client, config, cli_args, logger, summary)

    if "tables" in config.enabled_types:
        sync_tables(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            resume_state=resume_state,
        )

    if "schemas" in config.enabled_types:
        sync_schemas(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            resume_state=resume_state,
        )

    if "functions" in config.enabled_types:
        if cli_args.dynamic and summary_has_incomplete_work(summary):
            logger.fail(
                "Function step skipped because prerequisite resources failed or were blocked."
            )
            print_summary(summary, logger)
            return 1
        function_registry, _ = sync_functions(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            resume_state=resume_state,
            dynamic_plan=dynamic_plan,
        )
        if cli_args.dynamic and summary_has_incomplete_work(summary):
            print_summary(summary, logger)
            return 1

    if "ai-functions" in config.enabled_types:
        sync_ai_functions(client, config, cli_args, logger, summary)

    owned_function_registry = function_registry
    if "webhooks" in config.enabled_types:
        # Shared resources are available for references only, never function sync.
        function_registry = reference_registry(client, function_registry)
        webhook_index = sync_webhooks(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            function_registry=function_registry,
            resume_state=resume_state,
        )
    else:
        webhook_index = build_remote_webhook_index(client)

    owned_webhook_index = webhook_index

    if "webhooks" not in config.enabled_types and {"triggers", "jobs"}.intersection(
        config.enabled_types
    ):
        function_registry = reference_registry(client, function_registry)

    if "triggers" in config.enabled_types:
        webhook_index = reference_webhooks(client, webhook_index)
        sync_triggers(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            function_registry=function_registry,
            webhook_index=webhook_index,
            otp_manager=otp_manager,
            resume_state=resume_state,
        )

    if "jobs" in config.enabled_types:
        sync_jobs(
            client=client,
            config=config,
            cli_args=cli_args,
            logger=logger,
            summary=summary,
            function_registry=function_registry,
            resume_state=resume_state,
        )

    if "subscriptions" in config.enabled_types:
        sync_subscriptions(client, config, cli_args, logger, summary)

    process_orphans(
        client=client,
        config=config,
        cli_args=cli_args,
        logger=logger,
        summary=summary,
        function_registry=owned_function_registry,
        webhook_index=owned_webhook_index,
        otp_manager=otp_manager,
    )

    should_generate = all(
        (
            bool({"functions", "ai-functions"}.intersection(config.enabled_types)),
            config.generate_after_function_sync,
            not cli_args.dry_run,
            not cli_args.skip_generate,
        )
    )
    if should_generate:
        run_poly_generate(config, logger, "after full sync", cli_args)

    print_summary(summary, logger)
    if summary_has_incomplete_work(summary):
        logger.warn(f"Keeping {ResumeState.FILENAME} so the run can be resumed.")
        return 1

    if not cli_args.dry_run:
        resume_state.clear()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except DeployError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
