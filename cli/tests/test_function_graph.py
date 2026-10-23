from __future__ import annotations

import textwrap

import pytest

from cli.function_graph import (
    FunctionGraph,
    GraphError,
    extract_references,
    match_reference,
)


@pytest.mark.parametrize(
    "source",
    [
        "from polyapi import poly\npoly.example.helper()",
        "from polyapi import poly as api\napi.example.helper()",
        "import polyapi as sdk\nsdk.poly.example.helper()",
        "import polyapi.poly.example as api\napi.helper()",
        "from polyapi.poly.example import helper as run\nrun()",
        "from polyapi import poly\na = poly\nb = a.example\nc = b.helper\nc()",
        "from polyapi import poly\na, b = (poly.example, 1)\na.helper()",
        "from typing import cast, Any\nfrom polyapi import poly\na = cast(Any, poly).example\na.helper()",
        "from polyapi import poly\ndef forward(api):\n    return api.helper()\nforward(poly.example)",
        "from polyapi import poly\ndef forward(api):\n    return api.helper()\nforward(api=poly.example)",
        "from polyapi import poly\ngetattr(poly.example, 'helper')()",
    ],
)
def test_python_sdk_reference_forms(source):
    assert "example.helper" in extract_references(source)


def test_lexical_aliases_and_non_code_do_not_create_false_edges():
    source = """
from polyapi import poly
# poly.example.comment()
text = "poly.example.string()"
def first():
    api = poly.example
    return api.first()
def second(api):
    return api.second()
"""
    assert extract_references(source) == {"example.first"}


def test_alias_fixed_point_is_not_limited_to_sixteen_links():
    source = "from polyapi import poly\na0 = poly.example\n"
    source += "\n".join(f"a{n} = a{n - 1}" for n in reversed(range(1, 30)))
    source += "\na29.helper()"
    assert extract_references(source) == {"example.helper"}


def test_unknown_generated_import_and_recursive_alias_fail():
    with pytest.raises(GraphError):
        extract_references("from polyapi.poly.example import *")
    with pytest.raises(GraphError):
        extract_references(
            "from polyapi import poly\na = poly.example\na = a.child\na.run()"
        )


def graph():
    return FunctionGraph.build(
        {
            "example.helper_async": ("client", ""),
            "example.middle": (
                "client",
                "from polyapi import poly\npoly.example.helper_async()",
            ),
            "example.worker": (
                "server",
                "from polyapi import poly\npoly.example.middle()",
            ),
            "example.http_caller": (
                "server",
                "from polyapi import poly\npoly.example.worker_async()",
            ),
        }
    )


def test_nested_closure_stops_at_server_http_boundary():
    dependency_graph = graph()
    selected = dependency_graph.closure({"example.helper_async"})
    assert selected == {"example.helper_async", "example.middle", "example.worker"}
    assert dependency_graph.order(selected) == (
        ["example.helper_async", "example.middle", "example.worker"],
        [],
    )
    assert dependency_graph.closure({"example.worker"}) == {"example.worker"}


def test_shared_consumers_and_cycles_are_ordered_once():
    dependency_graph = FunctionGraph.build(
        {
            "example.a": ("client", "from polyapi import poly\npoly.example.b()"),
            "example.b": ("client", "from polyapi import poly\npoly.example.a()"),
            "example.z": (
                "server",
                "from polyapi import poly\npoly.example.a()\npoly.example.b()",
            ),
        }
    )
    assert dependency_graph.order(
        dependency_graph.closure({"example.a", "example.b"})
    ) == (["example.a", "example.b", "example.z"], [["example.a", "example.b"]])


def test_dynamic_getattr_includes_all_matching_local_functions():
    dependency_graph = FunctionGraph.build(
        {
            "example.a": ("client", ""),
            "example.b": ("client", ""),
            "other.c": ("client", ""),
            "example.worker": (
                "server",
                "from polyapi import poly\ngetattr(poly.example, name)()",
            ),
        }
    )
    assert dependency_graph.dependencies["example.worker"] == {
        "example.a",
        "example.b",
        "example.worker",
    }
    assert dependency_graph.dynamic == {"example.worker"}


def test_ambiguous_generated_async_name_fails():
    with pytest.raises(GraphError, match="Ambiguous"):
        FunctionGraph.build(
            {"example.run": ("server", ""), "example.run_async": ("client", "")}
        )


def test_python_names_are_case_sensitive():
    dependency_graph = FunctionGraph.build(
        {
            "example.Run": ("client", ""),
            "example.worker": (
                "server",
                "from polyapi import poly\npoly.example.run()",
            ),
        }
    )
    assert dependency_graph.dependencies["example.worker"] == set()


@pytest.mark.parametrize(
    "source",
    [
        "from polyapi import poly\ncallbacks = {'run': poly.example.helper}\ncallbacks['run']()",
        "import typing as t\nfrom polyapi import poly\np = t.cast(object, poly)\np.example.helper()",
        "from typing import cast as typed\nfrom polyapi import poly\np = typed(object, poly)\np.example.helper()",
    ],
)
def test_callback_containers_and_cast_aliases_keep_dependencies(source):
    assert "example.helper" in extract_references(source)


@pytest.mark.parametrize(
    "assignment",
    [
        "api = poly.example",
        "api: object = poly.example",
        "api, unused = (poly.example, None)",
        "api = shared",
    ],
)
def test_class_namespace_aliases_retain_conservative_dependencies(assignment):
    source = f"""
from polyapi import poly
shared = poly.example
class Calls:
    {assignment}
    async def run(self):
        return await self.api.helper()
"""
    assert "example" in extract_references(source)


def test_class_aliases_do_not_shadow_module_or_method_globals():
    source = """
from polyapi import poly
api = poly.example
class Calls:
    api = object()
    def run(self):
        return api.helper()
api.other()
"""
    assert extract_references(source) == {"example.helper", "example.other"}


@pytest.mark.parametrize(
    "import_line, expected",
    [
        ("import polyapi as api", "*"),
        ("from polyapi import poly as api", "*"),
        ("import polyapi.poly.example as api", "example"),
        ("from polyapi.poly import example as api", "example"),
    ],
)
def test_class_namespace_imports_retain_dependencies(import_line, expected):
    source = f"class Calls:\n    {import_line}\n    def run(self):\n        return self.api.helper()\n"
    assert expected in extract_references(source)


@pytest.mark.parametrize(
    "signature, argument, member, expected",
    [
        ("**kwargs", "api=poly.example", "helper", "example"),
        ("api=None, /, **kwargs", "api=poly.example", "helper", "example"),
        ("**kwargs", "api=shared", "helper", "example"),
        ("**kwargs", "api=poly", "example.helper", "*"),
        ("**kwargs", "api=sdk", "poly.example.helper", "*"),
    ],
)
def test_kwargs_namespace_keeps_all_matching_dependencies(
    signature, argument, member, expected
):
    source = f"""
import polyapi as sdk
from polyapi import poly
shared = poly.example
async def invoke({signature}):
    return await kwargs["api"].{member}()
async def middle():
    return await invoke({argument})
"""
    assert expected in extract_references(source)
    dependency_graph = FunctionGraph.build(
        {
            "example.helper": ("client", ""),
            "example.middle": ("client", source),
            "example.worker": (
                "server",
                "from polyapi import poly\npoly.example.middle()",
            ),
        }
    )
    assert dependency_graph.closure({"example.helper"}) == {
        "example.helper",
        "example.middle",
        "example.worker",
    }


@pytest.mark.parametrize(
    "signature, argument",
    [
        ("api", "poly.example"),
        ("api", "api=poly.example"),
        ("*, api", "api=poly.example"),
        ("api=poly.example", ""),
        ("*, api=poly.example", ""),
    ],
)
def test_declared_helper_parameters_keep_precise_dependencies(signature, argument):
    source = f"""
from polyapi import poly
def invoke({signature}):
    return api.helper()
invoke({argument})
"""
    assert extract_references(source) == {"example.helper"}


def test_reports_cross_server_boundary_without_changing_deploy_closure():
    dependency_graph = graph()
    seeds = {"example.helper_async"}
    assert "example.http_caller" in dependency_graph.closure(
        seeds, through_servers=True
    )
    assert "example.http_caller" not in dependency_graph.closure(seeds)
    assert dependency_graph.explain(seeds, "example.http_caller") == ([], False)
    assert dependency_graph.explain(
        seeds, "example.http_caller", through_servers=True
    ) == (
        [
            [
                "example.helper_async",
                "example.middle",
                "example.worker",
                "example.http_caller",
            ]
        ],
        False,
    )
    assert dependency_graph.hubs()[0] == {
        "key": "example.helper_async",
        "clientCallers": 1,
        "serverLeaves": 1,
    }


def test_explanations_are_cycle_safe_and_report_truncation():
    dependency_graph = FunctionGraph.build(
        {
            "x.a": ("client", "from polyapi import poly\npoly.x.b()"),
            "x.b": ("client", "from polyapi import poly\npoly.x.a()"),
            "x.target": ("server", "from polyapi import poly\npoly.x.a()\npoly.x.b()"),
        }
    )
    paths, truncated = dependency_graph.explain({"x.a"}, "x.target", limit=1)
    assert paths == [["x.a", "x.b", "x.target"]]
    assert truncated


@pytest.mark.parametrize("declaration", ["global", "nonlocal"])
@pytest.mark.parametrize(
    "binding",
    ["api = poly.example", "from polyapi.poly import example as api"],
)
def test_declared_scope_aliases_keep_exact_sdk_dependencies(declaration, binding):
    configure = f"def configure():\n    {declaration} api\n    {binding}\n"
    call = "configure()\nreturn api.helper()\n"
    if declaration == "global":
        source = configure + "def worker():\n" + textwrap.indent(call, "    ")
    else:
        source = "def worker():\n" + textwrap.indent(
            configure + "api = None\n" + call, "    "
        )
    source = "from polyapi import poly\n" + source
    compile(source, "<scope-alias>", "exec")
    assert extract_references(source) == {"example.helper"}


def test_global_reads_bypass_enclosing_local_shadow():
    source = """
from polyapi import poly
api = poly.example
def outer(api):
    def worker():
        global api
        return api.helper()
    return worker()
"""
    assert extract_references(source) == {"example.helper"}


def test_nonlocal_aliases_follow_chained_declarations_and_skip_class_bindings():
    source = """
from polyapi import poly
def outer():
    api = None
    class Container:
        api = None
        def configure():
            nonlocal api
            def update():
                nonlocal api
                api = poly.example
            update()
    Container.configure()
    return api.helper()
"""
    compile(source, "<scope-alias>", "exec")
    assert extract_references(source) == {"example.helper"}


def test_global_helper_definition_keeps_forwarded_namespace_dependency():
    source = """
def configure():
    global forward
    def forward(api):
        return api.helper()
from polyapi import poly
configure()
forward(poly.example)
"""
    assert extract_references(source) == {"example.helper"}


@pytest.mark.parametrize(
    "expression",
    [
        "[str(api) for api in (1, 2)]",
        "{str(api) for api in (1, 2)}",
        "{api: str(api) for api in (1, 2)}",
        "all(api > 0 for api in (1, 2))",
    ],
)
def test_comprehension_bindings_do_not_hide_enclosing_sdk_alias(expression):
    source = f"""
from polyapi import poly
api = poly.example
def worker():
    return bool({expression}) and api.helper()
"""
    assert extract_references(source) == {"example.helper"}


@pytest.mark.parametrize(
    "expression",
    [
        "[api for api in api.helper()]",
        "{api for api in api.helper()}",
        "{api: api for api in api.helper()}",
        "(api for api in api.helper())",
    ],
)
def test_first_comprehension_iterable_uses_enclosing_scope(expression):
    source = f"from polyapi import poly\napi = poly.example\ndef worker():\n    return {expression}\n"
    assert extract_references(source) == {"example.helper"}


def test_nested_comprehensions_keep_filters_and_later_iterables_in_inner_scope():
    source = """
from polyapi import poly
api = poly.example
def worker(objects):
    return [
        [api.helper() for api in objects]
        for api in objects
        for item in api.items()
        if api.accept(item)
    ]
"""
    assert extract_references(source) == set()


def test_comprehension_body_skips_class_namespace_but_first_iterable_uses_it():
    source = """
from polyapi import poly
api = poly.outer
class Container:
    api = poly.inner
    values = [api.helper() for item in api.items()]
"""
    references = extract_references(source)
    assert {"outer.helper", "inner.items"} <= references
    assert "inner.helper" not in references


@pytest.mark.parametrize("declaration", ["", "global api\n", "nonlocal api\n"])
def test_comprehension_assignment_expression_binds_in_containing_scope(declaration):
    body = (
        declaration
        + "values = [[(api := poly.example) for _ in (1,)] for _ in (1,)]\nreturn bool(values) and api.helper()\n"
    )
    worker = "def worker():\n" + textwrap.indent(body, "    ")
    if declaration.startswith("nonlocal"):
        worker = "def outer():\n    api = None\n" + textwrap.indent(worker, "    ")
    source = "from polyapi import poly\n" + worker
    compile(source, "<comprehension>", "exec")
    assert extract_references(source) == {"example.helper"}


def test_comprehension_assignment_expression_can_be_called_directly():
    source = """
from polyapi import poly
def worker():
    return [(api := poly.example).helper() for _ in (1,)]
"""
    assert extract_references(source) == {"example.helper"}


@pytest.mark.parametrize(
    "default",
    [
        "lambda api: api.shadowed()",
        "lambda api, /: api.shadowed()",
        "lambda *, api=None: api.shadowed()",
        "lambda *api: api.shadowed()",
        "lambda **api: api.shadowed()",
        "lambda value=(lambda api: api.shadowed()): value",
    ],
)
def test_default_lambda_parameters_stay_in_their_own_scope(default):
    source = f"""
from polyapi import poly
api = poly.example
def invoke(transform={default}):
    return api.helper()
def worker():
    return invoke()
"""
    compile(source, "<default-lambda>", "exec")
    assert extract_references(source) == {"example.helper"}


@pytest.mark.parametrize("parameters", ["api", "api, /", "*, api", "*api", "**api"])
def test_only_declared_function_parameters_shadow_enclosing_alias(parameters):
    source = f"""
from polyapi import poly
api = poly.example
def invoke({parameters}):
    return api.helper()
"""
    compile(source, "<parameters>", "exec")
    assert extract_references(source) == set()


@pytest.mark.parametrize(
    "assignment, expression, expected",
    [
        ("*unused, api = (None, 1, poly.example)", "api.helper()", {"example.helper"}),
        ("api, *unused = (poly.example, None, 1)", "api.helper()", {"example.helper"}),
        (
            "first, *unused, last = (poly.example, 0, 1, poly.other)",
            "(first.helper(), last.utility())",
            {"example.helper", "other.utility"},
        ),
        (
            "_, (*unused, api) = (None, (0, poly.example))",
            "api.helper()",
            {"example.helper"},
        ),
        (
            "[*unused, api] = [None, 1, poly.example]",
            "api.helper()",
            {"example.helper"},
        ),
        ("_, *apis = (None, poly.example)", "apis[0].helper()", {"example"}),
        ("*apis, _ = (poly.example, None)", "apis[0].helper()", {"example"}),
        ("first, *apis, last = (0, poly.example, 1)", "apis[0].helper()", {"example"}),
        ("*apis, = (poly.example,)", "apis[0].helper()", {"example"}),
        ("api, = (*[poly.example],)", "api.helper()", {"example"}),
        ("api, *unused = (*[poly.example], 0)", "api.helper()", {"example"}),
    ],
)
def test_starred_unpacking_preserves_exact_or_conservative_references(
    assignment, expression, expected
):
    source = f"from polyapi import poly\n{assignment}\ndef worker():\n    return {expression}\n"
    compile(source, "<unpacking>", "exec")
    assert extract_references(source) == expected


@pytest.mark.parametrize(
    "parameters, arguments, expected",
    [
        ("first, second, api", "*[None, None], poly.example", {"example"}),
        ("first, second, api", "None, *[None], poly.example", {"example"}),
        ("api", "*[], poly.example", {"example"}),
        ("first, second, api", "*(None,), *(None,), poly.example", {"example"}),
        ("first, second, api", "*[None, None], namespace", {"example"}),
        ("first, second, api", "*(None, None, poly.example)", {"example"}),
        ("api, *rest", "poly.example, *[None, None]", {"example.helper"}),
        ("first, second, api", "*[None, None], api=poly.example", {"example.helper"}),
        (
            "first, second, *, api",
            "*[None, None], api=poly.example",
            {"example.helper"},
        ),
    ],
)
def test_starred_helper_calls_keep_uncertain_arguments_visible(
    parameters, arguments, expected
):
    source = (
        "from polyapi import poly\nnamespace = poly.example\n"
        f"async def invoke({parameters}):\n    return await api.helper()\n"
        f"async def worker():\n    return await invoke({arguments})\n"
    )
    compile(source, "<starred-call>", "exec")
    assert extract_references(source) == expected


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("example.helper", {"example.helper"}),
        ("example.helper.__call__", {"example.helper"}),
        ("example.helper.__call__.__call__", {"example.helper"}),
        ("example.helper.deep", {"example.helper.deep"}),
        ("example.helper.deep.__call__", {"example.helper.deep"}),
        ("example.helper_extra.__call__", set()),
        ("example.Helper.__call__", set()),
        ("example.worker_async.__call__", {"example.worker"}),
        ("example.client_async.__call__", {"example.client_async"}),
        (
            "example",
            {
                "example.helper",
                "example.helper.deep",
                "example.worker",
                "example.client_async",
            },
        ),
        (
            "example.*",
            {
                "example.helper",
                "example.helper.deep",
                "example.worker",
                "example.client_async",
            },
        ),
        ("example.*.deep", {"example.helper.deep"}),
    ],
)
def test_callable_attribute_matching_uses_longest_known_function_prefix(
    reference, expected
):
    aliases = {
        key: key
        for key in (
            "example.helper",
            "example.helper.deep",
            "example.worker",
            "example.client_async",
        )
    }
    aliases["example.worker_async"] = "example.worker"
    assert match_reference(reference, aliases) == expected


def test_callable_attribute_graph_preserves_server_http_boundary():
    dependency_graph = FunctionGraph.build(
        {
            "example.helper_async": ("client", ""),
            "example.worker": (
                "server",
                "from polyapi import poly\npoly.example.helper_async.__call__()",
            ),
            "example.http_caller": (
                "server",
                "from polyapi import poly\npoly.example.worker_async.__call__()",
            ),
        }
    )
    assert dependency_graph.dependencies["example.worker"] == {"example.helper_async"}
    assert dependency_graph.dependencies["example.http_caller"] == {"example.worker"}
    assert dependency_graph.closure({"example.helper_async"}) == {
        "example.helper_async",
        "example.worker",
    }


@pytest.mark.parametrize(
    "body",
    [
        "table = tabi.root_table\ntable.select_many({})",
        "table, = (tabi.root_table,)\ntable.select_many({})",
        "(unused, (table,)) = (None, (tabi.root_table,))\ntable.select_many({})",
        "*unused, table = (None, tabi.root_table)\ntable.select_many({})",
        "def read(table):\n    return table.select_many({})\nread(tabi.root_table)",
        "def read(*, table):\n    return table.select_many({})\nread(table=tabi.root_table)",
        "def read(table=tabi.root_table):\n    return table.select_many({})\nread()",
        "def inner(table):\n    return table.select_many({})\ndef outer(table):\n    return inner(table)\nouter(tabi.root_table)",
        "def read(tables):\n    return tables.root_table.select_many({})\nread(tabi)",
        "table = tabi.root_table\ndef read():\n    global table\n    return table.select_many({})",
        "def outer():\n    table = tabi.root_table\n    def read():\n        nonlocal table\n        return table.select_many({})\n    return read()",
    ],
)
def test_tabi_aliases_helpers_and_tuple_bindings(body):
    source = "from polyapi import poly, tabi\n" + body
    references = extract_references(source, namespace="tabi")
    assert references - {"root_table"} == {"root_table.select_many"}
    assert extract_references(source) == set()


def test_tabi_aliases_keep_lexical_ownership_and_union_assignments():
    source = """
from polyapi import tabi

def root():
    table = tabi.root_table
    return table.select_many({})

def scoped():
    table = tabi.catalog.records
    return table.count({})

def shadow(table):
    return table.select_one({})

def conditional(flag):
    if flag:
        table = tabi.root_table
    else:
        table = tabi.other_table
    return table.select_one({})
"""
    assert extract_references(source, namespace="tabi") == {
        "root_table",
        "catalog.records",
        "other_table",
        "root_table.select_many",
        "catalog.records.count",
        "root_table.select_one",
        "other_table.select_one",
    }


@pytest.mark.parametrize("member", ["select_many", "table_id"])
def test_tabi_member_aliases_remain_dependencies_without_a_call(member):
    source = f"from polyapi import tabi\nvalue = tabi.root_table.{member}\n"
    assert extract_references(source, namespace="tabi") == {f"root_table.{member}"}


@pytest.mark.parametrize(
    "body",
    [
        "setting = vari.root_setting\nsetting.get()",
        "setting, = (vari.root_setting,)\nsetting.get()",
        "unused, (setting,) = (None, (vari.root_setting,))\nsetting.get()",
        "*unused, setting = (None, vari.root_setting)\nsetting.get()",
        "def read(setting):\n    return setting.get()\nread(vari.root_setting)",
        "def read(*, setting):\n    return setting.get()\nread(setting=vari.root_setting)",
        "def read(setting=vari.root_setting):\n    return setting.get()\nread()",
        "def inner(setting):\n    return setting.get()\ndef outer(setting):\n    return inner(setting)\nouter(vari.root_setting)",
        "def read(settings):\n    return settings.root_setting.get()\nread(vari)",
        "setting = vari.root_setting\ndef read():\n    global setting\n    return setting.get()",
        "def outer():\n    setting = vari.root_setting\n    def read():\n        nonlocal setting\n        return setting.get()\n    return read()",
    ],
)
def test_vari_aliases_helpers_and_tuple_bindings(body):
    source = "from polyapi import poly, vari\n" + body
    references = extract_references(source, namespace="vari")
    assert references - {"root_setting"} == {"root_setting.get"}
    assert extract_references(source) == set()


def test_vari_aliases_keep_lexical_ownership_and_union_assignments():
    source = """
from polyapi import vari

def root():
    setting = vari.root_setting
    return setting.get()

def scoped():
    setting = vari.catalog.setting
    return setting.inject()

def shadow(setting):
    return setting.get_async()

def conditional(flag):
    if flag:
        setting = vari.root_setting
    else:
        setting = vari.other_setting
    return setting.get_async()
"""
    assert extract_references(source, namespace="vari") == {
        "root_setting",
        "catalog.setting",
        "other_setting",
        "root_setting.get",
        "catalog.setting.inject",
        "root_setting.get_async",
        "other_setting.get_async",
    }


@pytest.mark.parametrize("namespace", ["vari", "tabi"])
def test_resource_module_alias_is_narrowed_before_dependency_matching(namespace):
    source = f"from polyapi import {namespace}\nresources = {namespace}\nresources.catalog.item\n"
    assert extract_references(source, namespace=namespace) == {"catalog.item"}


@pytest.mark.parametrize("namespace", ["tabi", "vari"])
@pytest.mark.parametrize(
    "expression", ["cast(object, sdk).catalog.item", "getattr(sdk, 'catalog').item"]
)
def test_resource_identity_expressions_do_not_add_a_root_wildcard(
    namespace, expression
):
    source = f"from typing import cast\nfrom polyapi import {namespace} as sdk\nvalue = {expression}\nvalue.get()\n"
    assert extract_references(source, namespace=namespace) == {
        "catalog.item",
        "catalog.item.get",
    }


@pytest.mark.parametrize(
    "setup",
    [
        "from polyapi.tabi import catalog",
        "import polyapi.tabi.catalog as catalog",
        "from polyapi import tabi\ncatalog = tabi.catalog",
        "from polyapi import tabi\ncatalog, = (tabi.catalog,)",
        "from polyapi import tabi\ncatalog = getattr(tabi, 'catalog')",
        "from typing import cast\nfrom polyapi import tabi\ncatalog = cast(object, tabi.catalog)",
        "from polyapi import tabi\nparent = tabi.catalog\ncatalog = parent",
    ],
)
@pytest.mark.parametrize("root_collision", [False, True])
def test_catalog_module_aliases_narrow_without_materializing_siblings(
    setup, root_collision
):
    exports = {"catalog.good.events", "catalog.bad.events"}
    if root_collision:
        exports.add("catalog")
    assert extract_references(
        setup + "\ncatalog.good.events.count({})\n",
        namespace="tabi",
        known_exports=exports,
    ) == {"catalog.good.events.count"}


@pytest.mark.parametrize(
    "setup",
    [
        "from polyapi.tabi import catalog",
        "import polyapi.tabi.catalog as catalog",
        "from polyapi import tabi\ncatalog = tabi.catalog",
    ],
)
@pytest.mark.parametrize("root_collision", [False, True])
def test_unused_catalog_module_only_needs_an_available_generated_parent(
    setup, root_collision
):
    exports = {"catalog.good.events"}
    if root_collision:
        exports.add("catalog")
    assert extract_references(setup, namespace="tabi", known_exports=exports) == {
        "catalog"
    }
    assert (
        extract_references(
            setup,
            namespace="tabi",
            known_exports=exports,
            available_modules={"catalog"},
        )
        == set()
    )


@pytest.mark.parametrize(
    "usage",
    [
        "external(catalog)",
        "def read():\n    return catalog",
        "getattr(catalog, branch).events.count({})",
        "class Reader:\n    tables = catalog",
    ],
)
def test_catalog_module_escape_remains_a_dependency(usage):
    references = extract_references(
        "from polyapi.tabi import catalog\ncatalog.good.events.count({})\n" + usage,
        namespace="tabi",
        known_exports={"catalog.good.events", "catalog.bad.events"},
        available_modules={"catalog", "catalog.good"},
    )
    assert "catalog" in references or "catalog.*.events.count" in references


@pytest.mark.parametrize("name", ["events", "eventsSelectOneQuery"])
def test_catalog_exports_remain_required_even_when_unused(name):
    reference = f"catalog.bad.{name}"
    assert reference in extract_references(
        f"from polyapi.tabi.catalog.bad import {name}\n{name} = None\n",
        namespace="tabi",
        known_exports={reference, "catalog.good.events"},
        available_modules={"catalog", "catalog.good"},
    )


def test_catalog_module_aliases_have_deterministic_narrowest_unused_scope():
    assert extract_references(
        "from polyapi import tabi\nparent = tabi.catalog\nchild = parent.good\n",
        namespace="tabi",
        known_exports={"catalog.good.events", "catalog.bad.events"},
    ) == {"catalog.good"}


def test_catalog_module_names_do_not_capture_unrelated_python_aliases():
    assert extract_references(
        "from typing import cast\nhelper = cast\nfrom polyapi import tabi\n"
        "tabi.catalog.good.events.count({})\n",
        namespace="tabi",
        known_exports={"catalog.good.events", "typing.cast.records"},
    ) == {"catalog.good.events.count"}


@pytest.mark.parametrize(
    "usage, expected",
    [
        ("catalog.count({})", "catalog.count"),
        ("value = catalog\nexternal(value)", "catalog"),
        ("def read():\n    return catalog", "catalog"),
        ("getattr(catalog, operation)({})", "catalog.*"),
        ("class Reader:\n    table = catalog", "catalog"),
        ("from polyapi.tabi import catalogSelectOneQuery", "catalogSelectOneQuery"),
    ],
)
def test_colliding_tabi_module_keeps_actual_root_table_references(usage, expected):
    assert expected in extract_references(
        "from polyapi.tabi import catalog\ncatalog.good.events.count({})\n" + usage,
        namespace="tabi",
        known_exports={"catalog", "catalogSelectOneQuery", "catalog.good.events"},
        available_modules={"catalog", "catalog.good"},
    )


def test_colliding_tabi_import_without_child_keeps_root_table_dependency():
    assert extract_references(
        "from polyapi.tabi import catalog\n",
        namespace="tabi",
        known_exports={"catalog", "catalog.good.events"},
    ) == {"catalog"}


@pytest.mark.parametrize("namespace", ["poly", "tabi", "vari"])
@pytest.mark.parametrize("resource", ["root_resource", "catalog.item"])
@pytest.mark.parametrize(
    "body",
    [
        "def accept(resource):\n    return True\naccept(sdk.RESOURCE)",
        "def accept(*, resource):\n    return True\naccept(resource=sdk.RESOURCE)",
        "def accept(resource=sdk.RESOURCE):\n    return True\naccept()",
        "def accept(*, resource=sdk.RESOURCE):\n    return True\naccept(resource=None)",
        "def accept(resource):\n    return True\ndef forward(resource):\n    return accept(resource)\nforward(sdk.RESOURCE)",
        "def accept(resource):\n    return True\naccept(getattr(sdk, 'RESOURCE'))",
        "def accept(resource):\n    return True\naccept(cast(object, sdk.RESOURCE))",
    ],
)
def test_unused_resource_helper_arguments_require_evaluated_exports(
    namespace, resource, body
):
    # getattr takes one attribute, so the scoped form uses an explicit parent.
    source = body.replace(
        "sdk, 'RESOURCE'",
        "sdk.catalog, 'item'" if "." in resource else "sdk, 'root_resource'",
    )
    source = source.replace("RESOURCE", resource)
    assert extract_references(
        f"from typing import cast\nfrom polyapi import {namespace} as sdk\n" + source,
        namespace=namespace,
        known_exports={"root_resource", "catalog.item"},
    ) == {resource}


@pytest.mark.parametrize("namespace", ["poly", "tabi", "vari"])
@pytest.mark.parametrize("with_catalog", [False, True])
@pytest.mark.parametrize("used", [False, True])
def test_forwarded_resource_namespaces_keep_materialization_and_narrowing(
    namespace, with_catalog, used
):
    source = (
        f"from polyapi import {namespace} as sdk\n"
        "def accept(resources):\n    return "
        + ("resources.good.item" if used else "True")
        + "\naccept(sdk.catalog)\n"
    )
    exports = (
        {"catalog", "catalog.good.item", "catalog.bad.item"} if with_catalog else ()
    )
    assert extract_references(source, namespace=namespace, known_exports=exports) == (
        {"catalog.good.item"} if used else {"catalog"}
    )
    assert extract_references(
        source,
        namespace=namespace,
        known_exports=exports,
        available_modules={"catalog", "catalog.good"},
    ) == ({"catalog.good.item"} if used else set())


@pytest.mark.parametrize("namespace", ["poly", "tabi", "vari"])
def test_unused_helper_argument_resolution_respects_lexical_shadowing(namespace):
    source = f"""
from polyapi import {namespace} as sdk
def accept(resource):
    return True
def real():
    return accept(sdk.root_resource)
def shadow(sdk):
    return accept(sdk.unrelated)
"""
    assert extract_references(source, namespace=namespace) == {"root_resource"}


@pytest.mark.parametrize(
    "source",
    [
        "from polyapi.poly.example import helper as unused",
        "from polyapi import poly\nunused = poly.example.helper",
        "from polyapi import poly\nunused, = (poly.example.helper,)",
        "from polyapi import poly\nunused = getattr(poly.example, 'helper')",
        "from typing import cast\nfrom polyapi import poly\nunused = cast(object, poly.example.helper)",
    ],
)
def test_unused_function_imports_and_aliases_require_the_export(source):
    assert extract_references(source, known_exports={"example.helper"}) == {
        "example.helper"
    }


@pytest.mark.parametrize(
    "setup",
    [
        "from polyapi.poly import example as api",
        "import polyapi.poly.example as api",
        "from polyapi import poly\napi = poly.example",
    ],
)
def test_function_namespace_materialization_keeps_narrow_dependencies(setup):
    assert extract_references(setup + "\napi.helper()") == {"example.helper"}
