from __future__ import annotations

import ast
import json
from typing import Any

from policies.testing_patterns import FASTAPI_TESTING_POLICY


def collect_pytest_test_functions(test_content: str) -> list[str]:
    try:
        tree = ast.parse(test_content)
    except SyntaxError:
        return []
    collected: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            collected.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            collected.extend(
                f"{node.name}.{method.name}"
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                and method.name.startswith("test_")
            )
    return collected


def build_fastapi_health_test(
    package_name: str,
    endpoint: str,
    expected_status_code: int,
    expected_json: dict[str, Any],
) -> str:
    normalized_package = package_name.replace("-", "_")
    expected = json.dumps(expected_json, ensure_ascii=False, sort_keys=True)
    return (
        "from fastapi.testclient import TestClient\n"
        f"from {normalized_package}.main import app\n\n"
        "client = TestClient(app)\n\n\n"
        "def test_health():\n"
        f"    response = client.get({endpoint!r})\n\n"
        f"    assert response.status_code == {expected_status_code}\n"
        f"    assert response.json() == {expected}\n"
    )


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _has_app_argument(call: ast.Call) -> bool:
    return any(keyword.arg == "app" for keyword in call.keywords)


def _is_name(node: ast.AST | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _attribute_path(node: ast.AST) -> str:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _from_import_text(node: ast.ImportFrom) -> str:
    names = ", ".join(alias.name for alias in node.names)
    return f"from {node.module} import {names}"


def _is_status_assert(node: ast.Assert, expected: int = 200) -> bool:
    comparison = node.test
    if not isinstance(comparison, ast.Compare) or len(comparison.ops) != 1:
        return False
    if not isinstance(comparison.ops[0], ast.Eq) or len(comparison.comparators) != 1:
        return False
    left, right = comparison.left, comparison.comparators[0]
    return (
        isinstance(left, ast.Attribute)
        and left.attr == "status_code"
        and isinstance(right, ast.Constant)
        and right.value == expected
    ) or (
        isinstance(right, ast.Attribute)
        and right.attr == "status_code"
        and isinstance(left, ast.Constant)
        and left.value == expected
    )


def _json_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "json"
        and not node.args
    )


def _is_json_assert(node: ast.Assert, expected: dict[str, Any]) -> bool:
    comparison = node.test
    if not isinstance(comparison, ast.Compare) or len(comparison.ops) != 1:
        return False
    if not isinstance(comparison.ops[0], ast.Eq) or len(comparison.comparators) != 1:
        return False
    pairs = (
        (comparison.left, comparison.comparators[0]),
        (comparison.comparators[0], comparison.left),
    )
    for call, literal in pairs:
        if not _json_call(call):
            continue
        try:
            value = ast.literal_eval(literal)
        except (ValueError, TypeError):
            continue
        if value == expected:
            return True
    return False


def validate_fastapi_test_code(
    test_content: str,
    package_name: str,
    expected_endpoint: str,
    expected_json_literal: dict[str, Any],
    *,
    allow_async: bool = True,
) -> list[str]:
    try:
        tree = ast.parse(test_content)
    except SyntaxError:
        return ["invalid_python_syntax"]

    errors: list[str] = []
    if not collect_pytest_test_functions(test_content):
        errors.append("pytest_no_collectable_tests")
    imports: list[ast.Import | ast.ImportFrom] = [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    from_imports = [node for node in imports if isinstance(node, ast.ImportFrom)]
    has_testclient_import = any(
        _from_import_text(node) in FASTAPI_TESTING_POLICY.allowed_sync_imports
        for node in from_imports
    )
    forbidden_app_clients = {
        pattern.partition("(")[0] for pattern in FASTAPI_TESTING_POLICY.forbidden_patterns
    }
    has_httpx_client_import = any(
        node.module == "httpx" and any(alias.name == "Client" for alias in node.names)
        for node in from_imports
    )
    has_async_client_import = any(
        node.module == "httpx" and any(alias.name == "AsyncClient" for alias in node.names)
        for node in from_imports
    )
    has_transport_import = any(
        node.module == "httpx" and any(alias.name == "ASGITransport" for alias in node.names)
        for node in from_imports
    )
    has_allowed_async_import = any(
        _from_import_text(node) in FASTAPI_TESTING_POLICY.allowed_async_imports
        for node in from_imports
    )
    expected_module = f"{package_name}.main"
    app_imports = [
        node
        for node in from_imports
        if any(alias.name == "app" for alias in node.names)
    ]
    if not any(node.module == expected_module for node in app_imports):
        errors.append("incorrect_test_package_import")

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    client_calls = [call for call in calls if _call_name(call) == "Client"]
    async_client_calls = [call for call in calls if _call_name(call) == "AsyncClient"]
    testclient_calls = [call for call in calls if _call_name(call) == "TestClient"]
    if has_httpx_client_import:
        errors.append("unsupported_fastapi_sync_test_client")

    is_async = has_async_client_import or bool(async_client_calls)
    if is_async:
        if not allow_async:
            errors.append("async_test_not_explicitly_requested")
        transport_variables: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Call) or _call_name(value) != "ASGITransport":
                continue
            if not any(keyword.arg == "app" and _is_name(keyword.value, "app") for keyword in value.keywords):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            transport_variables.update(target.id for target in targets if isinstance(target, ast.Name))
        client_has_transport = any(
            any(
                keyword.arg == "transport"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id in transport_variables
                for keyword in call.keywords
            )
            for call in async_client_calls
        )
        awaited_get = any(
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "get"
            for node in ast.walk(tree)
        )
        async_marker = any(
            isinstance(node, ast.Attribute) and _attribute_path(node) in {"pytest.mark.anyio", "pytest.mark.asyncio"}
            for node in ast.walk(tree)
        )
        if not (
            has_transport_import
            and has_allowed_async_import
            and transport_variables
            and client_has_transport
            and awaited_get
            and async_marker
        ):
            errors.append("invalid_fastapi_async_test_transport")
    else:
        valid_testclient = any(
            (call.args and _is_name(call.args[0], "app"))
            or any(keyword.arg == "app" and _is_name(keyword.value, "app") for keyword in call.keywords)
            for call in testclient_calls
        )
        if not has_testclient_import:
            errors.append("missing_fastapi_testclient_import")
        if not valid_testclient:
            errors.append("invalid_fastapi_test_client_construction")

    get_endpoints = [
        call.args[0].value
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "get"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ]
    if expected_endpoint and expected_endpoint not in get_endpoints:
        errors.append("missing_expected_endpoint_call")
    if expected_endpoint and any(endpoint != expected_endpoint for endpoint in get_endpoints):
        errors.append("unexpected_test_endpoint")
    assertions = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    if not any(_is_status_assert(node) for node in assertions):
        errors.append("missing_status_code_assertion")
    if expected_json_literal and not any(
        _is_json_assert(node, expected_json_literal) for node in assertions
    ):
        errors.append("incorrect_json_assertion")

    pytest_imported = any(
        isinstance(node, ast.Import) and any(alias.name == "pytest" for alias in node.names)
        for node in imports
    )
    pytest_used = any(isinstance(node, ast.Name) and node.id == "pytest" for node in ast.walk(tree))
    if pytest_imported and not pytest_used:
        errors.append("unused_test_import: pytest")
    for call in calls:
        if _call_name(call) in forbidden_app_clients and _has_app_argument(call):
            if _call_name(call) == "Client":
                errors.append("httpx_client_app_argument_not_supported")
            elif _call_name(call) == "AsyncClient":
                errors.append("httpx_async_client_app_argument_not_supported")
    return list(dict.fromkeys(errors))
