"""Non-mutating OpenAPI selection and readable model metadata."""

from __future__ import annotations

import copy
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote

METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
BLOCK = set(
    "p div br hr ul ol li table tr h1 h2 h3 h4 h5 h6 pre blockquote section".split()
)
HTML = BLOCK | set(
    "a abbr b body code dd dl dt em font html i img label small span strong sub sup td th thead tbody u s strike".split()
)
BLOCK |= set(
    "address article aside caption dd details dl dt figcaption figure footer header main nav summary".split()
)
HTML |= BLOCK | set(
    "area audio base bdi bdo button canvas cite col colgroup data datalist del dfn dialog embed fieldset form g head hgroup iframe input ins kbd legend link map mark menu meta meter noscript object optgroup option output path picture progress q rp rt ruby samp search select slot source svg template textarea tfoot time title track var video wbr".split()
)


class DescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            if tag not in HTML:
                self.parts.append(self.get_starttag_text())
            elif tag == "li":
                self.parts.append("\n- ")
            elif tag in BLOCK:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            if tag not in HTML:
                self.parts.append(f"</{tag}>")
            elif tag == "li":
                pass
            elif tag in BLOCK:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag not in {"script", "style"}:
            self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def description_text(value: str) -> str:
    parser = DescriptionParser()
    parser.feed(value)
    parser.close()
    text = re.sub(r"[ \t\xa0]+", " ", "".join(parser.parts))
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_descriptions(value: Any, *, mapping: bool = False) -> Any:
    if isinstance(value, list):
        return [clean_descriptions(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        if not mapping and key in {
            "source",
            "example",
            "examples",
            "default",
            "enum",
            "const",
        }:
            result[key] = copy.deepcopy(child)
        elif not mapping and key == "description" and isinstance(child, str):
            result[key] = description_text(child)
        else:
            result[key] = clean_descriptions(
                child,
                mapping=not mapping
                and key
                in {"properties", "definitions", "schemas", "patternProperties"},
            )
    return result


def select_operations(
    document: dict[str, Any], operation_ids: list[str]
) -> dict[str, Any]:
    document = copy.deepcopy(document)
    if not operation_ids:
        return document
    requested = set(operation_ids)
    matches = dict.fromkeys(requested, 0)
    paths = {}
    for route, item in document.get("paths", {}).items():
        selected = {}
        for method, operation in item.items():
            if method in METHODS and operation.get("operationId") in requested:
                matches[operation["operationId"]] += 1
                selected[method] = operation
        if selected:
            selected.update(
                {key: value for key, value in item.items() if key not in METHODS}
            )
            paths[route] = selected
    for identifier, count in matches.items():
        if count != 1:
            raise ValueError(
                f"Operation ID {identifier} must match exactly once (found {count})."
            )
    document["paths"] = paths
    document.pop("webhooks", None)
    groups = (
        {f"components/{key}": value for key, value in document["components"].items()}
        if "components" in document
        else {
            key: document.get(key, {})
            for key in ("definitions", "parameters", "responses", "securityDefinitions")
        }
    )
    retained: set[str] = set()

    def visit(value, mapping=False):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#/"):
                    raise ValueError(
                        "Operation selection requires bundled local references; bundle external $ref files first."
                    )
                if ref not in retained:
                    retained.add(ref)
                    target = document
                    for part in ref[2:].split("/"):
                        key = unquote(part).replace("~1", "/").replace("~0", "~")
                        if not isinstance(target, dict) or key not in target:
                            raise ValueError(f"Unresolved OpenAPI reference: {ref}")
                        target = target[key]
                    visit(target)
            discriminator = value.get("discriminator")
            mapping_values = (
                discriminator.get("mapping", {}).values()
                if isinstance(discriminator, dict)
                else ()
            )
            for target in mapping_values:
                if isinstance(target, str):
                    visit(
                        {
                            "$ref": (
                                target
                                if target.startswith("#/")
                                else f"#/components/schemas/{target}"
                            )
                        }
                    )
            for key, child in value.items():
                if not mapping and key in {
                    "example",
                    "default",
                    "enum",
                    "const",
                    "value",
                }:
                    continue
                visit(
                    child,
                    not mapping
                    and key in {"properties", "patternProperties", "examples"},
                )

    visit(paths)
    security_group = (
        "components/securitySchemes"
        if "components" in document
        else "securityDefinitions"
    )
    for name, value in groups.get(security_group, {}).items():
        retained.add(f"#/{security_group}/{name}")
        visit(value)
    for group, entries in groups.items():
        keep = {}
        for name, value in entries.items():
            pointer = f"#/{group}/{name.replace('~', '~0').replace('/', '~1')}"
            if any(ref == pointer or ref.startswith(pointer + "/") for ref in retained):
                keep[name] = value
        if group.startswith("components/"):
            document["components"][group[11:]] = keep
        else:
            document[group] = keep
    return document


def normalize_schema_names(document: dict[str, Any]) -> dict[str, Any]:
    document = copy.deepcopy(document)
    mappings = {}
    for container, key, prefix in (
        (document.get("components", {}), "schemas", "#/components/schemas/"),
        (document, "definitions", "#/definitions/"),
    ):
        schemas = container.get(key)
        if not isinstance(schemas, dict):
            continue
        schemas = {str(name): value for name, value in schemas.items()}
        used = set(schemas)
        names = {}
        for name in schemas:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            base = re.sub(r"[^A-Za-z0-9_]", "_", name) or "Schema"
            if not re.match(r"[A-Za-z_]", base) or not re.search(r"[A-Za-z0-9]", base):
                base = "Schema" + base
            valid, suffix = base, 2
            while valid in used:
                valid, suffix = f"{base}_{suffix}", suffix + 1
            used.add(valid)
            names[name] = valid
        container[key] = {
            names.get(name, name): value for name, value in schemas.items()
        }
        mappings[prefix] = names

    def rewrite(ref):
        for prefix, names in mappings.items():
            if ref.startswith(prefix):
                segment, *remaining = ref[len(prefix) :].split("/")
                name = unquote(segment).replace("~1", "/").replace("~0", "~")
                if name in names:
                    return (
                        prefix
                        + names[name]
                        + ("/" + "/".join(remaining) if remaining else "")
                    )
        return ref

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            if isinstance(value.get("$ref"), str):
                value["$ref"] = rewrite(value["$ref"])
            discriminator = value.get("discriminator")
            mapping = (
                discriminator.get("mapping", {})
                if isinstance(discriminator, dict)
                else {}
            )
            for key, target in mapping.items():
                if isinstance(target, str):
                    mapping[key] = next(
                        (
                            names[target]
                            for names in mappings.values()
                            if target in names
                        ),
                        rewrite(target),
                    )
            for child in value.values():
                visit(child)

    visit(document)
    return document
