"""Main screen — the primary s4 interface."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, Select, Button, RichLog
from textual.containers import Horizontal, Vertical

from core.target import Target
from core.repo import RepoState


class MainScreen(Screen):
    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("f5", "build", "Build"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="content"):
            # Target selector
            with Horizontal(id="target-bar"):
                yield Static("  Target:", classes="label")
                targets = self._load_targets()
                yield Select(
                    options=targets if targets else [("(no targets)", None)],
                    id="target-select",
                    prompt="select target",
                )
                yield Button("New", id="btn-new", variant="primary")
                yield Button("Edit", id="btn-edit")
                yield Button("Duplicate", id="btn-dup")
                yield Button("Delete", id="btn-del", variant="error")

            # Info panel
            yield Static(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started.",
                id="info",
            )

            # Action buttons
            with Horizontal(id="action-bar"):
                yield Button("BUILD", id="btn-build", variant="success")
                yield Button("Upload", id="btn-upload")
                yield Button("Log", id="btn-log")

            # Output log
            yield RichLog(highlight=True, markup=True, id="output")

        # Bottom tabs for sub-menus
        with Horizontal(id="tab-bar"):
            yield Button("s4-Settings", id="tab-settings")
            yield Button("Repo", id="tab-repo")
            yield Button("Toolchains", id="tab-toolchains")

        yield Footer()

    def _load_targets(self) -> list[tuple[str, str]]:
        """Load available targets from disk."""
        paths = self.app.paths
        targets = Target.list_targets(paths.targets_dir)
        return [(name, str(path)) for name, path in targets]

    def _update_info(self, target: Target) -> None:
        """Update the info panel with target details."""
        upload_str = ", ".join(f"{u.user}@{u.host}" for u in target.upload) or "none"
        modules_short = " ".join(target.build.modules[:6])
        if len(target.build.modules) > 6:
            modules_short += f" +{len(target.build.modules) - 6}"

        info = self.query_one("#info", Static)
        info.update(
            f"  [bold]{target.name}[/bold] — {target.description}\n"
            f"  Toolchain:  {target.toolchain}\n"
            f"  Modules:    {modules_short}\n"
            f"  Upload:     {upload_str}\n"
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "target-select" and event.value is not None:
            from pathlib import Path
            target = Target.load(Path(event.value))
            self._update_info(target)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id
        log = self.query_one("#output", RichLog)

        if btn == "btn-build":
            log.write("[bold yellow]BUILD:[/bold yellow] not yet implemented")
        elif btn == "btn-upload":
            log.write("[bold cyan]UPLOAD:[/bold cyan] not yet implemented")
        elif btn == "btn-log":
            log.write("[bold]LOG:[/bold] not yet implemented")
        elif btn == "btn-new":
            log.write("[bold green]NEW TARGET:[/bold green] wizard not yet implemented")
        elif btn == "btn-edit":
            log.write("[bold]EDIT:[/bold] not yet implemented")
        elif btn == "btn-dup":
            log.write("[bold]DUPLICATE:[/bold] not yet implemented")
        elif btn == "btn-del":
            log.write("[bold red]DELETE:[/bold red] not yet implemented")
        elif btn == "tab-settings":
            log.write("[bold]s4-Settings:[/bold] not yet implemented")
        elif btn == "tab-repo":
            log.write("[bold]Repo:[/bold] not yet implemented")
        elif btn == "tab-toolchains":
            log.write("[bold]Toolchains:[/bold] not yet implemented")

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_build(self) -> None:
        log = self.query_one("#output", RichLog)
        log.write("[bold yellow]BUILD (F5):[/bold yellow] not yet implemented")
