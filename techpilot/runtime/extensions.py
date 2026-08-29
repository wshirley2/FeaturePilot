"""Domain-neutral Role/Skill contracts owned by the Task Runtime.

Roles describe a task boundary, Skills describe reusable workflows, and this
module only validates their declared contracts.  Permission, tool effects,
resource concurrency, Session facts, and execution remain Runtime-owned.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][A-Za-z0-9.-]+)?$")
_STABLE_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_FRONTMATTER_BOUNDARY = "---"
_FORBIDDEN_SKILL_FIELDS = frozenset({
    "effect", "effects", "concurrency", "permission", "permissions",
    "resource", "resources", "tool", "tools", "allowed_tools",
})
RUNTIME_ROLE_API_VERSION = "1.0.0"


def _identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{label} must be lowercase kebab-case")
    return normalized


def _text(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _stable_semver(value: str, label: str) -> str:
    normalized = value.strip()
    if not _STABLE_SEMVER.fullmatch(normalized):
        raise ValueError(f"{label} must be a stable semantic-version formatted value")
    return normalized


def _semver_key(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


class PayloadContract(BaseModel):
    """A deliberately small, serializable mapping contract.

    Rich domain schemas belong to the domain package.  Runtime only needs a
    stable boundary that can reject missing or unexpected top-level fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: str = "empty-v1"
    required_keys: tuple[str, ...] = ()
    optional_keys: tuple[str, ...] = ()
    allow_extra: bool = False

    @field_validator("schema_id")
    @classmethod
    def _validate_schema_id(cls, value: str) -> str:
        return _identifier(value, "contract schema id")

    @field_validator("required_keys", "optional_keys")
    @classmethod
    def _validate_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("contract keys must not contain duplicates")
        for value in values:
            _identifier(value, "contract key")
        return values

    @model_validator(mode="after")
    def _validate_disjoint_keys(self) -> PayloadContract:
        overlap = sorted(set(self.required_keys) & set(self.optional_keys))
        if overlap:
            raise ValueError(f"contract keys cannot be both required and optional: {', '.join(overlap)}")
        return self

    def validate_payload(self, payload: Mapping[str, Any], *, label: str) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError(f"{label} must be a mapping")
        values = dict(payload)
        missing = sorted(set(self.required_keys) - values.keys())
        if missing:
            raise ValueError(f"{label} is missing required fields: {', '.join(missing)}")
        if not self.allow_extra:
            unknown = sorted(values.keys() - set(self.required_keys) - set(self.optional_keys))
            if unknown:
                raise ValueError(f"{label} contains undeclared fields: {', '.join(unknown)}")
        return values


class ArtifactRequirement(BaseModel):
    """An artifact type a Role may require from a completed task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: str
    required: bool = True

    @field_validator("artifact_type")
    @classmethod
    def _validate_artifact_type(cls, value: str) -> str:
        return _identifier(value, "artifact type")


class EvaluationInterface(BaseModel):
    """Stable evaluator identity; RS-2 will provide the replay implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluator_id: str
    version: str = "1.0.0"

    @field_validator("evaluator_id")
    @classmethod
    def _validate_evaluator_id(cls, value: str) -> str:
        return _identifier(value, "evaluator id")

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("version must be semantic-version formatted")
        return value


class RuntimeCompatibility(BaseModel):
    """A Role's declared compatibility with the host-owned extension API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_api_version: str = RUNTIME_ROLE_API_VERSION
    maximum_api_version: str | None = None

    @field_validator("minimum_api_version")
    @classmethod
    def _validate_minimum_api_version(cls, value: str) -> str:
        return _stable_semver(value, "minimum Runtime API version")

    @field_validator("maximum_api_version")
    @classmethod
    def _validate_maximum_api_version(cls, value: str | None) -> str | None:
        return _stable_semver(value, "maximum Runtime API version") if value is not None else None

    @model_validator(mode="after")
    def _validate_version_range(self) -> RuntimeCompatibility:
        if self.maximum_api_version is not None and _semver_key(self.minimum_api_version) >= _semver_key(
            self.maximum_api_version
        ):
            raise ValueError("maximum Runtime API version must be greater than minimum Runtime API version")
        return self

    def supports(self, runtime_api_version: str) -> bool:
        """Return whether a stable host API version falls inside this declaration."""

        version = _stable_semver(runtime_api_version, "Runtime API version")
        version_key = _semver_key(version)
        if version_key < _semver_key(self.minimum_api_version):
            return False
        return self.maximum_api_version is None or version_key < _semver_key(self.maximum_api_version)


class RoleHostConfiguration(BaseModel):
    """Configuration supplied and retained by the host, never by a Role package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    values: dict[str, Any] = Field(default_factory=dict)

    @field_validator("role_id")
    @classmethod
    def _validate_role_id(cls, value: str) -> str:
        return _identifier(value, "host configuration role id")

    def validate_for(self, role: RoleSpec) -> dict[str, Any]:
        if self.role_id != role.id:
            raise ValueError(f"host configuration belongs to role {self.role_id}, not {role.id}")
        return role.host_configuration_contract.validate_payload(
            self.values,
            label=f"role {role.id} host configuration",
        )


class RoleSpec(BaseModel):
    """A code-owned task boundary that reuses the single Task Runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    system_prompt: str
    version: str = "1.0.0"
    description: str | None = None
    task_boundary: str | None = None
    allowed_skill_ids: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ()  # Compatibility view for existing domain packages.
    artifact_requirements: tuple[ArtifactRequirement, ...] = ()
    input_contract: PayloadContract = Field(default_factory=PayloadContract)
    output_contract: PayloadContract = Field(default_factory=PayloadContract)
    runtime_compatibility: RuntimeCompatibility = Field(default_factory=RuntimeCompatibility)
    host_configuration_contract: PayloadContract = Field(default_factory=PayloadContract)
    evaluator: EvaluationInterface | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _identifier(value, "role id")

    @field_validator("title", "system_prompt")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _text(value, "role text")

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("version must be semantic-version formatted")
        return value

    @field_validator("allowed_skill_ids", "artifact_types")
    @classmethod
    def _validate_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("role identifiers must not contain duplicates")
        return tuple(_identifier(value, "role identifier") for value in values)

    @model_validator(mode="before")
    @classmethod
    def _complete_boundary(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        title = values.get("title")
        system_prompt = values.get("system_prompt")
        if not values.get("description") and isinstance(title, str):
            values["description"] = title
        if not values.get("task_boundary") and isinstance(system_prompt, str):
            values["task_boundary"] = system_prompt
        artifacts = values.get("artifact_requirements") or tuple(
            ArtifactRequirement(artifact_type=item) for item in values.get("artifact_types", ())
        )
        if len({item.artifact_type for item in artifacts}) != len(artifacts):
            raise ValueError("artifact requirements must not contain duplicates")
        values["artifact_requirements"] = artifacts
        return values

    @field_validator("description", "task_boundary")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _text(value, "role boundary text") if value is not None else None

    def validate_input(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.input_contract.validate_payload(payload, label=f"role {self.id} input")

    def validate_output(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.output_contract.validate_payload(payload, label=f"role {self.id} output")

    def validate_host_configuration(self, configuration: RoleHostConfiguration) -> dict[str, Any]:
        return configuration.validate_for(self)


class SkillSpec(BaseModel):
    """A portable workflow contract without Runtime safety authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    version: str = "1.0.0"
    triggers: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    input_contract: PayloadContract = Field(default_factory=PayloadContract)
    output_contract: PayloadContract = Field(default_factory=PayloadContract)
    failure_behavior: str = "fail-closed"
    evidence_requirements: tuple[str, ...] = ()
    compatible_role_ids: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _identifier(value, "skill name")

    @field_validator("description", "failure_behavior")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _text(value, "skill text")

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("version must be semantic-version formatted")
        return value

    @field_validator("triggers", "exclusions", "evidence_requirements")
    @classmethod
    def _validate_text_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_text(value, "skill contract text") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("skill contract text must not contain duplicates")
        return normalized

    @field_validator("compatible_role_ids")
    @classmethod
    def _validate_compatible_roles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("compatible role ids must not contain duplicates")
        return tuple(_identifier(value, "compatible role id") for value in values)

    def validate_input(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.input_contract.validate_payload(payload, label=f"skill {self.name} input")

    def validate_output(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.output_contract.validate_payload(payload, label=f"skill {self.name} output")


class SkillVersion(BaseModel):
    """An immutable content-addressed Skill revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_name: str
    version: str
    content_digest: str
    source: str
    status: Literal["candidate", "active", "revoked"] = "candidate"
    compatible_role_ids: tuple[str, ...] = ()

    @field_validator("skill_name")
    @classmethod
    def _validate_skill_name(cls, value: str) -> str:
        return _identifier(value, "skill name")

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("version must be semantic-version formatted")
        return value

    @field_validator("content_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("skill content digest must be a sha256 hex value")
        return value

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        return _text(value, "skill source")

    @field_validator("compatible_role_ids")
    @classmethod
    def _validate_compatible_roles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("compatible role ids must not contain duplicates")
        return tuple(_identifier(value, "compatible role id") for value in values)

    @classmethod
    def from_content(
        cls,
        spec: SkillSpec,
        content: str,
        *,
        source: str,
        status: Literal["candidate", "active", "revoked"],
        compatible_role_ids: tuple[str, ...] = (),
    ) -> SkillVersion:
        return cls(
            skill_name=spec.name,
            version=spec.version,
            content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source=source,
            status=status,
            compatible_role_ids=compatible_role_ids,
        )


class ToolRequest(BaseModel):
    """A role's requested tool call, before Runtime policy evaluates it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_name")
    @classmethod
    def _validate_tool_name(cls, value: str) -> str:
        return _identifier(value.replace("_", "-"), "tool request name").replace("-", "_")


class ToolAllowlist(BaseModel):
    """Host-owned model-visible tool boundary; it declares no safety effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    tool_names: tuple[str, ...] = ()

    @field_validator("role_id")
    @classmethod
    def _validate_role_id(cls, value: str) -> str:
        return _identifier(value, "tool allowlist role id")

    @field_validator("tool_names")
    @classmethod
    def _validate_tool_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("tool allowlist must not contain duplicates")
        return tuple(_identifier(value.replace("_", "-"), "tool allowlist name").replace("-", "_") for value in values)

    def require_allowed(self, request: ToolRequest) -> None:
        if request.tool_name not in self.tool_names:
            raise ValueError(f"tool request is not allowlisted for role {self.role_id}: {request.tool_name}")


@dataclass(frozen=True)
class SkillPackage:
    spec: SkillSpec
    version: SkillVersion
    skill_markdown: str
    root: Path
    approved: bool
    builtin: bool = False

    @property
    def manifest(self) -> SkillSpec:
        """Compatibility name retained while callers migrate to ``spec``."""

        return self.spec


@dataclass(frozen=True)
class RoleRegistration:
    """Immutable, read-only lifecycle view for one registered Role.

    The view intentionally exposes only the Role definition and lifecycle state.
    It does not carry Runtime permissions, Tool effects, scheduling controls, or
    any activation authority.
    """

    role: RoleSpec
    status: Literal["active", "disabled"]


class RoleRegistry:
    """Runtime-owned Role registration with explicit disable semantics."""

    def __init__(
        self,
        definitions: tuple[RoleSpec, ...] = (),
        *,
        runtime_api_version: str = RUNTIME_ROLE_API_VERSION,
    ) -> None:
        self._definitions: dict[str, RoleSpec] = {}
        self._disabled: set[str] = set()
        self.runtime_api_version = _stable_semver(runtime_api_version, "Runtime API version")
        for definition in definitions:
            self.register(definition)

    def register(self, definition: RoleSpec) -> None:
        if definition.id in self._definitions:
            raise ValueError(f"role is already registered: {definition.id}")
        if not definition.runtime_compatibility.supports(self.runtime_api_version):
            raise ValueError(
                f"role is incompatible with Runtime API {self.runtime_api_version}: {definition.id}"
            )
        self._definitions[definition.id] = definition

    def get(self, role_id: str) -> RoleSpec:
        role = self._registered_role(role_id)
        if role_id in self._disabled:
            raise ValueError(f"role is disabled: {role_id}")
        return role

    def disable(self, role_id: str) -> None:
        self._registered_role(role_id)
        self._disabled.add(role_id)

    def enable(self, role_id: str) -> None:
        self._registered_role(role_id)
        self._disabled.discard(role_id)

    def all(self) -> tuple[RoleSpec, ...]:
        return tuple(role for role_id, role in self._definitions.items() if role_id not in self._disabled)

    def registration(self, role_id: str) -> RoleRegistration:
        """Return lifecycle state for a registered Role, including disabled Roles."""

        role = self._registered_role(role_id)
        status: Literal["active", "disabled"] = "disabled" if role_id in self._disabled else "active"
        return RoleRegistration(role=role, status=status)

    def registrations(self) -> tuple[RoleRegistration, ...]:
        """Return every registered Role in registration order as read-only views."""

        return tuple(self.registration(role_id) for role_id in self._definitions)

    def _registered_role(self, role_id: str) -> RoleSpec:
        try:
            return self._definitions[role_id]
        except KeyError as error:
            raise ValueError(f"unknown role: {role_id}") from error


class SkillRegistry:
    """Discover, version, approve, load, and revoke portable Skill packages."""

    def __init__(self, role_registry: RoleRegistry) -> None:
        self.role_registry = role_registry
        self._packages: dict[str, SkillPackage] = {}

    def discover(self, root: Path, *, approved: bool = False, builtin: bool = False) -> tuple[SkillPackage, ...]:
        resolved_root = root.expanduser().resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"skill root does not exist: {root}")
        discovered: list[SkillPackage] = []
        for path in sorted(resolved_root.rglob("SKILL.md")):
            package = self._read_package(path, resolved_root, approved=approved, builtin=builtin)
            self._register_package(package)
            discovered.append(package)
        return tuple(discovered)

    def approve(self, skill_name: str) -> SkillPackage:
        package = self._get_any(skill_name)
        if package.version.status == "revoked":
            raise ValueError(f"revoked skill cannot be approved: {skill_name}")
        approved = self._replace_status(package, "active", approved=True)
        self._packages[skill_name] = approved
        return approved

    def revoke(self, skill_name: str) -> SkillPackage:
        package = self._get_any(skill_name)
        revoked = self._replace_status(package, "revoked", approved=False)
        self._packages[skill_name] = revoked
        return revoked

    def get(self, skill_name: str) -> SkillPackage:
        package = self._get_any(skill_name)
        if not package.approved or package.version.status != "active":
            raise ValueError(f"skill is not active: {skill_name}")
        return package

    def load(self, skill_name: str) -> SkillPackage:
        return self.get(skill_name)

    def allowed_for_role(self, role_id: str) -> tuple[SkillPackage, ...]:
        role = self.role_registry.get(role_id)
        packages: list[SkillPackage] = []
        for name in role.allowed_skill_ids:
            if name not in self._packages:
                continue
            package = self._packages[name]
            if not package.approved or package.version.status != "active":
                continue
            compatible = package.version.compatible_role_ids
            if compatible and role.id not in compatible:
                continue
            packages.append(package)
        return tuple(packages)

    def match(self, role_id: str, request: str) -> tuple[SkillPackage, ...]:
        request_tokens = set(re.findall(r"[a-z0-9]+", request.lower()))

        def score(package: SkillPackage) -> tuple[int, str]:
            tokens = set(re.findall(r"[a-z0-9]+", f"{package.spec.name} {package.spec.description}".lower()))
            return (len(tokens & request_tokens), package.spec.name)

        return tuple(sorted(self.allowed_for_role(role_id), key=score, reverse=True))

    def _get_any(self, skill_name: str) -> SkillPackage:
        try:
            return self._packages[skill_name]
        except KeyError as error:
            raise ValueError(f"unknown skill: {skill_name}") from error

    def _register_package(self, package: SkillPackage) -> None:
        if package.spec.name in self._packages:
            raise ValueError(f"duplicate skill name: {package.spec.name}")
        self._packages[package.spec.name] = package

    @staticmethod
    def _replace_status(
        package: SkillPackage,
        status: Literal["candidate", "active", "revoked"],
        *,
        approved: bool,
    ) -> SkillPackage:
        return SkillPackage(
            spec=package.spec,
            version=package.version.model_copy(update={"status": status}),
            skill_markdown=package.skill_markdown,
            root=package.root,
            approved=approved,
            builtin=package.builtin,
        )

    @staticmethod
    def _read_package(path: Path, root: Path, *, approved: bool, builtin: bool) -> SkillPackage:
        resolved_path = path.resolve()
        if path.is_symlink() or not resolved_path.is_file():
            raise ValueError(f"skill manifest must be a regular file: {path}")
        try:
            resolved_path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"skill manifest escapes its discovery root: {path}") from error
        try:
            content = resolved_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"could not read skill manifest {path}: {error}") from error
        spec = parse_skill_spec(content, resolved_path.name)
        return SkillPackage(
            spec=spec,
            version=SkillVersion.from_content(
                spec,
                content,
                source=str(resolved_path),
                status="active" if approved else "candidate",
                compatible_role_ids=spec.compatible_role_ids,
            ),
            skill_markdown=content,
            root=resolved_path.parent,
            approved=approved,
            builtin=builtin,
        )


def parse_skill_spec(content: str, label: str = "SKILL.md") -> SkillSpec:
    """Parse a Skill frontmatter contract and reject Runtime safety fields."""

    lines = content.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_BOUNDARY:
        raise ValueError(f"skill manifest {label} must start with YAML frontmatter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == _FRONTMATTER_BOUNDARY)
    except StopIteration as error:
        raise ValueError(f"skill manifest {label} has unterminated YAML frontmatter") from error
    try:
        parsed = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as error:
        raise ValueError(f"skill manifest {label} has invalid YAML frontmatter: {error}") from error
    if not isinstance(parsed, dict):
        raise TypeError(f"skill manifest {label} frontmatter must be a mapping")
    forbidden = sorted(str(key) for key in parsed if str(key).lower() in _FORBIDDEN_SKILL_FIELDS)
    if forbidden:
        raise ValueError(f"skill manifest {label} cannot declare runtime safety fields: {', '.join(forbidden)}")
    try:
        return SkillSpec.model_validate(parsed)
    except ValueError as error:
        raise ValueError(f"skill manifest {label} is invalid: {error}") from error


class RuntimeRoleTarget(Protocol):
    def activate_role(self, role_id: str, role_context: str, *, tool_names: tuple[str, ...] = ()) -> None: ...


@dataclass(frozen=True)
class RoleActivation:
    role: RoleSpec
    skills: tuple[SkillPackage, ...]
    tool_names: tuple[str, ...]


class RoleSkillActivator:
    """Validate a Role activation before handing it to the existing Task Runtime."""

    def __init__(
        self,
        roles: RoleRegistry,
        skills: SkillRegistry,
        tool_allowlists: tuple[ToolAllowlist, ...] = (),
    ) -> None:
        self.roles = roles
        self.skills = skills
        self._allowlists = {item.role_id: item for item in tool_allowlists}

    def activate(
        self,
        runtime: RuntimeRoleTarget,
        *,
        role_id: str,
        role_context: str,
        skill_names: tuple[str, ...] = (),
        tool_requests: tuple[ToolRequest, ...] = (),
        role_input: Mapping[str, Any] | None = None,
        host_configuration: RoleHostConfiguration | None = None,
    ) -> RoleActivation:
        role = self.roles.get(role_id)
        if role_input is not None:
            role.validate_input(role_input)
        if host_configuration is not None:
            role.validate_host_configuration(host_configuration)
        requested = skill_names or role.allowed_skill_ids
        if len(requested) != len(set(requested)):
            raise ValueError("activation skill names must not contain duplicates")
        if any(name not in role.allowed_skill_ids for name in requested):
            raise ValueError(f"skill is not allowlisted for role {role.id}")
        packages = tuple(self.skills.load(name) for name in requested)
        allowlist = self._allowlists.get(role.id, ToolAllowlist(role_id=role.id))
        for request in tool_requests:
            allowlist.require_allowed(request)
        runtime.activate_role(role.id, role_context, tool_names=allowlist.tool_names)
        return RoleActivation(role=role, skills=packages, tool_names=allowlist.tool_names)

    def validate_outputs(
        self,
        activation: RoleActivation,
        *,
        role_output: Mapping[str, Any],
        skill_outputs: Mapping[str, Mapping[str, Any]] = {},
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Validate declared result shapes without writing Session facts."""

        validated_role = activation.role.validate_output(role_output)
        packages = {package.spec.name: package for package in activation.skills}
        unknown = sorted(set(skill_outputs) - packages.keys())
        if unknown:
            raise ValueError(f"output supplied for inactive skill: {', '.join(unknown)}")
        validated_skills = {
            name: packages[name].spec.validate_output(payload)
            for name, payload in skill_outputs.items()
        }
        return validated_role, validated_skills
