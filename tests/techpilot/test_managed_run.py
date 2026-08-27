"""M1-A Managed Agent orchestration tests."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from techpilot.advanced.managed import ManagedRunExecutionError, ManagedRunService
from techpilot.advanced.planning import PlanStore
from techpilot.advanced.workspace import CopyWorkspaceBackend, WorkspaceService
from techpilot.cli import main
from techpilot.domain import Plan, PlanRecord, Task
from techpilot.engine.events import NullEventSink
from techpilot.engine.llm import LLMResponse, ToolCall
from techpilot.engine.runtime_control import CancellationToken, RuntimeLimits
from techpilot.execution import ValidationCommandRunner, ValidationService, WorkspaceToolExecutor
from techpilot.runtime import RuntimeBootstrap
from techpilot.runtime.contracts import RuntimeMode, RuntimeResultScope, RuntimeResultStatus


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


def _stored_plan(
    tmp_path: Path,
    repository: Path,
    *,
    status: str = "approved",
    validation_commands: list[list[str]] | None = None,
) -> tuple[PlanStore, PlanRecord]:
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
        validation_commands=(
            [["python", "-c", "print('managed validation passed')"]]
            if validation_commands is None
            else validation_commands
        ),
    )
    store = PlanStore(tmp_path / "plans")
    record = store.save_draft(plan, repository, task=task, name="change-app")
    if status == "approved":
        record = store.approve(record.reference)
    elif status == "rejected":
        record = store.reject(record.reference, "not ready")
    return store, record


def _service(
    tmp_path: Path,
    store: PlanStore,
    provider: FakeProvider,
    *,
    validation_service: ValidationService | None = None,
) -> ManagedRunService:
    return ManagedRunService(
        plan_store=store,
        workspace_service=WorkspaceService(CopyWorkspaceBackend(tmp_path / "runs")),
        runtime_bootstrap=RuntimeBootstrap(provider_factory=lambda config: provider),
        event_sink=NullEventSink(),
        validation_service=validation_service,
    )


def _only_run_metadata(tmp_path: Path) -> tuple[Path, dict]:
    paths = list((tmp_path / "runs").glob("*/run.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_approved_plan_runs_agent_in_isolated_workspace_and_persists_success(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
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
    provider.total_prompt_tokens = 120
    provider.total_completion_tokens = 30
    provider.estimated_cost = 0.0125

    result = _service(tmp_path, store, provider).execute(record.reference)

    assert result.run.status == "succeeded"
    assert result.runtime_result.scope is RuntimeResultScope.RUN
    assert result.runtime_result.status is RuntimeResultStatus.SUCCEEDED
    assert result.runtime.last_result == result.runtime_result
    assert result.run.result["response"] == "Managed change complete."
    assert result.run.result["validation"]["status"] == "passed"
    assert result.validation.status == "passed"
    assert result.validation_path == result.workspace.path.parent / "validation.json"
    assert result.events_path == result.workspace.path.parent / "events.jsonl"
    assert isinstance(result.runtime.agent.tool_executor, WorkspaceToolExecutor)
    assert result.runtime.repository == result.workspace.path.resolve()
    assert result.runtime.runtime_mode is RuntimeMode.MANAGED_RUN
    assert result.runtime.task_id == record.plan.task_id
    assert result.runtime.run_id == result.run.id
    assert result.runtime.identity.source_repository == repository.resolve()
    assert result.runtime.identity.workspace_path == result.workspace.path.resolve()
    assert result.runtime.paths.run_directory == result.workspace.path.parent.resolve()
    assert result.runtime.session_path is not None
    assert result.runtime.session_path.parent == result.workspace.path.parent.resolve() / "sessions"
    assert not (result.workspace.path / ".techpilot").exists()
    assert "Mode: Managed Run" in result.runtime.agent._system
    session = result.runtime.session_store.replay(result.runtime.agent.session_id)
    assert session.mode == "managed_run"
    assert session.task_id == record.plan.task_id
    assert session.run_id == result.run.id
    assert session.source_repository_root == repository.resolve()
    assert (result.workspace.path / "app.py").read_text(encoding="utf-8") == "VALUE = 'managed'\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 'source'\n"
    metadata = json.loads((result.workspace.path.parent / "run.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "succeeded"
    assert metadata["result"]["runtime_result"] == result.runtime_result.to_dict()
    assert metadata["result"]["artifacts"] == {
        "events": str(result.events_path),
        "session": str(result.runtime.session_path),
        "patch": str(result.patch_path),
        "validation": str(result.validation_path),
        "report": str(result.report_path),
    }
    assert metadata["result"]["changes"]["files"][0]["path"] == "app.py"
    assert metadata["result"]["metrics"]["total_tokens"] == 150
    task_prompt = provider.requests[0]["messages"][-1]["content"]
    assert "Change the application value" in task_prompt
    assert "Read and update app.py" in task_prompt
    assert "Approved files to modify:\n- app.py" in task_prompt
    assert "app.py contains the managed value" in task_prompt
    assert "python -c print('managed validation passed')" in task_prompt
    assert "Do not invoke validation commands yourself" in task_prompt
    artifact = json.loads(result.validation_path.read_text(encoding="utf-8"))
    assert artifact["run_id"] == result.run.id
    assert artifact["status"] == "passed"
    assert artifact["commands"][0]["argv"][0] == "python"
    assert artifact["commands"][0]["resolved_argv"][0] == sys.executable
    assert artifact["commands"][0]["cwd"] == str(result.workspace.path.resolve())
    assert artifact["commands"][0]["exit_code"] == 0
    assert result.patch_path == result.workspace.path.parent / "changes.patch"
    assert result.report_path == result.workspace.path.parent / "report.md"
    assert result.events_path.is_file()
    assert result.patch_path.is_file()
    assert result.report_path.is_file()
    assert {path.name for path in result.workspace.path.parent.iterdir()} == {
        "changes.patch",
        "events.jsonl",
        "report.md",
        "run.json",
        "sessions",
        "validation.json",
        "workspace",
    }
    assert [change.path for change in result.changes.files] == ["app.py"]
    assert result.changes.files[0].planned
    patch = result.patch_path.read_text(encoding="utf-8")
    assert "-VALUE = 'source'" in patch
    assert "+VALUE = 'managed'" in patch
    report = result.report_path.read_text(encoding="utf-8")
    assert "# TechPilot Managed Run Report" in report
    assert "Managed change complete." in report
    assert "| modified | `app.py` | yes |" in report
    assert "Prompt tokens: 120" in report
    assert "Completion tokens: 30" in report
    assert "$0.012500 USD" in report
    assert f"Events: `{result.events_path}`" in report
    assert f"Session: `{result.runtime.session_path}`" in report
    events = _events(result.events_path)
    event_types = [event["event_type"] for event in events]
    assert event_types[:2] == ["run_created", "run_started"]
    assert event_types[-4:] == [
        "validation_completed",
        "changes_generated",
        "report_generated",
        "run_finished",
    ]
    assert "tool_requested" in event_types
    assert "tool_completed" in event_types
    assert {event["run_id"] for event in events} == {result.run.id}
    assert events[-1]["payload"]["status"] == "succeeded"
    assert events[-1]["payload"]["runtime_result"] == result.runtime_result.to_dict()
    assert events[0]["payload"]["runtime_paths"] == result.runtime.paths.to_dict()


@pytest.mark.parametrize("status", ["draft", "rejected"])
def test_non_approved_plan_is_rejected_before_workspace_creation(tmp_path, monkeypatch, status):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository, status=status)
    service = _service(tmp_path, store, FakeProvider([]))

    with pytest.raises(ValueError, match="Only approved plans can run"):
        service.execute(record.reference)

    assert not (tmp_path / "runs").exists()


def test_plan_outside_write_is_denied_while_run_can_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
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
    completed = [
        event for event in _events(result.events_path)
        if event["event_type"] == "tool_completed"
    ]
    assert completed[0]["payload"]["result"].startswith("Policy denied write_file")


def test_provider_failure_persists_failed_run_and_retains_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    service = _service(tmp_path, store, FakeProvider([RuntimeError("provider unavailable")]))

    with pytest.raises(ManagedRunExecutionError, match="provider unavailable") as failure:
        service.execute(record.reference)

    assert isinstance(failure.value.cause, RuntimeError)
    metadata_path, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "failed"
    assert metadata["result"]["error_type"] == "RuntimeError"
    assert metadata["result"]["error"] == "provider unavailable"
    assert metadata["result"]["runtime_result"]["status"] == "failed"
    assert metadata["result"]["runtime_result"]["scope"] == "run"
    assert metadata["result"]["artifacts"]["validation"] is None
    events_path = metadata_path.parent / "events.jsonl"
    assert failure.value.events_path == events_path
    assert failure.value.patch_path == metadata_path.parent / "changes.patch"
    assert failure.value.report_path == metadata_path.parent / "report.md"
    report = failure.value.report_path.read_text(encoding="utf-8")
    assert "RuntimeError: provider unavailable" in report
    assert "Status: **failed**" in report
    assert (metadata_path.parent / "workspace" / "app.py").is_file()
    events = _events(events_path)
    assert "turn_failed" in [event["event_type"] for event in events]
    assert events[-1]["event_type"] == "run_finished"
    assert events[-1]["payload"]["status"] == "failed"


def test_keyboard_interrupt_persists_cancelled_run_and_retains_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    service = _service(tmp_path, store, FakeProvider([KeyboardInterrupt()]))

    with pytest.raises(KeyboardInterrupt):
        service.execute(record.reference)

    metadata_path, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "cancelled"
    assert metadata["result"]["error_type"] == "KeyboardInterrupt"
    assert metadata["result"]["runtime_result"]["status"] == "cancelled"
    assert (metadata_path.parent / "workspace" / "app.py").is_file()
    assert (metadata_path.parent / "changes.patch").is_file()
    assert (metadata_path.parent / "report.md").is_file()
    events = _events(metadata_path.parent / "events.jsonl")
    assert "turn_interrupted" in [event["event_type"] for event in events]
    assert events[-1]["event_type"] == "run_finished"
    assert events[-1]["payload"]["status"] == "cancelled"


def test_managed_runtime_limit_skips_validation_and_persists_control_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "write-1",
            "write_file",
            {"file_path": "app.py", "content": "VALUE = 'limited'\n"},
        )]),
        LLMResponse(content="must not be requested"),
    ])

    result = _service(tmp_path, store, provider).execute(
        record.reference,
        limits=RuntimeLimits(max_provider_calls=1),
    )

    assert result.run.status == "limit_reached"
    assert result.validation is None
    assert result.validation_path is None
    assert result.runtime_result.scope is RuntimeResultScope.RUN
    assert result.runtime_result.status is RuntimeResultStatus.LIMIT_REACHED
    assert result.runtime_result.limit == "provider_calls"
    assert result.runtime_result.actual == 1
    assert result.runtime_result.maximum == 1
    assert len(provider.requests) == 1
    assert (result.workspace.path / "app.py").read_text(encoding="utf-8") == "VALUE = 'limited'\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 'source'\n"
    metadata = json.loads((result.workspace.path.parent / "run.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "limit_reached"
    assert metadata["result"]["runtime_result"] == result.runtime_result.to_dict()
    assert metadata["result"]["artifacts"]["validation"] is None
    events = _events(result.events_path)
    assert "turn_limit_reached" in [event["event_type"] for event in events]
    assert "validation_started" not in [event["event_type"] for event in events]
    run_started = next(event for event in events if event["event_type"] == "run_started")
    assert run_started["payload"]["runtime_limits"] == {"max_provider_calls": 1}
    assert events[-1]["payload"]["status"] == "limit_reached"


def test_pre_cancelled_managed_runtime_skips_provider_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(tmp_path, repository)
    provider = FakeProvider([LLMResponse(content="must not run")])
    token = CancellationToken()
    token.cancel("cancel before managed provider")

    result = _service(tmp_path, store, provider).execute(
        record.reference,
        cancellation_token=token,
    )

    assert result.run.status == "cancelled"
    assert result.validation is None
    assert result.runtime_result.status is RuntimeResultStatus.CANCELLED
    assert result.runtime_result.reason == "cancel before managed provider"
    assert provider.requests == []
    events = _events(result.events_path)
    assert "turn_interrupted" in [event["event_type"] for event in events]
    assert "validation_started" not in [event["event_type"] for event in events]
    assert events[-1]["payload"]["status"] == "cancelled"


def test_cancellation_during_validation_stops_command_and_persists_cancelled_run(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    commands = [
        [
            "python",
            "-c",
            (
                "import time; from pathlib import Path; "
                "Path('validation-started.txt').write_text('yes'); time.sleep(10)"
            ),
        ],
        ["python", "-c", "from pathlib import Path; Path('second-validation.txt').touch()"],
    ]
    store, record = _stored_plan(tmp_path, repository, validation_commands=commands)
    provider = FakeProvider([LLMResponse(content="Agent work completed.")])
    token = CancellationToken()

    def cancel_after_validation_starts() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if list((tmp_path / "runs").glob("*/workspace/validation-started.txt")):
                token.cancel("cancel during validation")
                return
            time.sleep(0.01)

    cancellation_thread = threading.Thread(target=cancel_after_validation_starts)
    cancellation_thread.start()
    result = _service(tmp_path, store, provider).execute(
        record.reference,
        cancellation_token=token,
    )
    cancellation_thread.join(timeout=5)

    assert result.run.status == "cancelled"
    assert result.runtime_result.status is RuntimeResultStatus.CANCELLED
    assert result.runtime_result.reason == "cancel during validation"
    assert result.validation is not None
    assert result.validation.status == "cancelled"
    assert [command.status for command in result.validation.commands] == ["cancelled"]
    assert result.validation_path is not None and result.validation_path.is_file()
    assert not (result.workspace.path / "second-validation.txt").exists()
    assert result.patch_path.is_file()
    assert result.report_path.is_file()
    metadata = json.loads(result.runtime.paths.run_metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "cancelled"
    assert metadata["result"]["validation"]["status"] == "cancelled"
    assert metadata["result"]["artifacts"]["session"] == str(result.runtime.session_path)
    events = _events(result.events_path)
    validation_event = next(event for event in events if event["event_type"] == "validation_completed")
    assert validation_event["payload"]["status"] == "cancelled"
    assert events[-1]["event_type"] == "run_finished"
    assert events[-1]["payload"]["status"] == "cancelled"


def test_run_cli_executes_approved_plan_with_the_shared_bootstrap(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
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
    monkeypatch.setattr("techpilot.cli.RuntimeBootstrap", lambda: bootstrap)

    exit_code = main([
        "run",
        record.reference,
        "--store-dir",
        str(store.directory),
        "--runs-dir",
        str(tmp_path / "runs"),
        "--max-provider-calls",
        "3",
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Managed Run succeeded." in output
    assert "Validation: passed" in output
    assert "Patch:" in output
    assert "Report:" in output
    assert "Events:" in output
    metadata_path, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "succeeded"
    events = _events(metadata_path.parent / "events.jsonl")
    run_started = next(event for event in events if event["event_type"] == "run_started")
    assert run_started["payload"]["runtime_limits"] == {"max_provider_calls": 3}
    assert (metadata_path.parent / "workspace" / "app.py").read_text(encoding="utf-8") == "VALUE = 'from-cli'\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "VALUE = 'source'\n"


def test_run_cli_returns_failure_when_system_validation_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    store, record = _stored_plan(
        tmp_path,
        repository,
        validation_commands=[["python", "-c", "import sys; sys.exit(7)"]],
    )
    provider = FakeProvider([LLMResponse(content="Agent work completed.")])
    bootstrap = RuntimeBootstrap(provider_factory=lambda config: provider)
    monkeypatch.setattr("techpilot.cli.RuntimeBootstrap", lambda: bootstrap)

    exit_code = main([
        "run",
        record.reference,
        "--store-dir",
        str(store.directory),
        "--runs-dir",
        str(tmp_path / "runs"),
    ])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "Validation: failed" in output
    assert "Managed Run failed validation" in output
    metadata_path, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "failed"
    artifact = json.loads((metadata_path.parent / "validation.json").read_text(encoding="utf-8"))
    assert artifact["commands"][0]["exit_code"] == 7
    events = _events(metadata_path.parent / "events.jsonl")
    assert events[-1]["payload"]["status"] == "failed"


def test_validation_failure_marks_run_failed_and_preserves_all_command_results(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    commands = [
        ["python", "-c", "import sys; print('bad'); print('details', file=sys.stderr); sys.exit(3)"],
        ["python", "-c", "from pathlib import Path; Path('second-ran.txt').write_text('yes')"],
    ]
    store, record = _stored_plan(tmp_path, repository, validation_commands=commands)
    provider = FakeProvider([LLMResponse(content="Implementation complete.")])

    result = _service(tmp_path, store, provider).execute(record.reference)

    assert result.run.status == "failed"
    assert result.runtime_result.status is RuntimeResultStatus.FAILED
    assert result.runtime_result.error_type == "ValidationFailed"
    assert result.runtime.last_result == result.runtime_result
    assert result.run.result["error_type"] == "ValidationFailed"
    assert result.validation.status == "failed"
    assert [command.status for command in result.validation.commands] == ["failed", "passed"]
    assert result.validation.commands[0].exit_code == 3
    assert result.validation.commands[0].stdout == "bad\n"
    assert result.validation.commands[0].stderr == "details\n"
    assert (result.workspace.path / "second-ran.txt").read_text(encoding="utf-8") == "yes"
    assert result.changes.out_of_plan_files == ["second-ran.txt"]
    report = result.report_path.read_text(encoding="utf-8")
    assert "Out-of-plan files changed: second-ran.txt" in report
    _, metadata = _only_run_metadata(tmp_path)
    assert metadata["status"] == "failed"
    events = _events(result.events_path)
    validation_event = next(event for event in events if event["event_type"] == "validation_completed")
    assert validation_event["payload"]["status"] == "failed"
    assert events[-1]["payload"]["status"] == "failed"


def test_validation_timeout_and_startup_error_are_distinct_artifact_results(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_LOAD_DOTENV", "0")
    repository = _repository(tmp_path)
    commands = [
        ["python", "-c", "import time; time.sleep(1)"],
        ["techpilot-command-that-does-not-exist"],
    ]
    store, record = _stored_plan(tmp_path, repository, validation_commands=commands)
    service = _service(
        tmp_path,
        store,
        FakeProvider([LLMResponse(content="Implementation complete.")]),
        validation_service=ValidationService(ValidationCommandRunner(timeout_seconds=0.01)),
    )

    result = service.execute(record.reference)

    assert result.run.status == "failed"
    assert [command.status for command in result.validation.commands] == ["timed_out", "startup_error"]
    assert all(command.exit_code is None for command in result.validation.commands)
    assert "timed out" in result.validation.commands[0].error
    assert result.validation.commands[1].error
