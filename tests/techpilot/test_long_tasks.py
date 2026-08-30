"""Deterministic RS-6.1 safety tests for long Task persistence and recovery."""

from __future__ import annotations

import pytest

from techpilot.runtime.long_tasks import (
    EffectDisposition,
    LongTaskBudget,
    LongTaskEvent,
    LongTaskStateError,
    LongTaskStatus,
    LongTaskStore,
)
from techpilot.runtime.sessions import SessionEvent, SessionStore


def _create(store: LongTaskStore, root, *, budget: LongTaskBudget | None = None):
    return store.create(
        "repair-task",
        goal="Repair the deterministic fixture and validate it.",
        repository_root=root,
        session_id="chat-session",
        budget=budget,
    )


def test_checkpoint_indexes_task_facts_without_overwriting_raw_session_facts(tmp_path):
    sessions = SessionStore(tmp_path / ".techpilot" / "sessions")
    sessions.create("chat-session", repository_root=tmp_path, model="fake-model")
    sessions.append(SessionEvent("user_fact", "chat-session", {"marker": "raw-session-fact"}))
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="inspect", kind="agent_turn")

    checkpoint = store.checkpoint(
        "repair-task",
        checkpoint_id="before-inspect",
        message_projection=[{"role": "system", "content": "[summary] raw-session-fact"}],
        recovery_reason="before deterministic inspection",
    )

    projection = store.replay("repair-task")
    assert checkpoint.event_cursor == projection.events[-2].event_id
    assert checkpoint.pending_action_ids == ("inspect",)
    assert sessions.replay("chat-session").events[-1].payload == {"marker": "raw-session-fact"}
    assert store.path_for("repair-task") != sessions.path_for("chat-session")


def test_resume_skips_a_durably_completed_effect_without_calling_it_again(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="write-fix", kind="tool_effect", effect_id="write-fix-v1")
    assert store.effect_disposition("repair-task", "write-fix") is EffectDisposition.EXECUTE
    store.start_effect("repair-task", "write-fix")
    store.complete_effect("repair-task", "write-fix", result="patch written")
    store.checkpoint("repair-task", message_projection=[], recovery_reason="after patch")
    store.pause("repair-task", reason="simulated restart")

    recovered = LongTaskStore.for_repository(tmp_path).resume("repair-task", reason="restart recovered")

    assert recovered.status is LongTaskStatus.RUNNING
    assert recovered.completed_effect_ids == ("write-fix-v1",)
    assert store.effect_disposition("repair-task", "write-fix") is EffectDisposition.SKIP_SUCCEEDED
    with pytest.raises(LongTaskStateError, match="skip_succeeded"):
        store.start_effect("repair-task", "write-fix")


def test_interruption_after_effect_start_requires_reconciliation_never_auto_retries(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="execute-migration", kind="tool_effect", effect_id="migration-v1")
    store.start_effect("repair-task", "execute-migration")
    externally_observed_calls = ["migration-v1"]  # The actual external effect happened before process loss.

    recovered = LongTaskStore.for_repository(tmp_path).resume("repair-task", reason="restart recovered")

    assert recovered.status is LongTaskStatus.RECOVERY_REQUIRED
    assert recovered.ambiguous_effect_ids == ("migration-v1",)
    assert store.effect_disposition("repair-task", "execute-migration") is EffectDisposition.RECONCILE_REQUIRED
    assert externally_observed_calls == ["migration-v1"]
    with pytest.raises(LongTaskStateError, match="reconcile_required"):
        store.start_effect("repair-task", "execute-migration")


def test_restart_recovers_a_running_task_when_no_pause_event_was_durable(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="inspect", kind="agent_turn")

    recovered = LongTaskStore.for_repository(tmp_path).resume("repair-task", reason="process restarted")

    assert recovered.status is LongTaskStatus.RUNNING
    assert recovered.actions["inspect"].status.value == "pending"


def test_completed_agent_turn_unblocks_a_dependent_side_effect(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="inspect", kind="agent_turn")
    store.complete_action("repair-task", "inspect", result="failure located")
    store.plan_action(
        "repair-task",
        action_id="write-fix",
        kind="tool_effect",
        effect_id="write-fix-v1",
        dependencies=("inspect",),
    )

    assert store.effect_disposition("repair-task", "write-fix") is EffectDisposition.EXECUTE


def test_task_success_is_terminal_and_blocks_later_effects(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="inspect", kind="agent_turn")
    store.complete_action("repair-task", "inspect")
    completed = store.succeed("repair-task", result="validated")

    assert completed.status is LongTaskStatus.SUCCEEDED
    with pytest.raises(LongTaskStateError, match="not running"):
        store.plan_action("repair-task", action_id="later", kind="tool_effect", effect_id="later-v1")
    with pytest.raises(LongTaskStateError, match="interrupted Tasks"):
        store.resume("repair-task", reason="must not resume")


def test_cancelled_task_never_starts_a_later_side_effect(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="write-fix", kind="tool_effect", effect_id="write-fix-v1")
    store.cancel("repair-task", reason="user cancelled")

    assert store.effect_disposition("repair-task", "write-fix") is EffectDisposition.BLOCKED_CANCELLED
    with pytest.raises(LongTaskStateError, match="blocked_cancelled"):
        store.start_effect("repair-task", "write-fix")


def test_effect_budget_stops_before_the_next_side_effect_and_survives_replay(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path, budget=LongTaskBudget(max_effects=1))
    store.plan_action("repair-task", action_id="write-fix", kind="tool_effect", effect_id="write-fix-v1")
    store.start_effect("repair-task", "write-fix")
    store.complete_effect("repair-task", "write-fix")
    store.plan_action("repair-task", action_id="run-command", kind="tool_effect", effect_id="test-v1", dependencies=("write-fix",))

    assert store.effect_disposition("repair-task", "run-command") is EffectDisposition.BLOCKED_LIMIT
    recovered = LongTaskStore.for_repository(tmp_path).replay("repair-task")
    assert recovered.status is LongTaskStatus.LIMIT_REACHED
    assert recovered.effects_used == 1


def test_replay_ignores_an_incomplete_trailing_task_event(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    with store.path_for("repair-task").open("ab") as stream:
        stream.write(b'{"schema_version":1')

    projection = LongTaskStore.for_repository(tmp_path).replay("repair-task")

    assert projection.goal.startswith("Repair")
    assert projection.warnings == ["Ignored incomplete trailing long Task event"]


def test_invalid_middle_task_event_fails_closed_and_cannot_resume(tmp_path):
    store = LongTaskStore.for_repository(tmp_path)
    _create(store, tmp_path)
    store.plan_action("repair-task", action_id="write-fix", kind="tool_effect", effect_id="write-fix-v1")
    with store.path_for("repair-task").open("ab") as stream:
        stream.write(b"not-json\n")
    store.append(
        # A later valid event proves the invalid line is in the middle, not a partial tail.
        LongTaskEvent(
            task_id="repair-task",
            event_type="task_paused",
            payload={"reason": "after corruption"},
        )
    )

    recovered = LongTaskStore.for_repository(tmp_path).resume("repair-task", reason="restart recovered")

    assert recovered.status is LongTaskStatus.RECOVERY_REQUIRED
    assert recovered.recovery_blockers == ["event_log_invalid"]
    with pytest.raises(LongTaskStateError, match="not running"):
        store.effect_disposition("repair-task", "write-fix")
