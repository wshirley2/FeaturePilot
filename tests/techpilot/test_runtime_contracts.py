"""Unified Task Runtime identity contract tests."""

from pathlib import Path

import pytest

from techpilot.runtime.contracts import (
    RuntimeMode,
    RuntimeResultScope,
    RuntimeResultStatus,
    TaskRuntimeIdentity,
    TaskRuntimePaths,
    TaskRuntimeResult,
)


def test_chat_runtime_identity_normalizes_paths_and_has_no_workspace(tmp_path):
    identity = TaskRuntimeIdentity(
        mode=RuntimeMode.CHAT,
        session_id=" session-1 ",
        working_directory=tmp_path,
        source_repository=tmp_path,
    )

    assert identity.session_id == "session-1"
    assert identity.working_directory == tmp_path.resolve()
    assert identity.source_repository == tmp_path.resolve()
    assert identity.workspace_path is None
    assert identity.to_dict() == {
        "mode": "chat",
        "session_id": "session-1",
        "task_id": None,
        "run_id": None,
        "source_repository": str(tmp_path.resolve()),
        "working_directory": str(tmp_path.resolve()),
        "workspace_path": None,
    }


def test_managed_runtime_identity_correlates_task_run_and_workspace(tmp_path):
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()

    identity = TaskRuntimeIdentity(
        mode=RuntimeMode.MANAGED_RUN,
        session_id="session-2",
        task_id="task-1",
        run_id="run-1",
        source_repository=source,
        working_directory=workspace,
    )

    assert identity.mode.display_name == "Managed Run"
    assert identity.workspace_path == workspace.resolve()


def test_chat_runtime_paths_keep_sessions_under_repository_by_default(tmp_path):
    paths = TaskRuntimePaths.for_runtime(RuntimeMode.CHAT, tmp_path)

    assert paths.working_directory == tmp_path.resolve()
    assert paths.session_directory == tmp_path.resolve() / ".techpilot" / "sessions"
    assert paths.run_directory is None
    with pytest.raises(ValueError, match="does not have Managed Run artifacts"):
        _ = paths.events_path


def test_managed_runtime_paths_keep_sessions_and_artifacts_outside_workspace(tmp_path):
    workspace = tmp_path / "run" / "workspace"
    workspace.mkdir(parents=True)

    paths = TaskRuntimePaths.for_runtime(RuntimeMode.MANAGED_RUN, workspace)

    assert paths.run_directory == workspace.parent.resolve()
    assert paths.session_directory == workspace.parent.resolve() / "sessions"
    assert paths.run_metadata_path == workspace.parent.resolve() / "run.json"
    assert paths.events_path == workspace.parent.resolve() / "events.jsonl"
    assert paths.validation_path == workspace.parent.resolve() / "validation.json"
    assert paths.patch_path == workspace.parent.resolve() / "changes.patch"
    assert paths.report_path == workspace.parent.resolve() / "report.md"


@pytest.mark.parametrize(
    ("task_id", "run_id"),
    [(None, None), ("task-1", None), (None, "run-1")],
)
def test_managed_runtime_identity_rejects_incomplete_correlation(tmp_path, task_id, run_id):
    with pytest.raises(ValueError):
        TaskRuntimeIdentity(
            mode=RuntimeMode.MANAGED_RUN,
            session_id="session-3",
            task_id=task_id,
            run_id=run_id,
            source_repository=Path(tmp_path),
            working_directory=Path(tmp_path),
        )


@pytest.mark.parametrize(
    ("event_type", "payload", "status"),
    [
        ("turn_completed", {"content": "done"}, RuntimeResultStatus.SUCCEEDED),
        ("turn_interrupted", {"reason": "cancelled"}, RuntimeResultStatus.CANCELLED),
        (
            "turn_failed",
            {"error_type": "RuntimeError", "error": "provider failed"},
            RuntimeResultStatus.FAILED,
        ),
        (
            "turn_limit_reached",
            {"limit": "provider_calls", "actual": 1, "maximum": 1},
            RuntimeResultStatus.LIMIT_REACHED,
        ),
    ],
)
def test_terminal_events_project_to_unified_turn_results(event_type, payload, status):
    result = TaskRuntimeResult.from_terminal_event(event_type, payload)

    assert result is not None
    assert result.scope is RuntimeResultScope.TURN
    assert result.status is status
    assert TaskRuntimeResult.from_dict(result.to_dict()) == result


def test_limit_result_requires_explicit_limit_facts():
    with pytest.raises(ValueError, match="requires a limit name"):
        TaskRuntimeResult(
            scope=RuntimeResultScope.TURN,
            status=RuntimeResultStatus.LIMIT_REACHED,
            reason="stopped",
        )


def test_escalation_required_is_a_non_failure_runtime_result():
    result = TaskRuntimeResult(
        scope=RuntimeResultScope.TURN,
        status=RuntimeResultStatus.ESCALATION_REQUIRED,
        response="需要隔离执行",
        reason="源仓库未执行",
    )

    assert TaskRuntimeResult.from_dict(result.to_dict()) == result
