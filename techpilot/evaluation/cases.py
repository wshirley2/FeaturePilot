"""The frozen, deterministic ``core-v0`` Runtime Replay case deck.

Each case is generated from a finite, explicit matrix.  The generated cards
have stable IDs and content fingerprints; the report records their combined
digest so a changed matrix cannot silently mutate the historical denominator.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import islice, product

from .contracts import ReplayCase, ReplayCaseOrigin, ReplayCategory, case_set_digest

CORE_V0_SUITE = "core-v0"
CORE_V0_CASE_COUNT = 144
CORE_V0_CASE_SET_DIGEST = "fba626e31135d2de7379a21529b308860220759f174bae2a8a097aa81adaa367"
RUNNER_VALIDATION_SUITE = "runner-validation-v0"
RUNNER_VALIDATION_CASE_COUNT = 24


def build_core_v0_cases() -> tuple[ReplayCase, ...]:
    """Return the stable core-v0 deck of 144 unique Runtime cases."""

    cases = (
        *_tool_cases(),
        *_scheduling_cases(),
        *_context_cases(),
        *_persistence_cases(),
        *_contract_cases(),
        *_instruction_cases(),
    )
    ids = [case.id for case in cases]
    if len(cases) != CORE_V0_CASE_COUNT or len(ids) != len(set(ids)):
        raise RuntimeError("core-v0 replay deck must contain 144 unique cases")
    if case_set_digest(cases) != CORE_V0_CASE_SET_DIGEST:
        raise RuntimeError("core-v0 case deck changed; create a new version instead of rewriting the frozen baseline")
    return cases


def build_runner_validation_cases() -> tuple[ReplayCase, ...]:
    """Return a small, independent-name deck that verifies every runner path."""

    core_cases = build_core_v0_cases()
    selected: list[ReplayCase] = []
    per_category = {
        ReplayCategory.TOOL: 4,
        ReplayCategory.SCHEDULING: 4,
        ReplayCategory.CONTEXT: 4,
        ReplayCategory.PERSISTENCE: 3,
        ReplayCategory.CONTRACT: 5,
        ReplayCategory.INSTRUCTION: 4,
    }
    for category, count in per_category.items():
        selected.extend(islice((case for case in core_cases if case.category is category), count))
    cases = tuple(
        replace(
            case,
            id=f"runner-{case.id}",
            suite=RUNNER_VALIDATION_SUITE,
            origin=ReplayCaseOrigin.RUNNER_VALIDATION,
        )
        for case in selected
    )
    if len(cases) != RUNNER_VALIDATION_CASE_COUNT:
        raise RuntimeError("runner validation deck must contain 24 cases")
    return cases


def _tool_cases() -> tuple[ReplayCase, ...]:
    cases: list[ReplayCase] = []
    for index in range(8):
        value = f"evidence-{index:02d}-路径.py"
        cases.append(_case(
            f"tool-success-{index:02d}",
            ReplayCategory.TOOL,
            "agent-tool-turn",
            {"mode": "success", "value": value, "provider_response": f"completed {value}"},
            {"response": f"completed {value}", "tool_result": f"echo:{value}"},
            "A valid Tool Call preserves its exact argument and emits a durable result.",
        ))
    for index in range(4):
        cases.append(_case(
            f"tool-invalid-argument-{index:02d}",
            ReplayCategory.TOOL,
            "agent-tool-turn",
            {
                "mode": "bad-arguments",
                "value": f"invalid-{index:02d}",
                "provider_response": f"recovered invalid-{index:02d}",
            },
            {"response": f"recovered invalid-{index:02d}", "tool_result_prefix": "Error: bad arguments"},
            "Malformed Tool arguments are recorded as a Tool result without a filesystem effect.",
        ))
    for index in range(4):
        cases.append(_case(
            f"tool-unknown-{index:02d}",
            ReplayCategory.TOOL,
            "agent-tool-turn",
            {
                "mode": "unknown-tool",
                "value": f"unknown-{index:02d}",
                "provider_response": f"recovered unknown-{index:02d}",
            },
            {"response": f"recovered unknown-{index:02d}", "tool_result_prefix": "Error: unknown tool"},
            "An unknown Tool Call remains visible in the event/session trace and cannot execute.",
        ))
    return tuple(cases)


def _scheduling_cases() -> tuple[ReplayCase, ...]:
    cases: list[ReplayCase] = []
    for sequence in product("rwxu", repeat=3):
        encoded = "".join(sequence)
        cases.append(_case(
            f"scheduling-{encoded}",
            ReplayCategory.SCHEDULING,
            "tool-execution-plan",
            {"effects": encoded},
            {"waves": _expected_waves(encoded)},
            "C5 may batch only contiguous safe reads; all other calls form barriers.",
        ))
    return tuple(cases)


def _context_cases() -> tuple[ReplayCase, ...]:
    cases: list[ReplayCase] = []
    for index in range(8):
        marker = f"evidence/snip-{index:02d}.py"
        cases.append(_case(
            f"context-snip-{index:02d}",
            ReplayCategory.CONTEXT,
            "context-snip",
            {"marker": marker, "line_count": 8 + index, "position": "head" if index % 2 == 0 else "tail"},
            {"marker": marker},
            "Tool-result compression keeps selected edge evidence rather than deleting raw Session facts.",
        ))
    for index in range(4):
        marker = f"evidence/summary-{index:02d}.py"
        cases.append(_case(
            f"context-summary-{index:02d}",
            ReplayCategory.CONTEXT,
            "context-summary",
            {"marker": marker, "filler_size": 300 + index * 4},
            {"marker": marker, "prefix": "[Context compressed - conversation summary]"},
            "Fallback summarization retains a file-evidence marker in the model projection.",
        ))
    for index in range(4):
        marker = f"evidence/collapse-{index:02d}.py"
        cases.append(_case(
            f"context-collapse-{index:02d}",
            ReplayCategory.CONTEXT,
            "context-collapse",
            {"marker": marker, "filler_size": 520 + index * 13},
            {"marker": marker, "prefix": "[Hard context reset]"},
            "Hard collapse preserves extracted evidence in a new projection and does not mutate raw events.",
        ))
    return tuple(cases)


def _persistence_cases() -> tuple[ReplayCase, ...]:
    cases: list[ReplayCase] = []
    modes = ("turn", "tool", "compressed")
    for index in range(16):
        mode = modes[index % len(modes)]
        marker = f"session-{mode}-{index:02d}"
        cases.append(_case(
            f"persistence-{mode}-{index:02d}",
            ReplayCategory.PERSISTENCE,
            "session-projection",
            {"mode": mode, "marker": marker},
            {"marker": marker, "mode": mode},
            "Raw Session events rebuild a valid message projection without replaying side effects.",
        ))
    return tuple(cases)


def _contract_cases() -> tuple[ReplayCase, ...]:
    cases: list[ReplayCase] = []
    outcomes = (
        "active",
        "unapproved",
        "revoked",
        "disabled",
        "tool-overreach",
        "output-overreach",
        "input-invalid",
        "unknown-skill",
    )
    for index in range(16):
        outcome = outcomes[index % len(outcomes)]
        cases.append(_case(
            f"contract-{outcome}-{index:02d}",
            ReplayCategory.CONTRACT,
            "role-skill-activation",
            {"outcome": outcome, "marker": f"contract-{index:02d}"},
            {"outcome": outcome},
            "Role/Skill lifecycle and Tool request boundaries fail closed before entering Runtime execution.",
        ))
    return tuple(cases)


def _instruction_cases() -> tuple[ReplayCase, ...]:
    cases: list[ReplayCase] = []
    for index in range(16):
        constraint = f"constraint-{index:02d}: do not modify evidence-{index:02d}.py"
        follow_up = f"step-{index:02d}: explain the next action while preserving the constraint"
        cases.append(_case(
            f"instruction-carry-{index:02d}",
            ReplayCategory.INSTRUCTION,
            "instruction-carry",
            {"constraint": constraint, "follow_up": follow_up, "provider_response": f"acknowledged {index:02d}"},
            {"response": f"acknowledged {index:02d}", "constraint": constraint},
            "A later turn receives the earlier user constraint in its real model message projection.",
        ))
    return tuple(cases)


def _case(
    case_id: str,
    category: ReplayCategory,
    scenario: str,
    input_value: dict[str, object],
    expected: dict[str, object],
    description: str,
) -> ReplayCase:
    return ReplayCase(
        id=case_id,
        suite=CORE_V0_SUITE,
        category=category,
        scenario=scenario,
        input=input_value,
        expected=expected,
        description=description,
    )


def _expected_waves(effects: str) -> tuple[tuple[int, ...], ...]:
    waves: list[tuple[int, ...]] = []
    current_reads: list[int] = []
    for index, effect in enumerate(effects):
        if effect == "r":
            current_reads.append(index)
            continue
        if current_reads:
            waves.append(tuple(current_reads))
            current_reads.clear()
        waves.append((index,))
    if current_reads:
        waves.append(tuple(current_reads))
    return tuple(waves)
