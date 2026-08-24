from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from graph.builder import build_software_factory_graph
from graph.git_workflow import (
    attributed_paths,
    execute_git_commit_node,
    git_commit_state_updates,
    prepare_git_workflow_node,
)
from graph.nodes import _execute
from graph.persistence_service import WorkflowPersistenceService
from graph.persistence_service import UNSAFE_REPLAY_NEXT_NODES
from graph.runtime import run_software_factory_graph, thread_config
from graph.state import SoftwareFactoryState, create_initial_state
from streaming import InMemoryWorkflowEventEmitter, WorkflowEventFactory, WorkflowEventType, workflow_event_context
from tests.test_langgraph_workflow import (
    CREATE_REQUEST,
    FakeGraphToolExecutor,
    dependencies,
)
from tool_executor import ToolExecutionOutcome


class GitIntegratedExecutor(FakeGraphToolExecutor):
    git_integration_enabled = True

    def __init__(
        self, *, no_changes: bool = False, git_init_timeout: bool = False,
        status_as_directories: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.no_changes = no_changes
        self.git_init_timeout = git_init_timeout
        self.status_as_directories = status_as_directories
        self.git_commit_count = 0

    async def execute(
        self, public_tool_name: str, arguments: dict[str, Any], *,
        state_context: SoftwareFactoryState, approval_mode: str = "prompt",
    ) -> ToolExecutionOutcome:
        if not public_tool_name.startswith("git__"):
            return await super().execute(
                public_tool_name, arguments, state_context=state_context,
                approval_mode=approval_mode,
            )
        self.calls.append((public_tool_name, arguments))
        payload: dict[str, Any] = {"success": True}
        if public_tool_name == "git__init":
            if self.git_init_timeout:
                payload = {
                    "success": False,
                    "status": "mcp_timeout",
                    "failure_type": "mcp_timeout",
                    "message": "Git MCP initialization timed out.",
                }
                return ToolExecutionOutcome(public_tool_name, arguments, payload, {}, is_error=True)
            payload.update(initialized=True, repository=True, head_commit=None)
        elif public_tool_name == "git__create_workflow_branch":
            payload.update(
                branch="workflow/12345678", created=True, switched=True,
                base_branch="main", base_commit=None,
            )
        elif public_tool_name == "git__git_status":
            files = [] if self.no_changes else list(state_context.get("generated_files") or [])
            if state_context.get("repair_attempts"):
                files = list(state_context.get("files_updated_during_repair") or [])
            if self.status_as_directories and files:
                files = [".venv/", "README.md", "phase_6_19_git_e2e_8/", "requirements.txt", "tests/"]
            payload.update(branch="workflow/12345678", clean=not files, staged=[],
                           modified=[], untracked=files, deleted=[])
        elif public_tool_name == "git__stage":
            payload.update(branch="workflow/12345678", staged_files=arguments["paths"],
                           staged_file_count=len(arguments["paths"]))
        elif public_tool_name == "git__prepare_commit":
            payload.update(
                approval_id="approval-git", branch="workflow/12345678",
                staged_files=list(state_context.get("git_staged_files") or arguments.get("paths") or
                                  state_context.get("files_updated_during_repair") or
                                  state_context.get("generated_files") or []),
                additions=10, deletions=1, diff_fingerprint="f" * 64,
                proposed_message=arguments["proposed_message"], ready=True,
                status="awaiting_approval",
            )
        elif public_tool_name == "git__approve_commit":
            payload.update(approval_id="approval-git")
        elif public_tool_name == "git__commit":
            self.git_commit_count += 1
            phase = state_context.get("git_commit_phase") or "implementation"
            payload.update(
                commit=f"{self.git_commit_count:040x}", short_commit=f"{self.git_commit_count:07x}",
                branch="workflow/12345678", message=(state_context.get("git_commit_preview") or {}).get("proposed_message", "commit"),
                files=list(state_context.get("git_staged_files") or []),
                created_at="2026-08-10T00:00:00Z", existing=False, phase=phase,
            )
        elif public_tool_name == "git__reject_commit":
            payload.update(status="rejected", approval_id="approval-git")
        elif public_tool_name == "git__prepare_promotion":
            payload.update(
                promotion_id="promotion-1", approval_id="promotion-approval-1",
                state="awaiting_approval", base_branch="main", base_commit_at_branch_creation=None,
                current_base_commit=None, base_advanced=False,
                workflow_branch="workflow/12345678", workflow_head=f"{self.git_commit_count:040x}",
                commits_ahead=1, commits_behind=0, commits=[f"{self.git_commit_count:040x}"],
                files_changed=list(state_context.get("git_staged_files") or []), additions=10,
                deletions=1, conflict_state="clean", conflicting_files=[],
                merge_strategy_candidate="fast_forward", promotion_fingerprint="p" * 64,
                ready=True,
            )
        elif public_tool_name == "git__approve_promotion":
            payload.update(approval_id="promotion-approval-1")
        elif public_tool_name == "git__merge_workflow_branch":
            payload.update(
                promotion_id="promotion-1", strategy="fast_forward", base_branch="main",
                workflow_branch="workflow/12345678", previous_base_commit=None,
                workflow_head=f"{self.git_commit_count:040x}", result_commit=f"{self.git_commit_count:040x}",
                merged_commits=[f"{self.git_commit_count:040x}"], files=[],
                completed_at="2026-08-10T00:00:00Z", existing=False,
            )
        elif public_tool_name == "git__reject_promotion":
            payload.update(status="rejected", approval_id="promotion-approval-1")
        return ToolExecutionOutcome(public_tool_name, arguments, payload, {})


def test_recovery_commit_updates_are_idempotent_and_clear_pending_state() -> None:
    state: SoftwareFactoryState = {
        "git_commit_phase": "implementation",
        "git_commit_history": [{"phase": "implementation", "sha": "a" * 40}],
        "pending_operation": "git_commit",
        "pending_tool_name": "git__commit",
        "terminal_status": "infrastructure_failed",
        "failure_type": "git_command_failed",
    }
    updates = git_commit_state_updates(
        state,
        {
            "commit": "a" * 40,
            "message": "fix: recovered",
            "files": ["README.md"],
            "existing": True,
        },
        recovery=True,
    )

    assert len(updates["git_commit_history"]) == 1
    assert updates["pending_operation"] is None
    assert updates["pending_tool_name"] is None
    assert updates["terminal_status"] is None
    assert updates["failure_type"] is None


@pytest.mark.asyncio
async def test_post_approval_recovery_skips_commit_node_and_continues_once_to_ci() -> None:
    calls: list[str] = []

    async def failed_commit(_state: SoftwareFactoryState) -> dict[str, Any]:
        calls.append("execute_git_commit")
        raise RuntimeError("failure after approval")

    async def supervisor(_state: SoftwareFactoryState) -> dict[str, Any]:
        calls.append("supervisor")
        return {}

    async def ci_pipeline(_state: SoftwareFactoryState) -> dict[str, Any]:
        calls.append("ci_pipeline")
        return {"ci_status": "passed", "terminal_status": "completed"}

    builder = StateGraph(SoftwareFactoryState)
    builder.add_node("execute_git_commit", failed_commit)
    builder.add_node("supervisor", supervisor)
    builder.add_node("ci_pipeline", ci_pipeline)
    builder.add_edge(START, "execute_git_commit")
    builder.add_edge("execute_git_commit", "supervisor")
    builder.add_edge("supervisor", "ci_pipeline")
    builder.add_edge("ci_pipeline", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    thread_id = "post-approval-recovery"
    initial = create_initial_state("recover Git commit")
    initial.update({
        "workflow_id": thread_id,
        "pending_operation": "git_commit",
        "pending_tool_name": "git__commit",
        "pending_approval_status": "approved",
    })
    with pytest.raises(RuntimeError, match="failure after approval"):
        await graph.ainvoke(initial, config=thread_config(thread_id))

    service = WorkflowPersistenceService(graph)
    result = await service.continue_after_recovered_git_operation(
        thread_id,
        {
            "git_commit_status": "committed",
            "git_head_commit": "a" * 40,
            "pending_operation": None,
            "pending_tool_name": None,
            "pending_approval_status": "none",
        },
        as_node="execute_git_commit",
    )

    assert calls == ["execute_git_commit", "supervisor", "ci_pipeline"]
    assert result.final_state["ci_status"] == "passed"
    assert result.final_state["git_head_commit"] == "a" * 40
    assert result.final_state["pending_operation"] is None


async def run_git_graph(executor: GitIntegratedExecutor):
    sequence: list[str] = []
    graph = build_software_factory_graph(
        dependencies(executor, sequence), checkpointer=InMemorySaver()
    )
    result = await run_software_factory_graph(graph, CREATE_REQUEST)
    persistence = WorkflowPersistenceService(graph)
    approvals = 0
    while result.interrupted:
        approvals += 1
        result = await persistence.approve(result.thread_id)
    return result, approvals, sequence


def state_with_repair_commit(*, source_commit: str) -> SoftwareFactoryState:
    return {
        "workflow_id": "workflow-ci-repair",
        "project_name": "health-api",
        "created_project_name": "health-api",
        "git_commit_phase": "repair",
        "git_commit_approval_id": "approval-git",
        "git_commit_preview": {"proposed_message": "Repair CI failure"},
        "git_staged_files": ["tests/test_health.py"],
        "git_commit_history": [],
        "ci_repair_state": "pending",
        "ci_repair_attempts": 1,
        "ci_repair_source_run_id": "ci-run-1",
        "ci_repair_source_commit": source_commit,
        "ci_validated_commit": source_commit,
        "ci_promotion_eligible": True,
        "ci_promotion_eligibility": {"eligible": True},
        "git_promotion_state": "awaiting_approval",
        "git_promotion_preview": {"promotion_id": "promotion-1"},
        "git_promotion_approval_id": "promotion-approval-1",
    }


@pytest.mark.asyncio
async def test_git_workspace_and_commit_are_automatic_and_ordered_after_tests() -> None:
    executor = GitIntegratedExecutor()
    result, approvals, sequence = await run_git_graph(executor)

    assert approvals == 4
    assert result.final_state["git_workflow_state"] == "committed"
    assert result.final_state["git_developer_commit_sha"] == f"{1:040x}"
    assert result.final_state["git_commit_history"][0]["phase"] == "implementation"
    assert sequence.index("git_workflow") < sequence.index("testing_repair")
    assert sequence.index("testing_repair") < sequence.index("execute_git_commit")
    calls = [name for name, _arguments in executor.calls]
    assert calls.index("git__init") < calls.index("git__create_workflow_branch")
    assert calls.index("git__create_workflow_branch") < calls.index("testing__run_tests")
    assert calls.index("testing__run_tests") < calls.index("git__prepare_commit")
    assert calls.count("git__commit") == 1


@pytest.mark.asyncio
async def test_git_init_timeout_marks_workflow_failed_without_continuing_git() -> None:
    executor = GitIntegratedExecutor(git_init_timeout=True)
    result, approvals, sequence = await run_git_graph(executor)
    calls = [name for name, _arguments in executor.calls]

    assert approvals == 2
    assert result.final_state["terminal_status"] == "infrastructure_failed"
    assert result.final_state["git_workflow_state"] == "git_initialization_failed"
    assert result.final_state["git_state"] == "git_unavailable"
    assert result.final_state["failure_type"] == "git_initialization_failed"
    assert result.final_state["failure_stage"] == "git.init"
    assert "git__git_status" not in calls
    assert "git__create_workflow_branch" not in calls
    assert "execute_git_commit" not in sequence


@pytest.mark.asyncio
async def test_git_init_timeout_event_history_has_terminal_tool_failure() -> None:
    executor = GitIntegratedExecutor(git_init_timeout=True)
    sequence: list[str] = []
    state = {
        "workflow_id": "timeout-thread",
        "project_name": "medical-booking",
        "created_project_name": "medical-booking",
        "git_workflow_state": "not_initialized",
        "repair_attempts": 0,
    }
    emitter = InMemoryWorkflowEventEmitter()
    with workflow_event_context(
        thread_id="timeout-thread",
        emitter=emitter,
        factory=WorkflowEventFactory(),
    ):
        await prepare_git_workflow_node(state, dependencies(executor, sequence))

    event_types = [event.type for event in emitter.get_events("timeout-thread")]
    assert event_types == [WorkflowEventType.TOOL_STARTED, WorkflowEventType.TOOL_FAILED]
    failure = emitter.get_events("timeout-thread")[-1]
    assert failure.data["server"] == "git"
    assert failure.data["tool"] == "init"
    assert failure.data["failure_type"] == "mcp_timeout"


@pytest.mark.asyncio
async def test_no_changes_skips_git_approval_and_commit() -> None:
    executor = GitIntegratedExecutor(no_changes=True)
    result, approvals, _sequence = await run_git_graph(executor)
    assert approvals == 3
    assert result.final_state["git_commit_status"] == "no_changes"
    assert executor.git_commit_count == 0


@pytest.mark.asyncio
async def test_failed_tests_never_prepare_or_execute_commit() -> None:
    executor = GitIntegratedExecutor(tests_always_fail=True)
    result, _approvals, _sequence = await run_git_graph(executor)
    calls = [name for name, _arguments in executor.calls]
    assert result.final_state.get("tests_passed") is not True
    assert "git__prepare_commit" not in calls
    assert "git__commit" not in calls


@pytest.mark.asyncio
async def test_repair_changes_are_attributed_to_repair_commit() -> None:
    executor = GitIntegratedExecutor(fail_first_test=True)
    result, _approvals, _sequence = await run_git_graph(executor)
    history = result.final_state["git_commit_history"]
    assert history[-1]["phase"] == "repair"
    assert history[-1]["agent"] == "Repair"
    assert history[-1]["files"] == ["tests/test_health.py"]
    assert result.final_state["git_repair_commit_sha"]


@pytest.mark.asyncio
async def test_ci_repair_commit_invalidates_previous_ci_evidence() -> None:
    executor = GitIntegratedExecutor()
    state = state_with_repair_commit(source_commit=f"{0:040x}")
    result = await execute_git_commit_node(state, dependencies(executor, []))

    assert result["git_repair_commit_sha"] == f"{1:040x}"
    assert result["ci_repair_state"] == "running"
    assert result["ci_repair_target_commit"] == f"{1:040x}"
    assert result["ci_validated_commit"] is None
    assert result["ci_promotion_eligible"] is False
    assert result["ci_promotion_eligibility"] is None
    assert result["git_promotion_state"] == "not_started"


@pytest.mark.asyncio
async def test_ci_repair_commit_must_advance_source_commit() -> None:
    executor = GitIntegratedExecutor()
    state = state_with_repair_commit(source_commit=f"{1:040x}")
    result = await execute_git_commit_node(state, dependencies(executor, []))

    assert result["git_repair_commit_sha"] == f"{1:040x}"
    assert result["ci_repair_state"] == "failed"
    assert result["terminal_status"] == "ci_failed"
    assert result["failure_type"] == "ci_repair_commit_not_advanced"


def test_change_attribution_excludes_unrelated_dirty_files() -> None:
    state: SoftwareFactoryState = {
        "project_name": "api", "generated_files": ["api/app.py", "api/tests/test_app.py"],
    }
    owned, unrelated = attributed_paths(state, {
        "modified": ["app.py", "notes/private.txt"], "untracked": ["tests/test_app.py"],
        "staged": [], "deleted": [],
    })
    assert owned == ["app.py", "tests/test_app.py"]
    assert unrelated == ["notes/private.txt"]


def test_infrastructure_artifacts_are_ignored_and_never_staged(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".venv").mkdir()
    (project / ".venv" / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")
    (project / ".pytest_cache").mkdir()
    (project / ".coverage").write_text("coverage\n", encoding="utf-8")
    state: SoftwareFactoryState = {
        "project_name": "project", "generated_files": ["README.md"], "_project_root": str(project),
    }

    owned, unrelated = attributed_paths(state, {
        "modified": [], "staged": [], "deleted": [],
        "untracked": [".venv/", ".pytest_cache/", ".coverage", "README.md"],
    })

    assert owned == ["README.md"]
    assert unrelated == []


def test_untracked_directories_with_only_owned_files_are_attributed(tmp_path: Path) -> None:
    project = tmp_path / "phase-6-19-git-e2e-8"
    package = project / "phase_6_19_git_e2e_8"
    tests = project / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (project / "README.md").write_text("readme\n", encoding="utf-8")
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text("app = None\n", encoding="utf-8")
    (tests / "test_health.py").write_text("def test_health(): pass\n", encoding="utf-8")
    state: SoftwareFactoryState = {
        "project_name": "phase-6-19-git-e2e-8",
        "generated_files": [
            "README.md",
            "requirements.txt",
            "phase_6_19_git_e2e_8/__init__.py",
            "phase_6_19_git_e2e_8/main.py",
            "tests/test_health.py",
        ],
        "_project_root": str(project),
    }

    owned, unrelated = attributed_paths(state, {
        "modified": [], "staged": [], "deleted": [],
        "untracked": [".venv/", "README.md", "phase_6_19_git_e2e_8/", "requirements.txt", "tests/"],
    })

    assert unrelated == []
    assert owned == [
        "README.md",
        "phase_6_19_git_e2e_8/__init__.py",
        "phase_6_19_git_e2e_8/main.py",
        "requirements.txt",
        "tests/test_health.py",
    ]


def test_untracked_directory_with_unrelated_file_conflicts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_health.py").write_text("def test_health(): pass\n", encoding="utf-8")
    (tests / "manual_secret.txt").write_text("secret\n", encoding="utf-8")
    state: SoftwareFactoryState = {
        "project_name": "project",
        "generated_files": ["tests/test_health.py"],
        "_project_root": str(project),
    }

    owned, unrelated = attributed_paths(state, {
        "modified": [], "staged": [], "deleted": [], "untracked": ["tests/"],
    })

    assert owned == []
    assert unrelated == ["tests"]


def test_windows_separators_are_normalized_for_attribution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_health.py").write_text("def test_health(): pass\n", encoding="utf-8")
    state: SoftwareFactoryState = {
        "project_name": "project",
        "generated_files": [r"project\tests\test_health.py"],
        "_project_root": str(project),
    }

    owned, unrelated = attributed_paths(state, {
        "modified": [], "staged": [], "deleted": [], "untracked": [r".\tests\\"],
    })

    assert owned == ["tests/test_health.py"]
    assert unrelated == []


@pytest.mark.asyncio
async def test_directory_status_allows_workflow_branch_creation_after_clean_attribution(tmp_path: Path) -> None:
    project = tmp_path / "phase-6-19-git-e2e-8"
    (project / ".venv").mkdir(parents=True)
    (project / ".venv" / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")
    (project / "phase_6_19_git_e2e_8").mkdir()
    (project / "tests").mkdir()
    for relative in (
        "README.md", "requirements.txt",
        "phase_6_19_git_e2e_8/__init__.py",
        "phase_6_19_git_e2e_8/main.py",
        "tests/test_health.py",
    ):
        (project / relative).write_text("content\n", encoding="utf-8")
    executor = GitIntegratedExecutor(status_as_directories=True)
    state: SoftwareFactoryState = {
        "workflow_id": "0dc1d5dc-a400-47f9-96ce-30b7bd04c2ad",
        "project_name": "phase-6-19-git-e2e-8",
        "created_project_name": "phase-6-19-git-e2e-8",
        "git_workflow_state": "not_initialized",
        "generated_files": [
            "README.md",
            "requirements.txt",
            "phase_6_19_git_e2e_8/__init__.py",
            "phase_6_19_git_e2e_8/main.py",
            "tests/test_health.py",
        ],
        "_project_root": str(project),
    }

    result = await prepare_git_workflow_node(state, dependencies(executor, []))

    calls = [name for name, _arguments in executor.calls]
    branch_call = executor.calls[calls.index("git__create_workflow_branch")]
    assert result["git_workflow_state"] == "workspace_prepared"
    assert result["git_workflow_branch"] == "workflow/12345678"
    assert branch_call[1]["allowed_dirty_paths"] == [
        "README.md",
        "phase_6_19_git_e2e_8/__init__.py",
        "phase_6_19_git_e2e_8/main.py",
        "requirements.txt",
        "tests/test_health.py",
    ]


@pytest.mark.asyncio
async def test_git_workspace_dirty_conflict_propagates_failure_details(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_health.py").write_text("def test_health(): pass\n", encoding="utf-8")
    (tests / "manual_secret.txt").write_text("secret\n", encoding="utf-8")
    executor = GitIntegratedExecutor(status_as_directories=True)
    state: SoftwareFactoryState = {
        "workflow_id": "workflow-conflict",
        "project_name": "project",
        "created_project_name": "project",
        "git_workflow_state": "not_initialized",
        "generated_files": ["tests/test_health.py"],
        "_project_root": str(project),
    }

    result = await prepare_git_workflow_node(state, dependencies(executor, []))

    calls = [name for name, _arguments in executor.calls]
    assert "git__create_workflow_branch" not in calls
    assert result["terminal_status"] == "implementation_failed"
    assert result["failure_type"] == "git_workspace_dirty_conflict"
    assert result["failure_stage"] == "prepare_git_workspace"
    assert result["failure_message"]


@pytest.mark.asyncio
async def test_tool_failed_event_preserves_git_error_code_as_failure_type() -> None:
    class DirtyConflictExecutor(GitIntegratedExecutor):
        async def execute(
            self, public_tool_name: str, arguments: dict[str, Any], *,
            state_context: SoftwareFactoryState, approval_mode: str = "prompt",
        ) -> ToolExecutionOutcome:
            return ToolExecutionOutcome(
                public_tool_name,
                arguments,
                {
                    "success": False,
                    "error_code": "git_workspace_dirty_conflict",
                    "message": "Existing working tree contains changes.",
                },
                {},
                is_error=True,
            )

    emitter = InMemoryWorkflowEventEmitter()
    with workflow_event_context(
        thread_id="dirty-event",
        emitter=emitter,
        factory=WorkflowEventFactory(),
    ):
        await _execute(
            "git__create_workflow_branch",
            {"project_id": "project", "workflow_id": "dirty-event"},
            {"repair_attempts": 0},
            dependencies(DirtyConflictExecutor(), []),
            node_name="prepare_git_workspace",
        )

    failure = emitter.get_events("dirty-event")[-1]
    assert failure.type == WorkflowEventType.TOOL_FAILED
    assert failure.data["failure_type"] == "git_workspace_dirty_conflict"


def test_replay_policy_never_allows_git_mutation_nodes() -> None:
    assert {"git_workflow", "git_approval", "execute_git_commit", "reject_git_commit"} <= UNSAFE_REPLAY_NEXT_NODES


@pytest.mark.asyncio
async def test_auto_prepare_promotion_pauses_and_merges_only_after_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_AUTO_PREPARE_PROMOTION", "true")
    executor = GitIntegratedExecutor()
    result, approvals, sequence = await run_git_graph(executor)
    calls = [name for name, _arguments in executor.calls]

    assert approvals == 5
    assert result.final_state["git_promotion_state"] == "completed"
    assert result.final_state["git_promotion_strategy"] == "fast_forward"
    assert calls.index("git__commit") < calls.index("git__prepare_promotion")
    assert calls.index("git__prepare_promotion") < calls.index("git__merge_workflow_branch")
    assert calls.count("git__merge_workflow_branch") == 1
    assert sequence.index("execute_git_commit") < sequence.index("git_promotion")
