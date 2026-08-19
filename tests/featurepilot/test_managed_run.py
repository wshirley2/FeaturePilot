"""M1-A Managed Agent orchestration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corecoder.events import NullEventSink
from corecoder.llm import LLMResponse, ToolCall
from featurepilot.cli import main
from featurepilot.domain import Plan, PlanRecord, Task
from featurepilot.execution import WorkspaceToolExecutor
from featurepilot.managed import ManagedRunExecutionError, ManagedRunService
from featurepilot.planning import PlanStore
from featurepilot.runtime import RuntimeBootstrap
from featurepilot.workspace import CopyWorkspaceBackend, WorkspaceService


class FakeProvider:
    model = "fake-managed-coder"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append({"messages": messages, "tools": tools})
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if on_token and response.content:
            on_token(response.content)
        return response


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 'source'\n", encoding="utf-8")
    (repository / "README.md").write_text("source readme\n", encoding="utf-8")
    return repository


def _stored_plan(tmp_path: Path, repository: Path, *, status: str = "approved") -> tuple[PlanStore, PlanRecord]:
    task = Task(
        project_id=str(repository),
        description="Change the application value",
        acceptance_criteria=["app.py contains the managed value"],
        id="task-id",
    )
    plan = Plan(
        task_id=task.id,
        summary=task.description,
        steps=["Read and update app.py"],
        read_files=["app.py", "README.md"],
        modify_files=["app.py"],
        validation_commands=[["python", "-m", "pytest", "-q"]],
    )
    store = PlanStore(tmp_path / "plans")
    record = store.save_draft(plan, repository, task=task, name="change-app")
    if status == "approved":
        record = store.approve(record.reference)
    elif status == "rejected":
        record = store.reject(record.reference, "not ready")
    return store, record


def _service(tmp_path: Path, store: PlanStore, provider: FakeProvider) -> ManagedRunService:
    return ManagedRunService(
        plan_store=store,
        workspace_service=WorkspaceService(CopyWorkspaceBackend(tmp_path / "runs")),
        runtime_bootstrap=RuntimeBootstrap(provider_factory=lambda config: provider),
        event_sink=NullEventSink(),
    )


def _only_run_metadata(tmp_path: Path) -> tuple[Path, dict]:
    paths = list((tmp_path / "runs").glob("*/run.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def test_approved_plan_runs_agent_in_isolated_workspace_and_persists_success(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "write-1",
            "write_file",
            {"file_path": "app.py", "content": "VALUE = 'managed'\n"},
        )]),
        LLMResponse(content="Managed change complete."),
    ])

    result = _service(tmp_path, store, provider).execute(record.reference)

    assert result.run.status == "succeeded"
    assert result.run.result == {"response": "Managed change complete."}
    assert isinstance(result.runtime.agent.tool_executor, WorkspaceToolExecutor)
    assert result.runtime.repository == result.workspace.path.resolve()
    assert (result.workspace.path / "app.py").read_text(encoding="utf-8") == "VALUE = 'managed'\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 'source'\n"
    metadata = json.loads((result.workspace.path.parent / "run.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "succeeded"
    task_prompt = provider.requests[0]["messages"][-1]["content"]
    assert "Change the application value" in task_prompt
    assert "Read and update app.py" in task_prompt
    assert "Approved files to modify:\n- app.py" in task_prompt
    assert "app.py contains the managed value" in task_prompt
    assert "python -m pytest -q" in task_prompt


@pytest.mark.parametrize("status", ["draft", "rejected"])
def test_non_approved_plan_is_rejected_before_workspace_creation(tmp_path, monkeypatch, status):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository, status=status)
    service = _service(tmp_path, store, FakeProvider([]))

    with pytest.raises(ValueError, match="Only approved plans can run"):
        service.execute(record.reference)

    assert not (tmp_path / "runs").exists()


def test_plan_outside_write_is_denied_while_run_can_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "write-1",
            "write_file",
            {"file_path": "outside.txt", "content": "forbidden\n"},
        )]),
        LLMResponse(content="The policy denied the out-of-plan write."),
    ])

    result = _service(tmp_path, store, provider).execute(record.reference)

    assert result.run.status == "succeeded"
    assert not (result.workspace.path / "outside.txt").exists()
    tool_result = provider.requests[1]["messages"][-1]["content"]
    assert tool_result.startswith("Policy denied write_file")


def test_provider_failure_persists_failed_run_and_retains_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    service = _service(tmp_path, store, FakeProvider([RuntimeError("provider unavailable")]))

    with pytest.raises(ManagedRunExecutionError, match="provider unavailable") as failure:
        service.execute(record.reference)

    assert isinstance(failure.value.cause, RuntimeError)
    metadata_path, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "failed"
    assert metadata["result"] == {
        "error_type": "RuntimeError",
        "error": "provider unavailable",
    }
    assert (metadata_path.parent / "workspace" / "app.py").is_file()


def test_keyboard_interrupt_persists_cancelled_run_and_retains_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    service = _service(tmp_path, store, FakeProvider([KeyboardInterrupt()]))

    with pytest.raises(KeyboardInterrupt):
        service.execute(record.reference)

    metadata_path, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "cancelled"
    assert metadata["result"]["error_type"] == "KeyboardInterrupt"
    assert (metadata_path.parent / "workspace" / "app.py").is_file()


def test_run_cli_executes_approved_plan_with_the_shared_bootstrap(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "write-1",
            "write_file",
            {"file_path": "app.py", "content": "VALUE = 'from-cli'\n"},
        )]),
        LLMResponse(content="CLI run complete."),
    ])
    bootstrap = RuntimeBootstrap(provider_factory=lambda config: provider)
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", lambda: bootstrap)

    exit_code = main([
        "run",
        record.reference,
        "--store-dir",
        str(store.directory),
        "--runs-dir",
        str(tmp_path / "runs"),
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Managed Run succeeded." in output
    metadata_path, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "succeeded"
    assert (metadata_path.parent / "workspace" / "app.py").read_text(encoding="utf-8") == "VALUE = 'from-cli'\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 'source'\n"
