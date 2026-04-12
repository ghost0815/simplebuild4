"""Custom command palette provider for s4.

Replaces Textual's default SystemCommandsProvider so we have full control
over the ordering of commands. Without this, commands appeared in random
order on each start because COMMANDS is a set.
"""

from __future__ import annotations

from textual.command import Provider, Hit, Hits, DiscoveryHit
from translation import t


# System command name remapping (built-in english name → translation key)
# Commands not in this map are filtered out (e.g. Screenshot, Maximize/Minimize)
_SYSTEM_RENAMES = {
    "Keys": ("cmd_shortcuts", "cmd_shortcuts_help"),
    "Theme": ("cmd_theme", "cmd_theme_help"),
    "Quit": ("cmd_quit", "cmd_quit_help"),
}


class S4Commands(Provider):
    """Unified command provider with deterministic ordering.

    Order:
      1. Update      (s4)
      2. Settings    (s4)
      3. Shortcuts   (system "Keys")
      4. License     (s4)
      5. Theme       (system)
      6. Quit        (system)

    Hidden: Screenshot, Maximize, Minimize.
    """

    def _make_app_callback(self, action: str):
        async def callback() -> None:
            self.app.call_later(getattr(self.app, action))
        return callback

    def _build_command_list(self):
        """Build the unified command list in fixed order.

        Returns list of (display, help, callback) tuples.
        """
        # Collect available system commands by english title
        system_by_name = {}
        try:
            for cmd in self.app.get_system_commands(self.screen):
                if getattr(cmd, "discover", True):
                    system_by_name[cmd.title] = cmd
        except Exception:
            pass

        commands = []

        # 1. Update (s4)
        commands.append((t("cmd_update"), t("cmd_update_help"),
                         self._make_app_callback("action_s4_update_modal")))
        # 2. Check Dependencies (s4)
        commands.append((t("cmd_check_deps"), t("cmd_check_deps_help"),
                         self._make_app_callback("action_check_deps")))
        # 3. Rescan Toolchains (s4)
        commands.append((t("cmd_rescan_tc"), t("cmd_rescan_tc_help"),
                         self._make_app_callback("action_rescan_toolchains")))
        # 4. Settings (s4)
        commands.append((t("cmd_settings"), t("cmd_settings_help"),
                         self._make_app_callback("action_s4_settings")))
        # 3. Shortcuts (system "Keys")
        if "Keys" in system_by_name:
            cmd = system_by_name["Keys"]
            commands.append((t("cmd_shortcuts"), t("cmd_shortcuts_help"), cmd.callback))
        # 4. License (s4)
        commands.append((t("cmd_license"), t("cmd_license_help"),
                         self._make_app_callback("action_show_license")))
        # 5. Theme (system)
        if "Theme" in system_by_name:
            cmd = system_by_name["Theme"]
            commands.append((t("cmd_theme"), t("cmd_theme_help"), cmd.callback))
        # 6. Quit (system)
        if "Quit" in system_by_name:
            cmd = system_by_name["Quit"]
            commands.append((t("cmd_quit"), t("cmd_quit_help"), cmd.callback))

        return commands

    async def discover(self) -> Hits:
        for display, help_text, callback in self._build_command_list():
            yield DiscoveryHit(
                display=display,
                command=callback,
                help=help_text,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, help_text, callback in self._build_command_list():
            score = matcher.match(display)
            if score > 0:
                yield Hit(
                    score=score,
                    match_display=matcher.highlight(display),
                    command=callback,
                    help=help_text,
                )
