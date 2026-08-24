from __future__ import annotations

import pytest

from graph.subgraphs.implementation.models import GeneratedFile
from policies.dependencies import (
    FASTAPI_DEPENDENCY_POLICY,
    dependency_normalization_errors,
    extract_dependency_name,
    normalize_dependency_file,
    validate_dependency_file,
)


def files_with_requirements(content: str) -> list[GeneratedFile]:
    return [
        GeneratedFile(path="app/main.py", content="app = object()"),
        GeneratedFile(path="requirements.txt", content=content),
    ]


@pytest.mark.parametrize(
    "content",
    [
        "fastapi\npytest\n",
        "fastapi[standard]\npytest>=8,<9\n",
        "fastapi==0.100.0\npytest==7.4.0\n",
        "fastapi>=0.100\n",
    ],
)
def test_controlled_dependencies_are_normalized(content: str) -> None:
    normalized = normalize_dependency_file("fastapi", files_with_requirements(content))

    assert normalized[-1].content == "fastapi[standard]==0.139.0\npytest>=8,<9\n"
    assert validate_dependency_file("fastapi", normalized) == []


def test_normalization_deduplicates_and_ends_with_newline() -> None:
    normalized = normalize_dependency_file(
        "fastapi",
        files_with_requirements("fastapi\nfastapi>=0.1\npytest\npytest\n"),
    )

    assert normalized[-1].content.count("fastapi[standard]") == 1
    assert normalized[-1].content.count("pytest>=8,<9") == 1
    assert normalized[-1].content.endswith("\n")


@pytest.mark.parametrize("dependency", ["httpx", "httpx==0.20", "httpx>=0.23,<1"])
def test_httpx_is_canonicalized(dependency: str) -> None:
    normalized = normalize_dependency_file(
        "fastapi",
        files_with_requirements(f"fastapi\n{dependency}\n"),
    )

    assert normalized[-1].content == (
        "fastapi[standard]==0.139.0\npytest>=8,<9\nhttpx>=0.23,<1\n"
    )


@pytest.mark.parametrize("dependency", ["uvicorn", "uvicorn==0.30"])
def test_uvicorn_is_canonicalized(dependency: str) -> None:
    normalized = normalize_dependency_file(
        "fastapi",
        files_with_requirements(f"fastapi\n{dependency}\n"),
    )

    assert normalized[-1].content == (
        "fastapi[standard]==0.139.0\npytest>=8,<9\nuvicorn>=0.17,<1\n"
    )


def test_optional_dependencies_are_deduplicated_in_policy_order() -> None:
    normalized = normalize_dependency_file(
        "fastapi",
        files_with_requirements(
            "uvicorn==0.30\nhttpx\nhttpx>=0.20\nfastapi\nuvicorn\n"
        ),
    )

    assert normalized[-1].content == (
        "fastapi[standard]==0.139.0\n"
        "pytest>=8,<9\n"
        "httpx>=0.23,<1\n"
        "uvicorn>=0.17,<1\n"
    )
    assert validate_dependency_file("fastapi", normalized) == []


def test_absent_optional_dependencies_are_not_added() -> None:
    normalized = normalize_dependency_file("fastapi", files_with_requirements("fastapi\n"))

    assert "httpx" not in normalized[-1].content
    assert "uvicorn" not in normalized[-1].content


def test_unsupported_dependencies_are_rejected_but_known_versions_are_correctable() -> None:
    unsupported = dependency_normalization_errors(
        "fastapi",
        files_with_requirements("fastapi\nrequests>=2\n"),
    )
    controlled = dependency_normalization_errors(
        "fastapi",
        files_with_requirements("fastapi\nhttpx==0.20.0\nuvicorn\n"),
    )

    assert unsupported == ["unsupported_dependency: requests>=2"]
    assert controlled == []


def test_missing_requirements_is_added_by_normalizer_and_rejected_by_raw_validator() -> None:
    files = [GeneratedFile(path="app/main.py", content="app = object()")]
    normalized = normalize_dependency_file("fastapi", files)

    assert normalized[-1].path == "requirements.txt"
    assert normalized[-1].content == "fastapi[standard]==0.139.0\npytest>=8,<9\n"
    assert validate_dependency_file("fastapi", files) == [
        "missing_required_dependency: requirements.txt"
    ]


def test_strict_validation_reports_all_stable_error_codes() -> None:
    files = files_with_requirements(
        "fastapi==0.100.0\nrequests>=2\nrequests>=2\n"
    )
    errors = validate_dependency_file("fastapi", files)

    assert any(error.startswith("missing_required_dependency") for error in errors)
    assert any(error.startswith("incompatible_dependency_version") for error in errors)
    assert any(error.startswith("unsupported_dependency") for error in errors)
    assert any(error.startswith("duplicate_dependency") for error in errors)


def test_policy_has_the_host_controlled_versions() -> None:
    assert dict(FASTAPI_DEPENDENCY_POLICY.required_dependencies) == {
        "fastapi": "fastapi[standard]==0.139.0",
        "pytest": "pytest>=8,<9",
    }
    assert dict(FASTAPI_DEPENDENCY_POLICY.optional_dependencies) == {
        "httpx": "httpx>=0.23,<1",
        "uvicorn": "uvicorn>=0.17,<1",
    }


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("fastapi", "fastapi"),
        (" fastapi>=0.100 ", "fastapi"),
        ("fastapi[standard]", "fastapi"),
        ("fastapi[standard]==0.139.0", "fastapi"),
        ("pytest>=8,<9 # tests", "pytest"),
        ("httpx", "httpx"),
        ("uvicorn==0.30.0", "uvicorn"),
    ],
)
def test_extract_dependency_name(line: str, expected: str) -> None:
    assert extract_dependency_name(line) == expected


def test_normalizer_is_pure_and_does_not_need_an_openai_client() -> None:
    original = files_with_requirements("fastapi\n")

    normalized = normalize_dependency_file("fastapi", original)

    assert original[-1].content == "fastapi\n"
    assert normalized[-1].content == "fastapi[standard]==0.139.0\npytest>=8,<9\n"
