"""RichLog that dispatches @click meta actions itself (RichLog skips them)."""

from __future__ import annotations

from textual import events
from textual.widgets import RichLog


class ArtifactRichLog(RichLog):

    def on_click(self, event: events.Click) -> None:
        meta = getattr(event.style, "meta", None) or {}
        action = meta.get("@click")
        if not action:
            return
        event.stop()
        self.app.run_action(action, default_namespace=self.screen)
