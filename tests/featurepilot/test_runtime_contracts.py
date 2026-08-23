"""Unified Task Runtime identity contract tests."""

from pathlib import Path

import pytest

from featurepilot.runtime_contracts import RuntimeMode, TaskRuntimeIdentity


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
