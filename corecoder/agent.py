"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import copy
import inspect
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from .context import ContextManager, estimate_tokens
from .events import EventSink, NullEventSink, RuntimeEvent, RuntimeEventType
from .llm import LLM
from .prompt import system_prompt
from .runtime_control import CancellationToken, RuntimeCancelled, RuntimeLimitExceeded, RuntimeLimits
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool


class ToolExecutor(Protocol):
    """Optional interception point for applications that need controlled Tool execution."""

    def execute(self, tool: Tool, arguments: dict[str, Any]) -> str:
        """Execute one validated Tool request and return its text result."""


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Correlation facts an application executor may use before a Tool effect."""

    session_id: str
    turn_id: str
    round_index: int
    event_sink: EventSink

    def emit(self, event_type: RuntimeEventType, *, tool_call_id: str, payload: dict[str, Any]) -> None:
        """Emit an executor fact without allowing an observer failure to alter execution."""

        try:
            self.event_sink.emit(RuntimeEvent(
                event_type=event_type,
                session_id=self.session_id,
                turn_id=self.turn_id,
                round_index=self.round_index,
                tool_call_id=tool_call_id,
                payload=payload,
            ))
        except Exception:
            pass


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
        working_directory: str | None = None,
        assistant_name: str = "CoreCoder",
        system_context: str | None = None,
        limits: RuntimeLimits | None = None,
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
        self.limits = limits or RuntimeLimits()
        self._working_directory = working_directory
        self._assistant_name = assistant_name
        self._system_context = system_context
        self._active_cancellation_token: CancellationToken | None = None
        self._system = self._build_system_prompt()

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

    def _full_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system}] + self.messages

    def _build_system_prompt(self) -> str:
        return system_prompt(
            self.tools,
            working_directory=self._working_directory,
            assistant_name=self._assistant_name,
            additional_context=self._system_context,
        )

    def update_system_context(self, system_context: str | None) -> None:
        """Refresh runtime facts without disturbing the conversation history."""

        self._system_context = system_context
        self._system = self._build_system_prompt()

    def cancel_current_turn(self, reason: str = "cancelled by user") -> bool:
        """Request cooperative cancellation at the next provider/tool boundary."""

        if self._active_cancellation_token is None:
            return False
        self._active_cancellation_token.cancel(reason)
        return True

    def compact_context(self) -> tuple[bool, int, int]:
        """Create a durable projection checkpoint without deleting Session facts."""

        before = estimate_tokens(self.messages)
        compressed = self.context.maybe_compress(self.messages, self.llm)
        after = estimate_tokens(self.messages)
        if compressed:
            self._emit_context_compressed(uuid4().hex, 0, phase="manual")
        return compressed, before, after

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(
        self,
        user_input: str,
        on_token=None,
        on_tool=None,
        cancellation_token: CancellationToken | None = None,
    ) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        turn_id = uuid4().hex
        round_index = 0
        pending_tool_calls = []
        token = cancellation_token or CancellationToken()
        started_at = time.monotonic()
        provider_calls = 0
        tool_rounds = 0
        turn_prompt_tokens = 0
        turn_completion_tokens = 0
        cost_before = self._estimated_cost()
        self._active_cancellation_token = token
        self._begin_tool_turn()
        self._emit_event(
            RuntimeEventType.TURN_STARTED,
            turn_id,
            round_index,
            payload={"user_input": user_input},
        )
        self.messages.append({"role": "user", "content": user_input})
        try:
            self._check_runtime_controls(
                token,
                started_at,
                provider_calls,
                tool_rounds,
                turn_prompt_tokens + turn_completion_tokens,
                cost_before,
            )
            if self.context.maybe_compress(self.messages, self.llm):
                self._emit_context_compressed(turn_id, round_index, phase="before_provider")

            for round_index in range(1, self.max_rounds + 1):
                self._check_runtime_controls(
                    token,
                    started_at,
                    provider_calls,
                    tool_rounds,
                    turn_prompt_tokens + turn_completion_tokens,
                    cost_before,
                )
                self._emit_event(
                    RuntimeEventType.PROVIDER_STARTED,
                    turn_id,
                    round_index,
                    payload={"message_count": len(self.messages)},
                )
                provider_calls += 1

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
                turn_prompt_tokens += resp.prompt_tokens
                turn_completion_tokens += resp.completion_tokens
                # no tool calls -> LLM is done, return text
                if not resp.tool_calls:
                    self._check_runtime_controls(
                        token,
                        started_at,
                        provider_calls,
                        tool_rounds,
                        turn_prompt_tokens + turn_completion_tokens,
                        cost_before,
                        check_provider_limit=False,
                    )
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
                    self._emit_tool_requested(
                        tc,
                        turn_id,
                        round_index,
                        on_tool,
                        assistant_content=resp.content,
                    )
                    self._check_runtime_controls(
                        token,
                        started_at,
                        provider_calls,
                        tool_rounds,
                        turn_prompt_tokens + turn_completion_tokens,
                        cost_before,
                        check_provider_limit=False,
                    )
                    self._check_tool_round_limit(tool_rounds)
                    tool_rounds += 1
                    token.raise_if_cancelled()
                    result = self._exec_tool_with_event(tc, turn_id, round_index)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                    stop_message = self._consume_tool_turn_stop()
                    if stop_message is not None:
                        return self._finish_stopped_turn(turn_id, round_index, stop_message)
                else:
                    for tc in resp.tool_calls:
                        self._emit_tool_requested(
                            tc,
                            turn_id,
                            round_index,
                            on_tool,
                            assistant_content=resp.content,
                        )
                    self._check_runtime_controls(
                        token,
                        started_at,
                        provider_calls,
                        tool_rounds,
                        turn_prompt_tokens + turn_completion_tokens,
                        cost_before,
                        check_provider_limit=False,
                    )
                    self._check_tool_round_limit(tool_rounds)
                    tool_rounds += 1
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(
                        resp.tool_calls,
                        on_tool,
                        turn_id=turn_id,
                        round_index=round_index,
                        assistant_content=resp.content,
                        emit_requested=False,
                    )
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
                    stop_message = self._consume_tool_turn_stop()
                    if stop_message is not None:
                        return self._finish_stopped_turn(turn_id, round_index, stop_message)
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
        except RuntimeCancelled as error:
            backfilled = self._answer_pending_tool_calls(pending_tool_calls)
            self._emit_backfilled_tools(backfilled, turn_id, round_index, "[cancelled]", interrupted=True)
            result = f"(turn cancelled: {error})"
            self._emit_event(
                RuntimeEventType.TURN_INTERRUPTED,
                turn_id,
                round_index,
                payload={
                    "reason": str(error),
                    "pending_tool_call_ids": [tc.id for tc in backfilled],
                },
            )
            return result
        except RuntimeLimitExceeded as error:
            backfilled = self._answer_pending_tool_calls(pending_tool_calls)
            self._emit_backfilled_tools(backfilled, turn_id, round_index, "[limit reached]", interrupted=False)
            result = f"(runtime limit reached: {error.limit})"
            self._emit_event(
                RuntimeEventType.TURN_LIMIT_REACHED,
                turn_id,
                round_index,
                payload={
                    "limit": error.limit,
                    "actual": error.actual,
                    "maximum": error.maximum,
                    "result": result,
                    "pending_tool_call_ids": [tc.id for tc in backfilled],
                },
            )
            return result
        except KeyboardInterrupt:
            # Ctrl+C mid-execution would leave the assistant tool_calls message
            # without replies, poisoning the next request; backfill it first.
            backfilled = self._answer_pending_tool_calls(pending_tool_calls)
            token.cancel("keyboard interrupt")
            self._emit_backfilled_tools(backfilled, turn_id, round_index, "[interrupted]", interrupted=True)
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
        finally:
            self._active_cancellation_token = None

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

    def _begin_tool_turn(self) -> None:
        """Give application executors a chance to reset per-turn control state."""

        begin_turn = getattr(self.tool_executor, "begin_turn", None)
        if begin_turn is not None:
            begin_turn()

    def _consume_tool_turn_stop(self) -> str | None:
        """Read an optional application request to end this turn after a tool reply."""

        consume = getattr(self.tool_executor, "consume_turn_stop_message", None)
        if consume is None:
            return None
        return consume()

    def _finish_stopped_turn(self, turn_id: str, round_index: int, message: str) -> str:
        """Close a valid tool transaction without inviting another tool-retry round."""

        self.messages.append({"role": "assistant", "content": message})
        self._emit_event(
            RuntimeEventType.TURN_COMPLETED,
            turn_id,
            round_index,
            payload={"content": message, "stopped_after_tool": True},
        )
        return message

    def _emit_context_compressed(self, turn_id: str, round_index: int, *, phase: str) -> None:
        self._emit_event(
            RuntimeEventType.CONTEXT_COMPRESSED,
            turn_id,
            round_index,
            payload={
                "phase": phase,
                "message_count": len(self.messages),
                # This is a model-view checkpoint only. Earlier raw events are
                # still retained by the Session store for audit and rebuild.
                "message_projection": copy.deepcopy(self.messages),
            },
        )

    def _emit_tool_requested(
        self,
        tc,
        turn_id: str,
        round_index: int,
        on_tool=None,
        *,
        assistant_content: str = "",
    ) -> None:
        self._emit_event(
            RuntimeEventType.TOOL_REQUESTED,
            turn_id,
            round_index,
            tool_call_id=tc.id,
            payload={
                "tool_name": tc.name,
                "arguments": dict(tc.arguments),
                "assistant_content": assistant_content,
            },
        )
        if on_tool:
            on_tool(tc.name, tc.arguments)

    def _exec_tool_with_event(self, tc, turn_id: str, round_index: int) -> str:
        result = self._exec_tool(tc, turn_id=turn_id, round_index=round_index)
        self._emit_event(
            RuntimeEventType.TOOL_COMPLETED,
            turn_id,
            round_index,
            tool_call_id=tc.id,
            payload={"tool_name": tc.name, "result": result, "interrupted": False},
        )
        return result

    def _exec_tool(self, tc, *, turn_id: str | None = None, round_index: int = 0) -> str:
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
                execute_call = getattr(self.tool_executor, "execute_call", None)
                if execute_call is not None:
                    kwargs: dict[str, Any] = {"tool_call_id": tc.id}
                    if "execution_context" in inspect.signature(execute_call).parameters and turn_id is not None:
                        kwargs["execution_context"] = ToolExecutionContext(
                            session_id=self.session_id,
                            turn_id=turn_id,
                            round_index=round_index,
                            event_sink=self.event_sink,
                        )
                    return execute_call(tool, dict(tc.arguments), **kwargs)
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
        assistant_content: str = "",
        emit_requested: bool = True,
    ) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        for tc in tool_calls:
            if turn_id is not None and emit_requested:
                self._emit_tool_requested(
                    tc,
                    turn_id,
                    round_index,
                    on_tool,
                    assistant_content=assistant_content,
                )
            elif on_tool:
                on_tool(tc.name, tc.arguments)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(self._exec_tool, tc, turn_id=turn_id, round_index=round_index)
                for tc in tool_calls
            ]
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

    def _emit_backfilled_tools(self, tool_calls, turn_id: str, round_index: int, result: str, *, interrupted: bool) -> None:
        for tc in tool_calls:
            self._emit_event(
                RuntimeEventType.TOOL_COMPLETED,
                turn_id,
                round_index,
                tool_call_id=tc.id,
                payload={"tool_name": tc.name, "result": result, "interrupted": interrupted},
            )

    def _check_tool_round_limit(self, tool_rounds: int) -> None:
        maximum = self.limits.max_tool_rounds
        if maximum is not None and tool_rounds >= maximum:
            raise RuntimeLimitExceeded("tool_rounds", tool_rounds, maximum)

    def _check_runtime_controls(
        self,
        token: CancellationToken,
        started_at: float,
        provider_calls: int,
        tool_rounds: int,
        total_tokens: int,
        cost_before: float | None,
        *,
        check_provider_limit: bool = True,
    ) -> None:
        token.raise_if_cancelled()
        elapsed = time.monotonic() - started_at
        if self.limits.max_turn_seconds is not None and elapsed > self.limits.max_turn_seconds:
            raise RuntimeLimitExceeded("turn_seconds", round(elapsed, 3), self.limits.max_turn_seconds)
        if self.limits.max_input_tokens is not None:
            input_tokens = estimate_tokens(self._full_messages())
            if input_tokens > self.limits.max_input_tokens:
                raise RuntimeLimitExceeded("input_tokens", input_tokens, self.limits.max_input_tokens)
        if (
            check_provider_limit
            and self.limits.max_provider_calls is not None
            and provider_calls >= self.limits.max_provider_calls
        ):
            raise RuntimeLimitExceeded("provider_calls", provider_calls, self.limits.max_provider_calls)
        if self.limits.max_tool_rounds is not None and tool_rounds > self.limits.max_tool_rounds:
            raise RuntimeLimitExceeded("tool_rounds", tool_rounds, self.limits.max_tool_rounds)
        if self.limits.max_total_tokens is not None and total_tokens > self.limits.max_total_tokens:
            raise RuntimeLimitExceeded("total_tokens", total_tokens, self.limits.max_total_tokens)
        if self.limits.max_cost_usd is not None:
            current_cost = self._estimated_cost()
            if current_cost is not None:
                used_cost = current_cost - (cost_before or 0.0)
                if used_cost > self.limits.max_cost_usd:
                    raise RuntimeLimitExceeded("cost_usd", round(used_cost, 8), self.limits.max_cost_usd)

    def _estimated_cost(self) -> float | None:
        value = getattr(self.llm, "estimated_cost", None)
        if callable(value):
            value = value()
        return float(value) if isinstance(value, (int, float)) else None

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
