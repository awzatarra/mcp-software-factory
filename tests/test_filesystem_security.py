from __future__ import annotations

from pathlib import Path

import pytest

from servers import filesystem_server as fs


@pytest.fixture()
def isolated_workspace(tmp_path: Path):
    original = fs.WORKSPACE_ROOT
    fs.set_workspace_root(tmp_path / "workspace")
    yield fs.get_workspace_root()
    fs.set_workspace_root(original)


def test_rejects_absolute_paths(isolated_workspace: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        fs.resolve_workspace_path(str(isolated_workspace.resolve()))


def test_rejects_parent_traversal(isolated_workspace: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        fs.resolve_workspace_path("../outside.txt")


def test_accepts_valid_paths(isolated_workspace: Path) -> None:
    resolved = fs.resolve_workspace_path("project/app.py")
    assert resolved == isolated_workspace / "project" / "app.py"


def test_workspace_root_can_be_configured_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "persistent-workspace"
    monkeypatch.setenv("WORKSPACE_ROOT", str(configured))

    assert fs.workspace_root_from_environment() == configured.resolve()


def test_detects_duplicate_project_files_case_insensitive(isolated_workspace: Path) -> None:
    files = [
        fs.ProjectFile(path="app.py", content="a"),
        fs.ProjectFile(path="APP.py", content="b"),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        fs.create_project_structure_impl("demo", files)


def test_rejects_invalid_project_names(isolated_workspace: Path) -> None:
    with pytest.raises(ValueError, match="project_name"):
        fs.create_project_structure_impl("../bad", [fs.ProjectFile(path="app.py", content="")])


def test_rejects_existing_projects(isolated_workspace: Path) -> None:
    (isolated_workspace / "demo").mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        fs.create_project_structure_impl("demo", [fs.ProjectFile(path="app.py", content="")])


def test_existing_project_is_not_deleted_when_creation_is_rejected(isolated_workspace: Path) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        fs.create_project_structure_impl("demo", [fs.ProjectFile(path="app.py", content="new")])

    assert project.is_dir()
    assert (project / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_create_project_failure_before_publish_cleans_temp_and_does_not_create_destination(
    isolated_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_text = Path.write_text

    def failing_write_text(self: Path, data: str, *args, **kwargs):
        if self.name == "app.py":
            raise OSError("simulated write failure before publish")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    with pytest.raises(OSError, match="before publish"):
        fs.create_project_structure_impl("demo", [fs.ProjectFile(path="app.py", content="new")])

    assert not (isolated_workspace / "demo").exists()
    assert list(isolated_workspace.glob(".demo.*")) == []


def test_rejects_project_file_paths_that_include_project_name_prefix(isolated_workspace: Path) -> None:
    with pytest.raises(ValueError, match="relativas a la raíz del proyecto"):
        fs.create_project_structure_impl(
            "medical-booking",
            [fs.ProjectFile(path="medical-booking/app/main.py", content="")],
        )


def test_accepts_project_file_paths_relative_to_project_root(isolated_workspace: Path) -> None:
    result = fs.create_project_structure_impl(
        "medical-booking",
        [
            fs.ProjectFile(path="medical_booking/main.py", content=""),
            fs.ProjectFile(path="tests/test_health.py", content=""),
            fs.ProjectFile(path="README.md", content=""),
        ],
    )

    assert result["success"] is True
    assert (isolated_workspace / "medical-booking" / "medical_booking" / "main.py").is_file()
    assert not (isolated_workspace / "medical-booking" / "medical-booking").exists()


def test_list_files_excludes_ignored_directories_by_default(isolated_workspace: Path) -> None:
    project = isolated_workspace / "medical-booking"
    (project / ".venv" / "Scripts").mkdir(parents=True)
    (project / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_health.py").write_text("", encoding="utf-8")

    result = fs.list_files_impl("medical-booking", recursive=True)
    paths = {entry["path"] for entry in result["entries"]}

    assert "medical-booking/tests/test_health.py" in paths
    assert not any(".venv" in path for path in paths)


def test_list_files_can_include_ignored_directories(isolated_workspace: Path) -> None:
    project = isolated_workspace / "medical-booking"
    (project / ".venv" / "Scripts").mkdir(parents=True)
    (project / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")

    result = fs.list_files_impl("medical-booking", recursive=True, include_ignored=True)
    paths = {entry["path"] for entry in result["entries"]}

    assert "medical-booking/.venv/Scripts/python.exe" in paths


def test_update_project_files_rolls_back_on_write_failure(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "a.py").write_text("old a", encoding="utf-8")
    (project / "b.py").write_text("old b", encoding="utf-8")
    original_write_text = Path.write_text

    def flaky_write_text(self: Path, data: str, *args, **kwargs):
        if self.name == "b.py" and data == "new b":
            raise OSError("simulated write failure")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    with pytest.raises(OSError, match="simulated"):
        fs.update_project_files_impl(
            "demo",
            [
                fs.FileUpdate(path="a.py", content="new a"),
                fs.FileUpdate(path="b.py", content="new b"),
            ],
        )

    assert (project / "a.py").read_text(encoding="utf-8") == "old a"
    assert (project / "b.py").read_text(encoding="utf-8") == "old b"

