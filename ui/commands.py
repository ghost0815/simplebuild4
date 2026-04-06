"""Custom command palette entries for s4."""

from __future__ import annotations

from textual.command import Provider, Hit, Hits, DiscoveryHit

S4_COMMANDS = [
    ("Update", "Online update", "action_s4_update"),
    ("Settings", "s4 settings", "action_s4_settings"),
    ("License", "Show license (COPYING)", "action_show_license"),
]


class S4Commands(Provider):
    """s4 commands — appear first in the command palette."""

    async def discover(self) -> Hits:
        for name, help_text, action in S4_COMMANDS:
            yield DiscoveryHit(
                display=name,
                command=self._make_callback(action),
                help=help_text,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, action in S4_COMMANDS:
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
