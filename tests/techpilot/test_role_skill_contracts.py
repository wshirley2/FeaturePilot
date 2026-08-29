from __future__ import annotations

from pathlib import Path

import pytest

from techpilot.runtime import (
    ArtifactRequirement,
    PayloadContract,
    RoleHostConfiguration,
    RoleRegistration,
    RoleRegistry,
    RoleSkillActivator,
    RoleSpec,
    RuntimeCompatibility,
    SkillRegistry,
    SkillSpec,
    ToolAllowlist,
    ToolRequest,
)
from techpilot.runtime.extensions import parse_skill_spec


class _RecordingRuntime:
    def __init__(self) -> None:
        self.activations: list[tuple[str, str, tuple[str, ...]]] = []

    def activate_role(self, role_id: str, role_context: str, *, tool_names: tuple[str, ...] = ()) -> None:
        self.activations.append((role_id, role_context, tool_names))


def _write_skill(root: Path) -> None:
    skill_path = root / "inspect-logs" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: inspect-logs
description: Inspect an incident log excerpt and report evidence.
version: 1.2.0
triggers: [incident, logs]
compatible_role_ids: [incident-response]
input_contract:
  schema_id: log-request-v1
  required_keys: [ticket-id]
output_contract:
  schema_id: log-result-v1
  required_keys: [finding]
evidence_requirements: [log-line]
---

# Inspect Logs
""",
        encoding="utf-8",
    )


def _incident_role() -> RoleSpec:
    return RoleSpec(
        id="incident-response",
        title="Incident response",
        system_prompt="Inspect evidence and summarize only verified facts.",
        version="2.0.0",
        task_boundary="Analyze an incident without changing production state.",
        allowed_skill_ids=("inspect-logs",),
        artifact_requirements=(ArtifactRequirement(artifact_type="incident-summary"),),
        input_contract=PayloadContract(schema_id="incident-input-v1", required_keys=("ticket-id",)),
        output_contract=PayloadContract(schema_id="incident-output-v1", required_keys=("summary",)),
    )


def test_domain_neutral_role_and_skill_contracts_validate_boundaries(tmp_path: Path) -> None:
    roles = RoleRegistry((_incident_role(),))
    skills = SkillRegistry(roles)
    _write_skill(tmp_path)
    package = skills.discover(tmp_path, approved=True)[0]

    assert package.spec.name == "inspect-logs"
    assert package.version.version == "1.2.0"
    assert package.version.status == "active"
    assert len(package.version.content_digest) == 64
    assert package.version.compatible_role_ids == ("incident-response",)
    assert package.spec.validate_input({"ticket-id": "INC-42"}) == {"ticket-id": "INC-42"}

    with pytest.raises(ValueError, match="frozen"):
        package.version.status = "revoked"  # type: ignore[misc]

    with pytest.raises(ValueError, match="missing required fields: ticket-id"):
        _incident_role().validate_input({})
    with pytest.raises(ValueError, match="undeclared fields: unverified"):
        package.spec.validate_output({"finding": "timeout", "unverified": True})


def test_activation_rejects_disabled_unapproved_and_tool_overreach(tmp_path: Path) -> None:
    role = _incident_role()
    roles = RoleRegistry((role,))
    skills = SkillRegistry(roles)
    _write_skill(tmp_path)
    skills.discover(tmp_path)
    activator = RoleSkillActivator(
        roles,
        skills,
        (ToolAllowlist(role_id=role.id, tool_names=("read_log",)),),
    )
    runtime = _RecordingRuntime()

    with pytest.raises(ValueError, match="skill is not active: inspect-logs"):
        activator.activate(runtime, role_id=role.id, role_context="incident", skill_names=("inspect-logs",))
    assert runtime.activations == []

    skills.approve("inspect-logs")
    with pytest.raises(ValueError, match="not allowlisted"):
        activator.activate(
            runtime,
            role_id=role.id,
            role_context="incident",
            skill_names=("inspect-logs",),
            tool_requests=(ToolRequest(tool_name="write_file"),),
        )
    assert runtime.activations == []

    activation = activator.activate(
        runtime,
        role_id=role.id,
        role_context="incident",
        skill_names=("inspect-logs",),
        role_input={"ticket-id": "INC-42"},
        tool_requests=(ToolRequest(tool_name="read_log"),),
    )
    assert runtime.activations == [("incident-response", "incident", ("read_log",))]
    assert activator.validate_outputs(
        activation,
        role_output={"summary": "Timeout is verified."},
        skill_outputs={"inspect-logs": {"finding": "timeout"}},
    ) == (
        {"summary": "Timeout is verified."},
        {"inspect-logs": {"finding": "timeout"}},
    )

    skills.revoke("inspect-logs")
    with pytest.raises(ValueError, match="skill is not active: inspect-logs"):
        activator.activate(runtime, role_id=role.id, role_context="incident", skill_names=("inspect-logs",))

    roles.disable(role.id)
    with pytest.raises(ValueError, match="role is disabled"):
        activator.activate(runtime, role_id=role.id, role_context="incident")


def test_role_registry_exposes_immutable_lifecycle_views_without_changing_activation_lookup() -> None:
    incident = _incident_role()
    audit = RoleSpec(
        id="audit-review",
        title="Audit review",
        system_prompt="Review supplied evidence.",
        version="1.0.0",
        task_boundary="Summarize evidence without changing state.",
    )
    roles = RoleRegistry((incident, audit))

    assert roles.registrations() == (
        RoleRegistration(role=incident, status="active"),
        RoleRegistration(role=audit, status="active"),
    )
    assert roles.all() == (incident, audit)

    roles.disable(incident.id)
    roles.disable(incident.id)

    assert roles.registration(incident.id) == RoleRegistration(role=incident, status="disabled")
    assert roles.registrations() == (
        RoleRegistration(role=incident, status="disabled"),
        RoleRegistration(role=audit, status="active"),
    )
    assert roles.all() == (audit,)
    with pytest.raises(ValueError, match="role is disabled"):
        roles.get(incident.id)

    roles.enable(incident.id)
    roles.enable(incident.id)

    assert roles.registration(incident.id) == RoleRegistration(role=incident, status="active")
    assert roles.get(incident.id) is incident


def test_role_registry_rejects_unknown_lifecycle_queries_and_duplicate_registration() -> None:
    role = _incident_role()
    roles = RoleRegistry((role,))

    with pytest.raises(ValueError, match="role is already registered"):
        roles.register(role)
    for action in (roles.registration, roles.disable, roles.enable):
        with pytest.raises(ValueError, match="unknown role: missing-role"):
            action("missing-role")


def test_role_registry_rejects_incompatible_runtime_api_versions() -> None:
    compatible = _incident_role().model_copy(
        update={"runtime_compatibility": RuntimeCompatibility(minimum_api_version="1.0.0", maximum_api_version="2.0.0")}
    )
    assert RoleRegistry((compatible,), runtime_api_version="1.5.0").get(compatible.id) is compatible

    with pytest.raises(ValueError, match="role is incompatible with Runtime API 2.0.0"):
        RoleRegistry((compatible,), runtime_api_version="2.0.0")
    with pytest.raises(ValueError, match="stable semantic-version"):
        RoleRegistry(runtime_api_version="1.0.0-rc.1")
    with pytest.raises(ValueError, match="greater than minimum"):
        RuntimeCompatibility(minimum_api_version="1.0.0", maximum_api_version="1.0.0")


def test_host_configuration_is_validated_before_activation_and_cannot_change_tool_allowlist(tmp_path: Path) -> None:
    role = _incident_role().model_copy(
        update={"host_configuration_contract": PayloadContract(schema_id="incident-config-v1", required_keys=("region",))}
    )
    roles = RoleRegistry((role,))
    skills = SkillRegistry(roles)
    _write_skill(tmp_path)
    skills.discover(tmp_path, approved=True)
    activator = RoleSkillActivator(
        roles,
        skills,
        (ToolAllowlist(role_id=role.id, tool_names=("read_log",)),),
    )
    runtime = _RecordingRuntime()

    with pytest.raises(ValueError, match="missing required fields: region"):
        activator.activate(
            runtime,
            role_id=role.id,
            role_context="incident",
            host_configuration=RoleHostConfiguration(role_id=role.id),
        )
    with pytest.raises(ValueError, match="belongs to role audit-review"):
        activator.activate(
            runtime,
            role_id=role.id,
            role_context="incident",
            host_configuration=RoleHostConfiguration(role_id="audit-review", values={"region": "cn"}),
        )
    assert runtime.activations == []

    activation = activator.activate(
        runtime,
        role_id=role.id,
        role_context="incident",
        host_configuration=RoleHostConfiguration(role_id=role.id, values={"region": "cn"}),
    )

    assert activation.tool_names == ("read_log",)
    assert runtime.activations == [(role.id, "incident", ("read_log",))]


def test_skill_contract_cannot_declare_runtime_safety_authority() -> None:
    unsafe = """---
name: unsafe-skill
description: This must not own Runtime policy.
effect: write
permissions: [network]
---
"""

    with pytest.raises(ValueError, match="runtime safety fields: effect, permissions"):
        parse_skill_spec(unsafe)
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        SkillSpec(name="safe-skill", description="No runtime authority.", tools=("shell",))
