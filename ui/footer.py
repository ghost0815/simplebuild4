"""Custom s4 footer widget — unified across all screens, clickable."""

from textual.widgets import Static
from textual.containers import Horizontal

# Left side: F-key menu items (key, label, action, available_on_screens)
# available_on = None means available everywhere
FOOTER_LEFT = [
    ("F1", "Settings", "action_command_palette", None),
    ("F2", "Targets", "action_open_targets", None),
    ("F3", "Repositories", "action_open_repository", None),
    ("F4", "Toolchains", "action_open_toolchains", None),
    ("F5", "Build", "action_build", {"main"}),
]

# Which screen each F-key navigates to (for highlighting)
SCREEN_MAP = {
    "F1": "settings",
    "F2": "main",
    "F3": "repository",
    "F4": "toolchains",
}

# Screens where Add/Remove are available
ADD_REMOVE_SCREENS = {"main", "repository"}
UPDATE_SCREENS = {"toolchains"}


class FooterItem(Static):
    """A single clickable footer item."""

    DEFAULT_CSS = """
    FooterItem {
        height: 1;
        width: auto;
    }
    FooterItem:hover {
        background: $primary-lighten-1;
    }
    FooterItem.disabled {
        opacity: 0.3;
    }
    FooterItem.disabled:hover {
        background: $primary;
    }
    """

    def __init__(self, key: str, label: str, action: str, disabled: bool = False, active: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._key = key
        self._label = label
        self._action = action
        self._disabled = disabled
        self._active = active
        if disabled:
            self.add_class("disabled")

    def render(self) -> str:
        if self._active:
            return f" [bold reverse]{self._key} {self._label}[/bold reverse] "
        elif self._disabled:
            return f" {self._key} {self._label} "
        else:
            return f" [bold]{self._key}[/bold] {self._label} "

    def on_click(self) -> None:
        if not self._disabled and self._action:
            method = getattr(self.app, self._action, None)
            if method:
                method()
            else:
                self.app.notify(f"Action not found: {self._action}", severity="error")


class FooterSpacer(Static):
    """Flexible spacer between left and right items."""

    DEFAULT_CSS = """
    FooterSpacer {
        height: 1;
        width: 1fr;
    }
    """

    def render(self) -> str:
        return ""


class s4Footer(Horizontal):
    """Unified clickable footer with left F-keys and right actions."""

    DEFAULT_CSS = """
    s4Footer {
        dock: bottom;
        width: 100%;
        height: 1;
        background: $primary;
        color: $text;
        text-style: bold;
    }
    """

    def __init__(self, active_screen: str = "main", **kwargs):
        super().__init__(**kwargs)
        self._active_screen = active_screen

    def compose(self):
        # Left: F-keys
        for key, label, action, available_on in FOOTER_LEFT:
            is_active = SCREEN_MAP.get(key) == self._active_screen
            is_available = available_on is None or self._active_screen in available_on
            yield FooterItem(
                key=key,
                label=label,
                action=action,
                disabled=not is_available,
                active=is_active,
            )

        # Spacer
        yield FooterSpacer()

        # Right: F10 Add/Update, F11 Remove
        if self._active_screen in UPDATE_SCREENS:
            yield FooterItem(key="F10", label="Update", action="action_add", disabled=True)
            yield FooterItem(key="F11", label="Remove", action="action_remove", disabled=True)
        else:
            add_disabled = self._active_screen not in ADD_REMOVE_SCREENS
            yield FooterItem(key="F10", label="Add", action="action_add", disabled=add_disabled)
            yield FooterItem(key="F11", label="Remove", action="action_remove", disabled=add_disabled)

        # Right: Save
        save_screens = {"settings", "repository", "target_edit"}
        save_disabled = self._active_screen not in save_screens
        yield FooterItem(key="F12", label="Save", action="action_save_action", disabled=save_disabled)

        # Right: Esc Quit/Back
        esc_label = "Quit" if self._active_screen == "main" else "Back"
        yield FooterItem(key="Esc", label=esc_label, action="action_esc")
