"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import math
from typing import ClassVar

from PyQt6.QtCore import QEvent, QRectF, Qt, QTimer, pyqtSignal

# SECTION 5 — Xbox360Osd overlay
# ════════════════════════════════════════════════════════════════════
# SPEC for SECTION 5 (Xbox360Osd) — contract, do not edit:
#   class Xbox360Osd(QWidget) — child overlay strip at the bottom of the
#     video widget (geometry set by MainWindow.resizeEvent). NOT
#     transparent-for-mouse: it intercepts clicks only while visible.
#   Signals:
#     button_clicked = pyqtSignal(str)   # one of the BUTTON names below
#     scrub_pressed = pyqtSignal(float)  # scrub fraction 0..1 at press
#     scrub_moved = pyqtSignal(float)    # scrub fraction 0..1 while held
#     scrub_released = pyqtSignal()      # left button released
#   API (signatures exact):
#     __init__(self, parent: Optional[QWidget] = None)
#     set_transport(self, *, state: PlayerState, speed: float = 1.0,
#                   time_pos: Optional[float] = None,
#                   duration: Optional[float] = None,
#                   chapter: Optional[int] = None,
#                   chapter_count: Optional[int] = None,
#                   audio_lang: Optional[str] = None,
#                   subtitle_lang: Optional[str] = None) -> None
#     set_preview(self, image: Optional[QImage]) -> None
#     show_osd(self) -> None
#     _seek_bar_rect(self) -> tuple[QRectF, QRectF]  # (track, padded hit)
#     _button_rects(self) -> list[tuple[str, QRectF]]   # hit-testing/tests
#     paintEvent(self, event: QPaintEvent) -> None
#     mousePressEvent(self, event: QMouseEvent) -> None
#     mouseMoveEvent(self, event: QMouseEvent) -> None  # hover/scrub repaint
#     mouseReleaseEvent(self, event: QMouseEvent) -> None
#   SEEK BAR: thin 6 px track at the TOP of the strip, spanning the width
#     inside LEFT_MARGIN; filled portion QColor(116, 195, 41), remainder
#     QColor(255, 255, 255, 40). Fraction = time_pos / duration when
#     duration is a finite positive float and time_pos is known, else the
#     bar paints empty. _seek_bar_rect returns the track plus a ~18 px tall
#     vertically padded hit rect. Left press inside the hit rect starts a
#     scrub (emit scrub_pressed(fraction)); moves while held emit
#     scrub_moved(fraction); release emits scrub_released(). Bar hits take
#     priority over button hits and moves are ignored unless pressed.
#   PREVIEW: set_preview stores a QImage (or None) and repaints. While a
#     preview is set, paintEvent draws it scaled into a 192x108 rounded rect
#     just above the bar, centered on the current scrub x. show_osd and
#     hide clear the preview (and scrub state).
#   BUTTONS (order = left->right on the bar; glyph = painted text):
#     ("prev", "⏮"), ("play_pause", "▶"), ("stop", "⏹"), ("next", "⏭"),
#     ("ff", "⏩"), ("rw", "⏪"), ("audio", "AUD"), ("subtitle", "SUB"),
#     ("menu", "MENU"), ("eject", "⏏")
#   Look (Xbox 360): bar height 64 px, left/right margin 16 px, rounded
#     rect radius 10, fill QColor(16, 16, 16, 190), 1 px border
#     QColor(255, 255, 255, 70). Left: status text, bold, 12 pt, white —
#     'MENU', 'PLAYING', 'PAUSED', 'STOPPED', 'FF 4x', 'RW 2x' (speed
#     multiplier when speed != 1.0, prefix FF/RW by sign). Then time
#     'mm:ss / h:mm:ss' or '--:--' when time_pos is None, and 'CH x/y'
#     when chapter_count is not None. Between time/chapter text and the
#     buttons: current tracks 'AUD en · SUB fr' (parts omitted when None),
#     10 pt, light gray QColor(200, 200, 200) — language switching visible
#     on the bar (Xbox track-toggle feedback). Buttons right-aligned,
#     34 px spacing, 14 pt font, base color white, hover color Xbox green
#     QColor(116, 195, 41) via setMouseTracking(True). The 'eject' button
#     is separated from 'menu' by an EXTRA BUTTON_GAP (2x spacing total)
#     so the irreversible action stands apart; the extra gap is applied
#     only when the bar is wide enough to fit the whole button row (a
#     narrower strip degrades to the normal spacing). Draw helper
#     _fmt_time(seconds: float) -> str ('mm:ss' or 'h:mm:ss').
#   * show_osd: show(), raise_(), (re)start a singleShot
#     QTimer(OSD_TIMEOUT_MS) -> hide() (Xbox: any activity resets timeout).
#   * set_transport: store fields; update().
#   * mousePressEvent: hit-test _button_rects(); on hit emit
#     button_clicked(name) and accept the event.
#   * Paint-only: no child widgets, no external assets, no QSS.
#   ADDITIONAL SECTION 5 WIDGETS (A1/A2/A3/C1/C2):
#   class HomeMenu(QWidget) — full-window launcher over the playing video:
#     * three blades [DVD|USB|Storage], 360 dashboard paint (dim veil, dark
#       blade panels, Xbox green QColor(116,195,41)); paint-only.
#     * signals: entry_selected(str), browse_requested(), close_requested(),
#       labels_ready(object)  # internal queued label delivery
#     * keys: Left/Right blade, Up/Down entry, Return/Enter select,
#       Escape close_requested. refresh(devices, present) repopulates the DVD
#       blade; entries() returns (display, payload) pairs; selecting DVD/USB
#       emits entry_selected(payload), Storage emits browse_requested().
#     * PVD labels are read on a daemon thread via DiscIdentity.label (guarded
#       with getattr) and delivered later through labels_ready — never blocks
#       the GUI thread. Stale label epochs are dropped.
#     * USB entries = /run/media/$USER/* dirs + /dev/sd* mounts; selecting
#       scans for VIDEO_TS (dir or ISO) and loads it via MainWindow.
#   class ChapterGrid(QWidget) — full-window chapter overlay for C:
#     * set_chapters(times) stores engine.chapter_list; tiles == len(times).
#     * keys: arrows navigate, Return/Enter choose, Escape close_requested.
#     * Enter schedules ONE coalesced chapter_chosen(float seconds) after the
#       engine's 350 ms settle; arrows during the burst update the pending
#       target; close stops the timer so no seek escapes after close.
#     * paint-only dark veil + green-highlighted "CH n" tiles.
#   class HelpOverlay(QWidget) — full-window 360-style control reference:
#     * BINDINGS = tuple[(key, action)] listing EVERY keybinding; bindings()
#       exposes the data for tests; KEYBINDINGS aliases BINDINGS.
#     * paint-only dim veil, dark rounded blade panel, green key column and
#       light action column split into two balanced columns (no scrolling).
#     * show/hide are driven by MainWindow (Ctrl+H); Escape emits
#       close_requested. The overlay never touches the engine — playback
#       keeps running underneath.
# ── [AGENT2: SECTION 5 BEGIN] ──
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
)
from PyQt6.QtWidgets import QWidget

from .state import OSD_TIMEOUT_MS, PlayerState


class Xbox360Osd(QWidget):
    """Translucent Xbox 360-style transport strip drawn with QPainter."""

    button_clicked = pyqtSignal(str, arguments=())
    scrub_pressed = pyqtSignal(float, arguments=())
    scrub_moved = pyqtSignal(float, arguments=())
    scrub_released = pyqtSignal(arguments=())

    BUTTONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("prev", "\u23ee"),
        ("play_pause", "\u25b6"),
        ("stop", "\u23f9"),
        ("next", "\u23ed"),
        ("ff", "\u23e9"),
        ("rw", "\u23ea"),
        ("audio", "AUD"),
        ("subtitle", "SUB"),
        ("menu", "MENU"),
        ("eject", "\u23cf"),
    )
    LEFT_MARGIN: ClassVar[float] = 16.0
    BUTTON_GAP: ClassVar[float] = 34.0
    BUTTON_MIN_WIDTH: ClassVar[float] = 34.0
    CORNER_RADIUS: ClassVar[float] = 10.0

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the strip and its single-shot auto-hide timer."""
        super().__init__(parent)  # type: ignore[arg-type]
        self._state = PlayerState.IDLE
        self._speed = 1.0
        self._time_pos: float | None = None
        self._duration: float | None = None
        self._chapter: int | None = None
        self._chapter_count: int | None = None
        self._audio_lang: str | None = None
        self._subtitle_lang: str | None = None
        self._hover: str | None = None
        self._scrub_pressed = False
        self._scrub_x: float | None = None
        self._preview: QImage | None = None
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    # ── public API ──────────────────────────────────────────────────
    def set_transport(
        self,
        *,
        state: PlayerState,
        speed: float = 1.0,
        time_pos: float | None = None,
        duration: float | None = None,
        chapter: int | None = None,
        chapter_count: int | None = None,
        audio_lang: str | None = None,
        subtitle_lang: str | None = None,
    ) -> None:
        """Store the values shown on the strip and repaint it."""
        self._state = state
        self._speed = float(speed)
        self._time_pos = None if time_pos is None else float(time_pos)
        self._duration = None if duration is None else float(duration)
        self._chapter = chapter
        self._chapter_count = chapter_count
        self._audio_lang = audio_lang
        self._subtitle_lang = subtitle_lang
        self.update()

    def show_osd(self) -> None:
        """Show the strip and restart the auto-hide timeout."""
        self._preview = None
        self._scrub_pressed = False
        self._scrub_x = None
        self.show()
        self.raise_()
        self._hide_timer.start(OSD_TIMEOUT_MS)
        self.update()

    def hideEvent(self, event: QEvent | None) -> None:
        """Drop any preview/scrub state whenever the strip is hidden."""
        self._preview = None
        self._scrub_pressed = False
        self._scrub_x = None
        super().hideEvent(event)  # type: ignore[arg-type]

    def hide(self) -> None:
        """Hide the strip and always drop preview/scrub state."""
        self._preview = None
        self._scrub_pressed = False
        self._scrub_x = None
        super().hide()

    def set_preview(self, image: QImage | None) -> None:
        """Store the snapshot shown above the bar while scrubbing."""
        self._preview = image
        self.update()

    def _seek_bar_rect(self) -> tuple[QRectF, QRectF]:
        """Return the (track, padded hit) rectangles of the seek bar."""
        left = self.LEFT_MARGIN
        width = max(0.0, float(self.width()) - 2.0 * self.LEFT_MARGIN)
        track = QRectF(left, 5.0, width, 6.0)
        hit_top = max(0.0, track.top() - 6.0)
        hit = QRectF(left, hit_top, width, 18.0)
        return track, hit

    def _fraction_at(self, x: float) -> float:
        """Map an x coordinate inside the track to a 0..1 fraction."""
        track, _hit = self._seek_bar_rect()
        if track.width() <= 0.0:
            return 0.0
        fraction = (float(x) - track.left()) / track.width()
        return max(0.0, min(1.0, fraction))

    def _seek_fraction(self) -> float:
        """Return the playback fraction shown by the bar (0 when unknown)."""
        duration = self._duration
        time_pos = self._time_pos
        if (
            duration is None
            or time_pos is None
            or not math.isfinite(duration)
            or not math.isfinite(time_pos)
            or duration <= 0.0
        ):
            return 0.0
        return max(0.0, min(1.0, time_pos / duration))

    def _button_rects(self) -> list[tuple[str, QRectF]]:
        """Return each button's name and hit rectangle, left to right."""
        font = QFont()
        font.setPointSize(14)
        metrics = QFontMetricsF(font)
        height = max(0.0, float(self.height()) - 16.0)
        right = float(self.width()) - self.LEFT_MARGIN
        widths = [
            (name, max(self.BUTTON_MIN_WIDTH, metrics.horizontalAdvance(glyph) + 8.0))
            for name, glyph in self.BUTTONS
        ]
        needed = sum(width for _, width in widths) + self.BUTTON_GAP * (
            len(widths) - 1
        )
        # Only spend the extra gap when the bar can actually fit every
        # button; on a bar too narrow for the row it would just push
        # buttons off the left edge (graceful degradation).
        eject_extra = (
            self.BUTTON_GAP
            if float(self.width()) - 2.0 * self.LEFT_MARGIN
            >= needed + self.BUTTON_GAP
            else 0.0
        )
        rects: list[tuple[str, QRectF]] = []
        for name, width in reversed(widths):
            left = max(0.0, right - width)
            visible = max(0.0, min(width, right - left))
            rects.append((name, QRectF(left, 8.0, visible, height)))
            right -= width + self.BUTTON_GAP
            if name == "eject":
                # Keep the irreversible eject action visually apart.
                right -= eject_extra
        rects.reverse()
        return rects

    def paintEvent(self, event: QPaintEvent | None) -> None:
        """Draw the translucent bar, status text and transport glyphs."""
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QColor(255, 255, 255, 70))
            painter.setBrush(QColor(16, 16, 16, 190))
            painter.drawRoundedRect(
                QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                self.CORNER_RADIUS,
                self.CORNER_RADIUS,
            )

            track, _hit = self._seek_bar_rect()
            if track.width() > 0.0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 40))
                painter.drawRoundedRect(track, 3.0, 3.0)
                fraction = self._seek_fraction()
                if fraction > 0.0:
                    filled = QRectF(
                        track.left(),
                        track.top(),
                        track.width() * fraction,
                        track.height(),
                    )
                    painter.setBrush(QColor(116, 195, 41))
                    painter.drawRoundedRect(filled, 3.0, 3.0)

            status_font = QFont()
            status_font.setPointSize(12)
            status_font.setBold(True)
            painter.setFont(status_font)
            painter.setPen(QColor(255, 255, 255))
            metrics = painter.fontMetrics()
            baseline = (self.height() + metrics.ascent() - metrics.descent()) / 2.0
            x = self.LEFT_MARGIN
            for text in self._text_chunks():
                painter.drawText(int(x), int(baseline), text)
                x += float(metrics.horizontalAdvance(text)) + 24.0

            track_text = self._track_text()
            if track_text is not None:
                track_font = QFont()
                track_font.setPointSize(10)
                painter.setFont(track_font)
                painter.setPen(QColor(200, 200, 200))
                track_metrics = painter.fontMetrics()
                track_baseline = (
                    self.height()
                    + track_metrics.ascent()
                    - track_metrics.descent()
                ) / 2.0
                painter.drawText(int(x), int(track_baseline), track_text)

            button_font = QFont()
            button_font.setPointSize(14)
            painter.setFont(button_font)
            glyphs = dict(self.BUTTONS)
            for name, rect in self._button_rects():
                if name == self._hover:
                    painter.setPen(QColor(116, 195, 41))
                else:
                    painter.setPen(QColor(255, 255, 255))
                painter.drawText(
                    rect, int(Qt.AlignmentFlag.AlignCenter), glyphs[name]
                )

            if self._preview is not None:
                self._paint_preview(painter)
        finally:
            painter.end()

    def _preview_rect(self) -> QRectF:
        """Return the 192x108 preview frame rect centred on the scrub x."""
        track, _hit = self._seek_bar_rect()
        if self._scrub_x is None:
            centre = track.center().x()
        else:
            centre = self._scrub_x
        width = 192.0
        height = 108.0
        left = centre - width / 2.0
        limit = float(self.width()) - self.LEFT_MARGIN - width
        if limit >= self.LEFT_MARGIN:
            left = max(self.LEFT_MARGIN, min(left, limit))
        else:
            left = max(0.0, min(left, max(0.0, float(self.width()) - width)))
        return QRectF(left, track.top() - height - 4.0, width, height)

    def _paint_preview(self, painter: QPainter) -> None:
        """Paint the stored snapshot above the bar inside a rounded rect."""
        preview = self._preview
        if preview is None or preview.isNull():
            return
        rect = self._preview_rect()
        path = QPainterPath()
        path.addRoundedRect(rect, 8.0, 8.0)
        painter.save()
        try:
            painter.setClipPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 220))
            painter.drawRect(rect)
            painter.drawImage(rect, preview)
        finally:
            painter.restore()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        """Start scrubbing on the bar or emit ``button_clicked``."""
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            position = event.position()
            _track, hit = self._seek_bar_rect()
            if hit.contains(position):
                # Bar hits take priority over transport buttons.
                track, _hit = self._seek_bar_rect()
                fraction = self._fraction_at(position.x())
                self._scrub_pressed = True
                self._scrub_x = track.left() + fraction * track.width()
                self._hide_timer.start(OSD_TIMEOUT_MS)
                self.update()
                self.scrub_pressed.emit(fraction)
                event.accept()
                return
            for name, rect in self._button_rects():
                if rect.contains(position):
                    self.button_clicked.emit(name)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        """Scrub while pressed, otherwise track the hovered button."""
        if event is None:
            return
        position = event.position()
        if self._scrub_pressed:
            track, _hit = self._seek_bar_rect()
            self._scrub_x = max(
                track.left(), min(float(position.x()), track.right())
            )
            self._hide_timer.start(OSD_TIMEOUT_MS)
            self.update()
            self.scrub_moved.emit(self._fraction_at(position.x()))
            event.accept()
            return
        hover: str | None = None
        for name, rect in self._button_rects():
            if rect.contains(position):
                hover = name
                break
        if hover != self._hover:
            self._hover = hover
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        """Finish a scrub and emit ``scrub_released``."""
        if event is None:
            return
        if self._scrub_pressed and event.button() == Qt.MouseButton.LeftButton:
            self._scrub_pressed = False
            self._scrub_x = None
            self.update()
            self.scrub_released.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ── painting helpers ────────────────────────────────────────────
    @staticmethod
    def _fmt_time(seconds: float | None) -> str:
        """Format seconds as ``mm:ss`` or ``h:mm:ss``."""
        try:
            total = 0 if seconds is None else max(0, int(seconds))
        except (OverflowError, ValueError, TypeError):
            total = 0
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _status_text(self) -> str:
        """Return the left-hand status label (speed-aware)."""
        speed = self._speed
        if math.isfinite(speed) and abs(speed - 1.0) > 1e-9:
            direction = "FF" if speed > 1.0 else "RW"
            magnitude = abs(speed)
            if abs(magnitude - round(magnitude)) < 1e-9:
                return f"{direction} {round(magnitude)}x"
            return f"{direction} {magnitude:g}x"
        if self._state is PlayerState.MENU_ACTIVE:
            return "MENU"
        return self._state.name

    def _time_text(self) -> str:
        """Return the position/duration readout."""
        if self._time_pos is None:
            return "--:--"
        text = self._fmt_time(self._time_pos)
        if self._duration is not None:
            text = f"{text} / {self._fmt_time(self._duration)}"
        return text

    def _chapter_text(self) -> str | None:
        """Return the chapter readout, or None when no count is known."""
        if self._chapter_count is None:
            return None
        chapter = "?" if self._chapter is None else str(self._chapter)
        return f"CH {chapter}/{self._chapter_count}"

    def _text_chunks(self) -> list[str]:
        """Return the left-aligned text segments in paint order."""
        chunks = [self._status_text(), self._time_text()]
        chapter = self._chapter_text()
        if chapter is not None:
            chunks.append(chapter)
        return chunks

    def _track_text(self) -> str | None:
        """Return the 'AUD xx · SUB yy' readout, or None when empty."""
        parts: list[str] = []
        if self._audio_lang:
            parts.append(f"AUD {self._audio_lang}")
        if self._subtitle_lang:
            parts.append(f"SUB {self._subtitle_lang}")
        return " · ".join(parts) if parts else None


