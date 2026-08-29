"""Negative controls proving that core-v0 detects representative Runtime regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from techpilot.engine.agent import Agent
from techpilot.engine.context import ContextManager
from techpilot.engine.tool_execution import ToolExecutionPlan, ToolExecutionWave
from techpilot.evaluation import ReplayCategory, ReplayRunner, build_core_v0_cases
from techpilot.runtime.extensions import RoleRegistry
from techpilot.runtime.sessions import SessionStore


def _category_cases(category: ReplayCategory):
    return tuple(case for case in build_core_v0_cases() if case.category is category)


def _assert_detected(runner: ReplayRunner, category: ReplayCategory) -> None:
    report = runner.run(_category_cases(category))
    assert report.passed < report.total


def test_core_detects_c5_write_barrier_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsafe_build(cls, descriptions):
        return cls(tuple(descriptions), (ToolExecutionWave(tuple(range(len(descriptions))), concurrent=True),))

    monkeypatch.setattr(ToolExecutionPlan, "build", classmethod(unsafe_build))
    _assert_detected(ReplayRunner(Path(__file__).parents[2]), ReplayCategory.SCHEDULING)


def test_core_detects_tool_snip_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ContextManager, "_snip_tool_outputs", staticmethod(lambda messages: False))
    _assert_detected(ReplayRunner(Path(__file__).parents[2]), ReplayCategory.CONTEXT)


def test_core_detects_summary_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ContextManager, "_summarize_old", lambda self, messages, llm, keep_recent=8: False)
    report = ReplayRunner(Path(__file__).parents[2]).run(
        tuple(case for case in _category_cases(ReplayCategory.CONTEXT) if case.scenario == "context-summary")
    )
    assert report.passed < report.total


def test_core_detects_hard_collapse_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ContextManager, "_hard_collapse", lambda self, messages, llm: None)
    report = ReplayRunner(Path(__file__).parents[2]).run(
        tuple(case for case in _category_cases(ReplayCategory.CONTEXT) if case.scenario == "context-collapse")
    )
    assert report.passed < report.total


def test_core_detects_session_projection_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    original_replay = SessionStore.replay

    def without_model_projection(self, session_id):
        projection = original_replay(self, session_id)
        projection.model_messages = []
        return projection

    monkeypatch.setattr(SessionStore, "replay", without_model_projection)
    _assert_detected(ReplayRunner(Path(__file__).parents[2]), ReplayCategory.PERSISTENCE)


def test_core_detects_disabled_role_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RoleRegistry, "disable", lambda self, role_id: None)
    report = ReplayRunner(Path(__file__).parents[2]).run(
        tuple(
            case
            for case in _category_cases(ReplayCategory.CONTRACT)
            if case.input["outcome"] == "disabled"
        )
    )
    assert report.passed < report.total


def test_core_detects_missing_tool_request_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Agent, "_emit_tool_requested", lambda self, *args, **kwargs: None)
    _assert_detected(ReplayRunner(Path(__file__).parents[2]), ReplayCategory.TOOL)


def test_core_detects_instruction_history_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Agent, "_full_messages", lambda self: [{"role": "system", "content": self._system}, self.messages[-1]])
    _assert_detected(ReplayRunner(Path(__file__).parents[2]), ReplayCategory.INSTRUCTION)
