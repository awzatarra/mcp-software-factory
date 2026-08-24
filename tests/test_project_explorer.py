from __future__ import annotations

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import zipfile

import pytest

from api.models import WorkflowSnapshotResponse
from api.services.project_explorer_service import (
    ExplorerPolicy,
    ProjectExplorerError,
    ProjectExplorerService,
    resolve_safe_project_path,
)
from api.services.workflow_metadata_store import WorkflowMetadataStore
from graph.persistence_service import WorkflowNotFoundError
from streaming.sqlite_store import SQLiteWorkflowEventStore


def workflow_snapshot(
    thread_id: str = "thread-1",
    project_name: str | None = "demo-api",
) -> WorkflowSnapshotResponse:
    return WorkflowSnapshotResponse(
        thread_id=thread_id,
        checkpoint_id="checkpoint-1",
        project_name=project_name,
        workflow_intent="create_project",
        terminal_status="completed",
        interrupted=False,
        pending_operation=None,
        pending_tool=None,
        planning={"attempts": 1},
        implementation={"attempts": 1, "detected_framework": "FastAPI"},
        testing={
            "executed": True,
            "passed": True,
            "detected_test_framework": "pytest",
        },
        supervisor={"decision": "finalize"},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


class FakeQuery:
    def __init__(
        self,
        selected: WorkflowSnapshotResponse | None = None,
        *,
        missing: bool = False,
    ) -> None:
        self.selected = selected or workflow_snapshot()
        self.missing = missing

    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshotResponse:
        if self.missing:
            raise WorkflowNotFoundError(thread_id)
        return self.selected.model_copy(update={"thread_id": thread_id})


@pytest.fixture
async def explorer(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = workspace / "demo-api"
    (project / "app").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "app" / "main.py").write_text(
        'from fastapi import FastAPI\napp = FastAPI()\n',
        encoding="utf-8",
    )
    (project / "tests" / "test_health.py").write_text(
        'def test_health():\n    assert True\n',
        encoding="utf-8",
    )
    (project / "README.md").write_text("# Demo\n", encoding="utf-8")
    event_store = SQLiteWorkflowEventStore(tmp_path / "explorer.sqlite")
    await event_store.initialize()
    metadata = WorkflowMetadataStore(event_store.database_path)
    service = ProjectExplorerService(
        query=FakeQuery(),  # type: ignore[arg-type]
        metadata_store=metadata,
        workspace_root=workspace,
    )
    yield service, metadata, workspace, project
    await event_store.close()


@pytest.mark.asyncio
async def test_summary_rejects_unknown_workflow(explorer) -> None:
    service, metadata, workspace, _ = explorer
    missing = ProjectExplorerService(
        query=FakeQuery(missing=True),  # type: ignore[arg-type]
        metadata_store=metadata,
        workspace_root=workspace,
    )
    with pytest.raises(WorkflowNotFoundError):
        await missing.summary("missing")


@pytest.mark.asyncio
async def test_summary_returns_project_not_created(explorer) -> None:
    service, metadata, workspace, _ = explorer
    pending = ProjectExplorerService(
        query=FakeQuery(workflow_snapshot(project_name="pending-api")),  # type: ignore[arg-type]
        metadata_store=metadata,
        workspace_root=workspace,
    )
    result = await pending.summary("pending")
    assert result.project_exists is False
    assert result.relative_project_path is None


@pytest.mark.asyncio
async def test_summary_returns_created_project_metadata(explorer) -> None:
    service, _, _, _ = explorer
    result = await service.summary("thread-1")
    assert result.project_exists is True
    assert result.project_name == "demo-api"
    assert result.total_files == 3
    assert result.total_directories == 2
    assert result.detected_framework == "FastAPI"
    assert result.detected_test_framework == "pytest"


@pytest.mark.asyncio
async def test_tree_returns_nested_structure(explorer) -> None:
    service, _, _, _ = explorer
    result = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=False
    )
    assert result.root.type == "directory"
    assert result.total_entries == 5
    assert result.root.children


@pytest.mark.asyncio
async def test_tree_sorts_directories_before_files(explorer) -> None:
    service, _, _, _ = explorer
    result = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=False
    )
    types = [node.type for node in result.root.children or []]
    assert types == ["directory", "directory", "file"]


@pytest.mark.asyncio
async def test_tree_sorts_case_insensitively(explorer) -> None:
    service, _, _, project = explorer
    (project / "aaa.txt").write_text("a", encoding="utf-8")
    (project / "Zzz.txt").write_text("z", encoding="utf-8")
    result = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=False
    )
    files = [node.name for node in result.root.children or [] if node.type == "file"]
    assert files == ["aaa.txt", "README.md", "Zzz.txt"]


@pytest.mark.asyncio
async def test_tree_paths_always_use_forward_slashes(explorer) -> None:
    service, _, _, _ = explorer
    result = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=False
    )
    app = next(node for node in result.root.children or [] if node.name == "app")
    assert app.children and app.children[0].path == "app/main.py"
    assert "\\" not in app.children[0].path


@pytest.mark.asyncio
async def test_text_content_is_returned_unchanged(explorer) -> None:
    service, _, _, _ = explorer
    result = await service.content("thread-1", "app/main.py")
    assert result.content == 'from fastapi import FastAPI\napp = FastAPI()\n'
    assert result.language == "python"
    assert result.line_count == 2


@pytest.mark.asyncio
async def test_binary_content_returns_415(explorer) -> None:
    service, _, _, project = explorer
    (project / "binary.dat").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ProjectExplorerError) as raised:
        await service.content("thread-1", "binary.dat")
    assert raised.value.status_code == 415


@pytest.mark.asyncio
async def test_large_content_returns_413(explorer) -> None:
    service, metadata, workspace, project = explorer
    (project / "large.txt").write_text("x" * 20, encoding="utf-8")
    limited = ProjectExplorerService(
        query=FakeQuery(), metadata_store=metadata, workspace_root=workspace,  # type: ignore[arg-type]
        policy=ExplorerPolicy(max_file_size=10),
    )
    with pytest.raises(ProjectExplorerError) as raised:
        await limited.content("thread-1", "large.txt")
    assert raised.value.status_code == 413


@pytest.mark.asyncio
async def test_directory_cannot_be_read_as_content(explorer) -> None:
    service, _, _, _ = explorer
    with pytest.raises(ProjectExplorerError) as raised:
        await service.content("thread-1", "app")
    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_file_returns_404(explorer) -> None:
    service, _, _, _ = explorer
    with pytest.raises(ProjectExplorerError) as raised:
        await service.content("thread-1", "missing.py")
    assert raised.value.status_code == 404


@pytest.mark.parametrize(
    "unsafe",
    [
        "../secret.env",
        r"..\secret.env",
        r"C:\Windows\System32",
        r"\\server\share",
        "%2e%2e%2fsecret.env",
        "%252e%252e%252fsecret.env",
    ],
)
def test_unsafe_paths_are_rejected(
    explorer,
    unsafe: str,
) -> None:
    _, _, workspace, _ = explorer
    with pytest.raises(ProjectExplorerError):
        resolve_safe_project_path(workspace, "demo-api", unsafe)


@pytest.mark.asyncio
async def test_external_symlink_is_rejected_or_hidden(
    explorer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, workspace, project = explorer
    outside = workspace.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = project / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        link.write_text("link placeholder", encoding="utf-8")
        original_resolve = Path.resolve

        def simulated_external_link(self: Path, *args, **kwargs):
            if self == link:
                return original_resolve(outside)
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", simulated_external_link)
    with pytest.raises(ProjectExplorerError):
        await service.content("thread-1", "outside-link.txt")
    tree = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=True
    )
    assert "outside-link.txt" not in {node.name for node in tree.root.children or []}


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".venv/pyvenv.cfg",
        ".git/config",
        "node_modules/pkg/index.js",
        "private.pem",
        "credentials.json",
    ],
)
@pytest.mark.asyncio
async def test_sensitive_files_are_excluded_from_tree(
    explorer,
    relative: str,
) -> None:
    service, _, _, project = explorer
    target = project / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sensitive", encoding="utf-8")
    result = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=True
    )

    def paths(node):
        return {node.path} | {
            child_path
            for child in node.children or []
            for child_path in paths(child)
        }

    assert relative.replace("\\", "/") not in paths(result.root)


@pytest.mark.asyncio
async def test_excluded_file_is_not_accessible_directly(explorer) -> None:
    service, _, _, project = explorer
    (project / ".env").write_text("TOKEN=secret", encoding="utf-8")
    with pytest.raises(ProjectExplorerError) as raised:
        await service.content("thread-1", ".env")
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_individual_download_target_is_correct_and_safe(explorer) -> None:
    service, _, _, project = explorer
    target = await service.download_target("thread-1", "app/main.py")
    assert target == (project / "app" / "main.py").resolve()
    assert target.name == "main.py"


@pytest.mark.asyncio
async def test_archive_is_a_valid_zip(explorer) -> None:
    service, _, _, _ = explorer
    archive, filename, _ = await service.archive("thread-1")
    try:
        with zipfile.ZipFile(archive) as selected:
            assert selected.testzip() is None
        assert filename == "demo-api.zip"
    finally:
        archive.close()


@pytest.mark.asyncio
async def test_archive_excludes_sensitive_files(explorer) -> None:
    service, _, _, project = explorer
    (project / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (project / "secret.key").write_text("key", encoding="utf-8")
    archive, _, _ = await service.archive("thread-1")
    try:
        with zipfile.ZipFile(archive) as selected:
            assert ".env" not in selected.namelist()
            assert "secret.key" not in selected.namelist()
    finally:
        archive.close()


@pytest.mark.asyncio
async def test_archive_preserves_relative_structure(explorer) -> None:
    service, _, _, _ = explorer
    archive, _, _ = await service.archive("thread-1")
    try:
        with zipfile.ZipFile(archive) as selected:
            assert "app/main.py" in selected.namelist()
            assert "tests/test_health.py" in selected.namelist()
    finally:
        archive.close()


@pytest.mark.asyncio
async def test_archive_respects_uncompressed_size_limit(explorer) -> None:
    _, metadata, workspace, project = explorer
    (project / "large.txt").write_text("x" * 50, encoding="utf-8")
    limited = ProjectExplorerService(
        query=FakeQuery(), metadata_store=metadata, workspace_root=workspace,  # type: ignore[arg-type]
        policy=ExplorerPolicy(max_archive_size=10),
    )
    with pytest.raises(ProjectExplorerError) as raised:
        await limited.archive("thread-1")
    assert raised.value.status_code == 413


@pytest.mark.asyncio
async def test_archive_filename_is_sanitized(explorer) -> None:
    service, _, _, _ = explorer
    archive, filename, _ = await service.archive("thread-1")
    archive.close()
    assert filename == "demo-api.zip"
    assert "/" not in filename and "\\" not in filename


@pytest.mark.asyncio
async def test_generated_file_is_marked(explorer) -> None:
    service, metadata, _, _ = explorer
    await metadata.sync_project_files(
        "thread-1", generated_files=["app/main.py"], updated_files=[]
    )
    tree = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=False
    )
    app = next(node for node in tree.root.children or [] if node.name == "app")
    assert app.children and app.children[0].is_generated is True


@pytest.mark.asyncio
async def test_updated_file_is_marked(explorer) -> None:
    service, metadata, _, _ = explorer
    await metadata.sync_project_files(
        "thread-1", generated_files=[], updated_files=["tests/test_health.py"]
    )
    content = await service.content("thread-1", "tests/test_health.py")
    assert content.is_updated is True


@pytest.mark.asyncio
async def test_project_file_metadata_survives_store_restart(explorer) -> None:
    _, metadata, _, _ = explorer
    await metadata.sync_project_files(
        "thread-1", generated_files=["README.md"], updated_files=[]
    )
    restarted = WorkflowMetadataStore(metadata.database_path)
    assert await restarted.get_project_files("thread-1") == {
        "README.md": "generated"
    }


@pytest.mark.asyncio
async def test_project_file_metadata_does_not_mix_branches(explorer) -> None:
    _, metadata, _, _ = explorer
    await metadata.sync_project_files(
        "thread-1", generated_files=["README.md"], updated_files=[]
    )
    await metadata.sync_project_files(
        "thread-1",
        generated_files=[],
        updated_files=["app/main.py"],
        branch_id="fork-1",
    )
    assert await metadata.get_project_files("thread-1") == {
        "README.md": "generated"
    }


@pytest.mark.asyncio
async def test_tree_is_truncated_at_configured_limit(explorer) -> None:
    _, metadata, workspace, _ = explorer
    limited = ProjectExplorerService(
        query=FakeQuery(), metadata_store=metadata, workspace_root=workspace,  # type: ignore[arg-type]
        policy=ExplorerPolicy(max_tree_entries=2),
    )
    tree = await limited.tree(
        "thread-1", relative_path="", depth=20, include_hidden=False
    )
    assert tree.truncated is True
    assert tree.total_entries == 2


@pytest.mark.asyncio
async def test_include_hidden_never_exposes_sensitive_files(explorer) -> None:
    service, _, _, project = explorer
    (project / ".env").write_text("secret", encoding="utf-8")
    (project / ".gitignore").write_text(".env\n", encoding="utf-8")
    tree = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=True
    )
    names = {node.name for node in tree.root.children or []}
    assert ".gitignore" in names
    assert ".env" not in names


@pytest.mark.asyncio
async def test_responses_never_include_absolute_workspace_path(explorer) -> None:
    service, _, workspace, _ = explorer
    summary = await service.summary("thread-1")
    tree = await service.tree(
        "thread-1", relative_path="", depth=20, include_hidden=False
    )
    serialized = summary.model_dump_json() + tree.model_dump_json()
    assert str(workspace) not in serialized


@pytest.mark.asyncio
async def test_audit_logs_do_not_contain_file_content(explorer, caplog) -> None:
    service, _, _, _ = explorer
    with caplog.at_level(logging.INFO):
        await service.content("thread-1", "app/main.py")
    assert "from fastapi import FastAPI" not in caplog.text
    assert "thread-1" in caplog.text
    assert "app/main.py" in caplog.text
