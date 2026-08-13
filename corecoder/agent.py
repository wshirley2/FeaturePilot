"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import inspect
from typing import Any, Protocol
from uuid import uuid4

from .context import ContextManager
from .events import EventSink, NullEventSink, RuntimeEvent, RuntimeEventType
from .llm import LLM
from .prompt import system_prompt
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool


class ToolExecutor(Protocol):
    """Optional interception point for applications that need controlled Tool execution."""

    def execute(self, tool: Tool, arguments: dict[str, Any]) -> str:
        """Execute one validated Tool request and return its text result."""


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        tool_executor: ToolExecutor | None = None,
        event_sink: EventSink | None = None,
        session_id: str | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else ALL_TOOLS
        self._tool_by_name = {t.name: t for t in self.tools}
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self.tool_executor = tool_executor
        self.event_sink = event_sink if event_sink is not None else NullEventSink()
        self.session_id = session_id or uuid4().hex
        self._system = system_prompt(self.tools)

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

    def _full_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(self, user_input: str, on_token=None, on_tool=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        turn_id = uuid4().hex
        round_index = 0
        pending_tool_calls = []
        self._emit_event(
            RuntimeEventType.TURN_STARTED,
            turn_id,
            round_index,
            payload={"user_input": user_input},
        )
        self.messages.append({"role": "user", "content": user_input})
        try:
            if self.context.maybe_compress(self.messages, self.llm):
                self._emit_context_compressed(turn_id, round_index, phase="before_provider")

            for round_index in range(1, self.max_rounds + 1):
                self._emit_event(
                    RuntimeEventType.PROVIDER_STARTED,
                    turn_id,
                    round_index,
                    payload={"message_count": len(self.messages)},
                )

                def handle_token(token: str, event_round: int = round_index) -> None:
                    self._emit_event(
                        RuntimeEventType.ASSISTANT_TOKEN,
                        turn_id,
                        event_round,
                        payload={"token": token},
                    )
                    if on_token:
                        on_token(token)

                resp = self.llm.chat(
                    messages=self._full_messages(),
                    tools=self._tool_schemas(),
                    on_token=handle_token,
                )

                # no tool calls -> LLM is done, return text
                if not resp.tool_calls:
                    self.messages.append(resp.message)
                    self._emit_event(
                        RuntimeEventType.TURN_COMPLETED,
                        turn_id,
                        round_index,
                        payload={
                            "content": resp.content,
                            "prompt_tokens": resp.prompt_tokens,
                            "completion_tokens": resp.completion_tokens,
                        },
                    )
                    return resp.content

                # tool calls -> execute (parallel when multiple, like Claude Code's
                # StreamingToolExecutor which runs independent tools concurrently)
                self.messages.append(resp.message)
                pending_tool_calls = list(resp.tool_calls)

                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    self._emit_tool_requested(tc, turn_id, round_index, on_tool)
                    result = self._exec_tool_with_event(tc, turn_id, round_index)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(
                        resp.tool_calls,
                        on_tool,
                        turn_id=turn_id,
                        round_index=round_index,
                    )
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
                pending_tool_calls = []

                # compress if tool outputs are big
                if self.context.maybe_compress(self.messages, self.llm):
                    self._emit_context_compressed(turn_id, round_index, phase="after_tools")

            result = "(reached maximum tool-call rounds)"
            self._emit_event(
                RuntimeEventType.TURN_LIMIT_REACHED,
                turn_id,
                self.max_rounds,
                payload={"max_rounds": self.max_rounds, "result": result},
            )
            return result
        except KeyboardInterrupt:
            # Ctrl+C mid-execution would leave the assistant tool_calls message
            # without replies, poisoning the next request; backfill it first.
            backfilled = self._answer_pending_tool_calls(pending_tool_calls)
            for tc in backfilled:
                self._emit_event(
                    RuntimeEventType.TOOL_COMPLETED,
                    turn_id,
                    round_index,
                    tool_call_id=tc.id,
                    payload={"tool_name": tc.name, "result": "[interrupted]", "interrupted": True},
                )
            self._emit_event(
                RuntimeEventType.TURN_INTERRUPTED,
                turn_id,
                round_index,
                payload={"pending_tool_call_ids": [tc.id for tc in backfilled]},
            )
            raise
        except Exception as exc:
            self._emit_event(
                RuntimeEventType.TURN_FAILED,
                turn_id,
                round_index,
                payload={"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise

    def _emit_event(
        self,
        event_type: RuntimeEventType,
        turn_id: str,
        round_index: int,
        *,
        payload: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """Emit an observation without allowing consumer failures to stop the Agent."""
        event = RuntimeEvent(
            event_type=event_type,
            session_id=self.session_id,
            turn_id=turn_id,
            round_index=round_index,
            tool_call_id=tool_call_id,
            payload=payload or {},
        )
        try:
            self.event_sink.emit(event)
        except Exception:
            # Runtime observers are diagnostic consumers. Their failures must
            # not change the model/tool transaction they are observing.
            pass

    def _emit_context_compressed(self, turn_id: str, round_index: int, *, phase: str) -> None:
        self._emit_event(
            RuntimeEventType.CONTEXT_COMPRESSED,
            turn_id,
            round_index,
            payload={"phase": phase, "message_count": len(self.messages)},
        )

    def _emit_tool_requested(self, tc, turn_id: str, round_index: int, on_tool=None) -> None:
        self._emit_event(
            RuntimeEventType.TOOL_REQUESTED,
            turn_id,
            round_index,
            tool_call_id=tc.id,
            payload={"tool_name": tc.name, "arguments": dict(tc.arguments)},
        )
        if on_tool:
            on_tool(tc.name, tc.arguments)

    def _exec_tool_with_event(self, tc, turn_id: str, round_index: int) -> str:
        result = self._exec_tool(tc)
        self._emit_event(
            RuntimeEventType.TOOL_COMPLETED,
            turn_id,
            round_index,
            tool_call_id=tc.id,
            payload={"tool_name": tc.name, "result": result, "interrupted": False},
        )
        return result

    def _exec_tool(self, tc) -> str:
        """Execute a single tool call, returning the result string."""
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            return f"Error: unknown tool '{tc.name}'"
        # validate arguments first so a TypeError raised *inside* the tool isn't
        # mislabelled as a bad-arguments error from the caller
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            return f"Error: bad arguments for {tc.name}: {e}"
        try:
            if self.tool_executor is not None:
                return self.tool_executor.execute(tool, dict(tc.arguments))
            return tool.execute(**tc.arguments)
        except Exception as e:
            return f"Error executing {tc.name}: {e}"

    def _exec_tools_parallel(
        self,
        tool_calls,
        on_tool=None,
        *,
        turn_id: str | None = None,
        round_index: int = 0,
    ) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        for tc in tool_calls:
            if turn_id is not None:
                self._emit_tool_requested(tc, turn_id, round_index, on_tool)
            elif on_tool:
                on_tool(tc.name, tc.arguments)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self._exec_tool, tc) for tc in tool_calls]
            results = [future.result() for future in futures]

        # Keep completion events in provider order, matching the Tool Result
        # messages that Agent.chat appends immediately afterwards.
        if turn_id is not None:
            for tc, result in zip(tool_calls, results):
                self._emit_event(
                    RuntimeEventType.TOOL_COMPLETED,
                    turn_id,
                    round_index,
                    tool_call_id=tc.id,
                    payload={"tool_name": tc.name, "result": result, "interrupted": False},
                )
        return results

    def _answer_pending_tool_calls(self, tool_calls):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        backfilled = []
        for tc in tool_calls:
            if tc.id not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[interrupted]",
                })
                backfilled.append(tc)
        return backfilled

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
