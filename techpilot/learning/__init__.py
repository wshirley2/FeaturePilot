"""Learning-domain contracts, local persistence, and approved Skill discovery."""

from .commands import LearningCommandController
from .contracts import (
    DocumentRecord,
    ExtractedText,
    KnowledgeDraft,
    KnowledgeSync,
    LearningGoal,
    LearningPlan,
    LearningProfile,
    LearningStep,
    LearningTask,
    QuizAttempt,
    RoleDefinition,
    SkillCandidate,
    SkillManifest,
    SkillRevision,
    SourceRecord,
    TrendBrief,
)
from .conversation import LearningChoice, LearningConversationController, LearningIntentRouter, LearningRoute, LearningTurn
from .registry import DEVELOPER_LEARNING_COACH, RoleRegistry, SkillPackage, SkillRegistry
from .research import DocumentIngestion, FetchedSource, ResearchConnector, ResearchError, ResearchResult, ResearchService
from .role_runtime import LearningRoleRuntime
from .service import ConfirmedLearningGoal, LearningGoalDraft, LearningService
from .store import LearningStore, learning_data_directory

__all__ = [
    "DEVELOPER_LEARNING_COACH",
    "ConfirmedLearningGoal",
    "DocumentIngestion",
    "DocumentRecord",
    "ExtractedText",
    "FetchedSource",
    "KnowledgeDraft",
    "KnowledgeSync",
    "LearningChoice",
    "LearningCommandController",
    "LearningConversationController",
    "LearningGoal",
    "LearningGoalDraft",
    "LearningIntentRouter",
    "LearningPlan",
    "LearningProfile",
    "LearningRoleRuntime",
    "LearningRoute",
    "LearningService",
    "LearningStep",
    "LearningStore",
    "LearningTask",
    "LearningTurn",
    "QuizAttempt",
    "ResearchConnector",
    "ResearchError",
    "ResearchResult",
    "ResearchService",
    "RoleDefinition",
    "RoleRegistry",
    "SkillCandidate",
    "SkillManifest",
    "SkillPackage",
    "SkillRegistry",
    "SkillRevision",
    "SourceRecord",
    "TrendBrief",
    "learning_data_directory",
]
