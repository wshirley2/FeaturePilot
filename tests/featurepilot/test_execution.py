import os
import subprocess
import sys
from pathlib import Path

import pytest

from corecoder.agent import Agent
from corecoder.llm import LLM
from corecoder.tools.base import Tool
from corecoder.tools.glob_tool import GlobTool
from corecoder.tools.grep import GrepTool
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool
from featurepilot.domain import Plan, PlanRecord, Run, Task
from featurepilot.execution import (
    ExecutionContext,
    ToolEffect,
    ToolPolicy,
    ValidationCommandRunner,
    ValidationService,
    WorkspaceToolExecutor,
    build_featurepilot_tools,
)
from featurepilot.workspace import Workspace


def _context(
    tmp_path: Path,
    *,
    modify_files: list[str] | None = None,
    expected_files: list[str] | None = None,
    validation_commands: list[list[str]] | None = None,
) -> ExecutionContext:
    source_path = tmp_path / "source"
    workspace_path = tmp_path / "workspace"
    source_path.mkdir()
    workspace_path.mkdir()
    plan = Plan(
        task_id="task-id",
        summary="Make a controlled change",
        steps=["Read and modify the application"],
        modify_files=modify_files or [],
        expected_files=expected_files or [],
        validation_commands=validation_commands or [],
    )
    task = Task(project_id=str(source_path), description=plan.summary, id=plan.task_id)
    record = PlanRecord(
        plan=plan,
        task=task,
        repository=str(source_path),
        version=1,
        status="approved",
    )
    workspace = Workspace(
        run_id="a" * 32,
        source_path=source_path,
        path=workspace_path,
        source_snapshot="snapshot",
    )
    run = Run(
        id=workspace.run_id,
        task_id=task.id,
        plan_id=plan.id,
        workspace_path=str(workspace_path),
        source_snapshot=workspace.source_snapshot,
    )
    return ExecutionContext(record=record, run=run, workspace=workspace)


def test_policy_limits_paths_writes_network_and_validation_commands(tmp_path):
    context = _context(
        tmp_path,
        modify_files=["src/app.py"],
        expected_files=["docs/new.md"],
        validation_commands=[["python", "-m", "pytest", "-q"]],
    )
    app_path = context.workspace.path / "src" / "app.py"
    app_path.parent.mkdir()
    app_path.write_text("VALUE = 'old'\n", encoding="utf-8")
    (context.workspace.path / "README.md").write_text("Out of Plan scope\n", encoding="utf-8")
    policy = ToolPolicy()

    read = policy.decide("read_file", {"file_path": "src/app.py"}, context)
    assert read.allowed
    assert Path(read.arguments["file_path"]) == app_path

    escaped = policy.decide("read_file", {"file_path": "../secret.txt"}, context)
    assert not escaped.allowed
    assert escaped.effect is ToolEffect.READ
    assert "outside" in escaped.reason

    allowed_edit = policy.decide("edit_file", {"file_path": "src/app.py"}, context)
    assert allowed_edit.allowed
    denied_edit = policy.decide("edit_file", {"file_path": "README.md"}, context)
    assert not denied_edit.allowed
    assert "modify_files" in denied_edit.reason

    allowed_new_file = policy.decide("write_file", {"file_path": "docs/new.md"}, context)
    assert allowed_new_file.allowed
    new_file = context.workspace.path / "docs" / "new.md"
    new_file.parent.mkdir()
    new_file.write_text("already here\n", encoding="utf-8")
    denied_overwrite = policy.decide("write_file", {"file_path": "docs/new.md"}, context)
    assert not denied_overwrite.allowed
    assert "created" in denied_overwrite.reason

    assert not policy.decide("fetch_url", {"url": "https://example.com"}, context).allowed
    assert not policy.decide("agent", {"task": "delegate"}, context).allowed
    assert not policy.decide("bash", {"command": "python -m pytest -q && whoami"}, context).allowed

    command = "python -m pytest -q"
    validation = policy.decide("bash", {"command": command}, context)
    assert validation.allowed
    assert validation.validation_command == ("python", "-m", "pytest", "-q")


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("glob", {"pattern": "../*", "path": "."}),
        ("glob", {"pattern": "..\\*", "path": "."}),
        ("glob", {"pattern": "C:\\Windows\\*", "path": "."}),
        ("grep", {"pattern": "secret", "path": ".", "include": "../*.txt"}),
        ("grep", {"pattern": "secret", "path": ".", "include": "..\\*.txt"}),
        ("grep", {"pattern": "secret", "path": ".", "include": "C:\\*.txt"}),
    ],
)
def test_policy_rejects_search_patterns_that_can_escape_workspace(tmp_path, tool_name, arguments):
    decision = ToolPolicy().decide(tool_name, arguments, _context(tmp_path))

    assert not decision.allowed
    assert decision.effect is ToolEffect.READ
    assert "relative" in decision.reason or "traversal" in decision.reason


def test_workspace_search_skips_symlinks_that_resolve_outside_workspace(tmp_path):
    context = _context(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("TOP SECRET\n", encoding="utf-8")
    link = context.workspace.path / "linked-secret.txt"
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"Symlink creation is unavailable in this environment: {error}")

    executor = WorkspaceToolExecutor(context)
    glob_result = executor.execute(GlobTool(), {"pattern": "*.txt", "path": "."})
    grep_result = executor.execute(GrepTool(), {"pattern": "TOP SECRET", "path": ".", "include": "*.txt"})

    assert str(link) not in glob_result
    assert "TOP SECRET" not in grep_result
    assert "No matches found." in grep_result


def test_executor_runs_allowed_file_operations_only_inside_workspace(tmp_path):
    context = _context(tmp_path, modify_files=["src/app.py"], expected_files=["docs/new.md"])
    app_path = context.workspace.path / "src" / "app.py"
    app_path.parent.mkdir()
    app_path.write_text("VALUE = 'old'\n", encoding="utf-8")
    executor = WorkspaceToolExecutor(context)

    read_result = executor.execute(ReadFileTool(), {"file_path": "src/app.py"})
    assert "VALUE = 'old'" in read_result

    write_result = executor.execute(WriteFileTool(), {"file_path": "docs/new.md", "content": "created\n"})
    assert "Wrote" in write_result
    assert (context.workspace.path / "docs" / "new.md").read_text(encoding="utf-8") == "created\n"

    denied = executor.execute(WriteFileTool(), {"file_path": "outside.txt", "content": "no\n"})
    assert denied == "Policy denied write_file: Path is not in the approved Plan write scope"
    assert not (context.workspace.path / "outside.txt").exists()


def test_corecoder_agent_uses_featurepilot_executor_for_a_workspace_write(tmp_path):
    context = _context(tmp_path, expected_files=["docs/new.md"])
    executor = WorkspaceToolExecutor(context)
    agent = Agent(llm=LLM.__new__(LLM), tools=[WriteFileTool()], tool_executor=executor)

    class _ToolCall:
        name = "write_file"
        id = "write-1"
        arguments = {"file_path": "docs/new.md", "content": "created by the Agent\n"}

    result = agent._exec_tool(_ToolCall())

    assert "Wrote" in result
    assert (context.workspace.path / "docs" / "new.md").read_text(encoding="utf-8") == "created by the Agent\n"


def test_executor_uses_structured_validation_runner_instead_of_bash_tool(tmp_path):
    command = [sys.executable, "-c", "print('validation passed')"]
    context = _context(tmp_path, validation_commands=[command])
    executor = WorkspaceToolExecutor(context)

    class _NeverRunBash(Tool):
        name = "bash"
        description = "Test stand-in"
        parameters = {"type": "object", "properties": {}, "required": []}

        def execute(self, command: str, timeout: int = 120) -> str:
            raise AssertionError("The general BashTool must not run in a FeaturePilot Workspace")

    result = executor.execute(_NeverRunBash(), {"command": subprocess.list2cmdline(command)})

    assert result == "validation passed"
    denied = executor.execute(_NeverRunBash(), {"command": "echo arbitrary command"})
    assert denied.startswith("Policy denied bash:")


def test_validation_command_matching_preserves_arguments_with_spaces(tmp_path):
    command = [sys.executable, "-c", "print('path with spaces')", "folder with spaces"]
    context = _context(tmp_path, validation_commands=[command])
    decision = ToolPolicy().decide("bash", {"command": subprocess.list2cmdline(command)}, context)

    assert decision.allowed
    assert decision.validation_command == tuple(command)


def test_validation_runner_uses_workspace_as_its_working_directory(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "marker.txt").write_text("workspace only\n", encoding="utf-8")
    command = [sys.executable, "-c", "from pathlib import Path; print(Path('marker.txt').read_text().strip())"]

    result = ValidationCommandRunner().run(command, workspace_path)

    assert result == "workspace only"


def test_validation_service_writes_a_passed_artifact_when_no_commands_are_required(tmp_path):
    workspace_path = tmp_path / "run" / "workspace"
    workspace_path.mkdir(parents=True)

    artifact, artifact_path = ValidationService().validate("run-id", workspace_path, [])

    assert artifact.status == "passed"
    assert artifact.commands == []
    assert artifact_path == workspace_path.parent / "validation.json"
    assert artifact_path.is_file()


def test_featurepilot_tool_set_excludes_network_and_delegation():
    names = [tool.name for tool in build_featurepilot_tools()]

    assert names == ["read_file", "glob", "grep", "edit_file", "write_file", "bash", "now"]
    assert "fetch_url" not in names
    assert "agent" not in names
