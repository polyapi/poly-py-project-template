"""Static Python SDK references and dependency ordering; no imports or network I/O."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

FunctionKind = Literal["client", "server"]


class GraphError(ValueError):
    pass


@dataclass(eq=False)
class _Scope:
    parent: _Scope | None
    is_class: bool = False
    is_comprehension: bool = False
    bindings: dict[str, set[str]] = field(default_factory=dict)
    declarations: dict[str, str] = field(default_factory=dict)
    redirects: dict[str, _Scope] = field(default_factory=dict)
    helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(
        default_factory=dict
    )

    def owner(self, name: str) -> _Scope | None:
        if name in self.redirects:
            return self.redirects[name].owner(name)
        if name in self.bindings:
            return self
        return self.parent.owner(name) if self.parent else None


def match_reference(reference: str, aliases: dict[str, str]) -> set[str]:
    """Resolve the longest function prefix before expanding SDK namespaces."""
    parts = reference.split(".")
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        if prefix in aliases:
            return {aliases[prefix]}
    return {
        target
        for alias, target in aliases.items()
        if len(alias.split(".")) >= len(parts)
        and all(
            left == "*" or left == right for left, right in zip(parts, alias.split("."))
        )
    }


def extract_references(
    source: str,
    *,
    namespace: Literal["poly", "tabi", "vari"] = "poly",
    known_exports: Iterable[str] = (),
    available_modules: Iterable[str] = (),
) -> set[str]:
    """Resolve imports, aliases and same-file helper arguments to SDK paths.

    Bindings are conservative unions within lexical scopes. Dynamic getattr
    selects a wildcard, so graph construction includes every matching local
    function instead of silently dropping a possible consumer. Known resource
    exports identify parent modules; available modules already exist in the
    selected SDK and need no additional exports just to support an import.
    """
    module = f"polyapi.{namespace}"
    prefix = f"{module}."
    exports = set(known_exports)

    def is_function_export(reference: str) -> bool:
        return any(
            reference == export or reference.startswith(f"{export}.")
            for export in exports
        )

    modules = {
        ".".join(parts[:length])
        for export in exports
        for parts in [export.split(".")]
        for length in range(1, len(parts))
    }
    # A visible table can share a name with a module outside its owning context.
    # Keep both identities: narrowed child references can require only the module,
    # while actual table values/members remain strong references below.
    module_references: set[str] = set()
    module_aliases: set[ast.AST] = set()
    tree = ast.parse(source)
    root = _Scope(None)
    scopes: dict[ast.AST, _Scope] = {}
    bodies: dict[ast.AST, _Scope] = {}
    parents: dict[ast.AST, ast.AST] = {}
    assignments: list[tuple[ast.AST, ast.AST]] = []
    ignored: set[ast.AST] = set()

    def bind(scope: _Scope, name: str, values: set[str]) -> bool:
        scope = scope.redirects.get(name, scope)
        target = scope.bindings.setdefault(name, set())
        before = len(target)
        target.update(values)
        return len(target) != before

    def index(node: ast.AST, scope: _Scope) -> None:
        scopes[node] = scope
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declaration = "global" if isinstance(node, ast.Global) else "nonlocal"
            scope.declarations.update((name, declaration) for name in node.names)
        if isinstance(node, ast.ClassDef):
            scope.bindings.setdefault(node.name, set())
            child = _Scope(scope, is_class=True)
            for part in ast.iter_child_nodes(node):
                parents[part] = node
                index(part, child if part in node.body else scope)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                scope.helpers[node.name] = node
                scope.bindings.setdefault(node.name, set())
            # Method bodies do not resolve bare names through the class namespace.
            child = _Scope(scope.parent if scope.is_class else scope)
            bodies[node] = child
            # Defaults can contain lambdas with their own, unrelated parameters.
            for arg in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                node.args.vararg,
                node.args.kwarg,
            ):
                if arg is not None:
                    child.bindings[arg.arg] = set()
            for part in ast.iter_child_nodes(node):
                parents[part] = node
                # Defaults/decorators run in the defining scope.
                index(
                    part,
                    (
                        child
                        if part
                        in (node.body if isinstance(node.body, list) else [node.body])
                        else scope
                    ),
                )
            return
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            enclosing = scope
            while enclosing.is_class and enclosing.parent is not None:
                enclosing = enclosing.parent
            child = _Scope(enclosing, is_comprehension=True)
            for position, generator in enumerate(node.generators):
                parents[generator] = node
                scopes[generator] = child
                for part in ast.iter_child_nodes(generator):
                    parents[part] = generator
                    # Only the first iterable runs outside the comprehension.
                    index(
                        part,
                        scope if position == 0 and part is generator.iter else child,
                    )
            expressions = (
                [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            )
            for part in expressions:
                parents[part] = node
                index(part, child)
            return
        if isinstance(node, ast.NamedExpr):
            # Assignment expressions escape all surrounding comprehension scopes.
            enclosing = scope
            while enclosing.is_comprehension and enclosing.parent is not None:
                enclosing = enclosing.parent
            assignments.append((node.target, node.value))
            parents[node.target] = node
            parents[node.value] = node
            index(node.target, enclosing)
            index(node.value, scope)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            scope.bindings.setdefault(node.id, set())
        if isinstance(node, ast.Import):
            for alias in node.names:
                value = alias.name if alias.asname else alias.name.split(".")[0]
                bind(
                    scope,
                    alias.asname or value,
                    (
                        {value}
                        if value.split(".")[0]
                        in {"polyapi", "typing", "typing_extensions"}
                        else set()
                    ),
                )
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            for alias in node.names:
                if module.startswith("polyapi") and alias.name == "*":
                    raise GraphError(
                        "Wildcard PolyAPI imports cannot be planned safely"
                    )
                bind(
                    scope,
                    alias.asname or alias.name,
                    (
                        {f"{module}.{alias.name}"}
                        if module.split(".")[0]
                        in {"polyapi", "typing", "typing_extensions"}
                        else set()
                    ),
                )
        if isinstance(node, ast.Assign):
            assignments.extend((target, node.value) for target in node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append((node.target, node.value))
        for part in ast.iter_child_nodes(node):
            parents[part] = node
            index(part, scope)

    index(tree, root)

    # Resolve declarations after indexing every scope: an enclosing binding may
    # occur later in the file. Nonlocal names must belong to an enclosing function.
    all_scopes = set(scopes.values())
    for scope in all_scopes:
        for name, declaration in scope.declarations.items():
            target = root
            if declaration == "nonlocal":
                target = scope.parent
                while target is not None and target is not root:
                    if target.declarations.get(name) == "global":
                        break
                    if (
                        not target.is_class
                        and name in target.bindings
                        and name not in target.declarations
                    ):
                        break
                    target = target.parent
                if (
                    target is None
                    or target is root
                    or target.declarations.get(name) == "global"
                ):
                    raise GraphError(f"No enclosing binding for nonlocal name: {name}")
            if target is not scope:
                scope.redirects[name] = target
    for scope in all_scopes:
        for name, target in scope.redirects.items():
            bind(target, name, scope.bindings.pop(name, set()))
            if name in scope.helpers:
                target.helpers[name] = scope.helpers.pop(name)

    def resolve(node: ast.AST) -> set[str]:
        if isinstance(node, ast.NamedExpr):
            return resolve(node.value)
        if isinstance(node, ast.Name):
            owner = scopes[node].owner(node.id)
            return set(owner.bindings[node.id]) if owner else set()
        if isinstance(node, ast.Attribute):
            return {f"{base}.{node.attr}" for base in resolve(node.value)}
        if isinstance(node, ast.Call):
            names = resolve(node.func)
            if names.intersection({"typing.cast", "typing_extensions.cast"}) or (
                isinstance(node.func, ast.Name) and node.func.id == "cast"
            ):
                return resolve(node.args[1]) if len(node.args) == 2 else set()
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
            ):
                attr = node.args[1]
                suffix = (
                    attr.value
                    if isinstance(attr, ast.Constant) and isinstance(attr.value, str)
                    else "*"
                )
                return {f"{base}.{suffix}" for base in resolve(node.args[0])}
        return set()

    def assignment_bindings(
        target: ast.AST, value: ast.AST
    ) -> list[tuple[ast.Name, ast.AST]]:
        if isinstance(target, ast.Name):
            return [(target, value)]
        if not isinstance(target, (ast.Tuple, ast.List)) or not isinstance(
            value, (ast.Tuple, ast.List)
        ):
            return []
        # Expanded RHS values have unknown positions; keep their references.
        if any(isinstance(item, ast.Starred) for item in value.elts):
            return []
        stars = [
            index
            for index, item in enumerate(target.elts)
            if isinstance(item, ast.Starred)
        ]
        if not stars:
            if len(target.elts) != len(value.elts):
                return []
            pairs = list(zip(target.elts, value.elts))
        elif len(stars) == 1:
            prefix = stars[0]
            suffix = len(target.elts) - prefix - 1
            if len(value.elts) < prefix + suffix:
                return []
            pairs = list(zip(target.elts[:prefix], value.elts[:prefix]))
            pairs.extend(
                zip(target.elts[prefix + 1 :], value.elts[len(value.elts) - suffix :])
            )
            # The starred target holds a list, not an SDK namespace. Its captured
            # expressions stay visible for conservative dependency matching.
        else:
            return []
        return [
            binding
            for left, right in pairs
            for binding in assignment_bindings(left, right)
        ]

    bindings = [
        binding
        for target, value in assignments
        for binding in assignment_bindings(target, value)
    ]

    def assign(target: ast.Name, value: ast.AST) -> bool:
        values = resolve(value)
        if any(len(path.split(".")) > 64 for path in values):
            raise GraphError("Recursive namespace alias cannot be resolved")
        return bind(scopes[target], target.id, values)

    forwarded: set[ast.AST] = set()
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    for _ in range(len(scopes) + 1):
        changed = any([assign(target, value) for target, value in bindings])
        for call in calls:
            # These expressions preserve their argument's SDK identity. Keep the
            # resolved call/path, not an additional broad namespace argument.
            if len(call.args) == 2 and (
                resolve(call.func).intersection(
                    {"typing.cast", "typing_extensions.cast"}
                )
                or isinstance(call.func, ast.Name)
                and call.func.id == "cast"
            ):
                forwarded.add(call.args[1])
            if (
                isinstance(call.func, ast.Name)
                and call.func.id == "getattr"
                and len(call.args) >= 2
            ):
                forwarded.add(call.args[0])
            if not isinstance(call.func, ast.Name):
                continue
            owner = scopes[call].owner(call.func.id)
            helper = owner.helpers.get(call.func.id) if owner else None
            if helper is None:
                continue
            parameters = [*helper.args.posonlyargs, *helper.args.args]
            values: list[tuple[str, ast.AST]] = []
            for arg, value in zip(parameters, call.args):
                if isinstance(value, ast.Starred):
                    # Expansion length is unknown; later positional arguments
                    # must retain their references instead of binding by index.
                    break
                values.append((arg.arg, value))
            keyword_parameters = {
                arg.arg for arg in [*helper.args.args, *helper.args.kwonlyargs]
            }
            # A keyword captured by **kwargs has no directly resolvable binding.
            # Retain its namespace expression for conservative dependency matching.
            values.extend(
                (item.arg, item.value)
                for item in call.keywords
                if item.arg is not None and item.arg in keyword_parameters
            )
            defaults = (
                list(
                    zip(parameters[-len(helper.args.defaults) :], helper.args.defaults)
                )
                if helper.args.defaults
                else []
            )
            values.extend((arg.arg, value) for arg, value in defaults)
            values.extend(
                (arg.arg, value)
                for arg, value in zip(helper.args.kwonlyargs, helper.args.kw_defaults)
                if value is not None
            )
            for name, value in values:
                paths = resolve(value)
                if paths:
                    changed = bind(bodies[helper], name, paths) or changed
                    forwarded.add(value)
                    # Arguments/defaults are evaluated even when unused. A
                    # narrowed child can satisfy a namespace argument without
                    # requiring its siblings, just like a module import.
                    module_references.update(
                        path.removeprefix(prefix)
                        for path in paths
                        if path.startswith(prefix)
                    )
        if not changed:
            break
    else:
        raise GraphError("Namespace aliases did not converge")

    for target, value in bindings:
        # Class attributes escape lexical alias tracking through self/cls/instances.
        # Retain their SDK namespace as a conservative dependency instead.
        if scopes[target].is_class:
            continue
        # Resource aliases materialize generated classes and types, even unused.
        # A module alias itself is not a wildcard dependency when later narrowed.
        paths = resolve(value)
        if paths and paths <= {"polyapi", module}:
            ignored.add(value)
        elif paths and (
            namespace == "poly"
            or all(
                path.startswith(prefix) and path.removeprefix(prefix) in modules
                for path in paths
            )
        ):
            module_references.update(
                path.removeprefix(prefix)
                for path in paths
                if path.startswith(prefix)
                and (
                    namespace != "poly" or is_function_export(path.removeprefix(prefix))
                )
            )
            module_aliases.add(value)

    references: set[str] = set()
    # Importing an SDK export requires it even when the binding is unused.
    # Function namespace imports can still narrow through resolved member uses.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_references.update(
                alias.name.removeprefix(prefix)
                for alias in node.names
                if alias.name.startswith(prefix)
                and (
                    is_function_export(alias.name.removeprefix(prefix))
                    if namespace == "poly"
                    else alias.name.removeprefix(prefix) in modules
                )
            )
        if isinstance(node, ast.ImportFrom) and not node.level:
            imported_module = node.module or ""
            if imported_module == module or imported_module.startswith(prefix):
                for alias in node.names:
                    reference = f"{imported_module}.{alias.name}".removeprefix(prefix)
                    if namespace == "poly" and not is_function_export(reference):
                        continue
                    if namespace == "poly" or reference in modules:
                        module_references.add(reference)
                    else:
                        references.add(reference)
    # Imports stored on a class are also accessed through instances, without an
    # assignment RHS for the AST walk below to retain.
    for scope in set(scopes.values()):
        if scope.is_class:
            for bound_paths in scope.bindings.values():
                for path in bound_paths:
                    if path.startswith(prefix):
                        references.add(path.removeprefix(prefix))
                    elif path in {"polyapi", module}:
                        references.add("*")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            continue
        parent = parents.get(node)
        # Inspect maximal paths. Calls inside assignments still count; only
        # plain alias RHS expressions are omitted from the reference set.
        if isinstance(parent, ast.Attribute) and parent.value is node:
            continue
        if node in forwarded or node in module_aliases:
            continue
        if node in ignored and not isinstance(parent, ast.Call):
            if not isinstance(node, ast.Call):
                continue
        for path in resolve(node):
            if path.startswith(prefix):
                references.add(path.removeprefix(prefix))
            elif path in {"polyapi", module} and node not in ignored:
                references.add("*")
    # A generated child satisfies its parent path. Narrow module imports/aliases
    # and forwarded arguments; escaping values stay strong.
    materialized = references | module_references
    available = set(available_modules)
    references.update(
        {
            reference
            for reference in module_references
            if reference not in available
            and not any(other.startswith(f"{reference}.") for other in materialized)
        }
    )
    return references


@dataclass
class FunctionGraph:
    kinds: dict[str, FunctionKind]
    dependencies: dict[str, set[str]]
    callers: dict[str, set[str]]
    dynamic: set[str]
    references: dict[str, set[str]]

    @classmethod
    def build(cls, sources: dict[str, tuple[FunctionKind, str]]) -> FunctionGraph:
        kinds: dict[str, FunctionKind] = {key: item[0] for key, item in sources.items()}
        aliases: dict[str, str] = {}
        for key, kind in kinds.items():
            for alias in ([key, f"{key}_async"] if kind == "server" else [key]):
                if alias in aliases and aliases[alias] != key:
                    raise GraphError(f"Ambiguous generated SDK name: {alias}")
                aliases[alias] = key
        dependencies: dict[str, set[str]] = {key: set() for key in kinds}
        callers: dict[str, set[str]] = {key: set() for key in kinds}
        dynamic: set[str] = set()
        references = {
            key: extract_references(source, known_exports=aliases)
            for key, (_, source) in sources.items()
        }
        for key in sources:
            for ref in references[key]:
                if ref in aliases:
                    matches = {aliases[ref]}
                else:
                    matches = match_reference(ref, aliases)
                    if matches:
                        dynamic.add(key)
                for target in matches:
                    dependencies[key].add(target)
                    callers[target].add(key)
        return cls(kinds, dependencies, callers, dynamic, references)

    def closure(self, seeds: set[str], *, through_servers: bool = False) -> set[str]:
        selected = set(seeds)
        pending = list(seeds)
        while pending:
            key = pending.pop()
            if self.kinds[key] == "server" and not through_servers:
                continue
            for caller in self.callers[key] - selected:
                selected.add(caller)
                pending.append(caller)
        return selected

    def hubs(self) -> list[dict]:
        result = []
        for key, kind in self.kinds.items():
            if kind != "client":
                continue
            reached = self.closure({key}) - {key}
            result.append(
                {
                    "key": key,
                    "clientCallers": sum(
                        self.kinds[node] == "client" for node in reached
                    ),
                    "serverLeaves": sum(
                        self.kinds[node] == "server" for node in reached
                    ),
                }
            )
        return sorted(
            result,
            key=lambda row: (-row["serverLeaves"], -row["clientCallers"], row["key"]),
        )

    def explain(
        self,
        seeds: set[str],
        target: str,
        *,
        through_servers: bool = False,
        limit: int = 25,
    ) -> tuple[list[list[str]], bool]:
        # Only traverse nodes that can reach the target, and never repeat a cycle.
        ancestors = {target}
        pending = [target]
        while pending:
            key = pending.pop()
            for dependency in self.dependencies[key] - ancestors:
                if self.kinds[dependency] == "server" and not through_servers:
                    continue
                ancestors.add(dependency)
                pending.append(dependency)
        paths = []
        trails = [[seed] for seed in sorted(seeds & ancestors, reverse=True)]
        while trails and len(paths) <= limit:
            trail = trails.pop()
            key = trail[-1]
            if key == target:
                paths.append(trail)
            elif through_servers or self.kinds[key] != "server":
                trails.extend(
                    trail + [caller]
                    for caller in sorted(self.callers[key] & ancestors, reverse=True)
                    if caller not in trail
                )
        return paths[:limit], len(paths) > limit

    def order(self, selected: set[str]) -> tuple[list[str], list[list[str]]]:
        """Order strongly connected components with dependencies first."""
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        active: set[str] = set()
        stack: list[str] = []
        groups: list[list[str]] = []
        rank = lambda key: (self.kinds[key] == "server", key)  # noqa: E731

        def visit(key: str) -> None:
            indices[key] = low[key] = len(indices)
            stack.append(key)
            active.add(key)
            for dep in sorted(self.dependencies[key] & selected, key=rank):
                if dep not in indices:
                    visit(dep)
                    low[key] = min(low[key], low[dep])
                elif dep in active:
                    low[key] = min(low[key], indices[dep])
            if low[key] == indices[key]:
                group: list[str] = []
                while True:
                    member = stack.pop()
                    active.remove(member)
                    group.append(member)
                    if member == key:
                        break
                groups.append(sorted(group, key=rank))

        for key in sorted(selected, key=rank):
            if key not in indices:
                visit(key)
        cycles = [
            group
            for group in groups
            if len(group) > 1 or group[0] in self.dependencies[group[0]]
        ]
        return [key for group in groups for key in group], cycles
