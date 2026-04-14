"""Multi-select dropdown widget for Textual.

Displays current selection as summary text. On click/Enter opens an overlay
with checkboxes (OptionList) where multiple items can be toggled. Closes on
Escape or blur. Special entries "All" and "None" toggle all items.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rich.text import Text

from textual import events, on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import var
from textual.widgets import Static
from textual.widgets._option_list import Option, OptionList

from textual.app import ComposeResult


# ---------------------------------------------------------------------------
# Overlay: OptionList with toggle behavior (no dismiss on click)
# ---------------------------------------------------------------------------

_ALL_VALUE = "__all__"
_NONE_VALUE = "__none__"


class MultiSelectOverlay(OptionList):
    """Overlay with checkboxes for multi-select."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("space", "toggle_item", "Toggle", show=False),
        Binding("enter", "toggle_item", "Toggle", show=False),
    ]

    ALLOW_SELECT = False

    @dataclass
    class Dismiss(Message):
        lost_focus: bool = False

    @dataclass
    class SelectionChanged(Message):
        """Fired when any checkbox is toggled."""
        selected: set[str]

    def __init__(self, items: list[str], selected: set[str], show_all_none: bool = True) -> None:
        super().__init__()
        self._items = items
        self._selected: set[str] = set(selected)
        self._show_all_none = show_all_none

    def on_mount(self) -> None:
        self._rebuild_options()

    def _rebuild_options(self) -> None:
        """Rebuild the option list with current check states."""
        self.clear_options()
        all_checked = self._selected == set(self._items) or _ALL_VALUE in self._selected
        none_checked = len(self._selected) == 0

        if self._show_all_none:
            # "None" option
            mark = "[green]✓[/green]" if none_checked else "[dim]○[/dim]"
            self.add_option(Option(Text.from_markup(f" {mark}  None"), id=_NONE_VALUE))

            # "All" option
            mark = "[green]✓[/green]" if all_checked else "[dim]○[/dim]"
            self.add_option(Option(Text.from_markup(f" {mark}  All"), id=_ALL_VALUE))

        # Individual items (keep order if no All/None, otherwise sort)
        items = self._items if not self._show_all_none else sorted(self._items)
        for item in items:
            checked = item in self._selected or (self._show_all_none and all_checked)
            mark = "[green]✓[/green]" if checked else "[dim]○[/dim]"
            self.add_option(Option(Text.from_markup(f" {mark}  {item}"), id=item))

    def action_dismiss(self) -> None:
        self.post_message(self.Dismiss())

    def action_toggle_item(self) -> None:
        """Toggle the highlighted item."""
        if self.highlighted is not None:
            option = self._options[self.highlighted]
            self._toggle(option.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Toggle on click instead of dismissing."""
        event.stop()
        if event.option.id:
            self._toggle(event.option.id)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()

    def _toggle(self, item_id: str) -> None:
        """Toggle a single item."""
        highlighted = self.highlighted

        if item_id == _ALL_VALUE:
            self._selected = set(self._items)
        elif item_id == _NONE_VALUE:
            self._selected.clear()
        else:
            if item_id in self._selected:
                self._selected.discard(item_id)
                self._selected.discard(_ALL_VALUE)
            else:
                self._selected.add(item_id)
                if self._selected == set(self._items):
                    self._selected.add(_ALL_VALUE)

        self._rebuild_options()
        # Restore cursor position
        if highlighted is not None and highlighted < self.option_count:
            self.highlighted = highlighted

        self.post_message(self.SelectionChanged(set(self._selected)))

    def _on_blur(self, _event: events.Blur) -> None:
        self.post_message(self.Dismiss(lost_focus=True))
        self.suppress_click()


# ---------------------------------------------------------------------------
# Display: shows summary of current selection
# ---------------------------------------------------------------------------

class MultiSelectCurrent(Horizontal):
    """Displays the current multi-selection summary."""

    DEFAULT_CSS = """
    MultiSelectCurrent {
        border: tall $border-blurred;
        color: $foreground;
        background: $surface;
        width: 1fr;
        height: auto;
        padding: 0 2;
        pointer: pointer;

        Static#ms-label {
            width: 1fr;
            height: auto;
            color: $foreground 50%;
            background: transparent;
        }
        &.-has-value Static#ms-label {
            color: $foreground;
        }
        .arrow {
            box-sizing: content-box;
            width: 1;
            height: 1;
            padding: 0 0 0 1;
            color: $foreground 50%;
            background: transparent;
        }
    }
    """

    ALLOW_SELECT = False

    has_value: var[bool] = var(False)

    class Toggle(Message):
        pass

    def __init__(self, placeholder: str = "Select...") -> None:
        super().__init__()
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Static(self.placeholder, id="ms-label")
        yield Static("▼", classes="arrow down-arrow")
        yield Static("▲", classes="arrow up-arrow")

    def update_label(self, text: str) -> None:
        self.has_value = bool(text)
        label = self.query_one("#ms-label", Static)
        label.update(text if text else self.placeholder)

    def _watch_has_value(self, has_value: bool) -> None:
        self.set_class(has_value, "-has-value")

    def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Toggle())


# ---------------------------------------------------------------------------
# Main widget: MultiSelect
# ---------------------------------------------------------------------------

class MultiSelect(Vertical, can_focus=True):
    """A dropdown multi-select widget.

    Usage:
        MultiSelect(
            items=["repo1", "repo2", "repo3"],
            selected=["repo1"],
            placeholder="Repositories",
            id="my-multi",
        )
    """

    BINDINGS = [
        Binding("enter,down,space,up", "show_overlay", "Open", show=False),
    ]

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    MultiSelect {
        height: auto;
        color: $foreground;

        .up-arrow {
            display: none;
        }
        &:focus > MultiSelectCurrent {
            border: tall $border;
            background-tint: $foreground 5%;
        }
        & > MultiSelectOverlay {
            width: 1fr;
            display: none;
            height: auto;
            max-height: 12;
            overlay: screen;
            constrain: none inside;
            color: $foreground;
            border: tall $border-blurred;
            background: $surface;
            &:focus {
                background-tint: $foreground 5%;
            }
            & > .option-list--option {
                padding: 0 1;
            }
        }
        &.-expanded {
            .down-arrow {
                display: none;
            }
            .up-arrow {
                display: block;
            }
            & > MultiSelectOverlay {
                display: block;
            }
        }
    }
    """

    expanded: var[bool] = var(False, init=False)

    @dataclass
    class Changed(Message):
        """Posted when the selection changes."""
        multi_select: MultiSelect
        selected: set[str]

        @property
        def control(self) -> MultiSelect:
            return self.multi_select

    def __init__(
        self,
        items: list[str],
        selected: list[str] | None = None,
        placeholder: str = "Select...",
        show_all_none: bool = True,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._items = list(items)
        self._selected: set[str] = set(selected) if selected else set()
        self._placeholder = placeholder
        self._show_all_none = show_all_none

    @property
    def selected(self) -> set[str]:
        return set(self._selected)

    @selected.setter
    def selected(self, value: set[str]) -> None:
        self._selected = set(value)
        self._update_display()

    def compose(self) -> ComposeResult:
        yield MultiSelectCurrent(placeholder=self._placeholder)
        yield MultiSelectOverlay(self._items, self._selected, show_all_none=self._show_all_none)

    def _on_mount(self, _event: events.Mount) -> None:
        self._update_display()

    def _update_display(self) -> None:
        """Update the summary label."""
        try:
            current = self.query_one(MultiSelectCurrent)
        except NoMatches:
            return
        sel = self._selected - {_ALL_VALUE, _NONE_VALUE}
        if not sel:
            current.update_label("")
        elif sel == set(self._items):
            current.update_label("All")
        else:
            # Preserve original item order (not alphabetical)
            ordered = [item for item in self._items if item in sel]
            current.update_label(", ".join(ordered))

    def _watch_expanded(self, expanded: bool) -> None:
        self.set_class(expanded, "-expanded")
        if expanded:
            try:
                overlay = self.query_one(MultiSelectOverlay)
                overlay.focus(scroll_visible=False)
            except NoMatches:
                pass

    @on(MultiSelectCurrent.Toggle)
    def _toggle(self, event: MultiSelectCurrent.Toggle) -> None:
        event.stop()
        self.expanded = not self.expanded

    @on(MultiSelectOverlay.Dismiss)
    def _dismiss(self, event: MultiSelectOverlay.Dismiss) -> None:
        event.stop()
        self.expanded = False
        if not event.lost_focus:
            self.focus()

    @on(MultiSelectOverlay.SelectionChanged)
    def _selection_changed(self, event: MultiSelectOverlay.SelectionChanged) -> None:
        event.stop()
        self._selected = event.selected - {_ALL_VALUE, _NONE_VALUE}
        self._update_display()
        self.post_message(self.Changed(self, self.selected))

    def action_show_overlay(self) -> None:
        self.expanded = True

    def get_selected_values(self) -> list[str]:
        """Return selected values as a list (without special markers)."""
        sel = self._selected - {_ALL_VALUE, _NONE_VALUE}
        if sel == set(self._items):
            return ["all"]
        return sorted(sel)
