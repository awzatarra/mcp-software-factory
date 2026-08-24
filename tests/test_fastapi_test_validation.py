from __future__ import annotations

import pytest

from graph.subgraphs.implementation.test_validation import (
    build_fastapi_health_test,
    collect_pytest_test_functions,
    validate_fastapi_test_code,
)


PACKAGE = "implementation_perfect"
ENDPOINT = "/health"
EXPECTED = {"status": "ok"}


def validate(content: str) -> list[str]:
    return validate_fastapi_test_code(content, PACKAGE, ENDPOINT, EXPECTED)


def sync_test(*, package: str = PACKAGE, endpoint: str = ENDPOINT, status: bool = True, expected=None) -> str:
    expected = EXPECTED if expected is None else expected
    status_assertion = "    assert response.status_code == 200\n" if status else ""
    return (
        "from fastapi.testclient import TestClient\n"
        f"from {package}.main import app\n\n"
        "client = TestClient(app)\n\n"
        "def test_health():\n"
        f"    response = client.get({endpoint!r})\n"
        f"{status_assertion}"
        f"    assert response.json() == {expected!r}\n"
    )


def async_test(*, client_arguments: str = "transport=transport, base_url='http://test'", transport: bool = True) -> str:
    transport_line = "    transport = ASGITransport(app=app)\n" if transport else ""
    return (
        "import pytest\n"
        "from httpx import ASGITransport, AsyncClient\n"
        f"from {PACKAGE}.main import app\n\n"
        "@pytest.mark.anyio\n"
        "async def test_health():\n"
        f"{transport_line}"
        f"    async with AsyncClient({client_arguments}) as client:\n"
        "        response = await client.get('/health')\n"
        "    assert response.status_code == 200\n"
        "    assert response.json() == {'status': 'ok'}\n"
    )


def test_testclient_app_is_valid() -> None:
    assert validate(sync_test()) == []
    assert collect_pytest_test_functions(sync_test()) == ["test_health"]


def test_async_test_health_is_collectable() -> None:
    assert collect_pytest_test_functions(async_test()) == ["test_health"]


def test_health_test_name_is_not_collectable() -> None:
    content = sync_test().replace("def test_health():", "def health_test():")
    assert "pytest_no_collectable_tests" in validate(content)


def test_module_without_functions_is_not_collectable() -> None:
    content = sync_test().replace("def test_health():\n", "").replace("    ", "")
    assert "pytest_no_collectable_tests" in validate(content)


def test_pytest_class_method_is_collectable() -> None:
    content = sync_test().replace(
        "def test_health():\n"
        "    response = client.get('/health')\n"
        "    assert response.status_code == 200\n"
        "    assert response.json() == {'status': 'ok'}\n",
        "class TestHealth:\n"
        "    def test_status(self):\n"
        "        response = client.get('/health')\n"
        "        assert response.status_code == 200\n"
        "        assert response.json() == {'status': 'ok'}\n",
    )
    assert validate(content) == []
    assert collect_pytest_test_functions(content) == ["TestHealth.test_status"]


def test_non_pytest_class_name_is_not_collectable() -> None:
    content = sync_test().replace(
        "def test_health():\n"
        "    response = client.get('/health')\n"
        "    assert response.status_code == 200\n"
        "    assert response.json() == {'status': 'ok'}\n",
        "class HealthTests:\n"
        "    def test_health(self):\n"
        "        response = client.get('/health')\n"
        "        assert response.status_code == 200\n"
        "        assert response.json() == {'status': 'ok'}\n",
    )
    assert "pytest_no_collectable_tests" in validate(content)


def test_httpx_client_app_is_rejected_with_specific_errors() -> None:
    content = sync_test().replace(
        "from fastapi.testclient import TestClient", "from httpx import Client"
    ).replace("TestClient(app)", "Client(app=app, base_url='http://test')")
    errors = validate(content)
    assert "unsupported_fastapi_sync_test_client" in errors
    assert "httpx_client_app_argument_not_supported" in errors


def test_async_client_app_is_rejected() -> None:
    errors = validate(async_test(client_arguments="app=app, base_url='http://test'"))
    assert "httpx_async_client_app_argument_not_supported" in errors
    assert "invalid_fastapi_async_test_transport" in errors


def test_async_client_with_asgi_transport_is_valid() -> None:
    assert validate(async_test()) == []


def test_async_client_without_transport_is_rejected() -> None:
    assert "invalid_fastapi_async_test_transport" in validate(
        async_test(client_arguments="base_url='http://test'", transport=False)
    )


def test_wrong_package_import_is_rejected() -> None:
    assert "incorrect_test_package_import" in validate(sync_test(package="wrong_package"))


def test_wrong_endpoint_is_rejected() -> None:
    errors = validate(sync_test(endpoint="/ready"))
    assert "missing_expected_endpoint_call" in errors
    assert "unexpected_test_endpoint" in errors


def test_missing_status_assertion_is_rejected() -> None:
    assert "missing_status_code_assertion" in validate(sync_test(status=False))


def test_wrong_json_literal_is_rejected() -> None:
    assert "incorrect_json_assertion" in validate(sync_test(expected={"status": "healthy"}))


def test_invalid_python_is_rejected() -> None:
    assert validate("def broken(:\n") == ["invalid_python_syntax"]


def test_unused_pytest_import_is_rejected() -> None:
    assert "unused_test_import: pytest" in validate("import pytest\n" + sync_test())


@pytest.mark.parametrize("package", ["implementation_perfect", "implementation-perfect"])
def test_template_normalizes_package_and_generates_valid_code(package: str) -> None:
    content = build_fastapi_health_test(package, ENDPOINT, 200, EXPECTED)
    assert "from implementation_perfect.main import app" in content
    assert 'assert response.json() == {"status": "ok"}' in content
    assert "def test_health():" in content
    assert "client = TestClient(app)" in content
    assert validate(content) == []
