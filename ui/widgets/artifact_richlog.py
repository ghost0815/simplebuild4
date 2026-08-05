"""RichLog with line-range selection + clipboard copy.

Textual's stock ``RichLog`` has no built-in text selection
(https://github.com/Textualize/textual/issues/5333). Because our
Output pane needs "grab this error line and paste into a bug report",
we roll a minimal line-based selection ourselves:

  * Click-and-drag → select the covered lines.
  * Shift-click → extend the current selection to the clicked line.
  * ``Ctrl+C`` (when the log has focus) → copy the selected range to
    the system clipboard via ``app.copy_to_clipboard`` (OSC 52,
    ssh-safe). No selection → copies the entire log. ``Ctrl+Shift+C``
    is a fallback for terminals that eat Ctrl+C.
  * ``Escape`` → clear the selection.

Also preserves the existing @click-meta dispatch (RichLog itself
skips Rich meta actions, so we forward them manually).
"""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.binding import Binding
from textual.strip import Strip
from textual.widgets import RichLog


class ArtifactRichLog(RichLog):
    BINDINGS = [
        # Ctrl+C is the natural "copy" shortcut for GUI-trained users;
        # Textual normally reserves it for quit, but a widget-level
        # binding wins when the widget has focus. Ctrl+Shift+C stays
        # as a fallback that also works when the log doesn't have
        # focus (bubbles up from screen bindings).
        Binding("ctrl+c", "copy_selection", "Copy", show=False, priority=True),
        Binding("ctrl+shift+c", "copy_selection", "Copy", show=False),
        # Escape is handled in on_key so the event can bubble up to
        # Screen/App (usually "quit / back") when there's no selection
        # to clear — a widget-level Binding would swallow it either
        # way, even with check_action gating in some Textual versions.
    ]

    _sel_start: int | None
    _sel_end: int | None
    _pending_start: int | None
    _sel_dragging: bool

    # Actions the log itself owns — clicks on Rich @click meta with
    # any of these names get dispatched on the widget instance rather
    # than bubbling to the containing screen (backwards-compat for
    # existing file-open @click links).
    _WIDGET_ACTIONS = frozenset({"copy_selection", "clear_selection"})

    # Newer Textual versions ship a generic per-widget text selector
    # that catches Ctrl+C and copies the visible text region with a
    # "X copied to clipboard" system notification. That conflicts with
    # our line-based selection — turn it off if the attribute exists,
    # silently ignore if the running Textual doesn't have it.
    ALLOW_SELECT = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sel_start = None
        self._sel_end = None
        self._pending_start = None
        self._sel_dragging = False
        # Keybindings only fire when the widget can hold focus.
        self.can_focus = True
        # Same intent as the class var — belt-and-braces for versions
        # where the toggle is instance-level.
        for attr in ("allow_select", "text_select"):
            try:
                setattr(self, attr, False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Existing behaviour — dispatch Rich @click meta actions ourselves.

    def on_click(self, event: events.Click) -> None:
        meta = getattr(event.style, "meta", None) or {}
        action = meta.get("@click")
        if not action:
            return
        event.stop()
        # Widget-owned actions run on the widget (so `self.selected`
        # etc. resolves correctly). Anything else keeps the old
        # screen-dispatch behaviour — those are typically file-open
        # or deliver-link callbacks registered on the containing
        # screen.
        try:
            action_name = action.split("(", 1)[0].strip()
        except Exception:
            action_name = action
        target = self if action_name in self._WIDGET_ACTIONS else self.screen
        self.app.run_action(action, default_namespace=target)

    # ------------------------------------------------------------------
    # Line-range selection

    def _line_at(self, event: events.MouseEvent) -> int:
        """Absolute line index under the mouse.

        ``MouseEvent.y`` is relative to the widget's OUTER region
        (i.e. includes any border/padding above the content), whereas
        ``render_line(y)`` receives a CONTENT-relative y. Subtracting
        the gutter difference lines the two systems up — without it
        the selection anchor sits one row below where the user
        clicked when the widget has a top border, which showed up as
        "click line N, drag down → N is missing / drag up → N
        included" asymmetry.
        """
        top_gutter = 0
        try:
            top_gutter = self.content_region.y - self.region.y
        except Exception:
            top_gutter = 1  # sensible fallback for a solid border
        content_y = max(0, event.y - top_gutter)
        return content_y + int(self.scroll_offset.y)

    def _clamp(self, line: int) -> int:
        last = max(0, len(self.lines) - 1)
        return max(0, min(last, line))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        # Focus the widget so subsequent Ctrl+C reaches our binding —
        # Textual doesn't always auto-focus a RichLog on click.
        try:
            self.focus()
        except Exception:
            pass
        line = self._clamp(self._line_at(event))
        if event.shift and self._sel_start is not None:
            # Shift + click → extend existing selection to this line.
            # Anchor stays put; end moves. Committed immediately (no
            # need to drag).
            self._sel_end = line
            self._pending_start = None
            self._sel_dragging = False
            self.refresh()
            return
        # Non-shift: store an anchor but DON'T commit a selection yet.
        # A plain click (no drag) mustn't create a 1-line selection
        # otherwise the copy icon in the border becomes "copy this
        # line I just clicked" instead of "copy everything".
        self._pending_start = line
        self._sel_dragging = False
        try:
            self.capture_mouse()
        except Exception:
            pass

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._pending_start is None:
            return
        line = self._clamp(self._line_at(event))
        if not self._sel_dragging:
            if line == self._pending_start:
                return
            # First movement → commit the selection anchor.
            self._sel_dragging = True
            self._sel_start = self._pending_start
        if line != self._sel_end:
            self._sel_end = line
            self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._pending_start = None
        self._sel_dragging = False
        try:
            self.release_mouse()
        except Exception:
            pass

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if self._sel_start is None or self._sel_end is None:
            return strip
        line_idx = y + int(self.scroll_offset.y)
        lo, hi = sorted((self._sel_start, self._sel_end))
        if not (lo <= line_idx <= hi):
            return strip
        # Rebuild segments so the highlight background WINS against
        # per-segment styles. ``Strip.apply_style`` does ``base + seg``
        # which lets the segment's own style override anything we set,
        # so plain text on a coloured line stays uncoloured. Manual
        # merge with ``seg + bg`` flips priority so the highlight bg
        # covers every cell.
        bg = Style(bgcolor="#2c4a6b")
        try:
            segments = [
                Segment(seg.text, (seg.style or Style()) + bg, seg.control)
                for seg in strip
            ]
            strip = Strip(segments, strip.cell_length)
            # Pad the line to full width so the highlight extends past
            # the actual text to the right edge of the box.
            strip = strip.extend_cell_length(self.size.width, bg)
        except Exception:
            pass
        return strip

    # ------------------------------------------------------------------
    # Text extraction + actions

    def _line_text(self, i: int) -> str:
        """Plain text of line ``i`` — strips styling from the Strip."""
        if i < 0 or i >= len(self.lines):
            return ""
        strip = self.lines[i]
        try:
            return strip.text
        except Exception:
            try:
                return "".join(seg.text for seg in strip)
            except Exception:
                return ""

    def action_copy_selection(self) -> None:
        """Copy the selected range to the clipboard, or the whole log
        if nothing is selected."""
        if not self.lines:
            self.notify("Nothing to copy", severity="warning", timeout=3)
            return
        if self._sel_start is None or self._sel_end is None:
            lines = [self._line_text(i) for i in range(len(self.lines))]
            label = f"Copied entire output ({len(lines)} lines)"
        else:
            lo, hi = sorted((self._sel_start, self._sel_end))
            lines = [self._line_text(i) for i in range(lo, hi + 1)]
            label = f"Copied {hi - lo + 1} line(s)"
        text = "\n".join(lines)
        try:
            self.app.copy_to_clipboard(text)
            self.notify(label, timeout=3)
        except Exception as e:
            self.notify(f"Copy failed: {e}", severity="error", timeout=6)

    def action_clear_selection(self) -> None:
        if self._sel_start is not None or self._sel_end is not None:
            self._sel_start = None
            self._sel_end = None
            self.refresh()

    def on_key(self, event: events.Key) -> None:
        """Handle Escape ourselves: consume only when there's a
        selection to clear, otherwise let it bubble to the screen /
        app so global handlers (quit s4, close modal, go back) still
        work while the log has focus."""
        if event.key != "escape":
            return
        if self._sel_start is None and self._sel_end is None:
            return  # nothing to clear — don't stop the event
        self.action_clear_selection()
        event.stop()
