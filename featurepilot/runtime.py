"""Central assembly for FeaturePilot Chat and future Managed Runs."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from corecoder.agent import Agent, ToolExecutor
from corecoder.config import Config
from corecoder.events import EventSink
from corecoder.llm import LLM, LiteLLM
from corecoder.permissions import DenyPermissionPrompt, PermissionManager, PermissionPrompt
from corecoder.runtime_control import CancellationToken, RuntimeLimits
from corecoder.tools import get_tool
from corecoder.tools.base import Tool

from .chat_executor import RepositoryToolExecutor
from .permissions import ChatPermissionPolicy
from .repository import RepositoryProfiler
from .repository.profiler import RepositoryProfile
from .runtime_contracts import (
    RuntimeMode,
    RuntimeResultScope,
    RuntimeResultStatus,
    TaskRuntimeIdentity,
    TaskRuntimePaths,
    TaskRuntimeResult,
)
from .sessions import SessionEventSink, SessionProjection, SessionStore

ProviderFactory = Callable[[Config], object]
_CHAT_TOOL_NAMES = ("read_file", "glob", "grep", "edit_file", "write_file", "bash", "now")


@dataclass(frozen=True)
class RuntimeBootstrapInput:
    repository: Path
    event_sink: EventSink
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    tool_executor: ToolExecutor | None = None
    tools: list[Tool] | None = None
    system_context: str | None = None
    permission_mode: str | None = None
    permission_prompt: PermissionPrompt | None = None
    session_directory: Path | None = None
    resume_session_id: str | None = None
    limits: RuntimeLimits | None = None
    mode: RuntimeMode = RuntimeMode.CHAT
    task_id: str | None = None
    run_id: str | None = None
    source_repository: Path | None = None
    paths: TaskRuntimePaths | None = None


@dataclass
class TaskRuntime:
    identity: TaskRuntimeIdentity
    paths: TaskRuntimePaths
    repository: Path
    config: Config
    agent: Agent
    tools: list[Tool]
    profile: RepositoryProfile | None
    profile_warning: str | None
    permission_mode: str = "repository reads allowed; writes and commands policy-gated"
    permission_manager: PermissionManager | None = None
    session_store: SessionStore | None = None
    session_sink: SessionEventSink | None = None
    base_system_context: str = ""
    pending_isolation_requests: list[dict[str, object]] = field(default_factory=list)

    @property
    def runtime_mode(self) -> RuntimeMode:
        return self.identity.mode

    @property
    def mode(self) -> str:
        """Compatibility display label retained for existing CLI integrations."""

        return self.identity.mode.display_name

    @property
    def task_id(self) -> str | None:
        return self.identity.task_id

    @property
    def run_id(self) -> str | None:
        return self.identity.run_id

    @property
    def last_result(self) -> TaskRuntimeResult | None:
        return self.session_sink.last_result if self.session_sink is not None else None

    @property
    def session_path(self) -> Path | None:
        if self.session_store is None:
            return None
        return self.session_store.path_for(self.identity.session_id)

    def record_result(self, result: TaskRuntimeResult) -> None:
        """Record a complete Runtime result after orchestration-level work finishes."""

        if self.session_sink is not None:
            self.session_sink.record_result(self.identity.session_id, result)

    def run_turn(
        self,
        user_input: str,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> str:
        """Run one controlled Agent turn through the shared Runtime boundary."""

        response = self.agent.chat(user_input, cancellation_token=cancellation_token)
        take_pending = getattr(self.agent.tool_executor, "take_pending_isolation_request", None)
        pending = take_pending() if callable(take_pending) else None
        if isinstance(pending, dict):
            pending["user_request"] = user_input
            self.pending_isolation_requests.append(pending)
            if self.session_sink is not None:
                self.session_sink.record("isolation_pending", self.agent.session_id, pending)
            self.record_result(TaskRuntimeResult(
                scope=RuntimeResultScope.TURN,
                status=RuntimeResultStatus.ESCALATION_REQUIRED,
                response=response,
                reason=str(pending.get("message") or "Execution requires isolation"),
            ))
        return response

    def record_isolation_event(
        self,
        event_type: str,
        tool_call_id: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Persist a user decision about one pending Chat isolation request."""

        if self.session_sink is not None:
            self.session_sink.record(event_type, self.agent.session_id, {
                "tool_call_id": tool_call_id,
                **dict(payload or {}),
            })

    def resolve_pending_isolation(self, tool_call_id: str) -> None:
        """Remove only an accepted or cancelled request from the live projection."""

        self.pending_isolation_requests = [
            item for item in self.pending_isolation_requests
            if item.get("tool_call_id") != tool_call_id
        ]

    def ensure_persisted(self) -> None:
        """Require the latest Runtime and Session facts to be durable."""

        if self.session_sink is not None:
            self.session_sink.ensure_persisted()

    def set_model(self, model: str, *, record: bool = True) -> None:
        """Update both the provider configuration and model-visible runtime facts."""

        self.config.model = model
        if hasattr(self.agent.llm, "model"):
            self.agent.llm.model = model
        self.agent.update_system_context(_with_runtime_identity(self.base_system_context, self.mode, model))
        if record and self.session_sink is not None:
            self.session_sink.record("session_model_changed", self.agent.session_id, {"model": model})

    def resume_session(self, session_id: str) -> SessionProjection:
        """Load a Session projection without replaying prior filesystem effects."""

        if self.session_store is None:
            raise RuntimeError("Event-based Session storage is not configured")
        projection = self.session_store.replay(session_id)
        if projection.repository_root is not None and projection.repository_root != self.repository:
            raise ValueError("Session belongs to a different repository")
        if projection.mode != self.identity.mode.value:
            raise ValueError("Session belongs to a different Runtime mode")
        self.agent.session_id = projection.session_id
        self.agent.messages = projection.model_messages
        self.identity = TaskRuntimeIdentity(
            mode=self.identity.mode,
            session_id=projection.session_id,
            task_id=projection.task_id,
            run_id=projection.run_id,
            source_repository=(projection.source_repository_root or self.identity.source_repository),
            working_directory=self.repository,
        )
        if projection.model:
            self.set_model(projection.model, record=False)
        if self.permission_manager is not None:
            # Grants are intentionally process-local. A resumed session always
            # reuses C3's Trusted Diff and fresh approval path.
            self.permission_manager.clear_session_grants()
        set_task_id = getattr(self.agent.tool_executor, "set_task_id", None)
        if callable(set_task_id):
            set_task_id(projection.task_id)
        if self.session_sink is not None:
            self.session_sink.last_result = projection.last_result
            self.session_sink.record("session_resumed", projection.session_id, {
                "repository_root": str(self.repository),
                "event_count": len(projection.events),
                "warnings": projection.warnings,
            })
        self.pending_isolation_requests = list(projection.pending_isolation_requests)
        return projection


# Compatibility alias for integrations written before the unified Task Runtime contract.
ChatRuntime = TaskRuntime


class RuntimeBootstrap:
    """Build the complete runtime once so CLI commands do not duplicate it."""

    def __init__(
        self,
        *,
        provider_factory: ProviderFactory | None = None,
        profiler: RepositoryProfiler | None = None,
    ):
        self.provider_factory = provider_factory
        self.profiler = profiler or RepositoryProfiler()

    def build(self, inputs: RuntimeBootstrapInput) -> TaskRuntime:
        repository = inputs.repository.resolve()
        if not repository.is_dir():
            raise ValueError(f"Repository directory does not exist: {inputs.repository}")
        mode = RuntimeMode(inputs.mode)
        paths = inputs.paths or TaskRuntimePaths.for_runtime(
            mode,
            repository,
            inputs.session_directory,
        )
        if inputs.paths is not None and inputs.session_directory is not None:
            raise ValueError("Pass either Runtime paths or a Session directory, not both")
        if paths.mode is not mode or paths.working_directory != repository:
            raise ValueError("Runtime paths do not match the requested mode and working directory")
        source_repository = (inputs.source_repository or repository).resolve()
        if not source_repository.is_dir():
            raise ValueError(f"Source repository directory does not exist: {source_repository}")
        _validate_runtime_scope(mode, inputs.task_id, inputs.run_id)

        config = Config.from_env()
        if inputs.model:
            config.model = inputs.model
        elif featurepilot_model := os.getenv("FEATUREPILOT_MODEL"):
            # FeaturePilot owns the public model setting. Config.from_env()
            # still supplies CORECODER_MODEL as a compatibility fallback.
            config.model = featurepilot_model
        if inputs.base_url:
            config.base_url = inputs.base_url
        if inputs.api_key:
            config.api_key = inputs.api_key

        if self.provider_factory is None and not config.api_key:
            raise ValueError(
                "No API key found. Set OPENAI_API_KEY or pass --api-key."
            )

        profile = None
        profile_warning = None
        try:
            profile = self.profiler.profile(repository)
        except Exception as error:
            profile_warning = f"Repository profile unavailable: {error}"

        tools = (
            inputs.tools
            if inputs.tools is not None
            else [tool for name in _CHAT_TOOL_NAMES if (tool := get_tool(name)) is not None]
        )
        session_store = SessionStore(paths.session_directory)
        resume_projection = None
        if inputs.resume_session_id:
            resume_projection = session_store.replay(inputs.resume_session_id)
            if resume_projection.repository_root is not None and resume_projection.repository_root != repository:
                raise ValueError("Session belongs to a different repository")
            if resume_projection.mode != mode.value:
                raise ValueError("Session belongs to a different Runtime mode")
            if resume_projection.model and not inputs.model:
                config.model = resume_projection.model

        provider = self._build_provider(config)
        repository_context = _repository_summary(profile, profile_warning)
        base_system_context = "\n\n".join(
            part for part in (repository_context, inputs.system_context) if part
        )
        system_context = _with_runtime_identity(base_system_context, mode.display_name, config.model)
        permission_manager = None
        tool_executor = inputs.tool_executor
        if tool_executor is None:
            permission_manager = PermissionManager(
                ChatPermissionPolicy(profile.validation_commands if profile else ()),
                inputs.permission_prompt or DenyPermissionPrompt(),
            )
            tool_executor = RepositoryToolExecutor(repository, permission_manager, task_id=inputs.task_id)
        session_id = resume_projection.session_id if resume_projection is not None else None
        session_sink = SessionEventSink(session_store, inputs.event_sink)
        agent = Agent(
            llm=provider,
            tools=tools,
            max_context_tokens=config.max_context_tokens,
            tool_executor=tool_executor,
            event_sink=session_sink,
            session_id=session_id,
            working_directory=str(repository),
            assistant_name="FeaturePilot",
            system_context=system_context,
            limits=inputs.limits,
        )
        if resume_projection is not None:
            agent.messages = resume_projection.model_messages
        else:
            session_store.create(
                agent.session_id,
                repository_root=repository,
                model=config.model,
                mode=mode.value,
                task_id=inputs.task_id,
                run_id=inputs.run_id,
                source_repository_root=source_repository,
            )
        runtime = TaskRuntime(
            identity=TaskRuntimeIdentity(
                mode=mode,
                session_id=agent.session_id,
                task_id=inputs.task_id,
                run_id=inputs.run_id,
                source_repository=source_repository,
                working_directory=repository,
            ),
            paths=paths,
            repository=repository,
            config=config,
            agent=agent,
            tools=tools,
            profile=profile,
            profile_warning=profile_warning,
            permission_mode=(
                inputs.permission_mode
                or "repository reads allowed; writes and commands policy-gated"
            ),
            permission_manager=permission_manager,
            session_store=session_store,
            session_sink=session_sink,
            base_system_context=base_system_context,
        )
        if resume_projection is not None:
            runtime.resume_session(resume_projection.session_id)
        return runtime

    def _build_provider(self, config: Config):
        if self.provider_factory is not None:
            return self.provider_factory(config)
        provider_class = LiteLLM if config.provider == "litellm" else LLM
        return provider_class(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )


def _repository_summary(profile: RepositoryProfile | None, warning: str | None) -> str:
    if profile is None:
        return warning or "Repository profile is unavailable; inspect files with tools as needed."

    def values(items: list[str]) -> str:
        return ", ".join(items[:8]) or "(none detected)"

    commands = [" ".join(command) for command in profile.validation_commands]
    return "\n".join([
        f"Repository root: {profile.root}",
        f"Language: {profile.language}",
        f"Frameworks: {values(profile.frameworks)}",
        f"Entrypoints: {values(profile.entrypoints)}",
        f"Tests: {values(profile.test_files)}",
        f"Validation commands: {values(commands)}",
        "This is a lightweight profile. Read detailed files only when needed.",
    ])


def _with_runtime_identity(base_context: str, mode: str, model: str) -> str:
    """Inject only runtime facts that the Agent may safely explain to the user."""

    identity = "\n".join([
        "Runtime identity (authoritative facts):",
        "Product: FeaturePilot",
        f"Mode: {mode}",
        f"Current model: {model}",
        "For questions about product, mode, or model, answer from these facts without calling tools.",
        "Do not reveal API keys, endpoints, or other provider secrets.",
    ])
    return "\n\n".join(part for part in (base_context, identity) if part)


def _validate_runtime_scope(mode: RuntimeMode, task_id: str | None, run_id: str | None) -> None:
    """Reject incomplete correlation data before creating Session artifacts."""

    if run_id is not None and task_id is None:
        raise ValueError("A Runtime run id requires a task id")
    if mode is RuntimeMode.MANAGED_RUN and (task_id is None or run_id is None):
        raise ValueError("Managed Run Runtime requires task and run ids")
