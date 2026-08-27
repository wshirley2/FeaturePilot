"""M1-C aggregated Diff and M1-D review report tests."""

from __future__ import annotations

from pathlib import Path

from techpilot.advanced.changes import ChangeService
from techpilot.advanced.reporting import ReportService, RunMetrics
from techpilot.advanced.workspace import Workspace
from techpilot.domain import Plan, PlanRecord, Run, Task
from techpilot.execution import ValidationArtifact, ValidationCommandResult


def _record(source: Path) -> PlanRecord:
    task = Task(project_id=str(source), description="更新 Unicode 文档和产物", id="task-id")
    plan = Plan(
        task_id=task.id,
        summary=task.description,
        steps=["更新文档", "生成新文件"],
        modify_files=["文档.md", "deleted.txt", "asset.bin"],
        expected_files=["new.txt"],
        risks=["需要人工审核最终 Diff。"],
    )
    return PlanRecord(
        plan=plan,
        task=task,
        repository=str(source),
        version=1,
        name="unicode-change",
        status="approved",
    )


def _workspace(source: Path, path: Path) -> Workspace:
    return Workspace(
        run_id="a" * 32,
        source_path=source,
        path=path,
        source_snapshot="source-hash",
    )


def test_change_service_classifies_text_binary_and_out_of_plan_files(tmp_path):
    source = tmp_path / "source"
    workspace_path = tmp_path / "run" / "workspace"
    source.mkdir()
    workspace_path.mkdir(parents=True)
    initial = {
        "文档.md": "旧内容",
        "deleted.txt": "remove me\n",
        "unchanged.txt": "same\n",
    }
    for path, content in initial.items():
        (source / path).write_text(content, encoding="utf-8")
        (workspace_path / path).write_text(content, encoding="utf-8")
    (source / "asset.bin").write_bytes(b"\x00old")
    (workspace_path / "asset.bin").write_bytes(b"\x00old")
    (source / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    service = ChangeService()
    snapshot = service.capture(source)

    (workspace_path / "文档.md").write_text("新内容", encoding="utf-8")
    (workspace_path / "deleted.txt").unlink()
    (workspace_path / "new.txt").write_text("新增\n", encoding="utf-8")
    (workspace_path / "surprise.txt").write_text("outside plan\n", encoding="utf-8")
    (workspace_path / "asset.bin").write_bytes(b"\x00new")
    cache = workspace_path / ".pytest_cache" / "cache.txt"
    cache.parent.mkdir()
    cache.write_text("ignored\n", encoding="utf-8")

    artifact, patch_path = service.generate(snapshot, _workspace(source, workspace_path), _record(source))

    changes = {change.path: change for change in artifact.files}
    assert set(changes) == {"asset.bin", "deleted.txt", "new.txt", "surprise.txt", "文档.md"}
    assert changes["new.txt"].status == "added"
    assert changes["deleted.txt"].status == "deleted"
    assert changes["文档.md"].status == "modified"
    assert changes["asset.bin"].binary
    assert changes["new.txt"].planned
    assert not changes["surprise.txt"].planned
    assert artifact.out_of_plan_files == ["surprise.txt"]
    assert artifact.additions == 3
    assert artifact.deletions == 2

    patch = patch_path.read_text(encoding="utf-8")
    assert "--- a/文档.md" in patch
    assert "+新内容" in patch
    assert "\\ No newline at end of file" in patch
    assert "--- /dev/null" in patch
    assert "+++ b/new.txt" in patch
    assert "+++ /dev/null" in patch
    assert "Binary files a/asset.bin and b/asset.bin differ" in patch
    assert ".pytest_cache" not in patch
    assert "TOKEN=secret" not in patch


def test_change_service_ignores_line_ending_only_rewrites_in_patch(tmp_path):
    source = tmp_path / "source"
    workspace_path = tmp_path / "run" / "workspace"
    source.mkdir()
    workspace_path.mkdir(parents=True)
    source_file = source / "pyproject.toml"
    workspace_file = workspace_path / "pyproject.toml"
    source_file.write_bytes(
        b"[project]\ndescription = 'before'\nrequires-python = '>=3.10'\n"
    )
    workspace_file.write_bytes(
        b"[project]\r\ndescription = 'after'\r\nrequires-python = '>=3.10'\r\n"
    )

    changes, patch_path = ChangeService().generate(
        ChangeService().capture(source),
        _workspace(source, workspace_path),
        _record(source),
    )

    assert len(changes.files) == 1
    assert changes.additions == 1
    assert changes.deletions == 1
    patch = patch_path.read_text(encoding="utf-8")
    assert "-description = 'before'" in patch
    assert "+description = 'after'" in patch
    assert "-requires-python = '>=3.10'" not in patch
    assert "+requires-python = '>=3.10'" not in patch


def test_report_service_summarizes_failures_validation_changes_and_usage(tmp_path):
    source = tmp_path / "source"
    workspace_path = tmp_path / "run" / "workspace"
    source.mkdir()
    workspace_path.mkdir(parents=True)
    (source / "文档.md").write_text("旧\n", encoding="utf-8")
    (workspace_path / "文档.md").write_text("新\n", encoding="utf-8")
    record = _record(source)
    workspace = _workspace(source, workspace_path)
    changes, patch_path = ChangeService().generate(
        ChangeService().capture(source),
        workspace,
        record,
    )
    validation = ValidationArtifact(
        run_id=workspace.run_id,
        status="failed",
        started_at="2026-08-20T00:00:00+00:00",
        completed_at="2026-08-20T00:00:01+00:00",
        commands=[ValidationCommandResult(
            argv=["python", "-m", "pytest", "-q"],
            resolved_argv=["python.exe", "-m", "pytest", "-q"],
            cwd=str(workspace_path),
            status="failed",
            exit_code=1,
            duration_seconds=1.25,
            stdout="one failed\n",
            stderr="",
        )],
    )
    validation_path = workspace_path.parent / "validation.json"
    validation_path.write_text("{}\n", encoding="utf-8")
    events_path = workspace_path.parent / "events.jsonl"
    events_path.write_text("{}\n", encoding="utf-8")
    run = Run(
        id=workspace.run_id,
        task_id=record.plan.task_id,
        plan_id=record.id,
        workspace_path=str(workspace_path),
        status="failed",
        result={"error_type": "ValidationFailed", "error": "tests failed"},
    )

    report_path = ReportService().generate(
        record=record,
        run=run,
        workspace=workspace,
        response="已完成文档更新。",
        validation=validation,
        validation_path=validation_path,
        events_path=events_path,
        changes=changes,
        patch_path=patch_path,
        metrics=RunMetrics(100, 25, 0.0012, 2.5),
    )

    report = report_path.read_text(encoding="utf-8")
    assert "Task: 更新 Unicode 文档和产物" in report
    assert "Status: **failed**" in report
    assert f"Events: `{events_path}`" in report
    assert "已完成文档更新。" in report
    assert "| modified | `文档.md` | yes |" in report
    assert "Overall: **failed**" in report
    assert "python -m pytest -q" in report
    assert "Total tokens: 125" in report
    assert "$0.001200 USD" in report
    assert "ValidationFailed: tests failed" in report
    assert "One or more approved validation commands did not pass." in report
