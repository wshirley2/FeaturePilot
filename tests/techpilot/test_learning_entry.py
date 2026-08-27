from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from techpilot.chat.session import ChatSession
from techpilot.chat.tui import TechPilotTui
from techpilot.learning import LearningChoice, LearningCommandController, LearningService, LearningStore, LearningTurn


class RecordingSessionSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def record(self, event_type: str, session_id: str, payload: dict[str, object]) -> None:
        self.records.append((event_type, session_id, payload))


def test_learning_confirmation_creates_one_active_goal_and_a_observable_plan(tmp_path):
    store = LearningStore(tmp_path / "learning")
    service = LearningService(store)
    sink = RecordingSessionSink()

    result = service.confirm(
        service.draft_from_command("Python structured concurrency"),
        baseline_notes="I know asyncio basics.",
        intended_outcome="Explain and use task groups.",
        weekly_minutes=120,
        session_sink=sink,  # type: ignore[arg-type]
        session_id="chat-1",
    )

    assert result.goal.status == "active"
    assert [step.acceptance_criteria for step in result.plan.steps]
    assert LearningService(store).active_goal() == result.goal
    assert sink.records == [
        ("learning_goal_confirmed", "chat-1", {
            "goal_id": result.goal.id,
            "plan_id": result.plan.id,
            "role_id": "developer-learning-coach",
            "skill_name": "developer-learning",
        })
    ]

    with pytest.raises(ValueError, match="active learning goal already exists"):
        service.confirm(
            service.draft_from_command("Rust"),
            baseline_notes=None,
            intended_outcome="Write a command line tool.",
            weekly_minutes=60,
        )


def test_learning_command_starts_from_one_explicit_topic_and_supports_review(tmp_path):
    controller = LearningCommandController(LearningService(LearningStore(tmp_path / "learning")))

    started = controller.start_from_message("I want to learn Python async.")

    assert started is not None
    assert "已创建学习路径：Python async" in started
    assert "当前学习路径：Python async" in controller.handle("")
    assert "查看学习路径：Python async" in controller.handle("review")
    assert "查看学习路径：Python async" in controller.start_from_message("I want to review what I learned before.")
    assert "查看学习路径：Python async" in controller.start_from_message("帮我复习之前学过的内容。")


def test_learning_command_recognizes_chinese_start_intent(tmp_path):
    controller = LearningCommandController(LearningService(LearningStore(tmp_path / "learning")))

    started = controller.start_from_message("我想学习 Python 异步！")

    assert started is not None
    assert "已创建学习路径：Python 异步" in started


def test_chat_routes_explicit_learning_intent_without_calling_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_CONFIG_DIR", str(tmp_path / "config"))
    runtime = SimpleNamespace(
        session_sink=None,
        agent=SimpleNamespace(session_id="chat-1"),
        run_turn=lambda _input: (_ for _ in ()).throw(AssertionError("learning must not call the coding runtime")),
    )
    output = StringIO()
    chat = ChatSession(runtime, console=Console(file=output, force_terminal=False), input_fn=lambda _prompt: (_ for _ in ()).throw(EOFError))

    response = chat.learning.start_from_message("Help me learn Python packaging")

    assert response is not None
    assert "已创建学习路径：Python packaging" in response


def test_tui_routes_learn_command_without_starting_a_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_CONFIG_DIR", str(tmp_path / "config"))
    runtime = SimpleNamespace(
        session_sink=None,
        agent=SimpleNamespace(session_id="tui-1"),
        config=SimpleNamespace(model="fake-model"),
    )
    tui = TechPilotTui()
    tui.bind_runtime(runtime)
    buffer = SimpleNamespace(text="/learn Python packaging")

    assert tui._submit(buffer) is True
    assert not tui._running
    assert "❯ you\n  /learn Python packaging" in tui.transcript_text
    assert "· 学习\n  已创建学习路径：Python packaging" in tui.transcript_text


def test_tui_echoes_natural_language_learning_trigger_before_the_chinese_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_CONFIG_DIR", str(tmp_path / "config"))
    runtime = SimpleNamespace(
        session_sink=None,
        agent=SimpleNamespace(session_id="tui-natural-language"),
        config=SimpleNamespace(model="fake-model"),
    )
    tui = TechPilotTui()
    tui.bind_runtime(runtime)
    buffer = SimpleNamespace(text="我想学 Python 异步")

    assert tui._submit(buffer) is True
    assert not tui._running
    assert "❯ you\n  我想学 Python 异步" in tui.transcript_text
    assert "· 学习\n  已创建学习路径：Python 异步" in tui.transcript_text


def test_tui_learning_choice_uses_highlighted_index_for_keyboard_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHPILOT_CONFIG_DIR", str(tmp_path / "config"))
    runtime = SimpleNamespace(
        session_sink=None,
        agent=SimpleNamespace(session_id="tui-choice", llm=object()),
        config=SimpleNamespace(model="fake-model"),
    )
    tui = TechPilotTui()
    tui.bind_runtime(runtime)

    class ChoiceController:
        def choose(self, selected_runtime, index):
            assert selected_runtime is runtime
            assert index == 1
            return LearningTurn(notice="已切换学习路径。")

    tui.learning_conversation = ChoiceController()
    tui._apply_learning_turn(
        "我想系统学习 Python 异步",
        LearningTurn(choice=LearningChoice("当前有学习路径。", ("先了解", "暂停后开始", "取消"))),
    )

    assert "❯ 1. 先了解" in tui.transcript_text
    tui._move_learning_choice(1)
    assert "❯ 2. 暂停后开始" in tui.transcript_text
    tui._choose_learning_option(1)
    assert "已选择：暂停后开始" in tui.transcript_text
    assert "已切换学习路径。" in tui.transcript_text
