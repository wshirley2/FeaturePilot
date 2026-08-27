"""Startup safety check and local trust registry for interactive Chat workspaces.

The registry only remembers that the user approved opening a workspace.  It
does not grant tool permissions or replace the existing Permission/Trusted
Diff flow used after the Runtime has started.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

InputFn = Callable[[str], str]
_TRUST_FILE_VERSION = 1


class WorkspaceTrustStore:
    """Persist approved workspace roots in the current user's config area."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or (Path.home() / ".featurepilot" / "trusted_workspaces.json")).expanduser()

    @staticmethod
    def _key(repository: Path) -> str:
        return os.path.normcase(str(repository.expanduser().resolve()))

    def is_trusted(self, repository: Path) -> bool:
        return self._key(repository) in self._read()

    def trust(self, repository: Path) -> bool:
        """Remember a workspace, returning False if the registry is unavailable."""

        trusted = self._read()
        trusted.add(self._key(repository))
        payload = {
            "version": _TRUST_FILE_VERSION,
            "trusted_workspaces": sorted(trusted),
        }
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
        return True

    def _read(self) -> set[str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _TRUST_FILE_VERSION:
                return set()
            values = payload.get("trusted_workspaces", [])
            if not isinstance(values, list):
                return set()
            return {value for value in values if isinstance(value, str)}
        except (OSError, json.JSONDecodeError, TypeError):
            # A missing, unreadable, or corrupt registry must fail closed and
            # show the user the trust prompt again.
            return set()


def confirm_workspace_access(
    repository: Path,
    *,
    console: Console | None = None,
    input_fn: InputFn = input,
    trust_store: WorkspaceTrustStore | None = None,
) -> bool:
    """Ask whether the user trusts ``repository`` for an interactive Chat.

    Existing approvals return immediately.  Otherwise, the default (empty)
    response confirms, matching Claude Code's ``Enter to confirm`` flow.
    ``1`` is an explicit confirmation; ``2``, ``n``, ``no`` and escape cancel
    the launch.  Invalid input is re-prompted.
    """

    console = console or Console()
    path = repository.expanduser().resolve()
    trust_store = trust_store or WorkspaceTrustStore()
    if trust_store.is_trusted(path):
        return True
    # Keep the exact path outside the panel so narrow terminals fold it onto
    # multiple lines instead of replacing the middle with an ellipsis.
    console.print("[bold white]Accessing workspace:[/bold white]")
    console.print(Text(str(path), style="cyan", overflow="fold"))
    body = Text()
    body.append("Quick safety check: Is this a project you created or one you trust?\n", style="white")
    body.append(
        "FeaturePilot will be able to read, edit, and execute files here.",
        style="white",
    )
    console.print(
        Panel(
            body,
            title="[bold green]Workspace trust[/bold green]",
            border_style="green",
            padding=(1, 2),
            expand=False,
        )
    )
    console.print("[bold green]❯ 1.[/bold green] Yes, I trust this folder")
    console.print("  [dim]2.[/dim] No, exit")
    console.print("[dim]Enter to confirm · Esc to cancel[/dim]")

    while True:
        try:
            answer = input_fn("")
        except (EOFError, KeyboardInterrupt):
            return False
        normalized = answer.strip().casefold()
        if normalized in {"", "1", "y", "yes"}:
            # The trust prompt is rendered in the normal terminal before the
            # TUI starts. Clear only a real terminal so that the next screen
            # begins with the FeaturePilot welcome panel; StringIO-based
            # tests and redirected output keep their captured transcript.
            if console.is_terminal:
                console.clear()
            if not trust_store.trust(path):
                console.print("[yellow]Could not save workspace trust; this prompt may appear again next time.[/yellow]")
            return True
        if normalized in {"2", "n", "no", "esc", "escape", "\x1b"}:
            return False
        console.print("[yellow]Please choose 1 to continue or 2 to exit.[/yellow]")
