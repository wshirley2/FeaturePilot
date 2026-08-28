from __future__ import annotations

from types import SimpleNamespace

from techpilot.engine.llm import LLMResponse
from techpilot.learning import (
    LearningConversationController,
    LearningIntentRouter,
    LearningRoleRuntime,
    LearningRoute,
    LearningService,
    LearningStore,
)


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def record(self, event_type: str, session_id: str, payload: dict[str, object] | None = None) -> None:
        self.records.append((event_type, session_id, payload or {}))


class FakeLlm:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[list[dict]] = []

    def chat(self, messages: list[dict]) -> LLMResponse:
        self.messages.append(messages)
        return LLMResponse(content=self.content)


class FakeRuntime:
    def __init__(self, content: str = '{"intent":"chat","topic":null,"confidence":1}') -> None:
        self.agent = SimpleNamespace(session_id="learning-runtime", llm=FakeLlm(content))
        self.session_sink = RecordingSink()
        self.activated: list[tuple[str, str]] = []
        self.cleared = 0

    def activate_role(self, role_id: str, context: str) -> None:
        self.activated.append((role_id, context))

    def clear_role(self) -> None:
        self.cleared += 1


class FixedRouter:
    def __init__(self, route: LearningRoute) -> None:
        self.value = route

    def route(self, runtime, message: str) -> LearningRoute:
        return self.value


def test_learning_role_runtime_activates_the_allowlisted_skill_without_a_second_runtime(tmp_path):
    service = LearningService(LearningStore(tmp_path / "learning"))
    runtime = FakeRuntime()

    LearningRoleRuntime(service.roles, service.skills).activate(runtime)

    assert runtime.activated[0][0] == "developer-learning-coach"
    assert "# Allowed Skill" in runtime.activated[0][1]
    assert "name: developer-learning" in runtime.activated[0][1]


def test_model_router_returns_only_validated_structured_intent_and_records_no_reasoning(tmp_path):
    service = LearningService(LearningStore(tmp_path / "learning"))
    runtime = FakeRuntime('{"intent":"introduce","topic":"Python async","confidence":0.91}')

    route = LearningIntentRouter(service).route(runtime, "带我简单了解一下 Python 异步")

    assert route == LearningRoute("introduce", "Python async", 0.91)
    assert runtime.session_sink.records == [
        ("learning_intent_routed", "learning-runtime", {
            "intent": "introduce", "topic": "Python async", "confidence": 0.91,
        })
    ]
    assert "developer-learning" in runtime.agent.llm.messages[0][0]["content"]


def test_model_router_fails_closed_to_plain_chat_on_invalid_output(tmp_path):
    service = LearningService(LearningStore(tmp_path / "learning"))
    runtime = FakeRuntime("not json")

    assert LearningIntentRouter(service).route(runtime, "我最近想系统了解异步") == LearningRoute("chat")


def test_model_router_fails_closed_to_plain_chat_on_low_confidence(tmp_path):
    service = LearningService(LearningStore(tmp_path / "learning"))
    runtime = FakeRuntime('{"intent":"start","topic":"Python async","confidence":0.2}')

    assert LearningIntentRouter(service).route(runtime, "我最近想系统了解异步") == LearningRoute("chat")


def test_start_route_exposes_progress_and_first_step_before_model_turn(tmp_path):
    service = LearningService(LearningStore(tmp_path / "learning"))
    runtime = FakeRuntime()
    controller = LearningConversationController(
        service,
        router=FixedRouter(LearningRoute("start", "Python 异步", 0.96)),
    )

    turn = controller.prepare(runtime, "我想学 Python 异步")

    assert turn.user_input == "我想学 Python 异步"
    assert turn.should_run_model
    assert turn.stage == "正在准备第 1 步…"
    assert turn.notice is not None
    assert "路径已创建" in turn.notice
    assert "学习范围" in turn.notice
    assert "Skill 已准备好" in turn.notice
    assert "当前第 1 步：梳理 Python 异步" in runtime.activated[0][1]


def test_starting_a_different_path_requires_a_choice_and_pause_then_start_is_safe(tmp_path):
    service = LearningService(LearningStore(tmp_path / "learning"))
    active = service.confirm(service.draft_from_command("Agent"), baseline_notes=None).goal
    runtime = FakeRuntime()
    controller = LearningConversationController(
        service,
        router=FixedRouter(LearningRoute("start", "Python 异步", 0.96)),
    )

    pending = controller.prepare(runtime, "我想系统学习 Python 异步")

    assert pending.choice is not None
    assert "暂停 Agent" in pending.choice.options[1]
    resolved = controller.choose(runtime, 1)

    assert resolved.user_input == "我想系统学习 Python 异步"
    assert service.store.load_goal(active.id).status == "paused"
    assert service.active_goal() is not None
    assert service.active_goal().topic == "Python 异步"


def test_cancelled_goal_is_reviewable_but_never_resumed_by_continue(tmp_path):
    service = LearningService(LearningStore(tmp_path / "learning"))
    goal = service.confirm(service.draft_from_command("Agent"), baseline_notes=None).goal
    service.cancel_active_goal()

    assert service.review_goal() == service.store.load_goal(goal.id)
    assert service.continue_goal() is None
