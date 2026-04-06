"""Custom command palette entries for s4."""

from __future__ import annotations

from textual.command import Provider, Hit, Hits, DiscoveryHit
from translation import t


def _s4_commands():
    """Return translated command list (called at runtime so language is current)."""
    return [
        (t("cmd_update"), t("cmd_update_help"), "action_s4_update"),
        (t("cmd_settings"), t("cmd_settings_help"), "action_s4_settings"),
        (t("cmd_license"), t("cmd_license_help"), "action_show_license"),
    ]


class S4Commands(Provider):
    """s4 commands — appear first in the command palette."""

    async def discover(self) -> Hits:
        for name, help_text, action in _s4_commands():
            yield DiscoveryHit(
                display=name,
                command=self._make_callback(action),
                help=help_text,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, action in _s4_commands():
            score = matcher.match(name)
            if score > 0:
                yield Hit(
                    score=score + 100,
                    match_display=matcher.highlight(name),
                    command=self._make_callback(action),
                    help=help_text,
                )

    def _make_callback(self, action: str):
        async def callback() -> None:
            self.app.call_later(getattr(self.app, action))
        return callback
