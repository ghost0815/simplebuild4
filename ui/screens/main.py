"""Main screen — the primary s4 interface."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Button
from textual.containers import Horizontal, Vertical, VerticalScroll

from core.target import Target
from core.repo import RepoState
from ui.header import S4Header
from ui.footer import S4Footer


class MainScreen(Screen):
    BINDINGS = []

    def compose(self) -> ComposeResult:
        yield S4Header()

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
                yield Button("Edit", id="btn-edit")
                yield Button("Duplicate", id="btn-dup")

            # Info panel
            info = Static(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started.",
                id="info",
            )
            info.border_title = "Settings"
            yield info

            # Output log
            with VerticalScroll(id="output"):
                yield Static("", id="output-text")

        yield S4Footer(active_screen="main")

    def on_mount(self) -> None:
        self.query_one("#output", VerticalScroll).border_title = "Output"

    def _log(self, msg: str) -> None:
        """Append a line to the output panel."""
        out = self.query_one("#output-text", Static)
        current = str(out.renderable) if out.renderable else ""
        new_text = f"{current}\n{msg}" if current else msg
        out.update(new_text)
        self.query_one("#output", VerticalScroll).scroll_end(animate=False)

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

        # Check if source repo still exists
        from core.repos_config import load_repos
        repos = load_repos(self.app.paths.repos_config)
        repo_names = [r.name for r in repos]
        source_parts = []
        for s in target.sources:
            if s in repo_names:
                source_parts.append(s)
            else:
                source_parts.append(f"[red]{s} (not found!)[/red]")
        source_str = ", ".join(source_parts) or "none"
        tc_str = ", ".join(target.toolchains) or "none"

        info = self.query_one("#info", Static)
        info.update(
            f"  [bold]{target.name}[/bold] — {target.description}\n"
            f"  Sources:    {source_str}\n"
            f"  Toolchains: {tc_str}\n"
            f"  Modules:    {modules_short}\n"
            f"  Upload:     {upload_str}\n"
        )

    def on_screen_resume(self) -> None:
        """Refresh target list when returning from another screen."""
        sel = self.query_one("#target-select", Select)
        previous = sel.value if isinstance(sel.value, str) else None
        self._refresh_targets()
        if previous:
            try:
                sel.value = previous
            except Exception:
                pass

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "target-select" and event.value is not None and isinstance(event.value, str):
            from pathlib import Path
            target = Target.load(Path(event.value))
            self._update_info(target)

    def _get_selected_target(self) -> tuple[Target, "Path"] | None:
        """Get currently selected target and its path."""
        from pathlib import Path as P
        sel = self.query_one("#target-select", Select)
        if sel.value is None or not isinstance(sel.value, str):
            return None
        path = P(sel.value)
        return Target.load(path), path

    def _refresh_targets(self) -> None:
        """Reload the target dropdown after changes."""
        sel = self.query_one("#target-select", Select)
        targets = self._load_targets()
        sel.set_options(targets if targets else [("(no targets)", None)])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from ui.screens.target_edit import TargetEditScreen
        btn = event.button.id

        if btn == "btn-edit":
            result = self._get_selected_target()
            if result:
                target, path = result
                self.app.push_screen(TargetEditScreen(target=target, save_path=path))
            else:
                self.notify("No target selected", severity="warning")
        elif btn == "btn-dup":
            result = self._get_selected_target()
            if result:
                target, path = result
                target.name = f"{target.name}_copy"
                target.description = f"Copy of {target.description}"
                self.app.push_screen(TargetEditScreen(target=target))
            else:
                self.notify("No target selected", severity="warning")

    def do_add(self) -> None:
        """F10 Add — create new target."""
        from ui.screens.target_edit import TargetEditScreen
        self.app.push_screen(TargetEditScreen())

    def do_remove(self) -> None:
        """F11 Remove — delete selected target."""
        result = self._get_selected_target()
        if result:
            target, path = result
            path.unlink(missing_ok=True)
            self._refresh_targets()
            self.query_one("#info", Static).update(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started."
            )
            self.notify(f"Target '{target.name}' deleted")
        else:
            self.notify("No target selected", severity="warning")

    def action_build(self) -> None:
        self._log("[bold yellow]BUILD:[/bold yellow] not yet implemented")
