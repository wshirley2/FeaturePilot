"""Command-line entry point for FeaturePilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console

from . import __version__
from .chat import ChatSession, TerminalEventSink
from .domain import PlanRecord, Task
from .domain.task import TASK_TYPES
from .managed import ManagedRunExecutionError, ManagedRunService
from .plan_chat import PlanChatSession
from .planning import PlanningService, PlanStore, PlanValidationError
from .repository import RepositoryProfiler
from .runtime import RuntimeBootstrap, RuntimeBootstrapInput
from .workspace import CopyWorkspaceBackend, WorkspaceService

_PLAN_COMMANDS = {"chat", "create", "list", "show", "approve", "reject", "regenerate"}
_DEFAULT_STORE_DIR = Path(".featurepilot/plans")
_TOP_LEVEL_COMMANDS = {"chat", "run", "status", "profile", "plan", "plans", "workspace"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="featurepilot",
        description="A repo-aware coding agent workspace.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Show the current FeaturePilot capabilities.")

    chat_parser = subparsers.add_parser("chat", help="Start an interactive coding-agent chat in a repository.")
    chat_parser.add_argument("repository", nargs="?", type=Path, default=Path("."), help="Repository directory.")
    chat_parser.add_argument("-m", "--model", help="Override the configured model.")
    chat_parser.add_argument("--base-url", help="Override the OpenAI-compatible API base URL.")
    chat_parser.add_argument("--api-key", help="Override the configured API key.")
    chat_parser.add_argument("--runs-dir", type=Path, default=Path("runs"), help=argparse.SUPPRESS)
    _add_store_directory(chat_parser)

    run_parser = subparsers.add_parser("run", help="Execute an approved Plan in an isolated Workspace.")
    run_parser.add_argument("plan_reference", help="Approved Plan reference, for example json-export-v1.")
    run_parser.add_argument("--runs-dir", type=Path, default=Path("runs"), help="Directory for retained Run workspaces.")
    run_parser.add_argument("-m", "--model", help="Override the configured model.")
    run_parser.add_argument("--base-url", help="Override the OpenAI-compatible API base URL.")
    run_parser.add_argument("--api-key", help="Override the configured API key.")
    _add_store_directory(run_parser)

    profile_parser = subparsers.add_parser("profile", help="Analyze a local repository and print JSON.")
    profile_parser.add_argument("repository", type=Path, help="Path to the local repository to analyze.")
    profile_parser.add_argument("--output", type=Path, help="Also save the JSON profile to this file.")

    plan_parser = subparsers.add_parser(
        "plan",
        aliases=["plans"],
        help="Create, review and decide implementation plans.",
    )
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)

    plan_chat_parser = plan_subparsers.add_parser("chat", help="Create, review and execute a Plan conversationally.")
    plan_chat_parser.add_argument("repository", nargs="?", type=Path, default=Path("."), help="Repository directory.")
    plan_chat_parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs"),
        help="Directory for retained Run workspaces.",
    )
    plan_chat_parser.add_argument("-m", "--model", help="Override the configured model used during execution.")
    plan_chat_parser.add_argument("--base-url", help="Override the OpenAI-compatible API base URL.")
    plan_chat_parser.add_argument("--api-key", help="Override the configured API key.")
    _add_store_directory(plan_chat_parser)

    create_parser = plan_subparsers.add_parser("create", help="Create and save a draft Plan.")
    create_parser.add_argument("repository", type=Path, help="Path to the local repository to analyze.")
    create_parser.add_argument("--task", required=True, help="Task description to turn into a Plan.")
    create_parser.add_argument("--name", help="Friendly name, for example json-export.")
    create_parser.add_argument(
        "--task-type",
        choices=sorted(TASK_TYPES),
        default="feature",
        help="Task type used by the domain model.",
    )
    create_parser.add_argument(
        "--acceptance",
        action="append",
        default=[],
        help="Acceptance criterion; may be provided more than once.",
    )
    create_parser.add_argument("--limit", type=int, default=10, help="Maximum candidate files to include.")
    create_parser.add_argument("--output", type=Path, help="Also save the raw Plan JSON to this file.")
    create_parser.add_argument("--json", action="store_true", help="Print the saved Plan record as JSON.")
    create_parser.add_argument("--store-dir", type=Path, default=_DEFAULT_STORE_DIR, help=argparse.SUPPRESS)
    create_parser.add_argument("--save", action="store_true", help=argparse.SUPPRESS)
    create_parser.add_argument("--task-id", help=argparse.SUPPRESS)

    list_parser = plan_subparsers.add_parser("list", help="List saved Plans.")
    list_parser.add_argument("--repository", type=Path, help="Only list Plans for this repository.")
    list_parser.add_argument("--json", action="store_true", help="Print full records as JSON.")
    _add_store_directory(list_parser)

    show_parser = plan_subparsers.add_parser("show", help="Show one saved Plan.")
    show_parser.add_argument("plan_reference", help="Reference such as json-export-v1.")
    show_parser.add_argument("--json", action="store_true", help="Print the full record as JSON.")
    _add_store_directory(show_parser)

    approve_parser = plan_subparsers.add_parser("approve", help="Approve a draft Plan.")
    approve_parser.add_argument("plan_reference", help="Draft reference such as json-export-v1.")
    approve_parser.add_argument("--json", action="store_true", help="Print the updated record as JSON.")
    _add_store_directory(approve_parser)

    reject_parser = plan_subparsers.add_parser("reject", help="Reject a draft Plan.")
    reject_parser.add_argument("plan_reference", help="Draft reference such as json-export-v1.")
    reject_parser.add_argument("--reason", required=True, help="Why this draft is being rejected.")
    reject_parser.add_argument("--json", action="store_true", help="Print the updated record as JSON.")
    _add_store_directory(reject_parser)

    regenerate_parser = plan_subparsers.add_parser("regenerate", help="Create the next version from a saved Plan.")
    regenerate_parser.add_argument("plan_reference", help="Existing reference such as json-export-v1.")
    regenerate_parser.add_argument("--task", help="Optional revised task description.")
    regenerate_parser.add_argument(
        "--acceptance",
        action="append",
        default=None,
        help="Replace acceptance criteria; may be provided more than once.",
    )
    regenerate_parser.add_argument("--limit", type=int, default=10, help="Maximum candidate files to include.")
    regenerate_parser.add_argument("--output", type=Path, help="Also save the raw Plan JSON to this file.")
    regenerate_parser.add_argument("--json", action="store_true", help="Print the saved Plan record as JSON.")
    _add_store_directory(regenerate_parser)

    workspace_parser = subparsers.add_parser("workspace", help="Create isolated workspaces from approved Plans.")
    workspace_subparsers = workspace_parser.add_subparsers(dest="workspace_command", required=True)
    workspace_create_parser = workspace_subparsers.add_parser(
        "create",
        help="Copy an approved Plan repository into a disposable workspace.",
    )
    workspace_create_parser.add_argument("plan_reference", help="Approved Plan reference, for example json-export-v1.")
    workspace_create_parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs"),
        help="Local directory used for disposable Run workspaces.",
    )
    workspace_create_parser.add_argument("--json", action="store_true", help="Print the Run metadata as JSON.")
    _add_store_directory(workspace_create_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_command(argv))

    if args.command == "status":
        print("FeaturePilot can profile repositories and manage reviewable implementation plans.")
        return 0
    if args.command == "chat":
        return _run_chat(parser, args)
    if args.command == "run":
        return _run_managed(parser, args)
    if args.command == "profile":
        return _run_profile(parser, args)
    if args.command == "workspace":
        return _run_workspace_create(parser, args)
    if args.command not in {"plan", "plans"}:
        parser.print_help()
        return 0

    if args.plan_command == "chat":
        return _run_plan_chat(parser, args)

    if args.plan_command == "create":
        task = Task(
            project_id=str(args.repository.resolve()),
            description=args.task,
            task_type=args.task_type,
            acceptance_criteria=args.acceptance,
            **({"id": args.task_id} if args.task_id else {}),
        )
        return _create_and_save_plan(parser, args, args.repository, task, args.name)

    store = PlanStore(args.store_dir)
    if args.plan_command == "regenerate":
        try:
            source = store.load(args.plan_reference)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        task = _task_for_regeneration(source, args.task, args.acceptance)
        return _create_and_save_plan(parser, args, Path(source.repository), task, source.name or None)

    if args.plan_command == "list" and args.repository and not args.repository.is_dir():
        parser.error(f"Repository directory does not exist: {args.repository}")
    try:
        if args.plan_command == "list":
            records = store.list(repository=args.repository)
            _print_records(records, args.json)
        elif args.plan_command == "show":
            _print_record(store.load(args.plan_reference), args.json, detail=True)
        elif args.plan_command == "approve":
            _print_record(store.approve(args.plan_reference), args.json, message="Plan approved")
        else:
            _print_record(
                store.reject(args.plan_reference, args.reason),
                args.json,
                message="Plan rejected",
            )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


def _add_store_directory(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store-dir", type=Path, default=_DEFAULT_STORE_DIR, help=argparse.SUPPRESS)


def _normalize_legacy_plan_command(argv: list[str] | None) -> list[str]:
    """Keep the former `plan <repository> --task ...` spelling working."""

    values = list(sys.argv[1:] if argv is None else argv)
    if (
        len(values) >= 2
        and values[0] in {"plan", "plans"}
        and values[1] not in _PLAN_COMMANDS
        and not values[1].startswith("-")
    ):
        values.insert(1, "create")
    return values


def _normalize_command(argv: list[str] | None) -> list[str]:
    values = list(sys.argv[1:] if argv is None else argv)
    values = _normalize_legacy_plan_command(values)
    if not values:
        return ["chat", "."]
    if values[0] not in _TOP_LEVEL_COMMANDS and not values[0].startswith("-"):
        return ["chat", *values]
    return values


def _run_chat(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    console = Console()
    sink = TerminalEventSink(console)
    bootstrap = RuntimeBootstrap()
    try:
        runtime = bootstrap.build(RuntimeBootstrapInput(
            repository=args.repository,
            event_sink=sink,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
        ))
    except ValueError as error:
        parser.error(str(error))
    store = PlanStore(args.store_dir)
    plan_session = PlanChatSession(
        runtime.repository,
        planning_service=PlanningService(store),
        plan_store=store,
        managed_service=ManagedRunService(
            plan_store=store,
            workspace_service=WorkspaceService(CopyWorkspaceBackend(args.runs_dir)),
            runtime_bootstrap=bootstrap,
            event_sink=sink,
        ),
        console=console,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    return ChatSession(runtime, console=console, plan_session=plan_session).run()


def _run_managed(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    console = Console()
    sink = TerminalEventSink(console)
    service = ManagedRunService(
        plan_store=PlanStore(args.store_dir),
        workspace_service=WorkspaceService(CopyWorkspaceBackend(args.runs_dir)),
        runtime_bootstrap=RuntimeBootstrap(),
        event_sink=sink,
    )
    try:
        result = service.execute(
            args.plan_reference,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
        )
    except KeyboardInterrupt:
        console.print("[yellow]Managed Run cancelled. Workspace retained.[/yellow]")
        return 130
    except ManagedRunExecutionError as error:
        console.print(f"[red]{error}[/red]")
        console.print(f"Workspace retained at {error.workspace.path}")
        if error.events_path:
            console.print(f"Events: {error.events_path}")
        if error.patch_path:
            console.print(f"Patch: {error.patch_path}")
        if error.report_path:
            console.print(f"Report: {error.report_path}")
        return 1
    except (OSError, ValueError) as error:
        parser.error(str(error))
    except Exception as error:
        console.print(f"[red]Managed Run failed: {error}[/red]")
        console.print("Workspace and run.json were retained for inspection.")
        return 1

    if not sink.last_turn_streamed and result.response:
        console.print(result.response)
    validation_style = "green" if result.validation.status == "passed" else "red"
    console.print(
        f"[{validation_style}]Validation: {result.validation.status}[/{validation_style}] "
        f"({result.validation_path})"
    )
    console.print(f"Events: {result.events_path}")
    console.print(f"Patch: {result.patch_path}")
    console.print(f"Report: {result.report_path}")
    if result.run.status != "succeeded":
        console.print("[red]Managed Run failed validation. Workspace retained.[/red]")
        console.print(f"Run: {result.run.display_id}")
        console.print(f"Workspace: {result.workspace.path}")
        return 1
    console.print("[green]Managed Run succeeded.[/green]")
    console.print(f"Run: {result.run.display_id}")
    console.print(f"Workspace: {result.workspace.path}")
    return 0


def _run_plan_chat(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    console = Console()
    sink = TerminalEventSink(console)
    store = PlanStore(args.store_dir)
    session = PlanChatSession(
        args.repository,
        planning_service=PlanningService(store),
        plan_store=store,
        managed_service=ManagedRunService(
            plan_store=store,
            workspace_service=WorkspaceService(CopyWorkspaceBackend(args.runs_dir)),
            runtime_bootstrap=RuntimeBootstrap(),
            event_sink=sink,
        ),
        console=console,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    try:
        return session.run()
    except (OSError, ValueError) as error:
        parser.error(str(error))


def _run_profile(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not args.repository.is_dir():
        parser.error(f"Repository directory does not exist: {args.repository}")
    profile = RepositoryProfiler().profile(args.repository)
    payload = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(f"{payload}\n", encoding="utf-8")
        except OSError as error:
            parser.error(f"Could not write profile: {error}")
        print(f"Repository profile written to {args.output}")
    else:
        print(payload)
    return 0


def _run_workspace_create(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        record = PlanStore(args.store_dir).load(args.plan_reference)
        run, workspace = WorkspaceService(CopyWorkspaceBackend(args.runs_dir)).create_for_plan(record)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps({"run": run.to_dict(), "workspace": str(workspace.path)}, ensure_ascii=False, indent=2))
    else:
        print("Workspace created.")
        print(f"Plan: {record.reference}")
        print(f"Run: {run.display_id}")
        print(f"Workspace: {workspace.path}")
        print("The original repository has not been modified.")
    return 0


def _create_and_save_plan(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    repository: Path,
    task: Task,
    name: str | None,
) -> int:
    try:
        record = PlanningService(PlanStore(args.store_dir)).create_draft(
            repository,
            task,
            name=name,
            limit=args.limit,
        )
    except PlanValidationError as error:
        if args.json:
            print(json.dumps(
                {"errors": error.result.errors, "warnings": error.result.warnings},
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print("Plan validation failed:\n" + "\n".join(f"- {item}" for item in error.result.errors))
        return 2
    except (OSError, ValueError) as error:
        parser.error(f"Could not save plan: {error}")

    if args.output:
        payload = json.dumps(record.plan.to_dict(), ensure_ascii=False, indent=2)
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(f"{payload}\n", encoding="utf-8")
        except OSError as error:
            parser.error(f"Could not write plan: {error}")
    if args.json:
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Draft Plan created: {record.reference}\n")
        print(_format_plan_detail(record))
        if args.output:
            print(f"\nRaw Plan JSON written to {args.output}")
        print(f"\nNext: featurepilot plan approve {record.reference}")
    return 0


def _task_for_regeneration(
    source: PlanRecord,
    description: str | None,
    acceptance: list[str] | None,
) -> Task:
    stored_task = source.task or Task(
        project_id=source.repository,
        description=source.plan.summary,
        acceptance_criteria=_acceptance_from_legacy_plan(source),
        id=source.plan.task_id,
    )
    return Task(
        project_id=str(Path(source.repository).resolve()),
        description=description or stored_task.description,
        task_type=stored_task.task_type,
        acceptance_criteria=acceptance if acceptance is not None else stored_task.acceptance_criteria,
        id=stored_task.id,
    )


def _acceptance_from_legacy_plan(record: PlanRecord) -> list[str]:
    """Recover acceptance criteria from Plans saved before Task persistence existed."""

    prefix = "验证验收条件："
    return [step.removeprefix(prefix) for step in record.plan.steps if step.startswith(prefix)]


def _print_records(records: list[PlanRecord], as_json: bool) -> None:
    if as_json:
        print(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))
        return
    if not records:
        print("No saved plans.")
        return
    rows = [(record.reference, record.status, record.plan.summary) for record in records]
    width = max(len("REFERENCE"), *(len(reference) for reference, _, _ in rows))
    print(f"{'REFERENCE'.ljust(width)}  STATUS     SUMMARY")
    print(f"{'-' * width}  ---------  -------")
    for reference, status, summary in rows:
        print(f"{reference.ljust(width)}  {status.ljust(9)}  {summary}")


def _print_record(
    record: PlanRecord,
    as_json: bool,
    *,
    detail: bool = False,
    message: str | None = None,
) -> None:
    if as_json:
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return
    if message:
        print(f"{message}: {record.reference}\n")
    print(_format_plan_detail(record) if detail else _format_plan_status(record))


def _format_plan_status(record: PlanRecord) -> str:
    lines = [
        f"Reference: {record.reference}",
        f"Status: {record.status}",
        f"Version: {record.version}",
    ]
    if record.decision_reason:
        lines.append(f"Reason: {record.decision_reason}")
    return "\n".join(lines)


def _format_plan_detail(record: PlanRecord) -> str:
    plan = record.plan
    lines = [
        f"Reference: {record.reference}",
        f"Status: {record.status} (version {record.version})",
        f"Task: {plan.summary}",
        "",
        "Files to read:",
        *_format_items(plan.read_files),
        "",
        "Files to modify:",
        *_format_items(plan.modify_files),
        "",
        "Validation commands:",
        *_format_items([" ".join(command) for command in plan.validation_commands]),
    ]
    if plan.risks:
        lines.extend(["", "Risks:", *_format_items(plan.risks)])
    if record.decision_reason:
        lines.extend(["", f"Decision reason: {record.decision_reason}"])
    return "\n".join(lines)


def _format_items(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- (none)"]


if __name__ == "__main__":
    raise SystemExit(main())
