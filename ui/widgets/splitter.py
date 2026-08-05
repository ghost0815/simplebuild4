"""Draggable splitter widget for two horizontal sibling panes.

Textual has no built-in resizable-pane primitive
(https://github.com/Textualize/textual/discussions/4230). This widget
provides one via the standard mouse-capture pattern:

  on_mouse_down → capture_mouse() + snapshot start state
  on_mouse_move → compute delta, update the left pane's width in cells
  on_mouse_up   → release_mouse()

Layout expectation: the splitter sits between two siblings in a
``Horizontal`` container. The left sibling's ``width`` is set in cells
(bounded by ``min_cells`` on both sides so the panes can't collapse to
zero), the right sibling stays ``1fr`` and fills the remainder. Mouse
must be enabled in the terminal for the drag to work — keyboard-only
users see the handle but can't move it.
"""

from __future__ import annotations

from rich.segment import Segment
from textual import events
from textual.css.scalar import Scalar
from textual.message import Message
from textual.strip import Strip
from textual.widgets import Static


class HSplitter(Static):
    """Vertical drag handle between two horizontal panes."""

    class Changed(Message):
        """Emitted when the user finishes a drag. Carries the final
        left-pane width in cells so the containing screen can remember
        it (session-scoped, not persisted to disk)."""

        def __init__(self, splitter: "HSplitter", left_cells: int) -> None:
            self.splitter = splitter
            self.left_cells = left_cells
            super().__init__()

        @property
        def control(self):
            return self.splitter

    DEFAULT_CSS = """
    HSplitter {
        /* 1-cell strip. render_line draws ``│`` on every row so the
           visual is a thin continuous rule; hover / active-drag flip
           to $accent for grab feedback. The single-cell click target
           is fine on a standard-mouse terminal — users click the
           visible line itself. */
        width: 1;
        min-width: 1;
        max-width: 1;
        height: 1fr;
        color: $foreground 40%;
        background: transparent;
    }
    HSplitter:hover {
        color: $text;
        background: $foreground 25%;
    }
    HSplitter.-dragging {
        color: $text;
        background: $foreground 25%;
    }
    """

    def __init__(
        self,
        left_id: str,
        right_id: str,
        min_cells: int = 20,
        **kwargs,
    ) -> None:
        # Empty content — the background colour alone draws the strip
        # (full height because of height: 1fr in the DEFAULT_CSS).
        super().__init__("", **kwargs)
        self._left_id = left_id
        self._right_id = right_id
        self._min_cells = min_cells
        self._drag_start_x: int | None = None
        self._start_left_cells: int = 0
        # Latest width computed during drag — used by on_mouse_up to
        # report the final value in the Changed message. Reading
        # ``self._left().size.width`` there would race with Textual's
        # async layout and often give the pre-drag size.
        self._last_left_cells: int = 0

    # ------------------------------------------------------------------
    # Lookups

    def _left(self):
        return self.screen.query_one(f"#{self._left_id}")

    def _right(self):
        return self.screen.query_one(f"#{self._right_id}")

    # ------------------------------------------------------------------
    # Rendering — replaces Static's content rendering so every row of
    # the widget draws " │ " (space, glyph, space). Without this, a
    # Static with content "│" only paints row 0; using background alone
    # makes the whole cell a solid bar (visually too heavy).

    def render_line(self, y: int) -> Strip:
        return Strip([Segment("│", self.rich_style)])

    # ------------------------------------------------------------------
    # Mouse handlers

    def on_mouse_down(self, event: events.MouseDown) -> None:
        try:
            self._start_left_cells = self._left().size.width
        except Exception:
            return
        self._drag_start_x = event.screen_x
        self.add_class("-dragging")
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_start_x is None:
            return
        try:
            left = self._left()
            right = self._right()
            parent = left.parent
            if parent is None:
                return
            # Reserve room for the splitter itself + the min-width of
            # the right pane. Total is the parent's inner width minus
            # the widget's own width (see DEFAULT_CSS — currently 3).
            total = parent.size.width - self.size.width
            delta = event.screen_x - self._drag_start_x
            new_left = self._start_left_cells + delta
            new_left = max(self._min_cells, min(total - self._min_cells, new_left))
            left.styles.width = Scalar.parse(f"{new_left}")
            # Right must be explicit 1fr so it re-fills the remainder
            # (fractional units of 1 collapse to whatever's left after
            # cell-sized siblings claim theirs).
            right.styles.width = Scalar.parse("1fr")
            self._last_left_cells = new_left
        except Exception:
            pass

    def on_mouse_up(self, event: events.MouseUp) -> None:
        was_dragging = self._drag_start_x is not None
        self._drag_start_x = None
        self.remove_class("-dragging")
        try:
            self.release_mouse()
        except Exception:
            pass
        # Notify the containing screen of the final width so it can
        # remember the position for the rest of this session. Use the
        # last value we computed during drag rather than reading the
        # pane's size — Textual applies style changes asynchronously,
        # so the reported size at mouse-up may still be pre-drag.
        if was_dragging and self._last_left_cells > 0:
            try:
                self.post_message(self.Changed(self, self._last_left_cells))
            except Exception:
                pass
        event.stop()

    def on_click(self, event: events.Click) -> None:
        """Double-click resets the split to the CSS default.

        Chain==1 is the trailing click of a normal drag (mouse_up
        already fired) — ignore. Chain>=2 means the user genuinely
        double-clicked without moving; clearing the inline widths
        lets the CSS defaults (both panes at 1fr) take over again.
        Emits ``Changed(0)`` so the screen wipes its stored session
        override too.
        """
        if event.chain < 2:
            return
        try:
            self._left().styles.width = None
            self._right().styles.width = None
        except Exception:
            pass
        self._last_left_cells = 0
        try:
            self.post_message(self.Changed(self, 0))
        except Exception:
            pass
        event.stop()


class VSplitter(Static):
    """Horizontal drag handle between two stacked panes.

    Mirror of HSplitter for vertical layouts: sits between two children
    of a ``Vertical`` container, dragging changes the top pane's
    ``height`` in rows. Bottom pane stays ``1fr`` so it re-fills the
    remainder. The top pane's ``max-height`` cap (if any) is lifted on
    first drag so the user can pull the divider past a CSS default
    (e.g. the 50% cap on ``#tc-installed-box`` is intentional as an
    auto-growth ceiling but not a user-drag ceiling).
    """

    class Changed(Message):
        """Emitted when the user finishes a drag. Carries the final
        top-pane height in rows."""

        def __init__(self, splitter: "VSplitter", top_cells: int) -> None:
            self.splitter = splitter
            self.top_cells = top_cells
            super().__init__()

        @property
        def control(self):
            return self.splitter

    DEFAULT_CSS = """
    VSplitter {
        width: 1fr;
        height: 1;
        min-height: 1;
        max-height: 1;
        color: $foreground 40%;
        background: transparent;
    }
    /* Terminal cells are ~2:1 tall/wide — a full-width background
       tint reads much heavier here than on the vertical splitter.
       Compromise: very low alpha (~8%) + text-style bold on hover,
       so the line noticeably lights up without the whole strip
       feeling like a filled bar. */
    VSplitter:hover {
        color: $text;
        background: $foreground 15%;
    }
    VSplitter.-dragging {
        color: $text;
        background: $foreground 15%;
    }
    """

    def __init__(
        self,
        top_id: str,
        bottom_id: str,
        min_cells: int = 5,
        **kwargs,
    ) -> None:
        super().__init__("", **kwargs)
        self._top_id = top_id
        self._bottom_id = bottom_id
        self._min_cells = min_cells
        self._drag_start_y: int | None = None
        self._start_top_cells: int = 0
        self._last_top_cells: int = 0

    def _top(self):
        return self.screen.query_one(f"#{self._top_id}")

    def _bottom(self):
        return self.screen.query_one(f"#{self._bottom_id}")

    def render_line(self, y: int) -> Strip:
        # Single-row widget; y is always 0. Fill full width with the
        # thin horizontal-rule glyph so the divider spans both panes.
        w = max(1, self.size.width)
        return Strip([Segment("─" * w, self.rich_style)])

    def on_mouse_down(self, event: events.MouseDown) -> None:
        try:
            top = self._top()
        except Exception:
            return
        # Store the top pane's absolute top-edge in screen coords. The
        # splitter always sits directly below the top pane, so
        # (splitter_screen_y - top_start_y) is how much room the top
        # pane occupies (in the "styles.height" sense — this includes
        # whatever box-sizing Textual uses). Tracking absolute Y makes
        # the drag independent of padding/border overhead.
        self._top_start_y = top.region.y
        # How far into the splitter widget the mouse was clicked, so
        # the splitter's top edge stays under the same cursor pixel.
        self._mouse_offset = event.screen_y - self.region.y
        self._drag_start_y = event.screen_y
        # Lift the CSS max-height cap for the whole drag. ``None`` might
        # not always clear the compiled style; setting an explicit
        # 100% works reliably and expresses "as tall as the parent
        # allows" — Textual will still clip to whatever's actually
        # available (parent height minus fixed siblings).
        try:
            top.styles.max_height = Scalar.parse("100%")
        except Exception:
            pass
        self.add_class("-dragging")
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_start_y is None:
            return
        try:
            top = self._top()
            bottom = self._bottom()
            parent = top.parent
            if parent is None:
                return
            # Compute how much space the top pane can grow into:
            # parent height minus every sibling that isn't top itself
            # and isn't the ``1fr`` bottom pane we're draggin against.
            # (splitter + any fixed-height siblings like a tabbar or
            # action row — they keep their sizes regardless of drag.)
            # Then reserve ``min_cells`` for bottom so it can't be
            # crushed to zero.
            reserved = 0
            for c in parent.children:
                if c is top or c is bottom:
                    continue
                reserved += c.size.height
            max_top = parent.size.height - reserved - self._min_cells
            target_splitter_y = event.screen_y - self._mouse_offset
            new_top = target_splitter_y - self._top_start_y
            new_top = max(self._min_cells, min(max_top, new_top))
            top.styles.height = Scalar.parse(f"{new_top}")
            bottom.styles.height = Scalar.parse("1fr")
            self._last_top_cells = new_top
        except Exception:
            pass

    def on_mouse_up(self, event: events.MouseUp) -> None:
        was_dragging = self._drag_start_y is not None
        self._drag_start_y = None
        self.remove_class("-dragging")
        try:
            self.release_mouse()
        except Exception:
            pass
        if was_dragging and self._last_top_cells > 0:
            try:
                self.post_message(self.Changed(self, self._last_top_cells))
            except Exception:
                pass
        event.stop()

    def on_click(self, event: events.Click) -> None:
        """Double-click restores the CSS default (auto height + the
        max-height cap that on_mouse_down had lifted). Chain==1 is a
        drag-tail click — ignore.
        """
        if event.chain < 2:
            return
        try:
            top = self._top()
            top.styles.height = None
            top.styles.max_height = None
        except Exception:
            pass
        self._last_top_cells = 0
        try:
            self.post_message(self.Changed(self, 0))
        except Exception:
            pass
        event.stop()
