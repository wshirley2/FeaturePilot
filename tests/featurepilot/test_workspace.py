import json
from pathlib import Path

import pytest

from featurepilot.cli import main
from featurepilot.domain import Plan, PlanRecord, Task
from featurepilot.planning import PlanStore
from featurepilot.workspace import CopyWorkspaceBackend, WorkspaceService


def _write_source_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 'source'\n", encoding="utf-8")
    (repository / ".git").mkdir()
    (repository / ".git" / "HEAD").write_text("ref: main\n", encoding="utf-8")
    (repository / ".venv").mkdir()
    (repository / ".venv" / "private.txt").write_text("ignore me\n", encoding="utf-8")
    return repository


def _plan_record(repository: Path, status: str = "approved") -> PlanRecord:
    task = Task(project_id=str(repository), description="Change app value", id="task-id")
    plan = Plan(
        task_id=task.id,
        summary=task.description,
        steps=["Read app.py"],
        read_files=["app.py"],
        modify_files=["app.py"],
    )
    return PlanRecord(
        plan=plan,
        task=task,
        repository=str(repository),
        version=1,
        name="change-app",
        status=status,
    )


def test_copy_workspace_is_isolated_and_blocks_escaping_paths(tmp_path):
    repository = _write_source_repository(tmp_path)
    backend = CopyWorkspaceBackend(tmp_path / "runs")

    workspace = backend.create(repository, "a" * 32)

    assert workspace.path == tmp_path / "runs" / "aaaaaaaa" / "workspace"
    assert workspace.display_id == "aaaaaaaa"
    assert (workspace.path / "app.py").read_text(encoding="utf-8") == "VALUE = 'source'\n"
    assert not (workspace.path / ".git").exists()
    assert not (workspace.path / ".venv").exists()

    (workspace.path / "app.py").write_text("VALUE = 'workspace'\n", encoding="utf-8")
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 'source'\n"
    assert backend.source_is_unchanged(workspace)

    assert workspace.resolve_path(Path("app.py")) == workspace.path / "app.py"
    with pytest.raises(ValueError, match="escapes"):
        workspace.resolve_path(Path("..") / "outside.py")
    with pytest.raises(ValueError, match="relative"):
        workspace.resolve_path((tmp_path / "outside.py").resolve())

    with pytest.raises(ValueError, match="collision"):
        backend.create(repository, "a" * 8 + "b" * 24)

    concurrent_workspace = backend.create(repository, "b" * 32, label="change-app-v1")
    assert concurrent_workspace.path == tmp_path / "runs" / "change-app-v1-bbbbbbbb" / "workspace"


def test_workspace_service_only_accepts_approved_plan_and_writes_run_metadata(tmp_path):
    repository = _write_source_repository(tmp_path)
    backend = CopyWorkspaceBackend(tmp_path / "runs")
    service = WorkspaceService(backend)

    with pytest.raises(ValueError, match="Only approved"):
        service.create_for_plan(_plan_record(repository, status="draft"))

    run, workspace = service.create_for_plan(_plan_record(repository))
    metadata = json.loads((workspace.path.parent / "run.json").read_text(encoding="utf-8"))

    assert run.plan_id
    assert run.display_id == run.id[:8]
    assert workspace.path.parent.name == f"change-app-v1-{run.display_id}"
    assert run.workspace_path == str(workspace.path)
    assert metadata["id"] == run.id
    assert metadata["display_id"] == run.display_id
    assert metadata["source_snapshot"] == workspace.source_snapshot


def test_workspace_create_command_uses_an_approved_saved_plan(tmp_path, capsys):
    repository = _write_source_repository(tmp_path)
    store = PlanStore(tmp_path / "plans")
    record = _plan_record(repository)
    stored = store.save_draft(record.plan, repository, task=record.task, name=record.name)
    store.approve(stored.reference)

    assert main(
        [
            "workspace",
            "create",
            stored.reference,
            "--store-dir",
            str(store.directory),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "Workspace created." in output
    assert stored.reference in output
    assert (tmp_path / "runs").is_dir()
