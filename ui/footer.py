"""Custom s4 footer widget — unified across all screens, clickable."""

from textual.widgets import Static
from textual.containers import Horizontal
from translation import t

# Left side: F-key menu items (key, label, action, available_on_screens)
# available_on = None means available everywhere
FOOTER_LEFT = [
    ("F1", "settings_nav", "action_s4_settings", None),
    ("F2", "targets_nav", "action_open_targets", None),
    ("F3", "repositories_nav", "action_open_repository", None),
    ("F4", "toolchains_nav", "action_open_toolchains", None),
    ("F5", "build", "action_build", {"main", "toolchains"}),
    ("F6", "customize", "action_customize", {"toolchains"}),
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


class S4Footer(Horizontal):
    """Unified clickable footer with left F-keys and right actions."""

    DEFAULT_CSS = """
    S4Footer {
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
        for key, label_key, action, available_on in FOOTER_LEFT:
            is_active = SCREEN_MAP.get(key) == self._active_screen
            is_available = available_on is None or self._active_screen in available_on
            yield FooterItem(
                key=key,
                label=t(label_key),
                action=action,
                disabled=not is_available,
                active=is_active,
            )

        # Spacer
        yield FooterSpacer()

        # Right: F9 Update, F10 Add, F11 Remove
        f9_disabled = self._active_screen not in UPDATE_SCREENS
        yield FooterItem(key="F9", label=t("update"), action="action_tc_update", disabled=f9_disabled)

        add_disabled = self._active_screen not in ADD_REMOVE_SCREENS
        yield FooterItem(key="F10", label=t("add"), action="action_add", disabled=add_disabled)

        remove_disabled = self._active_screen not in ADD_REMOVE_SCREENS
        yield FooterItem(key="F11", label=t("remove"), action="action_remove", disabled=remove_disabled)

        # Right: Save
        save_screens = {"settings", "repository", "target_edit"}
        save_disabled = self._active_screen not in save_screens
        yield FooterItem(key="F12", label=t("save"), action="action_save_action", disabled=save_disabled)

        # Right: Esc Quit/Back
        esc_label = t("quit") if self._active_screen == "main" else t("back")
        yield FooterItem(key="Esc", label=esc_label, action="action_esc")
