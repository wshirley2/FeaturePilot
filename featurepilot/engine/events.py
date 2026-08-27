"""Structured runtime events emitted by the FeaturePilot agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


class RuntimeEventType(str, Enum):
    """Stable event names shared by CLI, sessions, and managed reports."""

    TURN_STARTED = "turn_started"
    PROVIDER_STARTED = "provider_started"
    ASSISTANT_TOKEN = "assistant_token"
    TOOL_REQUESTED = "tool_requested"
    EXECUTION_CONTROL_ASSESSED = "execution_control_assessed"
    TOOL_COMPLETED = "tool_completed"
    CONTEXT_COMPRESSED = "context_compressed"
    TURN_COMPLETED = "turn_completed"
    TURN_INTERRUPTED = "turn_interrupted"
    TURN_FAILED = "turn_failed"
    TURN_LIMIT_REACHED = "turn_limit_reached"


@dataclass(frozen=True)
class RuntimeEvent:
    """One immutable, JSON-serializable fact from an Agent turn."""

    event_type: RuntimeEventType
    session_id: str
    turn_id: str
    round_index: int
    payload: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    event_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Reject payloads that a future JSONL store could not persist."""
        json.dumps(self.payload)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable wire representation used by future consumers."""
        result = {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "round_index": self.round_index,
            "tool_call_id": self.tool_call_id,
            "created_at": self.created_at.isoformat(),
            "payload": dict(self.payload),
        }
        return result


class EventHandler(Protocol):
    """Callable consumer for one RuntimeEvent."""

    def __call__(self, event: RuntimeEvent) -> None:
        """Consume one event."""


class EventSink(Protocol):
    """Consumer interface used by Agent without knowing the final UI/store."""

    def emit(self, event: RuntimeEvent) -> None:
        """Consume one event."""


class NullEventSink:
    """Default sink preserving the runtime's no-observer behavior."""

    def emit(self, event: RuntimeEvent) -> None:
        del event


class CallbackEventSink:
    """Small adapter useful for tests and simple in-process consumers."""

    def __init__(self, handler: EventHandler):
        self._handler = handler

    def emit(self, event: RuntimeEvent) -> None:
        self._handler(event)
