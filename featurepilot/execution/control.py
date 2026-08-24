"""Pure, explainable execution-control decisions for normalized tool requests.

This module deliberately has no dependency on ToolExecutor, Permission, Runtime
events, a model provider, or the filesystem. A future integration is responsible
for converting a real tool call into :class:`NormalizedToolRequest` before asking
this policy for a RequiredControl.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RequiredControl(str, Enum):
    """The execution boundary required for one normalized operation."""

    DIRECT = "direct"
    CONFIRM = "confirm"
    ISOLATE = "isolate"
    BLOCK = "block"


class OperationKind(str, Enum):
    """Normalized operation kinds independent from a concrete tool implementation."""

    READ = "read"
    SEARCH = "search"
    WRITE = "write"
    DELETE = "delete"
    MOVE = "move"
    RENAME = "rename"
    COMMAND = "command"
    NETWORK = "network"
    PUBLISH = "publish"


class PathBoundary(str, Enum):
    """Where the normalized operation targets relative to its repository."""

    REPOSITORY = "repository"
    OUTSIDE_REPOSITORY = "outside_repository"
    DANGEROUS_SYSTEM = "dangerous_system"
    UNRESOLVED = "unresolved"


class ImpactScope(str, Enum):
    """The normalized scope of the files an operation can affect."""

    SINGLE_FILE = "single_file"
    MULTI_FILE = "multi_file"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


class Reversibility(str, Enum):
    """Whether the operation is known to be reversible at the repository level."""

    REVERSIBLE = "reversible"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class FileCategory(str, Enum):
    """Special file categories that need a stronger execution boundary."""

    SOURCE = "source"
    DEPENDENCY_MANIFEST = "dependency_manifest"
    LOCK_FILE = "lock_file"
    DATABASE_MIGRATION = "database_migration"
    DEPLOYMENT_CONFIG = "deployment_config"
    CI_CONFIG = "ci_config"
    OTHER = "other"


class CommandKind(str, Enum):
    """The normalized intent of a parseable command, not an LLM classification."""

    TEST = "test"
    LINT = "lint"
    READ_ONLY_GIT = "read_only_git"
    GENERAL = "general"
    FORMAT = "format"
    CODE_GENERATION = "code_generation"
    PUBLISH = "publish"
    PUSH = "push"


class ExternalEffect(str, Enum):
    """External effects that a future dedicated capability may handle explicitly."""

    NONE = "none"
    NETWORK = "network"
    PUBLISH = "publish"
    PUSH = "push"
    DEPLOY = "deploy"


class ControlReasonCode(str, Enum):
    """Stable machine-readable explanations for execution-control decisions."""

    REPOSITORY_READ = "repository_read"
    REPOSITORY_SEARCH = "repository_search"
    SAFE_TEST_OR_LINT = "safe_test_or_lint"
    READ_ONLY_GIT = "read_only_git"
    SINGLE_FILE_WRITE = "single_file_write"
    GENERAL_COMMAND = "general_command"
    NETWORK_EFFECT = "network_effect"
    PATH_OUTSIDE_REPOSITORY = "path_outside_repository"
    DANGEROUS_SYSTEM_PATH = "dangerous_system_path"
    UNRESOLVED_PATH = "unresolved_path"
    DESTRUCTIVE_GIT_COMMAND = "destructive_git_command"
    COMPLEX_COMMAND = "complex_command"
    UNPARSEABLE_COMMAND = "unparseable_command"
    UNSUPPORTED_EXTERNAL_EFFECT = "unsupported_external_effect"
    MULTI_FILE_SCOPE = "multi_file_scope"
    DIRECTORY_SCOPE = "directory_scope"
    UNKNOWN_SCOPE = "unknown_scope"
    DESTRUCTIVE_OPERATION = "destructive_operation"
    DELETE_OPERATION = "delete_operation"
    BULK_MOVE_OR_RENAME = "bulk_move_or_rename"
    DEPENDENCY_MANIFEST = "dependency_manifest"
    LOCK_FILE = "lock_file"
    DATABASE_MIGRATION = "database_migration"
    DEPLOYMENT_CONFIG = "deployment_config"
    CI_CONFIG = "ci_config"
    FORMAT_FIX = "format_fix"
    CODE_GENERATION = "code_generation"
    DEFAULT_CONFIRM = "default_confirm"


@dataclass(frozen=True, slots=True)
class NormalizedCommand:
    """Command facts produced by a conservative command normalizer."""

    tokens: tuple[str, ...] = ()
    kind: CommandKind = CommandKind.GENERAL
    is_parseable: bool = True
    has_pipeline: bool = False
    has_redirection: bool = False
    has_command_substitution: bool = False
    has_fix: bool = False

    @property
    def display(self) -> str:
        """Return the normalized command text suitable for evidence."""

        return " ".join(self.tokens) if self.tokens else "<no normalized tokens>"


@dataclass(frozen=True, slots=True)
class NormalizedToolRequest:
    """All policy inputs are normalized facts; no tool or filesystem is consulted."""

    tool_name: str
    operation: OperationKind
    path_boundary: PathBoundary = PathBoundary.REPOSITORY
    affected_paths: tuple[str, ...] = ()
    file_categories: frozenset[FileCategory] = field(default_factory=frozenset)
    impact_scope: ImpactScope = ImpactScope.SINGLE_FILE
    reversibility: Reversibility = Reversibility.REVERSIBLE
    command: NormalizedCommand | None = None
    external_effect: ExternalEffect = ExternalEffect.NONE


@dataclass(frozen=True, slots=True)
class ControlReason:
    """One explainable fact that contributed to a RequiredControl."""

    code: ControlReasonCode
    required_control: RequiredControl
    message: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionControlAssessment:
    """The pure-policy decision and all facts that justify it."""

    required_control: RequiredControl
    reasons: tuple[ControlReason, ...]

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("ExecutionControlAssessment requires at least one ControlReason")


class ExecutionControlPolicy:
    """Choose the strongest required control from normalized operation facts.

    ``BLOCK > ISOLATE > CONFIRM > DIRECT`` is encoded as precedence, never as a
    numerical score. The policy only raises control strength; a later integration
    must still enforce Permission, Trusted Diff, path policy, and BLOCK rules.
    """

    _PRECEDENCE = (
        RequiredControl.BLOCK,
        RequiredControl.ISOLATE,
        RequiredControl.CONFIRM,
        RequiredControl.DIRECT,
    )
    def assess(self, request: NormalizedToolRequest) -> ExecutionControlAssessment:
        """Return an explainable control decision without executing anything."""

        reasons = [
            *self._block_reasons(request),
            *self._isolate_reasons(request),
            *self._confirm_reasons(request),
            *self._direct_reasons(request),
        ]
        if not reasons:
            reasons.append(self._reason(
                ControlReasonCode.DEFAULT_CONFIRM,
                RequiredControl.CONFIRM,
                "Operation has no direct-execution rule and requires confirmation",
                f"tool={request.tool_name}",
                f"operation={request.operation.value}",
            ))
        return ExecutionControlAssessment(self._required_control(reasons), tuple(reasons))

    def _block_reasons(self, request: NormalizedToolRequest) -> list[ControlReason]:
        reasons: list[ControlReason] = []
        if request.path_boundary is PathBoundary.OUTSIDE_REPOSITORY:
            reasons.append(self._reason(
                ControlReasonCode.PATH_OUTSIDE_REPOSITORY,
                RequiredControl.BLOCK,
                "Target is outside the repository boundary",
                f"paths={self._paths_evidence(request)}",
            ))
        if request.path_boundary is PathBoundary.DANGEROUS_SYSTEM:
            reasons.append(self._reason(
                ControlReasonCode.DANGEROUS_SYSTEM_PATH,
                RequiredControl.BLOCK,
                "Target is a dangerous system path",
                f"paths={self._paths_evidence(request)}",
            ))
        if request.path_boundary is PathBoundary.UNRESOLVED:
            reasons.append(self._reason(
                ControlReasonCode.UNRESOLVED_PATH,
                RequiredControl.BLOCK,
                "Target path could not be normalized safely",
                f"paths={self._paths_evidence(request)}",
            ))
        if request.external_effect in {ExternalEffect.PUBLISH, ExternalEffect.PUSH, ExternalEffect.DEPLOY}:
            reasons.append(self._reason(
                ControlReasonCode.UNSUPPORTED_EXTERNAL_EFFECT,
                RequiredControl.BLOCK,
                "External publish, push, or deployment needs a dedicated capability",
                f"external_effect={request.external_effect.value}",
            ))
        if request.operation is OperationKind.PUBLISH:
            reasons.append(self._reason(
                ControlReasonCode.UNSUPPORTED_EXTERNAL_EFFECT,
                RequiredControl.BLOCK,
                "Publish operations need a dedicated capability",
                f"operation={request.operation.value}",
            ))
        command = request.command
        if command is None:
            return reasons
        if not command.is_parseable:
            reasons.append(self._reason(
                ControlReasonCode.UNPARSEABLE_COMMAND,
                RequiredControl.BLOCK,
                "Command could not be parsed reliably",
                f"command={command.display}",
            ))
        if command.has_pipeline or command.has_redirection or command.has_command_substitution:
            features = []
            if command.has_pipeline:
                features.append("pipeline")
            if command.has_redirection:
                features.append("redirection")
            if command.has_command_substitution:
                features.append("command_substitution")
            reasons.append(self._reason(
                ControlReasonCode.COMPLEX_COMMAND,
                RequiredControl.BLOCK,
                "Command contains shell structure that requires reliable decomposition",
                f"command={command.display}",
                f"features={','.join(features)}",
            ))
        if self._is_destructive_git(command.tokens):
            reasons.append(self._reason(
                ControlReasonCode.DESTRUCTIVE_GIT_COMMAND,
                RequiredControl.BLOCK,
                "Destructive Git cleanup is always blocked",
                f"command={command.display}",
            ))
        if command.kind in {CommandKind.PUBLISH, CommandKind.PUSH}:
            reasons.append(self._reason(
                ControlReasonCode.UNSUPPORTED_EXTERNAL_EFFECT,
                RequiredControl.BLOCK,
                "Publish or push command needs a dedicated capability",
                f"command={command.display}",
                f"command_kind={command.kind.value}",
            ))
        return reasons

    def _isolate_reasons(self, request: NormalizedToolRequest) -> list[ControlReason]:
        reasons: list[ControlReason] = []
        if request.impact_scope is ImpactScope.MULTI_FILE:
            reasons.append(self._reason(
                ControlReasonCode.MULTI_FILE_SCOPE,
                RequiredControl.ISOLATE,
                "Operation affects multiple files",
                f"paths={self._paths_evidence(request)}",
            ))
        if request.impact_scope is ImpactScope.DIRECTORY:
            reasons.append(self._reason(
                ControlReasonCode.DIRECTORY_SCOPE,
                RequiredControl.ISOLATE,
                "Operation affects a directory scope",
                f"paths={self._paths_evidence(request)}",
            ))
        if request.impact_scope is ImpactScope.UNKNOWN:
            reasons.append(self._reason(
                ControlReasonCode.UNKNOWN_SCOPE,
                RequiredControl.ISOLATE,
                "Operation impact scope is unknown",
                f"tool={request.tool_name}",
            ))
        if request.reversibility in {Reversibility.DESTRUCTIVE, Reversibility.UNKNOWN}:
            reasons.append(self._reason(
                ControlReasonCode.DESTRUCTIVE_OPERATION,
                RequiredControl.ISOLATE,
                "Operation is destructive or its reversibility is unknown",
                f"reversibility={request.reversibility.value}",
            ))
        if request.operation is OperationKind.DELETE:
            reasons.append(self._reason(
                ControlReasonCode.DELETE_OPERATION,
                RequiredControl.ISOLATE,
                "Delete operations require an isolated execution boundary",
                f"paths={self._paths_evidence(request)}",
            ))
        if request.operation in {OperationKind.MOVE, OperationKind.RENAME}:
            reasons.append(self._reason(
                ControlReasonCode.BULK_MOVE_OR_RENAME,
                RequiredControl.ISOLATE,
                "Move or rename operations require an isolated execution boundary",
                f"operation={request.operation.value}",
                f"paths={self._paths_evidence(request)}",
            ))
        for category, code, label in (
            (FileCategory.DEPENDENCY_MANIFEST, ControlReasonCode.DEPENDENCY_MANIFEST, "dependency manifest"),
            (FileCategory.LOCK_FILE, ControlReasonCode.LOCK_FILE, "lock file"),
            (FileCategory.DATABASE_MIGRATION, ControlReasonCode.DATABASE_MIGRATION, "database migration"),
            (FileCategory.DEPLOYMENT_CONFIG, ControlReasonCode.DEPLOYMENT_CONFIG, "deployment config"),
            (FileCategory.CI_CONFIG, ControlReasonCode.CI_CONFIG, "CI config"),
        ):
            if category in request.file_categories:
                reasons.append(self._reason(
                    code,
                    RequiredControl.ISOLATE,
                    f"Operation modifies a {label}",
                    f"file_category={category.value}",
                    f"paths={self._paths_evidence(request)}",
                ))
        command = request.command
        if command and command.kind is CommandKind.FORMAT and command.has_fix:
            reasons.append(self._reason(
                ControlReasonCode.FORMAT_FIX,
                RequiredControl.ISOLATE,
                "Formatting command includes a fix mode",
                f"command={command.display}",
            ))
        if command and command.kind is CommandKind.CODE_GENERATION:
            reasons.append(self._reason(
                ControlReasonCode.CODE_GENERATION,
                RequiredControl.ISOLATE,
                "Code generation can modify a broad file set",
                f"command={command.display}",
            ))
        return reasons

    def _confirm_reasons(self, request: NormalizedToolRequest) -> list[ControlReason]:
        reasons: list[ControlReason] = []
        if request.operation is OperationKind.WRITE:
            reasons.append(self._reason(
                ControlReasonCode.SINGLE_FILE_WRITE,
                RequiredControl.CONFIRM,
                "Repository write requires a reviewable confirmation",
                f"paths={self._paths_evidence(request)}",
                f"scope={request.impact_scope.value}",
            ))
        if request.operation is OperationKind.NETWORK or request.external_effect is ExternalEffect.NETWORK:
            reasons.append(self._reason(
                ControlReasonCode.NETWORK_EFFECT,
                RequiredControl.CONFIRM,
                "Network effect requires explicit confirmation",
                f"external_effect={request.external_effect.value}",
            ))
        if request.operation is OperationKind.COMMAND and request.command and request.command.kind is CommandKind.GENERAL:
            reasons.append(self._reason(
                ControlReasonCode.GENERAL_COMMAND,
                RequiredControl.CONFIRM,
                "Parseable general command requires confirmation",
                f"command={request.command.display}",
            ))
        return reasons

    def _direct_reasons(self, request: NormalizedToolRequest) -> list[ControlReason]:
        if request.operation is OperationKind.READ:
            return [self._reason(
                ControlReasonCode.REPOSITORY_READ,
                RequiredControl.DIRECT,
                "Repository read can execute directly",
                f"paths={self._paths_evidence(request)}",
            )]
        if request.operation is OperationKind.SEARCH:
            return [self._reason(
                ControlReasonCode.REPOSITORY_SEARCH,
                RequiredControl.DIRECT,
                "Repository search can execute directly",
                f"paths={self._paths_evidence(request)}",
            )]
        command = request.command
        if request.operation is OperationKind.COMMAND and command:
            if command.kind in {CommandKind.TEST, CommandKind.LINT} and not command.has_fix:
                return [self._reason(
                    ControlReasonCode.SAFE_TEST_OR_LINT,
                    RequiredControl.DIRECT,
                    "Test or lint command has no fix mode",
                    f"command={command.display}",
                )]
            if command.kind is CommandKind.READ_ONLY_GIT:
                return [self._reason(
                    ControlReasonCode.READ_ONLY_GIT,
                    RequiredControl.DIRECT,
                    "Read-only Git command can execute directly",
                    f"command={command.display}",
                )]
        return []

    @staticmethod
    def _reason(
        code: ControlReasonCode,
        required_control: RequiredControl,
        message: str,
        *evidence: str,
    ) -> ControlReason:
        return ControlReason(code, required_control, message, evidence)

    @classmethod
    def _required_control(cls, reasons: list[ControlReason]) -> RequiredControl:
        for required_control in cls._PRECEDENCE:
            if any(reason.required_control is required_control for reason in reasons):
                return required_control
        raise AssertionError("Execution control precedence is incomplete")

    @staticmethod
    def _paths_evidence(request: NormalizedToolRequest) -> str:
        return ",".join(request.affected_paths) if request.affected_paths else "<no affected paths>"

    @staticmethod
    def _is_destructive_git(tokens: tuple[str, ...]) -> bool:
        lowered = tuple(token.lower() for token in tokens)
        if len(lowered) < 3 or lowered[0] != "git":
            return False
        if lowered[1] == "reset" and "--hard" in lowered[2:]:
            return True
        if lowered[1] != "clean":
            return False
        flags = lowered[2:]
        has_force = any("f" in flag for flag in flags if flag.startswith("-"))
        has_directory = any("d" in flag for flag in flags if flag.startswith("-"))
        return has_force and has_directory
