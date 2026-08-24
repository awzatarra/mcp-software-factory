from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("software-factory-planning")


BACKEND_STANDARDS = """Backend standards:
- Separate routes/controllers, services, and data access.
- Use dependency injection for external services and repositories.
- Do not put business logic in controllers.
- Validate all external inputs.
- Load secrets from environment variables only.
- Use centralized error handling.
- Include automated tests for critical behavior.
- Publish OpenAPI documentation when the framework supports it.
- Provide Docker support for repeatable execution.
"""


def analyze_requirement_impl(requirement: str) -> dict:
    normalized = " ".join(requirement.split())
    if not normalized:
        raise ValueError("requirement must not be empty")
    return {
        "requirement": normalized,
        "objective": f"Build a minimal executable software project for: {normalized}",
        "functional_scope": [
            "Capture the main user workflow.",
            "Expose a small but usable backend surface.",
            "Include automated validation through tests.",
        ],
        "non_functional_requirements": [
            "Keep the implementation simple and maintainable.",
            "Use environment variables for secrets.",
            "Include clear project documentation.",
        ],
    }


def create_tasks_impl(
    project_type: str,
    backend: Literal["fastapi", "dotnet", "node"] = "fastapi",
) -> list[dict]:
    project = " ".join(project_type.split())
    if not project:
        raise ValueError("project_type must not be empty")
    return [
        {
            "order": 1,
            "agent": "business analyst",
            "task": f"Clarify goals, actors, user stories, and acceptance criteria for {project}.",
        },
        {
            "order": 2,
            "agent": "software architect",
            "task": f"Design a minimal {backend} architecture with routes, services, data access, and tests.",
        },
        {
            "order": 3,
            "agent": "backend developer",
            "task": f"Implement the executable {backend} project and keep secrets outside source code.",
        },
        {
            "order": 4,
            "agent": "QA reviewer",
            "task": "Run automated tests, inspect failures, and verify acceptance criteria.",
        },
    ]


def plan_project_impl(requirement: str, backend: str = "fastapi") -> str:
    return f"""Plan this software project using the available MCP tools.

Requirement:
{requirement}

Backend preference:
{backend}

Steps:
1. Analyze the requirement with analyze_requirement.
2. Create ordered tasks with create_tasks.
3. Apply the backend standards resource.
4. Do not invent tool results.
5. Return objective, scope, architecture, tasks, risks, and next action.
"""


@mcp.tool()
def analyze_requirement(requirement: str) -> dict:
    return analyze_requirement_impl(requirement)


@mcp.tool()
def create_tasks(
    project_type: str,
    backend: Literal["fastapi", "dotnet", "node"] = "fastapi",
) -> list[dict]:
    return create_tasks_impl(project_type, backend)


@mcp.resource("standards://backend")
def backend_standards() -> str:
    return BACKEND_STANDARDS


@mcp.prompt()
def plan_project(requirement: str, backend: str = "fastapi") -> str:
    return plan_project_impl(requirement, backend)


if __name__ == "__main__":
    mcp.run(transport="stdio")
