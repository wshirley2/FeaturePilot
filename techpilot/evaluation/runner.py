"""Offline, deterministic runner for the frozen Runtime Replay deck."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from techpilot.engine.agent import Agent
from techpilot.engine.context import ContextManager
from techpilot.engine.events import CallbackEventSink, RuntimeEvent, RuntimeEventType
from techpilot.engine.llm import LLMResponse, ToolCall
from techpilot.engine.tool_execution import ToolConcurrency, ToolEffect, ToolExecutionDescription, ToolExecutionPlan
from techpilot.engine.tools.base import Tool
from techpilot.runtime.extensions import (
    PayloadContract,
    RoleRegistry,
    RoleSkillActivator,
    RoleSpec,
    SkillRegistry,
    ToolAllowlist,
    ToolRequest,
)
from techpilot.runtime.sessions import SessionEventSink, SessionStore

from .contracts import ReplayCase, ReplayOutcome, ReplayReport, ReplayTrack


class ReplayRunner:
    """Run structured cases against the current Runtime implementation."""

    def __init__(self, repository_root: Path | None = None) -> None:
        self.repository_root = (repository_root or Path.cwd()).resolve()
        self._handlers: dict[str, Callable[[ReplayCase, Path], Mapping[str, Any]]] = {
            "agent-tool-turn": self._run_agent_tool_turn,
            "tool-execution-plan": self._run_execution_plan,
            "context-snip": self._run_context_snip,
            "context-summary": self._run_context_summary,
            "context-collapse": self._run_context_collapse,
            "session-projection": self._run_session_projection,
            "role-skill-activation": self._run_role_skill_activation,
            "instruction-carry": self._run_instruction_carry,
        }

    def run(self, cases: Sequence[ReplayCase]) -> ReplayReport:
        """Run one fixed-suite, Runtime-only deck and retain every outcome."""

        selected = tuple(cases)
        if not selected:
            raise ValueError("replay run requires at least one case")
        if any(case.track is not ReplayTrack.RUNTIME for case in selected):
            raise ValueError("ReplayRunner only runs deterministic runtime cases")
        suites = {case.suite for case in selected}
        if len(suites) != 1:
            raise ValueError("a replay run cannot mix suites")
        outcomes: list[ReplayOutcome] = []
        with tempfile.TemporaryDirectory(prefix="techpilot-replay-") as directory:
            root = Path(directory)
            for case in selected:
                outcomes.append(self._run_case(case, root / case.id))
        return ReplayReport(
            suite=selected[0].suite,
            track=ReplayTrack.RUNTIME,
            cases=selected,
            outcomes=tuple(outcomes),
            git_commit=self._git_commit(),
            git_dirty=self._git_dirty(),
        )

    @staticmethod
    def write_report(report: ReplayReport, output: Path) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return output

    @classmethod
    def write_baseline(cls, report: ReplayReport, output: Path) -> Path:
        """Persist a formal baseline only when its source revision is immutable."""

        if report.track is not ReplayTrack.RUNTIME:
            raise ValueError("baseline-v0 only accepts Runtime replay reports")
        if report.suite != "core-v0":
            raise ValueError("baseline-v0 only accepts the frozen core-v0 suite")
        if report.passed != report.total:
            raise ValueError("baseline-v0 requires every core-v0 case to pass")
        if report.git_dirty:
            raise ValueError("baseline-v0 requires a clean Git worktree")
        payload = report.to_dict() | {
            "baseline": {
                "kind": "baseline-v0",
                "frozen_case_set_digest": report.case_set_digest,
            }
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return output

    def _run_case(self, case: ReplayCase, root: Path) -> ReplayOutcome:
        handler = self._handlers.get(case.scenario)
        if handler is None:
            return ReplayOutcome(case.id, case.category, False, f"unknown replay scenario: {case.scenario}")
        try:
            root.mkdir(parents=True, exist_ok=True)
            observed = handler(case, root)
        except (AssertionError, KeyError, TypeError, ValueError) as error:
            return ReplayOutcome(case.id, case.category, False, str(error))
        except Exception as error:  # pragma: no cover - defensive report boundary
            return ReplayOutcome(case.id, case.category, False, f"{type(error).__name__}: {error}")
        return ReplayOutcome(case.id, case.category, True, observed=observed)

    def _run_agent_tool_turn(self, case: ReplayCase, root: Path) -> Mapping[str, Any]:
        mode = _string(case.input, "mode")
        value = _string(case.input, "value")
        provider_response = _string(case.input, "provider_response")
        expected_response = _string(case.expected, "response")
        if mode == "success":
            call = ToolCall("tool-1", "echo", {"value": value})
        elif mode == "bad-arguments":
            call = ToolCall("tool-1", "echo", {"unexpected": value})
        elif mode == "unknown-tool":
            call = ToolCall("tool-1", "missing_echo", {"value": value})
        else:
            raise AssertionError(f"unsupported tool replay mode: {mode}")
        provider = _ReplayProvider([LLMResponse(tool_calls=[call]), LLMResponse(content=provider_response)])
        events: list[RuntimeEvent] = []
        store = SessionStore(root / "sessions")
        store.create("replay", repository_root=root, model="fake-replay")
        sink = SessionEventSink(store, CallbackEventSink(events.append))
        agent = Agent(llm=provider, tools=[_EchoTool()], event_sink=sink, session_id="replay")

        response = agent.chat(f"execute {value}")
        projection = store.replay("replay")
        tool_messages = [message for message in projection.messages if message.get("role") == "tool"]
        _expect(response == expected_response, "unexpected final response")
        _expect(len(tool_messages) == 1, "tool replay must produce one durable tool result")
        result = str(tool_messages[0]["content"])
        if "tool_result" in case.expected:
            _expect(result == _string(case.expected, "tool_result"), "Tool result did not preserve the expected argument")
        else:
            _expect(result.startswith(_string(case.expected, "tool_result_prefix")), "Tool rejection result mismatch")
        event_types = [event.event_type.value for event in events]
        _expect(RuntimeEventType.TOOL_REQUESTED.value in event_types, "missing tool_requested event")
        _expect(RuntimeEventType.TOOL_COMPLETED.value in event_types, "missing tool_completed event")
        _expect(event_types[-1] == RuntimeEventType.TURN_COMPLETED.value, "turn did not complete after tool result")
        return {"response": response, "tool_result": result, "event_types": event_types, "projection_size": len(projection.messages)}

    @staticmethod
    def _run_execution_plan(case: ReplayCase, root: Path) -> Mapping[str, Any]:
        del root
        effects = _string(case.input, "effects")
        descriptions = [_description_for(effect, index) for index, effect in enumerate(effects)]
        plan = ToolExecutionPlan.build(descriptions)
        waves = tuple(wave.indexes for wave in plan.waves)
        expected_waves = tuple(tuple(indexes) for indexes in case.expected["waves"])
        _expect(waves == expected_waves, f"unexpected execution waves: {waves}")
        return {"waves": [list(wave) for wave in waves]}

    @staticmethod
    def _run_context_snip(case: ReplayCase, root: Path) -> Mapping[str, Any]:
        del root
        marker = _string(case.input, "marker")
        line_count = _integer(case.input, "line_count")
        position = _string(case.input, "position")
        lines = [f"noise-{index:02d} {'x' * 240}" for index in range(line_count)]
        lines[0 if position == "head" else -1] = f"marker {marker} {'x' * 240}"
        messages = [{"role": "tool", "content": "\n".join(lines)}]
        compressed = ContextManager(max_tokens=200).maybe_compress(messages, None)
        projection = messages[0]["content"]
        _expect(compressed, "tool output did not enter snip compression")
        _expect(marker in projection, "edge evidence was lost during tool snip")
        return {"projection": projection, "marker": marker}

    @staticmethod
    def _run_context_summary(case: ReplayCase, root: Path) -> Mapping[str, Any]:
        del root
        marker = _string(case.input, "marker")
        filler_size = _integer(case.input, "filler_size")
        messages = _context_messages(marker, filler_size)
        compressed = ContextManager(max_tokens=1200).maybe_compress(messages, None)
        projection = str(messages[0].get("content", ""))
        _expect(compressed, "context did not enter summary compression")
        _expect(projection.startswith(_string(case.expected, "prefix")), "expected summary projection was not created")
        _expect(marker in projection, "summary projection lost its file-evidence marker")
        return {"projection": projection, "message_count": len(messages)}

    @staticmethod
    def _run_context_collapse(case: ReplayCase, root: Path) -> Mapping[str, Any]:
        del root
        marker = _string(case.input, "marker")
        filler_size = _integer(case.input, "filler_size")
        messages = _context_messages(marker, filler_size)
        compressed = ContextManager(max_tokens=900).maybe_compress(messages, None)
        projection = str(messages[0].get("content", ""))
        _expect(compressed, "context did not enter hard-collapse compression")
        _expect(projection.startswith(_string(case.expected, "prefix")), "expected hard-collapse projection was not created")
        _expect(marker in projection, "hard-collapse projection lost its file-evidence marker")
        return {"projection": projection, "message_count": len(messages)}

    @staticmethod
    def _run_session_projection(case: ReplayCase, root: Path) -> Mapping[str, Any]:
        mode = _string(case.input, "mode")
        marker = _string(case.input, "marker")
        store = SessionStore(root / "sessions")
        store.create("replay", repository_root=root, model="fake-replay")
        store.append_runtime(_event(RuntimeEventType.TURN_STARTED, "replay", {"user_input": marker}))
        if mode == "tool":
            store.append_runtime(_event(
                RuntimeEventType.TOOL_REQUESTED,
                "replay",
                {"tool_name": "echo", "arguments": {"value": marker}},
                tool_call_id="tool-1",
            ))
            store.append_runtime(_event(
                RuntimeEventType.TOOL_COMPLETED,
                "replay",
                {"tool_name": "echo", "result": f"echo:{marker}", "interrupted": False},
                tool_call_id="tool-1",
            ))
        elif mode == "compressed":
            store.append_runtime(_event(
                RuntimeEventType.CONTEXT_COMPRESSED,
                "replay",
                {"message_projection": [{"role": "user", "content": f"projection:{marker}"}]},
            ))
        elif mode != "turn":
            raise AssertionError(f"unsupported persistence mode: {mode}")
        store.append_runtime(_event(RuntimeEventType.TURN_COMPLETED, "replay", {"content": f"done:{marker}"}))
        projection = store.replay("replay")
        raw_events = [event.to_dict() for event in projection.events]
        model_text = "\n".join(str(message.get("content", "")) for message in projection.model_messages)
        _expect(any(marker in json.dumps(event, ensure_ascii=False) for event in raw_events), "raw Session facts lost marker")
        expected_marker = f"projection:{marker}" if mode == "compressed" else marker
        _expect(expected_marker in model_text, "Session projection lost expected marker")
        return {"event_count": len(raw_events), "model_messages": projection.model_messages}

    @staticmethod
    def _run_role_skill_activation(case: ReplayCase, root: Path) -> Mapping[str, Any]:
        outcome = _string(case.input, "outcome")
        marker = _string(case.input, "marker")
        role = RoleSpec(
            id="replay-role",
            title="Replay role",
            system_prompt="Handle replay evidence.",
            allowed_skill_ids=("replay-skill",),
            input_contract=PayloadContract(schema_id="replay-input-v1", required_keys=("ticket-id",)),
            output_contract=PayloadContract(schema_id="replay-output-v1", required_keys=("summary",)),
        )
        roles = RoleRegistry((role,))
        skills = SkillRegistry(roles)
        skill_path = root / "skills" / "replay" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: replay-skill\ndescription: Replay evidence workflow.\ncompatible_role_ids: [replay-role]\n---\n",
            encoding="utf-8",
        )
        skills.discover(root / "skills", approved=outcome not in {"unapproved", "unknown-skill"})
        if outcome == "revoked":
            skills.revoke("replay-skill")
        if outcome == "disabled":
            roles.disable("replay-role")
        runtime = _RoleTarget()
        activator = RoleSkillActivator(roles, skills, (ToolAllowlist(role_id="replay-role", tool_names=("read_log",)),))
        try:
            activation = activator.activate(
                runtime,
                role_id="replay-role",
                role_context=marker,
                skill_names=("missing-skill",) if outcome == "unknown-skill" else ("replay-skill",),
                role_input={} if outcome == "input-invalid" else {"ticket-id": marker},
                tool_requests=(ToolRequest(tool_name="write_file"),) if outcome == "tool-overreach" else (),
            )
            if outcome == "output-overreach":
                activator.validate_outputs(activation, role_output={"unexpected": marker})
            _expect(outcome == "active", f"expected {outcome} to fail closed")
            _expect(runtime.activations == [("replay-role", marker, ("read_log",))], "active Role did not use allowlisted tools")
            return {"outcome": "active", "activations": runtime.activations}
        except ValueError as error:
            _expect(outcome != "active", f"active Role unexpectedly failed: {error}")
            if outcome == "output-overreach":
                _expect(len(runtime.activations) == 1, "output validation must follow a valid Role activation")
            else:
                _expect(runtime.activations == [], "failed Role/Skill activation reached Runtime")
            return {"outcome": outcome, "error": str(error)}

    @staticmethod
    def _run_instruction_carry(case: ReplayCase, root: Path) -> Mapping[str, Any]:
        del root
        constraint = _string(case.input, "constraint")
        follow_up = _string(case.input, "follow_up")
        provider_response = _string(case.input, "provider_response")
        response = _string(case.expected, "response")
        provider = _ReplayProvider([LLMResponse(content="constraint stored"), LLMResponse(content=provider_response)])
        agent = Agent(llm=provider, tools=[])
        first = agent.chat(constraint, allow_tools=False)
        second = agent.chat(follow_up, allow_tools=False)
        second_request = provider.requests[1]["messages"]
        visible_context = json.dumps(second_request, ensure_ascii=False)
        _expect(first == "constraint stored", "first instruction response mismatch")
        _expect(second == response, "second instruction response mismatch")
        _expect(_string(case.expected, "constraint") in visible_context, "later turn lost the initial user constraint")
        return {"response": second, "second_request": second_request}

    def _git_commit(self) -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.repository_root, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return "unavailable"

    def _git_dirty(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=self.repository_root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return True
        return bool(result.stdout.strip())


class _ReplayProvider:
    model = "fake-replay"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, on_token=None) -> LLMResponse:
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        response = next(self._responses)
        if on_token is not None and response.content:
            on_token(response.content)
        return response


class _EchoTool(Tool):
    name = "echo"
    description = "Return the supplied value for deterministic replay."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    def execute(self, value: str) -> str:
        return f"echo:{value}"


class _RoleTarget:
    def __init__(self) -> None:
        self.activations: list[tuple[str, str, tuple[str, ...]]] = []

    def activate_role(self, role_id: str, role_context: str, *, tool_names: tuple[str, ...] = ()) -> None:
        self.activations.append((role_id, role_context, tool_names))


def _description_for(effect: str, index: int) -> ToolExecutionDescription:
    resource = (f"resource-{index}",)
    if effect == "r":
        return ToolExecutionDescription(ToolEffect.READ, ToolConcurrency.SAFE, resource)
    if effect == "w":
        return ToolExecutionDescription(ToolEffect.WRITE, ToolConcurrency.EXCLUSIVE, resource)
    if effect == "x":
        return ToolExecutionDescription(ToolEffect.EXECUTE, ToolConcurrency.EXCLUSIVE, resources_known=False)
    if effect == "u":
        return ToolExecutionDescription.unknown()
    raise AssertionError(f"unknown scheduling effect: {effect}")


def _context_messages(marker: str, filler_size: int) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for index in range(12):
        evidence = marker if index == 0 else f"noise-{index}.txt"
        messages.append({"role": "user" if index % 2 == 0 else "assistant", "content": f"{evidence} {'x' * filler_size}"})
    return messages


def _event(
    event_type: RuntimeEventType,
    session_id: str,
    payload: dict[str, Any],
    *,
    tool_call_id: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_type=event_type,
        session_id=session_id,
        turn_id="replay-turn",
        round_index=1,
        tool_call_id=tool_call_id,
        payload=payload,
    )


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise TypeError(f"replay case requires string {key}")
    return item


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int):
        raise TypeError(f"replay case requires integer {key}")
    return item


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
