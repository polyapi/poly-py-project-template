"""Customize starter contexts, SDK references, mocks, and webhook routes."""

import ast
import json
import keyword
from pathlib import Path
import re


def valid_namespace(namespace: str) -> bool:
    return bool(namespace) and all(
        re.fullmatch(r"[a-z_][a-z0-9_]*", part) and not keyword.iskeyword(part)
        for part in namespace.split(".")
    )


def replace_reference(value: str, namespace: str) -> str:
    if value == "example" or value.startswith("example."):
        return namespace + value[len("example") :]
    for prefix in ("polyapi.poly.", "polyapi.vari."):
        if value == prefix + "example" or value.startswith(prefix + "example."):
            return prefix + namespace + value[len(prefix + "example") :]
    slug = namespace.replace(".", "-").replace("_", "-")
    return value.replace("{{example.", "{{" + namespace + ".").replace(
        "/apis/example-", "/apis/" + slug + "-"
    )


def rewrite_python(source: str, namespace: str) -> str:
    encoded = source.encode("utf-8")
    offsets = [0]
    for line in encoded.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def start(node):
        return offsets[node.lineno - 1] + node.col_offset

    def end(node):
        return offsets[node.end_lineno - 1] + node.end_col_offset

    edits = []
    segments = namespace.split(".")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            updated = replace_reference(node.value, namespace)
            if updated != node.value:
                edits.append((start(node), end(node), repr(updated)))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "example"
            and isinstance(node.value, ast.Name)
            and node.value.id in {"poly", "vari"}
        ):
            edits.append((start(node), end(node), node.value.id + "." + namespace))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id != "SimpleNamespace":
                continue
            for argument in node.keywords:
                if argument.arg != "example":
                    continue
                edits.append((start(argument), start(argument) + 7, segments[0]))
                if len(segments) > 1:
                    prefix = "".join(
                        f"SimpleNamespace({part}=" for part in segments[1:]
                    )
                    edits.extend(
                        [
                            (start(argument.value), start(argument.value), prefix),
                            (
                                end(argument.value),
                                end(argument.value),
                                ")" * (len(segments) - 1),
                            ),
                        ]
                    )
    for begin, finish, text in sorted(edits, reverse=True):
        encoded = encoded[:begin] + text.encode("utf-8") + encoded[finish:]
    return encoded.decode("utf-8")


def rewrite_namespace(root: Path, namespace: str) -> None:
    if not valid_namespace(namespace):
        raise ValueError(
            "Use dotted snake_case identifiers, excluding Python keywords."
        )

    def replace_json(value, key=None, parent=None):
        if isinstance(value, dict):
            return {name: replace_json(item, name, key) for name, item in value.items()}
        if isinstance(value, list):
            return [replace_json(item) for item in value]
        if isinstance(value, str):
            if key == "slug" and value.startswith("example-"):
                slug = namespace.replace(".", "-").replace("_", "-")
                return slug + value[len("example") :]
            if key and (
                key.lower().endswith("context")
                or key == "path"
                and parent == "errorHandler"
            ):
                return replace_reference(value, namespace)
            slug = namespace.replace(".", "-").replace("_", "-")
            return value.replace("{{example.", "{{" + namespace + ".").replace(
                "/apis/example-", "/apis/" + slug + "-"
            )
        return value

    for file in (root / "workflows/src").rglob("*"):
        if not file.is_file() or file.is_symlink() or "specs" in file.parts:
            continue
        if file.suffix not in {".py", ".json", ".md"}:
            continue
        original = file.read_text(encoding="utf-8")
        if file.suffix == ".py":
            updated = rewrite_python(original, namespace)
        elif file.suffix == ".json":
            before = json.loads(original)
            after = replace_json(before)
            updated = (
                json.dumps(after, indent=2, ensure_ascii=False) + "\n"
                if after != before
                else original
            )
        else:
            slug = namespace.replace(".", "-").replace("_", "-")
            updated = (
                re.sub(r"\bexample\.", namespace + ".", original)
                .replace("example-motd", slug + "-motd")
                .replace("POLYAPI_NAMESPACE=example", "POLYAPI_NAMESPACE=" + namespace)
            )
        if updated != original:
            file.write_text(updated, encoding="utf-8")
