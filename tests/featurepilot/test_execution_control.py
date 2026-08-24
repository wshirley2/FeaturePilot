"""Pure execution-control policy tests with no Runtime or filesystem integration."""

from __future__ import annotations

import pytest

from featurepilot.execution import (
    CommandKind,
    ControlReasonCode,
    ExecutionControlPolicy,
    ExternalEffect,
    FileCategory,
    ImpactScope,
    NormalizedCommand,
    NormalizedToolRequest,
    OperationKind,
    PathBoundary,
    RequiredControl,
)


def _request(**overrides) -> NormalizedToolRequest:
    values = {
        "tool_name": "read_file",
        "operation": OperationKind.READ,
        "affected_paths": ("README.md",),
    }
    values.update(overrides)
    return NormalizedToolRequest(**values)


@pytest.mark.parametrize(
    ("normalized_request", "expected", "reason_code"),
    [
        (_request(), RequiredControl.DIRECT, ControlReasonCode.REPOSITORY_READ),
        (
            _request(path_boundary=PathBoundary.APPROVED_ARTIFACT),
            RequiredControl.DIRECT,
            ControlReasonCode.APPROVED_ARTIFACT_READ,
        ),
        (
            _request(
                affected_paths=("pyproject.toml",),
                file_categories=frozenset({FileCategory.DEPENDENCY_MANIFEST}),
            ),
            RequiredControl.DIRECT,
            ControlReasonCode.REPOSITORY_READ,
        ),
        (_request(tool_name="grep", operation=OperationKind.SEARCH), RequiredControl.DIRECT, ControlReasonCode.REPOSITORY_SEARCH),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("python", "-m", "pytest", "-q"), CommandKind.TEST),
            ),
            RequiredControl.DIRECT,
            ControlReasonCode.SAFE_TEST_OR_LINT,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("ruff", "check", "."), CommandKind.LINT),
            ),
            RequiredControl.DIRECT,
            ControlReasonCode.SAFE_TEST_OR_LINT,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("dir",), CommandKind.READ_ONLY_SHELL),
            ),
            RequiredControl.DIRECT,
            ControlReasonCode.READ_ONLY_SHELL,
        ),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                affected_paths=("src/app.py",),
                file_categories=frozenset({FileCategory.SOURCE}),
            ),
            RequiredControl.CONFIRM,
            ControlReasonCode.SINGLE_FILE_WRITE,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("python", "tools/check.py"), CommandKind.GENERAL),
            ),
            RequiredControl.CONFIRM,
            ControlReasonCode.GENERAL_COMMAND,
        ),
    ],
)
def test_direct_and_confirm_controls_are_explained(normalized_request, expected, reason_code):
    assessment = ExecutionControlPolicy().assess(normalized_request)

    assert assessment.required_control is expected
    assert any(reason.code is reason_code for reason in assessment.reasons)
    assert all(reason.evidence for reason in assessment.reasons)


@pytest.mark.parametrize(
    ("path", "category", "isolate_reason"),
    [
        ("pyproject.toml", FileCategory.DEPENDENCY_MANIFEST, ControlReasonCode.DEPENDENCY_MANIFEST),
        ("poetry.lock", FileCategory.LOCK_FILE, ControlReasonCode.LOCK_FILE),
        ("migrations/001.sql", FileCategory.DATABASE_MIGRATION, ControlReasonCode.DATABASE_MIGRATION),
        ("deploy/app.yaml", FileCategory.DEPLOYMENT_CONFIG, ControlReasonCode.DEPLOYMENT_CONFIG),
        (".github/workflows/check.yml", FileCategory.CI_CONFIG, ControlReasonCode.CI_CONFIG),
    ],
)
def test_reading_special_files_does_not_require_isolation(path, category, isolate_reason):
    assessment = ExecutionControlPolicy().assess(_request(
        affected_paths=(path,),
        file_categories=frozenset({category}),
    ))

    assert assessment.required_control is RequiredControl.DIRECT
    assert ControlReasonCode.REPOSITORY_READ in {reason.code for reason in assessment.reasons}
    assert isolate_reason not in {reason.code for reason in assessment.reasons}


@pytest.mark.parametrize(
    ("normalized_request", "reason_code"),
    [
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                impact_scope=ImpactScope.MULTI_FILE,
                affected_paths=("src/a.py", "src/b.py"),
            ),
            ControlReasonCode.MULTI_FILE_SCOPE,
        ),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                impact_scope=ImpactScope.DIRECTORY,
                affected_paths=("src/",),
            ),
            ControlReasonCode.DIRECTORY_SCOPE,
        ),
        (_request(tool_name="delete_file", operation=OperationKind.DELETE), ControlReasonCode.DELETE_OPERATION),
        (_request(tool_name="move_file", operation=OperationKind.MOVE), ControlReasonCode.BULK_MOVE_OR_RENAME),
        (_request(tool_name="rename_file", operation=OperationKind.RENAME), ControlReasonCode.BULK_MOVE_OR_RENAME),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                affected_paths=("pyproject.toml",),
                file_categories=frozenset({FileCategory.DEPENDENCY_MANIFEST}),
            ),
            ControlReasonCode.DEPENDENCY_MANIFEST,
        ),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                affected_paths=("poetry.lock",),
                file_categories=frozenset({FileCategory.LOCK_FILE}),
            ),
            ControlReasonCode.LOCK_FILE,
        ),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                affected_paths=("migrations/001.sql",),
                file_categories=frozenset({FileCategory.DATABASE_MIGRATION}),
            ),
            ControlReasonCode.DATABASE_MIGRATION,
        ),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                affected_paths=("deploy/app.yaml",),
                file_categories=frozenset({FileCategory.DEPLOYMENT_CONFIG}),
            ),
            ControlReasonCode.DEPLOYMENT_CONFIG,
        ),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                affected_paths=(".github/workflows/check.yml",),
                file_categories=frozenset({FileCategory.CI_CONFIG}),
            ),
            ControlReasonCode.CI_CONFIG,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("ruff", "format", "--fix"), CommandKind.FORMAT, has_fix=True),
            ),
            ControlReasonCode.FORMAT_FIX,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("protoc", "schema.proto"), CommandKind.CODE_GENERATION),
            ),
            ControlReasonCode.CODE_GENERATION,
        ),
    ],
)
def test_isolate_controls_are_explained(normalized_request, reason_code):
    assessment = ExecutionControlPolicy().assess(normalized_request)

    assert assessment.required_control is RequiredControl.ISOLATE
    assert any(reason.code is reason_code for reason in assessment.reasons)
    assert all(reason.evidence for reason in assessment.reasons)


@pytest.mark.parametrize(
    ("normalized_request", "reason_code"),
    [
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                path_boundary=PathBoundary.OUTSIDE_REPOSITORY,
                affected_paths=("../outside.txt",),
            ),
            ControlReasonCode.PATH_OUTSIDE_REPOSITORY,
        ),
        (
            _request(
                tool_name="write_file",
                operation=OperationKind.WRITE,
                path_boundary=PathBoundary.DANGEROUS_SYSTEM,
                affected_paths=("C:/Windows/System32/config",),
            ),
            ControlReasonCode.DANGEROUS_SYSTEM_PATH,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("git", "reset", "--hard"), CommandKind.GENERAL),
            ),
            ControlReasonCode.DESTRUCTIVE_GIT_COMMAND,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("git", "clean", "-fd"), CommandKind.GENERAL),
            ),
            ControlReasonCode.DESTRUCTIVE_GIT_COMMAND,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("pytest", "|", "tee", "out.txt"), CommandKind.TEST, has_pipeline=True),
            ),
            ControlReasonCode.COMPLEX_COMMAND,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("echo", "ok", ">", "out.txt"), CommandKind.GENERAL, has_redirection=True),
            ),
            ControlReasonCode.COMPLEX_COMMAND,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(("echo", "$(date)"), CommandKind.GENERAL, has_command_substitution=True),
            ),
            ControlReasonCode.COMPLEX_COMMAND,
        ),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                command=NormalizedCommand(is_parseable=False),
            ),
            ControlReasonCode.UNPARSEABLE_COMMAND,
        ),
        (_request(tool_name="publish", operation=OperationKind.PUBLISH), ControlReasonCode.UNSUPPORTED_EXTERNAL_EFFECT),
        (
            _request(
                tool_name="bash",
                operation=OperationKind.COMMAND,
                external_effect=ExternalEffect.PUSH,
                command=NormalizedCommand(("git", "push"), CommandKind.PUSH),
            ),
            ControlReasonCode.UNSUPPORTED_EXTERNAL_EFFECT,
        ),
    ],
)
def test_block_controls_are_explained(normalized_request, reason_code):
    assessment = ExecutionControlPolicy().assess(normalized_request)

    assert assessment.required_control is RequiredControl.BLOCK
    assert any(reason.code is reason_code for reason in assessment.reasons)
    assert all(reason.evidence for reason in assessment.reasons)


def test_block_wins_over_isolate_and_preserves_all_explanations():
    assessment = ExecutionControlPolicy().assess(_request(
        tool_name="write_file",
        operation=OperationKind.WRITE,
        path_boundary=PathBoundary.OUTSIDE_REPOSITORY,
        affected_paths=("../poetry.lock",),
        file_categories=frozenset({FileCategory.LOCK_FILE}),
    ))

    assert assessment.required_control is RequiredControl.BLOCK
    assert {reason.code for reason in assessment.reasons} >= {
        ControlReasonCode.PATH_OUTSIDE_REPOSITORY,
        ControlReasonCode.LOCK_FILE,
        ControlReasonCode.SINGLE_FILE_WRITE,
    }


def test_block_wins_over_a_normally_direct_repository_read():
    assessment = ExecutionControlPolicy().assess(_request(
        path_boundary=PathBoundary.OUTSIDE_REPOSITORY,
        affected_paths=("../outside-readme.md",),
    ))

    assert assessment.required_control is RequiredControl.BLOCK
    assert {reason.code for reason in assessment.reasons} >= {
        ControlReasonCode.PATH_OUTSIDE_REPOSITORY,
        ControlReasonCode.REPOSITORY_READ,
    }


def test_isolate_wins_over_confirm_and_direct_rules():
    assessment = ExecutionControlPolicy().assess(_request(
        tool_name="write_file",
        operation=OperationKind.WRITE,
        affected_paths=("src/app.py", "src/version.py"),
        impact_scope=ImpactScope.MULTI_FILE,
        file_categories=frozenset({FileCategory.SOURCE}),
    ))

    assert assessment.required_control is RequiredControl.ISOLATE
    assert {reason.code for reason in assessment.reasons} >= {
        ControlReasonCode.MULTI_FILE_SCOPE,
        ControlReasonCode.SINGLE_FILE_WRITE,
    }


def test_assessment_requires_at_least_one_structured_reason():
    with pytest.raises(ValueError, match="ControlReason"):
        from featurepilot.execution import ExecutionControlAssessment

        ExecutionControlAssessment(RequiredControl.CONFIRM, ())
