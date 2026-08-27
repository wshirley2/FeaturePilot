"""Append-only execution trace for one TechPilot Managed Run."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from techpilot.engine.events import EventSink, RuntimeEvent


class RunEventLog:
    """Persist Managed Run and Agent Runtime facts as one JSON object per line."""

    def __init__(self, run_id: str, path: Path) -> None:
        self.run_id = run_id
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def create(cls, run_id: str, run_directory: Path) -> RunEventLog:
        directory = run_directory.resolve()
        if not directory.is_dir():
            raise ValueError(f"Run directory does not exist: {run_directory}")
        return cls.create_at(run_id, directory / "events.jsonl")

    @classmethod
    def create_at(cls, run_id: str, path: Path) -> RunEventLog:
        """Create the event log at one canonical Managed Run artifact path."""

        path = path.resolve()
        if not path.parent.is_dir():
            raise ValueError(f"Run directory does not exist: {path.parent}")
        if path.name != "events.jsonl":
            raise ValueError("Managed Run events must be stored at <run>/events.jsonl")
        with path.open("x", encoding="utf-8", newline="\n"):
            pass
        return cls(run_id, path)

    def emit(self, event: RuntimeEvent) -> None:
        """Persist one C1 Runtime Event while preserving its stable wire fields."""

        self._append({
            "source": "runtime",
            "run_id": self.run_id,
            **event.to_dict(),
        })

    def record(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Append one TechPilot orchestration fact for this Run."""

        self._append({
            "event_id": uuid4().hex,
            "event_type": event_type,
            "source": "managed_run",
            "run_id": self.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": dict(payload or {}),
        })

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{line}\n")
            stream.flush()
            os.fsync(stream.fileno())


class ManagedRunEventSink:
    """Fan Runtime Events out to the Run log and the configured live observer."""

    def __init__(self, event_log: RunEventLog, downstream: EventSink) -> None:
        self.event_log = event_log
        self.downstream = downstream
        self.event_log_error: Exception | None = None

    @property
    def last_turn_streamed(self) -> bool:
        """Preserve the terminal sink contract used to avoid duplicate answers."""

        return bool(getattr(self.downstream, "last_turn_streamed", False))

    def emit(self, event: RuntimeEvent) -> None:
        try:
            self.event_log.emit(event)
        except Exception as error:
            self.event_log_error = self.event_log_error or error
        try:
            self.downstream.emit(event)
        except Exception:
            pass

    def ensure_persisted(self) -> None:
        """Fail the Managed Run if any Runtime Event could not reach its required artifact."""

        if self.event_log_error is not None:
            raise OSError(f"Could not append Runtime Event to {self.event_log.path}") from self.event_log_error
