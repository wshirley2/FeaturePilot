"""M1 append-only Managed Run event artifact tests."""

from __future__ import annotations

import json

import pytest

from techpilot.advanced.run_events import ManagedRunEventSink, RunEventLog
from techpilot.engine.events import RuntimeEvent, RuntimeEventType


class RecordingSink:
    last_turn_streamed = True

    def __init__(self) -> None:
        self.events = []

    def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


def test_run_event_log_appends_lifecycle_and_runtime_events(tmp_path):
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    event_log = RunEventLog.create("run-1", run_directory)
    downstream = RecordingSink()
    sink = ManagedRunEventSink(event_log, downstream)
    runtime_event = RuntimeEvent(
        event_type=RuntimeEventType.TOOL_REQUESTED,
        session_id="session-1",
        turn_id="turn-1",
        round_index=2,
        tool_call_id="call-1",
        payload={"tool_name": "read_file", "arguments": {"file_path": "README.md"}},
    )

    event_log.record("run_started", {"workspace": "workspace"})
    sink.emit(runtime_event)
    sink.ensure_persisted()

    records = [json.loads(line) for line in event_log.path.read_text(encoding="utf-8").splitlines()]
    assert [record["event_type"] for record in records] == ["run_started", "tool_requested"]
    assert {record["run_id"] for record in records} == {"run-1"}
    assert records[0]["source"] == "managed_run"
    assert records[1]["source"] == "runtime"
    assert records[1]["tool_call_id"] == "call-1"
    assert downstream.events == [runtime_event]
    assert sink.last_turn_streamed


def test_run_event_log_refuses_to_overwrite_an_existing_trace(tmp_path):
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    first = RunEventLog.create("run-1", run_directory)
    first.record("run_created")

    with pytest.raises(FileExistsError):
        RunEventLog.create("run-1", run_directory)

    records = first.path.read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
