import copy
from types import SimpleNamespace

import pytest

from cli.openapi_security import reconcile_security


def render_sdk(function):
    from polyapi.api import render_api_function

    arguments = [
        {**argument, "type": {"kind": "primitive", "type": "string"}}
        for argument in function["arguments"]
    ]
    source, _ = render_api_function(
        "apiFunction",
        "getMember",
        "synthetic-id",
        "Get member",
        arguments,
        {"kind": "any"},
    )
    return compile(
        "from __future__ import annotations\n" + source, "<synthetic-api>", "exec"
    )


@pytest.mark.parametrize("promoted", [False, True])
def test_repaired_required_arguments_precede_optional_arguments_in_sdk(promoted):
    original = model()
    original["functions"][0]["arguments"].append(
        {"name": "page", "type": "string", "required": False}
    )
    if promoted:
        original["functions"][0]["arguments"].append(
            {"name": "xApiKey", "type": "string", "required": False}
        )
    result = reconcile_security(original, specification())["functions"][0]
    assert [argument["name"] for argument in result["arguments"]] == [
        "apiKey",
        "xApiKey",
        "page",
    ]
    render_sdk(result)


@pytest.mark.parametrize("header,direct", [("execute", False), ("dict", True)])
def test_restored_header_arguments_do_not_shadow_sdk_transport_or_builtins(
    header, direct
):
    spec = specification()
    spec["components"]["securitySchemes"]["ApiKey"]["name"] = header
    result = reconcile_security(model(), spec)["functions"][0]
    calls = []

    def transport(kind, identifier, data):
        calls.append(data)
        return SimpleNamespace(status_code=200, headers={}, json=lambda: {"ok": True})

    namespace = {
        "get_direct_execute_config": lambda: direct,
        "execute": transport,
        "direct_execute": transport,
        "_GetMember_types": SimpleNamespace(getMemberResponse=lambda value: value),
    }
    exec(render_sdk(result), namespace)
    namespace["getMember"]("synthetic-auth", "synthetic-key")
    name = "header" + header.title()
    assert calls == [{"apiKey": "synthetic-auth", name: "synthetic-key"}]


def specification():
    return {
        "openapi": "3.0.3",
        "components": {
            "securitySchemes": {
                "Authorization": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                },
                "ApiKey": {"type": "apiKey", "in": "header", "name": "x-api-key"},
            }
        },
        "security": [{"Authorization": [], "ApiKey": []}],
        "paths": {"/members/{id}/": {"get": {}}},
    }


def model():
    return {
        "functions": [
            {
                "name": "getMember",
                "context": "example.demo",
                "description": "Get member",
                "arguments": [{"name": "apiKey", "type": "string", "required": True}],
                "source": {
                    "method": "GET",
                    "url": "{{hostUrl}}/v1/members/{{memberId}}/?page={{page}}",
                    "auth": {
                        "type": "apikey",
                        "apikey": [
                            {"key": "in", "value": "header"},
                            {"key": "key", "value": "Authorization"},
                            {"key": "value", "value": "{{apiKey}}"},
                        ],
                    },
                    "body": {"mode": "empty"},
                    "headers": [],
                },
            }
        ],
        "schemas": [],
        "webhooks": [],
    }


def test_combined_auth_restores_key_without_mutating_input_and_is_idempotent():
    original = model()
    snapshot = copy.deepcopy(original)
    result = reconcile_security(original, specification())
    function = result["functions"][0]
    assert function["source"]["auth"] == original["functions"][0]["source"]["auth"]
    assert function["source"]["headers"] == [
        {"key": "x-api-key", "value": "{{xApiKey}}"}
    ]
    assert function["arguments"][-1]["required"] is True
    assert function["arguments"][-1]["removeIfNotPresentOnExecute"] is False
    assert original == snapshot
    assert reconcile_security(result, specification()) == result


@pytest.mark.parametrize(
    "security",
    [
        [{"Authorization": []}, {"ApiKey": []}],
        [{"Authorization": [], "ApiKey": []}, {"Authorization": []}],
    ],
)
def test_security_alternatives_are_not_combined(security):
    spec = specification()
    spec["security"] = security
    assert reconcile_security(model(), spec) == model()


def test_operation_public_override_and_unrelated_specs():
    spec = specification()
    spec["paths"]["/members/{id}/"]["get"]["security"] = []
    function = reconcile_security(model(), spec)["functions"][0]
    assert function["source"]["auth"] == {"type": "noauth"}
    assert function["source"]["headers"] == []
    del spec["components"]
    assert reconcile_security(model(), spec) == model()


def test_header_only_auth_and_literal_authorization_are_preserved():
    original = model()
    function = original["functions"][0]
    function["source"]["auth"] = {"type": "noauth"}
    function["source"]["headers"] = [
        {"key": "Authorization", "value": "JWT {{apiKey}}"}
    ]
    result = reconcile_security(original, specification())["functions"][0]
    assert result["source"]["auth"] == {"type": "noauth"}
    assert result["source"]["headers"][0] == function["source"]["headers"][0]
    assert result["source"]["headers"][1]["key"] == "x-api-key"
    spec = specification()
    spec["security"] = [{"ApiKey": []}]
    function["source"]["headers"] = []
    assert reconcile_security(original, spec)["functions"][0]["source"]["auth"] == {
        "type": "noauth"
    }


@pytest.mark.parametrize("auth_type", ["bearer", "basic"])
def test_preserves_primary_http_auth(auth_type):
    spec = specification()
    spec["components"]["securitySchemes"]["Primary"] = {
        "type": "http",
        "scheme": auth_type,
    }
    spec["security"] = [{"Primary": [], "ApiKey": []}]
    original = model()
    original["functions"][0]["source"]["auth"] = {"type": auth_type, auth_type: []}
    result = reconcile_security(original, spec)["functions"][0]
    assert result["source"]["auth"] == original["functions"][0]["source"]["auth"]
    assert result["source"]["headers"][0]["key"] == "x-api-key"


@pytest.mark.parametrize("used", [True, False])
def test_argument_collision_or_unused_translated_argument(used):
    original = model()
    function = original["functions"][0]
    function["arguments"].append(
        {"name": "xApiKey", "type": "string", "required": False}
    )
    if used:
        function["source"]["body"] = {"mode": "raw", "raw": "{{xApiKey}}"}
    result = reconcile_security(original, specification())["functions"][0]
    expected = "xApiKey2" if used else "xApiKey"
    assert result["source"]["headers"][0]["value"] == "{{" + expected + "}}"
    assert (
        next(arg for arg in result["arguments"] if arg["name"] == expected)["required"]
        is True
    )


def test_existing_header_case_and_disabled_header_replacement():
    original = model()
    function = original["functions"][0]
    function["source"]["headers"] = [{"key": "X-API-KEY", "value": "{{existing}}"}]
    assert reconcile_security(original, specification()) == original
    function["source"]["headers"][0]["disabled"] = True
    result = reconcile_security(original, specification())["functions"][0]
    assert result["source"]["headers"] == [{"key": "x-api-key", "value": "{{xApiKey}}"}]


@pytest.mark.parametrize(
    "header, argument",
    [
        ("class", "headerClass"),
        ("enum", "headerEnum"),
        ("catch", "headerCatch"),
        ("debugger", "headerDebugger"),
        ("do", "headerDo"),
        ("this", "headerThis"),
        ("eval", "headerEval"),
        ("arguments", "headerArguments"),
        ("execute", "headerExecute"),
        ("dict", "headerDict"),
        ("constructor", "headerConstructor"),
        ("prototype", "headerPrototype"),
        ("123-key", "header123Key"),
    ],
)
def test_restored_argument_is_a_valid_cross_language_identifier(header, argument):
    spec = specification()
    spec["components"]["securitySchemes"]["ApiKey"]["name"] = header
    result = reconcile_security(model(), spec)["functions"][0]
    assert result["arguments"][-1]["name"] == argument
    assert result["source"]["headers"][-1]["value"] == "{{" + argument + "}}"


def test_swagger2_refs_method_matching_and_space_suffixes():
    spec = specification()
    spec["swagger"] = "2.0"
    spec["securityDefinitions"] = spec.pop("components")["securitySchemes"]
    spec["securityDefinitions"]["Alias"] = {"$ref": "#/securityDefinitions/ApiKey"}
    spec["security"] = [{"Authorization": [], "Alias": []}]
    spec["paths"]["/members/{id}/"]["post"] = {"security": []}
    spec["paths"]["/members/{id}/ "] = {"get": {}}
    original = model()
    function = original["functions"][0]
    function["source"]["url"] = "https://example.invalid/v1/members/{{id}}/%20?q=x"
    assert (
        reconcile_security(original, spec)["functions"][0]["source"]["headers"][0][
            "key"
        ]
        == "x-api-key"
    )
    function["source"]["method"] = "POST"
    assert reconcile_security(original, spec)["functions"][0]["source"]["auth"] == {
        "type": "noauth"
    }


@pytest.mark.parametrize(
    "failure", ["unmatched", "ambiguous", "undefined", "cycle", "external"]
)
def test_unsafe_security_mapping_fails_without_mutation(failure):
    spec, original = specification(), model()
    if failure == "unmatched":
        spec["paths"] = {}
    elif failure == "ambiguous":
        spec["paths"]["/members/{other}/"] = {"get": {"security": []}}
    elif failure == "undefined":
        spec["security"] = [{"Missing": []}]
    elif failure == "cycle":
        spec["components"]["securitySchemes"]["ApiKey"] = {
            "$ref": "#/components/securitySchemes/ApiKey"
        }
    else:
        spec["components"]["securitySchemes"]["ApiKey"] = {
            "$ref": "https://example.invalid/security.json"
        }
    snapshot = copy.deepcopy(original)
    with pytest.raises(ValueError):
        reconcile_security(original, spec)
    assert original == snapshot
