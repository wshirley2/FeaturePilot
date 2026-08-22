"""C4 event Session storage, replay, recovery, and runtime limit tests."""

from __future__ import annotations

from corecoder.agent import Agent
from corecoder.events import CallbackEventSink, RuntimeEvent, RuntimeEventType
from corecoder.llm import LLMResponse, ToolCall
from corecoder.runtime_control import CancellationToken, RuntimeLimits
from corecoder.tools.base import Tool
from featurepilot.sessions import SessionEvent, SessionEventSink, SessionStore


class EchoTool(Tool):
    name = "echo"
    description = "Return a value."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}

    def execute(self, value: str) -> str:
        return f"echo: {value}"


class FakeLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.estimated_cost = None

    def chat(self, messages, tools=None, on_token=None):
        response = next(self.responses)
        self.total_prompt_tokens += response.prompt_tokens
        self.total_completion_tokens += response.completion_tokens
        if on_token and response.content:
            on_token(response.content)
        return response


def _event(event_type, session_id="session-1", **kwargs):
    return RuntimeEvent(event_type=event_type, session_id=session_id, turn_id="turn-1", round_index=1, **kwargs)


def test_session_replay_rebuilds_valid_tool_history_and_ignores_partial_tail(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.create("session-1", repository_root=tmp_path, model="fake-model")
    store.append_runtime(_event(RuntimeEventType.TURN_STARTED, payload={"user_input": "inspect README"}))
    store.append_runtime(_event(
        RuntimeEventType.TOOL_REQUESTED,
        tool_call_id="read-1",
        payload={"tool_name": "read_file", "arguments": {"file_path": "README.md"}, "assistant_content": ""},
    ))
    store.append_runtime(_event(
        RuntimeEventType.TOOL_COMPLETED,
        tool_call_id="read-1",
        payload={"tool_name": "read_file", "result": "contents", "interrupted": False},
    ))
    store.append_runtime(_event(
        RuntimeEventType.TURN_COMPLETED,
        payload={"content": "README inspected", "prompt_tokens": 3, "completion_tokens": 2},
    ))

    path = store.directory / "session-1.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"schema_version":1')

    projection = store.replay("session-1")

    assert [message["role"] for message in projection.messages] == ["user", "assistant", "tool", "assistant"]
    assert projection.messages[1]["tool_calls"][0]["id"] == "read-1"
    assert projection.messages[2] == {"role": "tool", "tool_call_id": "read-1", "content": "contents"}
    assert projection.messages[-1]["content"] == "README inspected"
    assert projection.total_tokens == 5
    assert projection.warnings == ["Ignored incomplete trailing Session event"]


def test_session_keeps_raw_facts_when_context_projection_is_compressed(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.create("session-2", repository_root=tmp_path, model="fake-model")
    store.append_runtime(_event(RuntimeEventType.TURN_STARTED, "session-2", payload={"user_input": "first"}))
    store.append_runtime(_event(RuntimeEventType.TURN_COMPLETED, "session-2", payload={"content": "first answer"}))
    store.append(SessionEvent(
        event_type=RuntimeEventType.CONTEXT_COMPRESSED.value,
        session_id="session-2",
        payload={"message_projection": [{"role": "user", "content": "[summary] first"}]},
    ))

    projection = store.replay("session-2")

    assert [message["content"] for message in projection.messages] == ["first", "first answer"]
    assert projection.model_messages == [{"role": "user", "content": "[summary] first"}]


def test_session_sink_persists_agent_events_and_limits_leave_recoverable_boundary(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.create("session-3", repository_root=tmp_path, model="fake-model")
    observed = []
    sink = SessionEventSink(store, CallbackEventSink(observed.append))
    agent = Agent(
        llm=FakeLLM([
            LLMResponse(tool_calls=[ToolCall("echo-1", "echo", {"value": "one"})]),
            LLMResponse(content="this provider call must not happen"),
        ]),
        tools=[EchoTool()],
        event_sink=sink,
        session_id="session-3",
        limits=RuntimeLimits(max_provider_calls=1),
    )

    result = agent.chat("run once")
    projection = store.replay("session-3")

    assert result == "(runtime limit reached: provider_calls)"
    assert projection.status == "limit_reached"
    assert projection.messages[-1] == {"role": "tool", "tool_call_id": "echo-1", "content": "echo: one"}
    assert observed[-1].event_type is RuntimeEventType.TURN_LIMIT_REACHED


def test_pre_cancelled_token_records_interruption_without_provider_call():
    events = []
    token = CancellationToken()
    token.cancel("test cancellation")
    agent = Agent(
        llm=FakeLLM([LLMResponse(content="must not run")]),
        tools=[],
        event_sink=CallbackEventSink(events.append),
    )

    assert agent.chat("cancel", cancellation_token=token) == "(turn cancelled: test cancellation)"
    assert [event.event_type for event in events][-1] is RuntimeEventType.TURN_INTERRUPTED


def test_total_token_limit_stops_after_provider_response_with_explicit_reason():
    events = []
    agent = Agent(
        llm=FakeLLM([LLMResponse(content="too expensive", prompt_tokens=2, completion_tokens=2)]),
        tools=[],
        event_sink=CallbackEventSink(events.append),
        limits=RuntimeLimits(max_total_tokens=3),
    )

    assert agent.chat("limit tokens") == "(runtime limit reached: total_tokens)"
    assert events[-1].event_type is RuntimeEventType.TURN_LIMIT_REACHED
    assert events[-1].payload["limit"] == "total_tokens"


def test_tool_round_limit_backfills_requested_calls_for_a_valid_resumable_history(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.create("session-4", repository_root=tmp_path, model="fake-model")
    sink = SessionEventSink(store, CallbackEventSink(lambda event: None))
    agent = Agent(
        llm=FakeLLM([
            LLMResponse(tool_calls=[ToolCall("echo-1", "echo", {"value": "one"})]),
            LLMResponse(tool_calls=[ToolCall("echo-2", "echo", {"value": "two"})]),
        ]),
        tools=[EchoTool()],
        event_sink=sink,
        session_id="session-4",
        limits=RuntimeLimits(max_tool_rounds=1),
    )

    assert agent.chat("run twice") == "(runtime limit reached: tool_rounds)"
    projection = store.replay("session-4")

    assert projection.status == "limit_reached"
    assert projection.messages[-2]["role"] == "assistant"
    assert projection.messages[-2]["tool_calls"][0]["id"] == "echo-2"
    assert projection.messages[-1] == {
        "role": "tool",
        "tool_call_id": "echo-2",
        "content": "[limit reached]",
    }
