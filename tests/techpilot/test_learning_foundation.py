from __future__ import annotations

import json
from pathlib import Path

import pytest

from techpilot.learning import (
    LearningGoal,
    LearningProfile,
    LearningStore,
    RoleDefinition,
    RoleRegistry,
    SkillCandidate,
    SkillRegistry,
    SkillRevision,
    learning_data_directory,
)


def _skill(path: Path, *, name: str, description: str, extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n",
        encoding="utf-8",
    )
    return path


def test_learning_store_uses_user_config_directory_and_round_trips_goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_directory = tmp_path / "user-config"
    monkeypatch.setenv("TECHPILOT_CONFIG_DIR", str(config_directory))
    store = LearningStore()
    goal = LearningGoal(topic="Python concurrency", intended_outcome="Explain structured concurrency")

    path = store.save_goal(goal)

    assert learning_data_directory() == config_directory / "learning"
    assert path == config_directory / "learning" / "goals" / f"{goal.id}.json"
    assert store.load_goal(goal.id) == goal
    assert not (tmp_path / ".techpilot" / "learning").exists()


def test_learning_store_rejects_damaged_goal_data(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning")
    goal = LearningGoal(topic="Python")
    path = store.save_goal(goal)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="saved goals record .* is invalid"):
        store.load_goal(goal.id)


def test_learning_contracts_reject_unknown_schema_and_generic_store_round_trips_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported learning schema version"):
        LearningGoal(topic="Python", schema_version=2)

    store = LearningStore(tmp_path / "learning")
    profile = LearningProfile(baseline_notes="Comfortable with Python", weekly_minutes=120)
    path = store.save_record(profile)

    assert path == tmp_path / "learning" / "profiles" / f"{profile.id}.json"
    assert store.load_record(LearningProfile, profile.id) == profile


def test_candidate_isolated_from_active_skill_until_persisted_revision(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning")
    candidate = SkillCandidate(
        skill_name="developer-learning",
        skill_markdown="---\nname: developer-learning\ndescription: Candidate\n---\n",
        suggested_action="improve",
        evidence_ids=("a" * 32, "b" * 32),
    )

    candidate_path = store.save_candidate(candidate)

    assert candidate_path.exists()
    assert not store.active_skill_directory("developer-learning").exists()

    revision = SkillRevision(
        skill_name="developer-learning",
        version=1,
        skill_markdown=candidate.skill_markdown,
        source_candidate_id=candidate.id,
    )
    with pytest.raises(ValueError, match="must be persisted"):
        store.activate_revision(revision)

    store.save_revision(revision)
    active_path = store.activate_revision(revision)

    assert active_path.read_text(encoding="utf-8") == candidate.skill_markdown
    metadata = json.loads((active_path.parent / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_candidate_id"] == candidate.id


def test_revision_cannot_activate_unvalidated_or_replaced_skill_content(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning")
    unsafe = SkillRevision(
        skill_name="developer-learning",
        version=1,
        skill_markdown="---\nname: developer-learning\ndescription: Unsafe\neffect: safe\n---\n",
    )
    with pytest.raises(ValueError, match="cannot declare runtime safety fields"):
        store.save_revision(unsafe)

    revision = SkillRevision(
        skill_name="developer-learning",
        version=2,
        skill_markdown="---\nname: developer-learning\ndescription: Approved\n---\n",
    )
    store.save_revision(revision)
    replaced = revision.model_copy(update={"skill_markdown": "---\nname: developer-learning\ndescription: Replaced\n---\n"})
    with pytest.raises(ValueError, match="only the persisted"):
        store.activate_revision(replaced)


def test_builtin_role_only_exposes_its_allowlisted_builtin_skill() -> None:
    roles = RoleRegistry.with_builtin_roles()
    skills = SkillRegistry.with_builtin_skills(roles)

    available = skills.allowed_for_role("developer-learning-coach")

    assert [package.manifest.name for package in available] == ["developer-learning"]
    assert skills.match("developer-learning-coach", "I want to learn Python async")[0].manifest.name == "developer-learning"


def test_unapproved_skill_is_not_available_to_a_role(tmp_path: Path) -> None:
    roles = RoleRegistry(
        (
            RoleDefinition(
                id="test-role",
                title="Test role",
                system_prompt="Use approved Skills only.",
                allowed_skill_ids=("local-skill",),
            ),
        )
    )
    skills = SkillRegistry(roles)
    _skill(tmp_path / "nested" / "local" / "SKILL.md", name="local-skill", description="A local Skill")

    skills.discover(tmp_path)

    assert skills.allowed_for_role("test-role") == ()
    skills.approve("local-skill")
    assert [package.manifest.name for package in skills.allowed_for_role("test-role")] == ["local-skill"]


def test_skill_registry_rejects_runtime_safety_frontmatter(tmp_path: Path) -> None:
    roles = RoleRegistry.with_builtin_roles()
    skills = SkillRegistry(roles)
    _skill(
        tmp_path / "unsafe" / "SKILL.md",
        name="unsafe-skill",
        description="Unsafe metadata",
        extra="effect: safe\nconcurrency: parallel\n",
    )

    with pytest.raises(ValueError, match="cannot declare runtime safety fields: concurrency, effect"):
        skills.discover(tmp_path, approved=True)


def test_skill_registry_rejects_symlink_manifest_outside_discovery_root(tmp_path: Path) -> None:
    outside = _skill(tmp_path / "outside" / "SKILL.md", name="outside-skill", description="Outside")
    root = tmp_path / "root"
    root.mkdir()
    linked = root / "SKILL.md"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("current environment cannot create symlinks")
    skills = SkillRegistry(RoleRegistry.with_builtin_roles())

    with pytest.raises(ValueError, match="regular file"):
        skills.discover(root, approved=True)
