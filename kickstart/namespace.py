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


def rewrite_markdown_code(code: str, namespace: str) -> str:
    """Personalize owned examples without changing domains or external paths."""
    parts = re.split(r"((?:[a-z][a-z0-9+.-]*://|mailto:|git@)[^\s<>`\"']+)", code)
    if len(parts) > 1:
        return "".join(
            part if index % 2 else rewrite_markdown_code(part, namespace)
            for index, part in enumerate(parts)
        )
    if code.strip() == "example":
        return code.replace("example", namespace)
    code = re.sub(
        r"(?<![\w.])POLYAPI_NAMESPACES?=example(?=$|[\s,\"'])",
        lambda match: match[0].replace("=example", "=" + namespace),
        code,
    )
    code = re.sub(
        r"(?<![\w./:@-])(poly|vari|tabi|schemas)\.example(?=\.|\b)",
        lambda match: match[1] + "." + namespace,
        code,
    )
    # These suffixes denote domains or filenames, not owned context segments.
    suffixes = (
        r"com|org|net|io|invalid|test|localhost|py|ts|json|md|yaml|yml|txt|html|svg|png"
    )
    code = re.sub(
        rf"(?<![\w./:@-])example\.(?!(?:{suffixes})(?![\w]))(?=[a-z_])",
        lambda match: namespace + ".",
        code,
    )
    slug = namespace.replace(".", "-").replace("_", "-")
    code = re.sub(
        r"(?<![\w./:@-])example-(?=motd\b|workflow\b|weather-tools\b)",
        lambda match: slug + "-",
        code,
    )
    # MCP clients see tool names built from the context, with dots as underscores.
    return re.sub(
        r"(?<![\w./:@-])example_(?=sample_workflow_\w)",
        lambda match: namespace.replace(".", "_") + "_",
        code,
    )


def rewrite_markdown(source: str, namespace: str) -> str:
    """Only rewrite fenced examples and inline code, leaving prose/links intact."""
    result = []
    fence = None
    for line in source.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker and fence is None:
            fence = marker[1]
        elif (
            marker
            and marker[1][0] == fence[0]
            and len(marker[1]) >= len(fence)
            and not marker[2].strip()
        ):
            fence = None
        elif fence:
            line = rewrite_markdown_code(line, namespace)
        else:
            line = re.sub(
                r"(?P<ticks>`+)(?P<code>[^`\n]+)(?P=ticks)",
                lambda match: match["ticks"]
                + rewrite_markdown_code(match["code"], namespace)
                + match["ticks"],
                line,
            )
        result.append(line)
    return "".join(result)


def rewrite_project_docs(root: Path, namespace: str) -> None:
    docs = [root / "README.md", root / "cli/README.md"]
    docs.extend((root / "docs").glob("*.md"))
    docs.extend((root / "workflows/src").rglob("README.md"))
    docs.extend((root / "workflows/src").rglob("OVERVIEW.md"))
    for file in docs:
        if (
            not file.is_file()
            or file.is_symlink()
            or "specs" in file.relative_to(root).parts
            or any(
                (root / parent).is_symlink()
                for parent in file.relative_to(root).parents
            )
        ):
            continue
        original = file.read_text(encoding="utf-8")
        updated = rewrite_markdown(original, namespace)
        if updated != original:
            file.write_text(updated, encoding="utf-8")


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
        if file.suffix not in {".py", ".json"}:
            continue
        original = file.read_text(encoding="utf-8")
        if file.suffix == ".py":
            updated = rewrite_python(original, namespace)
        elif file.suffix == ".json":
            before = json.loads(original)
            after = replace_json(before)
            name = after.get("name") if isinstance(after, dict) else None
            if (
                "mcp-servers" in file.parts
                and isinstance(name, str)
                and name.startswith("example-")
            ):
                # MCP servers have no context; their environment-unique name,
                # such as example-weather-tools, carries the root instead.
                slug = namespace.replace(".", "-").replace("_", "-")
                after = {**after, "name": slug + name[len("example") :]}
            updated = (
                json.dumps(after, indent=2, ensure_ascii=False) + "\n"
                if after != before
                else original
            )
        if updated != original:
            file.write_text(updated, encoding="utf-8")
    rewrite_env_example(root, namespace)
    rewrite_project_docs(root, namespace)


def rewrite_env_example(root: Path, namespace: str) -> None:
    """Keep env files later created from the example aligned with the code."""
    file = root / "workflows/dev.env.example"
    if not file.is_file() or file.is_symlink():
        return
    original = file.read_text(encoding="utf-8")
    slug = namespace.replace(".", "-").replace("_", "-")
    updated = re.sub(
        r"(?m)^(POLYAPI_NAMESPACES?=)example(?=$|,)",
        lambda match: match[1] + namespace,
        original,
    ).replace("/apis/example-", "/apis/" + slug + "-")
    if updated != original:
        file.write_text(updated, encoding="utf-8")
