from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from techpilot.evaluation import (
    CORE_V0_CASE_COUNT,
    CORE_V0_CASE_SET_DIGEST,
    HOLDOUT_SCHEMA_VERSION,
    HOLDOUT_SUITE,
    RUNNER_VALIDATION_CASE_COUNT,
    BaselineReference,
    ExtendedCaseProvenance,
    ExtendedCaseSource,
    HoldoutFormatError,
    ModelEvaluationManifest,
    ReplayCase,
    ReplayCaseOrigin,
    ReplayCategory,
    ReplayRunner,
    build_core_v0_cases,
    build_runner_validation_cases,
    case_set_digest,
    holdout_case_set_metadata,
    inspect_holdout_case_schema,
    load_holdout_suite,
    run_holdout,
    write_holdout_summary,
)
from techpilot.evaluation.__main__ import main as evaluation_main


def test_core_v0_has_144_unique_cases_with_separate_runtime_categories() -> None:
    cases = build_core_v0_cases()

    assert len(cases) == CORE_V0_CASE_COUNT == 144
    assert case_set_digest(cases) == CORE_V0_CASE_SET_DIGEST
    assert len({case.id for case in cases}) == CORE_V0_CASE_COUNT
    assert {case.category for case in cases} == set(ReplayCategory)
    assert {category: sum(item.category is category for item in cases) for category in ReplayCategory} == {
        ReplayCategory.TOOL: 16,
        ReplayCategory.SCHEDULING: 64,
        ReplayCategory.CONTEXT: 16,
        ReplayCategory.PERSISTENCE: 16,
        ReplayCategory.CONTRACT: 16,
        ReplayCategory.INSTRUCTION: 16,
    }


def test_runner_validation_deck_has_24_cases_across_every_handler() -> None:
    cases = build_runner_validation_cases()

    assert len(cases) == RUNNER_VALIDATION_CASE_COUNT == 24
    assert {case.category for case in cases} == set(ReplayCategory)
    assert all(case.suite == "runner-validation-v0" for case in cases)
    assert all(case.origin is ReplayCaseOrigin.RUNNER_VALIDATION for case in cases)


def test_extended_cases_require_evidence_backed_provenance() -> None:
    source = build_core_v0_cases()[0]

    with pytest.raises(ValueError, match="require evidence-backed provenance"):
        replace(source, id="extended-without-evidence", suite="extended-v0", origin=ReplayCaseOrigin.EXTENDED)

    provenance = ExtendedCaseProvenance(
        source=ExtendedCaseSource.REAL_DEFECT,
        evidence_id="issue-123",
        first_observed_commit="abc123",
        pre_fix_failure="tool event was missing from the session projection",
        rationale="Protect the observed regression from recurring.",
    )
    with pytest.raises(ValueError, match=r"must use an extended-\* suite"):
        replace(source, id="extended-wrong-suite", origin=ReplayCaseOrigin.EXTENDED, provenance=provenance)

    case = replace(
        source,
        id="extended-real-defect-001",
        suite="extended-v0",
        origin=ReplayCaseOrigin.EXTENDED,
        provenance=provenance,
    )

    assert case.provenance is not None
    assert case.to_dict()["provenance"]["source"] == "real-defect"


def test_baseline_comparison_refuses_a_changed_case_deck() -> None:
    runner = ReplayRunner(Path(__file__).parents[2])
    report = runner.run(build_core_v0_cases())
    baseline = BaselineReference.from_report(report)

    assert baseline.compare(report).comparable is True
    changed_case = replace(report.cases[0], description="changed case definition")
    changed_report = replace(report, cases=(changed_case, *report.cases[1:]))
    comparison = baseline.compare(changed_report)

    assert comparison.comparable is False
    assert comparison.reason == "case_set_changed"


def test_candidate_report_cannot_be_used_as_a_baseline(tmp_path: Path) -> None:
    report = ReplayRunner(Path(__file__).parents[2]).run(build_core_v0_cases())
    candidate = ReplayRunner.write_report(report, tmp_path / "candidate.json")

    with pytest.raises(ValueError, match="invalid baseline report"):
        BaselineReference.from_path(candidate)


def test_core_v0_runner_is_offline_deterministic_and_writes_a_manifest(tmp_path: Path) -> None:
    cases = build_core_v0_cases()
    runner = ReplayRunner(Path(__file__).parents[2])

    first = runner.run(cases)
    second = runner.run(cases)
    output = runner.write_report(first, tmp_path / "baseline-v0-candidate.json")
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert first.passed == first.total == 144
    assert second.passed == second.total == 144
    assert first.case_set_digest == second.case_set_digest
    assert payload["case_set_digest"] == first.case_set_digest
    assert isinstance(payload["git_dirty"], bool)
    assert payload["categories"] == {
        "tool": {"passed": 16, "total": 16},
        "scheduling": {"passed": 64, "total": 64},
        "context": {"passed": 16, "total": 16},
        "persistence": {"passed": 16, "total": 16},
        "contract": {"passed": 16, "total": 16},
        "instruction": {"passed": 16, "total": 16},
    }


def test_baseline_rejects_dirty_or_failed_core_runs(tmp_path: Path) -> None:
    runner = ReplayRunner(Path(__file__).parents[2])
    report = runner.run(build_core_v0_cases())

    with pytest.raises(ValueError, match="clean Git worktree"):
        runner.write_baseline(replace(report, git_dirty=True), tmp_path / "baseline-v0.json")
    with pytest.raises(ValueError, match="every core-v0 case to pass"):
        runner.write_baseline(
            replace(report, outcomes=(replace(report.outcomes[0], passed=False), *report.outcomes[1:]), git_dirty=False),
            tmp_path / "baseline-v0.json",
        )
    output = runner.write_baseline(replace(report, git_dirty=False), tmp_path / "baseline-v0.json")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["git_dirty"] is False
    assert payload["baseline"]["kind"] == "baseline-v0"
    assert BaselineReference.from_path(output).compare(report).comparable is True


def test_replay_report_fails_one_case_without_hiding_its_category() -> None:
    source = build_core_v0_cases()[0]
    broken = ReplayCase(
        id="tool-broken-expectation",
        suite=source.suite,
        category=source.category,
        scenario=source.scenario,
        input=source.input,
        expected={**source.expected, "tool_result": "this must fail"},
        description=source.description,
    )

    report = ReplayRunner(Path(__file__).parents[2]).run((broken,))

    assert report.passed == 0
    assert report.total == 1
    assert report.outcomes[0].category is ReplayCategory.TOOL
    assert report.outcomes[0].failure == "Tool result did not preserve the expected argument"


def test_model_manifest_carries_conditions_but_never_a_fake_score() -> None:
    manifest = ModelEvaluationManifest(
        provider="example-provider",
        model="example-model",
        parameters={"temperature": 0},
        suite="model-core-v0",
        case_set_digest="a" * 64,
    )

    assert manifest.to_dict() == {
        "provider": "example-provider",
        "model": "example-model",
        "parameters": {"temperature": 0},
        "suite": "model-core-v0",
        "case_set_digest": "a" * 64,
        "track": "model",
    }
    with pytest.raises(ValueError, match="requires provider"):
        ModelEvaluationManifest(
            provider="",
            model="example-model",
            parameters={},
            suite="model-core-v0",
            case_set_digest="a" * 64,
        )


def test_private_holdout_loader_validates_integrity_and_writes_only_a_redacted_summary(tmp_path: Path) -> None:
    case = ReplayCase(
        id="holdout-tool-success-001",
        suite=HOLDOUT_SUITE,
        category=ReplayCategory.TOOL,
        scenario="agent-tool-turn",
        input={"mode": "success", "value": "private-marker", "provider_response": "done"},
        expected={"response": "done", "tool_result": "echo:private-marker"},
        origin=ReplayCaseOrigin.HOLDOUT,
        description="Synthetic stand-in; no external holdout content is used in this test.",
    )
    root = tmp_path / "private-holdout"
    root.mkdir()
    (root / "reports").mkdir()
    (root / "cases.jsonl").write_text(json.dumps(case.to_dict()) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "suite": HOLDOUT_SUITE,
        "schema_version": HOLDOUT_SCHEMA_VERSION,
        "case_count": 1,
        "case_set_digest": case_set_digest((case,)),
    }), encoding="utf-8")

    loaded = load_holdout_suite(root)
    summary = run_holdout(root, ReplayRunner(Path(__file__).parents[2]))
    output = write_holdout_summary(summary, root / "reports" / "summary.json")
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert loaded.cases == (case,)
    assert summary.passed == summary.total == 1
    assert summary.failed_case_ids == ()
    assert payload["kind"] == "holdout-summary-v0"
    assert payload["failed_case_ids"] == []
    assert "outcomes" not in payload
    assert "observed" not in payload
    assert "private-marker" not in output.read_text(encoding="utf-8")


def test_private_holdout_rejects_a_changed_or_non_holdout_case_deck(tmp_path: Path) -> None:
    root = tmp_path / "private-holdout"
    root.mkdir()
    case = ReplayCase(
        id="holdout-tool-success-001",
        suite=HOLDOUT_SUITE,
        category=ReplayCategory.TOOL,
        scenario="agent-tool-turn",
        input={"mode": "success", "value": "marker", "provider_response": "done"},
        expected={"response": "done", "tool_result": "echo:marker"},
        origin=ReplayCaseOrigin.HOLDOUT,
    )
    altered = case.to_dict() | {"origin": ReplayCaseOrigin.CORE.value}
    (root / "cases.jsonl").write_text(json.dumps(altered) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "suite": HOLDOUT_SUITE,
        "schema_version": HOLDOUT_SCHEMA_VERSION,
        "case_count": 1,
        "case_set_digest": case_set_digest((case,)),
    }), encoding="utf-8")

    with pytest.raises(HoldoutFormatError, match="invalid holdout case at line 1"):
        load_holdout_suite(root)


def test_private_holdout_manifest_errors_expose_only_safe_structure_details(tmp_path: Path) -> None:
    root = tmp_path / "private-holdout"
    root.mkdir()

    with pytest.raises(HoldoutFormatError, match="manifest.json is missing"):
        load_holdout_suite(root)

    (root / "manifest.json").write_text(json.dumps({"suite": HOLDOUT_SUITE}), encoding="utf-8")
    with pytest.raises(HoldoutFormatError, match="missing required fields: schema_version, case_count, case_set_digest"):
        load_holdout_suite(root)


def test_private_holdout_case_set_metadata_exposes_only_count_and_digest(tmp_path: Path) -> None:
    case = ReplayCase(
        id="holdout-tool-success-001",
        suite=HOLDOUT_SUITE,
        category=ReplayCategory.TOOL,
        scenario="agent-tool-turn",
        input={"mode": "success", "value": "private-marker", "provider_response": "done"},
        expected={"response": "done", "tool_result": "echo:private-marker"},
        origin=ReplayCaseOrigin.HOLDOUT,
    )
    root = tmp_path / "private-holdout"
    root.mkdir()
    (root / "cases.jsonl").write_text(json.dumps(case.to_dict()) + "\n", encoding="utf-8")

    assert holdout_case_set_metadata(root) == (1, case_set_digest((case,)))


def test_private_holdout_schema_inspection_exposes_field_names_without_values(tmp_path: Path) -> None:
    root = tmp_path / "private-holdout"
    root.mkdir()
    (root / "cases.jsonl").write_text(json.dumps({
        "opaque_id": "private-marker",
        "assertions": ["private expected output"],
    }) + "\n", encoding="utf-8")

    schema = inspect_holdout_case_schema(root)

    assert schema.case_count == 1
    assert schema.fields == ("assertions", "opaque_id")
    assert "private-marker" not in repr(schema)


def test_private_holdout_cli_prints_only_the_redacted_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    case = ReplayCase(
        id="holdout-tool-success-001",
        suite=HOLDOUT_SUITE,
        category=ReplayCategory.TOOL,
        scenario="agent-tool-turn",
        input={"mode": "success", "value": "private-marker", "provider_response": "done"},
        expected={"response": "done", "tool_result": "echo:private-marker"},
        origin=ReplayCaseOrigin.HOLDOUT,
    )
    root = tmp_path / "private-holdout"
    root.mkdir()
    (root / "cases.jsonl").write_text(json.dumps(case.to_dict()) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "suite": HOLDOUT_SUITE,
        "schema_version": HOLDOUT_SCHEMA_VERSION,
        "case_count": 1,
        "case_set_digest": case_set_digest((case,)),
    }), encoding="utf-8")

    assert evaluation_main(["--holdout-root", str(root)]) == 0

    rendered = capsys.readouterr().out
    reports = list((root / "reports").glob("*.json"))
    assert "holdout-v0: 1/1 passed" in rendered
    assert "failed_case_ids: none" in rendered
    assert "private-marker" not in rendered
    assert len(reports) == 1
    assert "private-marker" not in reports[0].read_text(encoding="utf-8")
