"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import getpass
import math
import os
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from PyQt6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QImage,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QResizeEvent,
    QShowEvent,
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .engine import DvdPlaybackEngine
from .hardware import check_environment, disc_present, list_dvd_devices
from .identity import DiscIdentity, ResumeStore
from .mpris import MprisService
from .osd import Xbox360Osd
from .state import DISC_POLL_MS, FF_SPEEDS, GAMEPAD_POLL_MS, PlaybackError, PlayerState, log


def _sd_mounts() -> list[Path]:
    """Return mount points backed by /dev/sd* block devices."""
    mounts: list[Path] = []
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return mounts
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith("/dev/sd"):
            continue
        try:
            path = Path(fields[1].replace("\\040", " "))
            if path.is_dir():
                mounts.append(path)
        except OSError:
            continue
    return mounts


@dataclass(frozen=True)
class HomeEntry:
    """One dashboard row: a headline, a supporting line and its payload."""

    title: str
    subtitle: str
    payload: str


class HomeMenu(QWidget):
    """2026 frosted-glass dashboard over the still-playing video."""

    entry_selected = pyqtSignal(str, arguments=())
    browse_requested = pyqtSignal(arguments=())
    close_requested = pyqtSignal(arguments=())
    labels_ready = pyqtSignal(object, arguments=())
    # Fired exactly once, from the first paintEvent: startup probes wait on it.
    first_paint = pyqtSignal(arguments=())
    # Blade highlights drive the MainWindow title preview lifecycle.
    blade_changed = pyqtSignal(int, arguments=())

    BLADES: ClassVar[tuple[str, ...]] = ("DVD", "USB", "Storage")
    BLADE_SUBTITLES: ClassVar[tuple[str, ...]] = (
        "Optical drive",
        "Removable media",
        "Browse files",
    )
    FOOTER_HINTS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Enter", "select"),
        ("←/→", "blades"),
        ("Esc", "back"),
        ("Ctrl+H", "help"),
    )
    # A key repeat or a double tap must never re-enter the load flow while
    # the first load is still in LOADING (Enter is edge-triggered).
    SELECT_DEBOUNCE_S: ClassVar[float] = 0.35
    # 2026 design tokens (paint-only, never stylesheet driven).
    PANEL_RADIUS: ClassVar[float] = 16.0
    ROW_RADIUS: ClassVar[float] = 14.0
    VEIL_ALPHA: ClassVar[int] = 172
    ACCENT: ClassVar[QColor] = QColor(116, 195, 41)
    ACCENT_BRIGHT: ClassVar[QColor] = QColor(159, 232, 85)
    TEXT: ClassVar[QColor] = QColor(234, 238, 231)
    MUTED: ClassVar[QColor] = QColor(158, 169, 158)
    DIM: ClassVar[QColor] = QColor(96, 108, 96)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the launcher; it starts hidden."""
        super().__init__(parent)  # type: ignore[arg-type]
        # D1: the DVD blade is hot on the very first frame — never -1/None,
        # so the Xbox-green border, label and entry rows always paint.
        self._blade = 0
        self._selection = 0
        self._last_select_at = 0.0
        self._hover_blade = -1
        self._hover_entry = -1
        self._dvd_devices: list[str] = []
        self._dvd_present: dict[str, bool | None] = {}
        self._labels: dict[str, str] = {}
        self._usb_entries: list[tuple[str, str]] = []
        self._label_epoch = 0
        self._label_thread: threading.Thread | None = None
        self._preview_live = False
        self.setMouseTracking(True)
        # 2026 frosted veil: translucent so the live DVD menu shows through
        # the glass while the panels keep text readable. Qt still promotes
        # this to a native sibling above mpv's X window.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAutoFillBackground(False)
        self._painted_once = False
        self.labels_ready.connect(self._apply_labels)
        self.hide()

    def showEvent(self, event: QShowEvent | None) -> None:
        """Log the first live show so a missing overlay is diagnosable."""
        super().showEvent(event)
        log.info(
            "milestone home menu shown: geom=%s parent=%r",
            self.geometry(),
            self.parentWidget(),
        )

    # ── data ────────────────────────────────────────────────────────
    def refresh(
        self,
        devices: list[str] | None = None,
        present: dict[str, bool | None] | None = None,
    ) -> None:
        """Repopulate the blades; DVD labels are read off the GUI thread."""
        if devices is not None:
            self._dvd_devices = list(devices)
        if present is not None:
            self._dvd_present = dict(present)
        else:
            for device in self._dvd_devices:
                self._dvd_present.setdefault(device, None)
        self._usb_entries = self._discover_usb()
        # A1: a summon always presents the DVD blade refreshed.
        previous_blade = self._blade
        self._blade = 0
        self._selection = 0
        self._refresh_labels(self._dvd_devices)
        self.update()
        if previous_blade != self._blade:
            self.blade_changed.emit(self._blade)

    def preview_device(self) -> str | None:
        """Return the disc path the DVD blade would preview, if any."""
        if self._blade != 0:
            return None
        for row in self.entry_rows():
            if row.payload and self._dvd_present.get(row.payload) is True:
                return row.payload
        return None

    def set_preview_live(self, live: bool) -> None:
        """Mark the header as showing a live behind-the-glass preview."""
        if live != self._preview_live:
            self._preview_live = live
            self.update()

    @property
    def preview_live(self) -> bool:
        """Whether the header currently advertises a live menu preview."""
        return self._preview_live

    def entries(self) -> list[tuple[str, str]]:
        """Return (display text, payload) for the active blade (compat view)."""
        return [
            (
                f"{row.title}  ·  {row.subtitle}" if row.subtitle else row.title,
                row.payload,
            )
            for row in self.entry_rows()
        ]

    def entry_rows(self) -> list[HomeEntry]:
        """Return structured rows (title, subtitle, payload) for the blade."""
        if self._blade == 0:
            rows: list[HomeEntry] = []
            reading = (
                self._label_thread is not None and self._label_thread.is_alive()
            )
            for device in self._dvd_devices:
                present = self._dvd_present.get(device)
                if present is True:
                    label = self._labels.get(device)
                    title = label or ("Reading disc…" if reading else "DVD video")
                    rows.append(HomeEntry(title, device, device))
                elif present is False:
                    rows.append(HomeEntry("No disc", device, device))
                else:
                    rows.append(HomeEntry("Checking disc…", device, device))
            return rows
        if self._blade == 1:
            return [
                HomeEntry(name, path, path) for name, path in self._usb_entries
            ]
        return [
            HomeEntry(
                "Browse for a disc…",
                "Choose a VIDEO_TS folder or .iso",
                "",
            )
        ]

    def empty_state_text(self) -> str | None:
        """Guidance shown when the active blade has no rows at all."""
        if self._blade == 0 and not self._dvd_devices:
            return "No disc — insert one"
        if self._blade == 1 and not self._usb_entries:
            return "No USB media — plug in a drive"
        return None

    def empty_state_hint(self) -> str:
        """Supporting line under the empty-state headline."""
        if self._blade == 0:
            return "The dashboard refreshes automatically when a disc is ready."
        return "Removable volumes appear here automatically."

    def footer_text(self) -> str:
        """Return the painted key-hint footer as plain text (test surface)."""
        return "  ·  ".join(
            f"{key} {action}" for key, action in self.FOOTER_HINTS
        )

    def blade_name(self) -> str:
        """Return the active blade name."""
        return self.BLADES[self._blade]

    def selection(self) -> int:
        """Return the highlighted entry index."""
        return self._selection

    def _discover_usb(self) -> list[tuple[str, str]]:
        """Find removable mount points under /run/media and /dev/sd*."""
        seen: set[str] = set()
        found: list[Path] = []
        try:
            base = Path("/run/media") / getpass.getuser()
            if base.is_dir():
                found.extend(sorted(base.iterdir()))
        except OSError:
            pass
        found.extend(_sd_mounts())
        entries: list[tuple[str, str]] = []
        for path in found:
            try:
                if not path.is_dir():
                    continue
                key = str(path.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            entries.append((path.name or str(path), str(path)))
        return entries

    def _refresh_labels(self, devices: list[str]) -> None:
        """Start a daemon thread that reads PVD labels (never blocks the UI)."""
        self._label_epoch += 1
        epoch = self._label_epoch
        reader = getattr(DiscIdentity, "label", None)
        if not devices or not callable(reader):
            return

        def worker() -> None:
            labels: dict[str, str] = {}
            for device in devices:
                try:
                    value = reader(device)
                except Exception:  # noqa: BLE001, S112 - a bad read is skippable
                    continue
                if isinstance(value, str) and value.strip():
                    labels[device] = value.strip()
            try:
                self.labels_ready.emit({"epoch": epoch, "labels": labels})
            except RuntimeError:
                pass  # widget gone during shutdown

        thread = threading.Thread(
            target=worker, name="dvd-label-reader", daemon=True
        )
        self._label_thread = thread
        thread.start()

    def _apply_labels(self, payload: object) -> None:
        """Merge a worker label result when it is still current."""
        if not isinstance(payload, dict):
            return
        epoch = payload.get("epoch")
        labels = payload.get("labels")
        if epoch != self._label_epoch or not isinstance(labels, dict):
            return
        for device, label in labels.items():
            if isinstance(device, str) and isinstance(label, str):
                self._labels[device] = label
        self.update()

    # ── navigation ──────────────────────────────────────────────────
    def _move_blade(self, delta: int) -> None:
        self._blade = (self._blade + delta) % len(self.BLADES)
        self._selection = 0
        self.update()
        self.blade_changed.emit(self._blade)

    def _move_selection(self, delta: int) -> None:
        count = len(self.entries())
        if count <= 0:
            self._selection = 0
        else:
            self._selection = (self._selection + delta) % count
        self.update()

    def _select(self) -> None:
        now = time.monotonic()
        if now - self._last_select_at < self.SELECT_DEBOUNCE_S:
            return  # key repeat / double tap: the first Enter is in charge
        entries = self.entries()
        if not entries:
            return
        self._selection = min(self._selection, len(entries) - 1)
        _display, payload = entries[self._selection]
        if self._blade == 2:
            self.browse_requested.emit()
            return
        if not payload:
            return
        if self._blade == 0 and self._dvd_present.get(payload) is False:
            return
        self._last_select_at = now
        self.entry_selected.emit(payload)

    def handle_key(self, key: int) -> bool:
        """Consume one launcher key; return True when handled."""
        codes = Qt.Key
        if key == codes.Key_Left.value:
            self._move_blade(-1)
        elif key == codes.Key_Right.value:
            self._move_blade(1)
        elif key == codes.Key_Up.value:
            self._move_selection(-1)
        elif key == codes.Key_Down.value:
            self._move_selection(1)
        elif key in (codes.Key_Return.value, codes.Key_Enter.value):
            self._select()
        elif key == codes.Key_Escape.value:
            self.close_requested.emit()
        else:
            return False
        return True

    # ── painting ────────────────────────────────────────────────────
    def _layout(self) -> dict[str, float]:
        """Return the generous 2026 spacing grid (16 px radii, 20 px gaps)."""
        width = float(self.width())
        height = float(self.height())
        margin = 32.0
        content = max(0.0, width - 2.0 * margin)
        blade_top = max(88.0, min(120.0, height * 0.15))
        blade_h = max(96.0, min(150.0, height * 0.19))
        entry_h = 64.0 if height >= 640.0 else 56.0
        footer_h = 30.0
        footer_top = height - margin - footer_h
        return {
            "margin": margin,
            "content": content,
            "blade_top": blade_top,
            "blade_h": blade_h,
            "blade_gap": 20.0,
            "entry_top": blade_top + blade_h + 28.0,
            "entry_h": entry_h,
            "entry_gap": 12.0,
            "list_w": min(content, 940.0),
            "footer_top": footer_top,
            "footer_h": footer_h,
        }

    def _blade_rects(self) -> list[tuple[str, QRectF]]:
        """Return the three blade panel rectangles, left to right."""
        m = self._layout()
        gap = m["blade_gap"]
        width = min(320.0, max(0.0, (m["content"] - 2.0 * gap) / 3.0))
        rects: list[tuple[str, QRectF]] = []
        for index, name in enumerate(self.BLADES):
            left = m["margin"] + index * (width + gap)
            rects.append(
                (name, QRectF(left, m["blade_top"], width, m["blade_h"]))
            )
        return rects

    def _entry_rects(self) -> list[QRectF]:
        """Return entry row rectangles under the blades."""
        m = self._layout()
        step = m["entry_h"] + m["entry_gap"]
        return [
            QRectF(m["margin"], m["entry_top"] + index * step, m["list_w"], m["entry_h"])
            for index in range(len(self.entry_rows()))
        ]

    def footer_rect(self) -> QRectF:
        """Return the key-hint footer band (inside the widget)."""
        m = self._layout()
        return QRectF(m["margin"], m["footer_top"], m["content"], m["footer_h"])

    @staticmethod
    def _font(size: int, bold: bool = False, italic: bool = False) -> QFont:
        """Build one dashboard font (no stylesheets, paint-only)."""
        font = QFont()
        font.setPointSize(size)
        font.setBold(bold)
        font.setItalic(italic)
        return font

    @staticmethod
    def _panel_gradient(rect: QRectF, active: bool) -> QLinearGradient:
        """Return the frosted-glass vertical fill for one panel."""
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        if active:
            gradient.setColorAt(0.0, QColor(44, 62, 42, 214))
            gradient.setColorAt(1.0, QColor(22, 32, 24, 196))
        else:
            gradient.setColorAt(0.0, QColor(30, 38, 32, 186))
            gradient.setColorAt(1.0, QColor(16, 23, 19, 176))
        return gradient

    def _paint_header(self, painter: QPainter, m: dict[str, float]) -> None:
        """Paint the modern title band with a live-preview status chip."""
        painter.setFont(self._font(22, True))
        painter.setPen(self.TEXT)
        painter.drawText(
            QRectF(m["margin"], m["margin"], m["content"], 34.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "DVD PLAYER",
        )
        painter.setFont(self._font(9, True))
        painter.setPen(self.ACCENT)
        painter.drawText(
            QRectF(m["margin"], m["margin"], m["content"], 34.0),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            "X B O X   3 6 0   ·   2 0 2 6",
        )
        if self._preview_live:
            chip = QRectF(
                m["margin"] + m["content"] - 140.0,
                m["margin"] + 38.0,
                140.0,
                24.0,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(116, 195, 41, 38))
            painter.drawRoundedRect(chip, 12.0, 12.0)
            painter.setPen(self.ACCENT)
            painter.setFont(self._font(8, True))
            painter.drawText(
                chip,
                int(Qt.AlignmentFlag.AlignCenter),
                "◉  LIVE TITLE PREVIEW",
            )
        rule_y = m["margin"] + 50.0
        line = QLinearGradient(
            m["margin"], rule_y, m["margin"] + m["content"], rule_y
        )
        line.setColorAt(0.0, QColor(116, 195, 41, 210))
        line.setColorAt(0.35, QColor(116, 195, 41, 70))
        line.setColorAt(1.0, QColor(116, 195, 41, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(line))
        painter.drawRect(
            QRectF(m["margin"], rule_y, m["content"], 1.5)
        )

    def _paint_blades(self, painter: QPainter) -> None:
        """Paint three frosted blade cards with glow, hover and muted states."""
        for index, (name, rect) in enumerate(self._blade_rects()):
            active = index == self._blade
            hover = index == self._hover_blade and not active
            if active:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(116, 195, 41, 34))
                painter.drawRoundedRect(
                    rect.adjusted(-3.0, -3.0, 3.0, 3.0), 19.0, 19.0
                )
                border = self.ACCENT
            elif hover:
                border = QColor(78, 122, 46)
            else:
                border = QColor(48, 58, 48, 210)
            painter.setPen(border)
            painter.setBrush(QBrush(self._panel_gradient(rect, active)))
            painter.drawRoundedRect(rect, self.PANEL_RADIUS, self.PANEL_RADIUS)
            if active:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(116, 195, 41))
                painter.drawRoundedRect(
                    QRectF(rect.left() + 20.0, rect.bottom() - 8.0, 48.0, 4.0),
                    2.0,
                    2.0,
                )
                painter.drawEllipse(
                    QRectF(rect.right() - 27.0, rect.center().y() - 5.0, 10.0, 10.0)
                )
            painter.setFont(self._font(16, True))
            painter.setPen(
                self.ACCENT_BRIGHT if active else (
                    self.TEXT if hover else self.MUTED
                )
            )
            painter.drawText(
                QRectF(rect.left() + 22.0, rect.top() + 18.0, rect.width() - 44.0, 28.0),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                name.upper(),
            )
            painter.setFont(self._font(10))
            painter.setPen(self.MUTED if active else self.DIM)
            painter.drawText(
                QRectF(rect.left() + 22.0, rect.top() + 52.0, rect.width() - 44.0, 22.0),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.BLADE_SUBTITLES[index],
            )

    def _paint_empty(self, painter: QPainter, m: dict[str, float]) -> None:
        """Paint a refined frosted empty card instead of leaving a void."""
        headline = self.empty_state_text()
        if headline is None:
            return
        panel = QRectF(
            m["margin"], m["entry_top"], m["list_w"], min(168.0, m["entry_h"] * 2.5)
        )
        painter.setPen(QColor(52, 64, 52, 220))
        painter.setBrush(QBrush(self._panel_gradient(panel, False)))
        painter.drawRoundedRect(panel, self.PANEL_RADIUS, self.PANEL_RADIUS)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(116, 195, 41, 36))
        painter.drawEllipse(
            QRectF(panel.left() + 26.0, panel.top() + 40.0, 44.0, 44.0)
        )
        painter.setPen(self.ACCENT)
        painter.setFont(self._font(18, True))
        painter.drawText(
            QRectF(panel.left() + 26.0, panel.top() + 40.0, 44.0, 44.0),
            int(Qt.AlignmentFlag.AlignCenter),
            "◎",
        )
        painter.setPen(self.TEXT)
        painter.setFont(self._font(15, True))
        painter.drawText(
            QRectF(panel.left() + 88.0, panel.top() + 38.0, panel.width() - 116.0, 26.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            headline,
        )
        painter.setPen(self.MUTED)
        painter.setFont(self._font(10))
        painter.drawText(
            QRectF(panel.left() + 88.0, panel.top() + 68.0, panel.width() - 116.0, 24.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.empty_state_hint(),
        )

    def _paint_entries(self, painter: QPainter) -> None:
        """Paint spacious glass rows with selected glow and hover lift."""
        rows = self.entry_rows()
        limit = self._layout()["footer_top"] - 10.0
        for index, (row, rect) in enumerate(
            zip(rows, self._entry_rects(), strict=False)
        ):
            if rect.bottom() > limit:
                break  # never paint rows over the key-hint footer
            chosen = index == self._selection
            hover = index == self._hover_entry and not chosen
            if chosen:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(116, 195, 41, 30))
                painter.drawRoundedRect(
                    rect.adjusted(-2.0, -2.0, 2.0, 2.0), 17.0, 17.0
                )
                border = self.ACCENT
            elif hover:
                border = QColor(78, 122, 46)
            else:
                border = QColor(48, 58, 48, 210)
            painter.setPen(border)
            painter.setBrush(QBrush(self._panel_gradient(rect, chosen)))
            painter.drawRoundedRect(rect, self.ROW_RADIUS, self.ROW_RADIUS)
            if chosen:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self.ACCENT)
                painter.drawRoundedRect(
                    QRectF(rect.left() + 14.0, rect.top() + 14.0, 4.0, rect.height() - 28.0),
                    2.0,
                    2.0,
                )
            painter.setFont(self._font(14, chosen))
            painter.setPen(self.ACCENT_BRIGHT if chosen else self.TEXT)
            painter.drawText(
                QRectF(
                    rect.left() + 28.0,
                    rect.top() + 8.0,
                    rect.width() - 78.0,
                    rect.height() * 0.5 - 6.0,
                ),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                row.title,
            )
            painter.setFont(self._font(10))
            painter.setPen(self.MUTED if chosen else self.DIM)
            painter.drawText(
                QRectF(
                    rect.left() + 28.0,
                    rect.top() + rect.height() * 0.5 - 2.0,
                    rect.width() - 78.0,
                    rect.height() * 0.5 - 6.0,
                ),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                row.subtitle,
            )
            actionable = bool(row.payload) and row.title != "No disc"
            if actionable:
                painter.setFont(self._font(17, chosen))
                painter.setPen(self.ACCENT if chosen else self.DIM)
                painter.drawText(
                    QRectF(rect.right() - 46.0, rect.top(), 30.0, rect.height()),
                    int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                    "›",
                )

    def _paint_footer(self, painter: QPainter, m: dict[str, float]) -> None:
        """Paint a subtle rule and centered key-hint glass capsules."""
        footer = self.footer_rect()
        painter.setPen(QColor(48, 58, 48, 180))
        painter.drawLine(
            int(m["margin"]), int(footer.top() - 10.0),
            int(m["margin"] + m["content"]), int(footer.top() - 10.0),
        )
        painter.setFont(self._font(9))
        metrics = painter.fontMetrics()
        labels = [f"{key}  {action}" for key, action in self.FOOTER_HINTS]
        widths = [float(metrics.horizontalAdvance(text)) + 26.0 for text in labels]
        gap = 10.0
        total = sum(widths) + gap * (len(widths) - 1)
        x = footer.center().x() - total / 2.0
        y = footer.top() + 3.0
        height = footer.height() - 6.0
        for text, width in zip(labels, widths, strict=False):
            pill = QRectF(x, y, width, height)
            painter.setPen(QColor(56, 68, 56, 210))
            painter.setBrush(QColor(20, 28, 22, 150))
            painter.drawRoundedRect(pill, 12.0, 12.0)
            painter.setPen(self.MUTED)
            painter.drawText(
                pill,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                text,
            )
            x += width + gap

    def paintEvent(self, event: QPaintEvent | None) -> None:
        """Draw the 360 dashboard: title band, blades, rows and hints."""
        del event
        if not self._painted_once:
            self._painted_once = True
            log.info(
                "milestone home first paint: geom=%s native=%#x",
                self.geometry(),
                int(self.winId()),
            )
            self.first_paint.emit()
        m = self._layout()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Frosted veil: low enough alpha that the live DVD menu is
            # visible through the glass, dark enough for text contrast.
            painter.fillRect(
                self.rect(), QColor(5, 9, 7, self.VEIL_ALPHA)
            )
            band = QLinearGradient(0.0, 0.0, 0.0, float(self.height()) * 0.55)
            band.setColorAt(0.0, QColor(116, 195, 41, 30))
            band.setColorAt(0.45, QColor(12, 18, 14, 12))
            band.setColorAt(1.0, QColor(5, 9, 7, 36))
            painter.fillRect(
                QRectF(0.0, 0.0, float(self.width()), float(self.height())),
                band,
            )
            glow = QLinearGradient(
                float(self.width()) * 0.55, 0.0, float(self.width()), 0.0
            )
            glow.setColorAt(0.0, QColor(116, 195, 41, 0))
            glow.setColorAt(1.0, QColor(116, 195, 41, 22))
            painter.fillRect(
                QRectF(0.0, 0.0, float(self.width()), float(self.height())),
                glow,
            )
            self._paint_header(painter, m)
            self._paint_blades(painter)
            self._paint_empty(painter, m)
            self._paint_entries(painter)
            self._paint_footer(painter, m)
        finally:
            painter.end()

    # ── pointer states ──────────────────────────────────────────────
    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        """Track hover over blades and rows; repaint only on change."""
        if event is not None:
            pos = event.position()
            blade = next(
                (
                    index
                    for index, (_name, rect) in enumerate(self._blade_rects())
                    if rect.contains(pos)
                ),
                -1,
            )
            entry = next(
                (
                    index
                    for index, rect in enumerate(self._entry_rects())
                    if rect.contains(pos)
                ),
                -1,
            )
            if (blade, entry) != (self._hover_blade, self._hover_entry):
                self._hover_blade = blade
                self._hover_entry = entry
                self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent | None) -> None:
        """Clear hover states when the pointer leaves the dashboard."""
        if self._hover_blade != -1 or self._hover_entry != -1:
            self._hover_blade = -1
            self._hover_entry = -1
            self.update()
        super().leaveEvent(event)


class ChapterGrid(QWidget):
    """Full-window chapter picker with coalesced, settle-aware seeking."""

    chapter_chosen = pyqtSignal(float, arguments=())
    close_requested = pyqtSignal(arguments=())

    SETTLE_MS: ClassVar[int] = 350
    COLUMNS: ClassVar[int] = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the grid and its settle timer; it starts hidden."""
        super().__init__(parent)  # type: ignore[arg-type]
        self._chapters: list[int] = []
        self._selected = 0
        self._pending: int | None = None
        # Native + opaque: same xcb reasoning as HomeMenu — a non-native
        # child cannot paint over mpv's native video window.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAutoFillBackground(False)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(self.SETTLE_MS)
        self._settle.timeout.connect(self._flush)
        self.hide()

    # ── data ────────────────────────────────────────────────────────
    def set_chapters(self, chapters: list[int]) -> None:
        """Replace the tiles with the engine's chapter start times."""
        self._chapters = [int(value) for value in chapters]
        self._selected = 0
        self._pending = None
        self._settle.stop()
        self.update()

    def chapters(self) -> list[int]:
        """Return the chapter start times currently shown."""
        return list(self._chapters)

    def selection(self) -> int:
        """Return the highlighted chapter index."""
        return self._selected

    def has_pending_seek(self) -> bool:
        """Whether a coalesced seek is waiting for the settle timer."""
        return self._pending is not None

    def close_grid(self) -> None:
        """Cancel any pending seek and hide (no seek after close)."""
        self._settle.stop()
        self._pending = None
        self.hide()

    # ── navigation ──────────────────────────────────────────────────
    def _move(self, delta: int) -> None:
        count = len(self._chapters)
        if count <= 0:
            self._selected = 0
        else:
            self._selected = (self._selected + delta) % count
        if self._pending is not None:
            self._pending = self._selected
            self._settle.start()
        self.update()

    def handle_key(self, key: int) -> bool:
        """Consume one grid key; return True when handled."""
        codes = Qt.Key
        if key == codes.Key_Left.value:
            self._move(-1)
        elif key == codes.Key_Right.value:
            self._move(1)
        elif key == codes.Key_Up.value:
            self._move(-self.COLUMNS)
        elif key == codes.Key_Down.value:
            self._move(self.COLUMNS)
        elif key in (codes.Key_Return.value, codes.Key_Enter.value):
            if not self._chapters:
                return True
            self._pending = self._selected
            self._settle.start()
            self.update()
        elif key == codes.Key_Escape.value:
            self.close_requested.emit()
        else:
            return False
        return True

    def _flush(self) -> None:
        """Emit exactly one final chapter seek after the burst settles."""
        index = self._pending
        self._pending = None
        if index is None or self.isHidden() or not self._chapters:
            return
        if index < 0 or index >= len(self._chapters):
            return
        self.chapter_chosen.emit(float(self._chapters[index]))

    # ── painting ────────────────────────────────────────────────────
    def _tile_rects(self) -> list[QRectF]:
        """Return the rectangle of every chapter tile."""
        count = len(self._chapters)
        if count == 0:
            return []
        margin = 56.0
        gap = 14.0
        top = 92.0
        columns = min(self.COLUMNS, count)
        width = max(
            0.0, (float(self.width()) - 2.0 * margin - (columns - 1) * gap) / columns
        )
        height = 54.0
        return [
            QRectF(
                margin + (index % columns) * (width + gap),
                top + (index // columns) * (height + gap),
                width,
                height,
            )
            for index in range(count)
        ]

    def paintEvent(self, event: QPaintEvent | None) -> None:
        """Draw the dim veil plus green-highlighted chapter tiles."""
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 205))
            title_font = QFont()
            title_font.setPointSize(18)
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(56, 48, "CHAPTERS")
            tile_font = QFont()
            tile_font.setPointSize(12)
            painter.setFont(tile_font)
            for index, rect in enumerate(self._tile_rects()):
                chosen = index == self._selected
                painter.setPen(
                    QColor(116, 195, 41)
                    if chosen
                    else QColor(255, 255, 255, 45)
                )
                painter.setBrush(
                    QColor(28, 38, 24, 235) if chosen else QColor(16, 18, 16, 210)
                )
                painter.drawRoundedRect(rect, 7.0, 7.0)
                painter.setPen(
                    QColor(116, 195, 41) if chosen else QColor(220, 220, 220)
                )
                painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), f"CH {index + 1}")
                painter.setPen(QColor(160, 160, 160))
                painter.drawText(
                    rect.adjusted(0.0, 0.0, 0.0, -10.0),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                    Xbox360Osd._fmt_time(float(self._chapters[index])),
                )
            hint_font = QFont()
            hint_font.setPointSize(9)
            painter.setFont(hint_font)
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(
                56,
                self.height() - 20,
                "←/→/↑/↓ Move    A Play    B Back",
            )
        finally:
            painter.end()


class HelpOverlay(QWidget):
    """Full-window 360-style control reference (Ctrl+H)."""

    close_requested = pyqtSignal(arguments=())

    BINDINGS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("↑ ↓ ← →", "DVD menu: move highlight"),
        ("Enter", "Menu: select · Playing: play/pause"),
        ("Backspace", "DVD menu: back"),
        ("X", "Root menu"),
        ("Y", "Title menu"),
        ("Space", "Play / pause"),
        ("S", "Stop"),
        ("F", "Fast-forward"),
        ("R", "Rewind"),
        ("N", "Next chapter"),
        ("P", "Previous chapter"),
        ("I", "Next audio track"),
        ("O", "Next subtitle track"),
        ("+", "Volume up"),
        ("-", "Volume down"),
        ("E", "Eject"),
        ("M", "Toggle OSD"),
        ("G", "Home menu"),
        ("C", "Chapter grid"),
        ("A", "Cycle aspect ratio"),
        ("Ctrl+F", "Toggle fullscreen"),
        ("Ctrl+L", "Load a disc image"),
        ("Ctrl+H", "Help overlay"),
        ("Esc", "Close overlay"),
    )
    KEYBINDINGS: ClassVar[tuple[tuple[str, str], ...]] = BINDINGS

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the hidden help panel."""
        super().__init__(parent)  # type: ignore[arg-type]
        # Native + opaque overlay; see HomeMenu for the xcb rationale.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAutoFillBackground(False)
        self.hide()

    @classmethod
    def bindings(cls) -> list[tuple[str, str]]:
        """Return the documented (key, action) rows (tests use this)."""
        return list(cls.BINDINGS)

    def _columns(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Split the binding rows into two balanced columns."""
        rows = list(self.BINDINGS)
        half = (len(rows) + 1) // 2
        return rows[:half], rows[half:]

    def paintEvent(self, event: QPaintEvent | None) -> None:
        """Paint the dim veil and the two-column control reference."""
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            veil = QLinearGradient(0.0, 0.0, 0.0, float(self.height()))
            veil.setColorAt(0.0, QColor(4, 10, 5, 238))
            veil.setColorAt(1.0, QColor(0, 0, 0, 218))
            painter.fillRect(self.rect(), veil)

            title_font = QFont()
            title_font.setPointSize(18)
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(56, 48, "CONTROLS")

            panel = QRectF(
                40.0, 72.0, float(self.width()) - 80.0, float(self.height()) - 112.0
            )
            painter.setPen(QColor(116, 195, 41))
            painter.setBrush(QColor(18, 24, 18, 232))
            painter.drawRoundedRect(panel, 10.0, 10.0)

            left, right = self._columns()
            column_width = panel.width() / 2.0
            top = panel.top() + 22.0
            line = 28.0
            key_font = QFont()
            key_font.setPointSize(11)
            key_font.setBold(True)
            action_font = QFont()
            action_font.setPointSize(10)
            for column, rows in enumerate((left, right)):
                x = panel.left() + 24.0 + column * column_width
                for index, (keys, action) in enumerate(rows):
                    y = top + index * line
                    painter.setFont(key_font)
                    painter.setPen(QColor(116, 195, 41))
                    painter.drawText(int(x), int(y), keys)
                    painter.setFont(action_font)
                    painter.setPen(QColor(210, 210, 210))
                    painter.drawText(int(x + 132.0), int(y), action)

            hint_font = QFont()
            hint_font.setPointSize(9)
            painter.setFont(hint_font)
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(
                56,
                self.height() - 20,
                "Ctrl+H / Esc Close",
            )
        finally:
            painter.end()
# ── [AGENT2: SECTION 5 END] ──

# ════════════════════════════════════════════════════════════════════
# SECTION 5b — render-api surface (R6/R7)
# ════════════════════════════════════════════════════════════════════

# R6/R7: the render API draws into a QOpenGLWidget instead of mpv's native
# wid surface. Overlays stay siblings above the GL widget (paint-only, no
# native-window tricks); mouse coordinates are scaled by the device pixel
# ratio so dvdnav sees the same widget-space points as the old wid path.
# Dispatch contract: DVD_PLAYER_RENDER=auto|render -> GL surface and
# attach_render(get_proc, fbo); wid embedding is only chosen when
# wid_embedding_safe() (xcb). On native Wayland render mode NEVER calls
# attach_widget; software on Wayland still attempts attach_render once,
# then shows the graceful renderer-unavailable status.
# Proc seam: Qt's QOpenGLContext.getProcAddress() takes bytes (QByteArray),
# while the render.py callback hands over the GL name as str. The loader
# wrapper below re-encodes it, so every Qt lookup receives bytes and never a
# str (a str argument raised TypeError and dropped every GL symbol).
# B1/B2: DVD_PLAYER_RENDER is the single switch understood by both the
# engine (auto = probe hardware, software = llvmpipe) and this dispatch.
# ``auto``/``render`` ask for the GL render surface; ``software``/``wid``
# keep the native-window path whenever the platform can embed it.
RENDER_MODE_ENV = "DVD_PLAYER_RENDER"
_RENDER_MODES: tuple[str, ...] = ("render", "gl", "opengl", "auto")
# Legacy spellings stay accepted so older launch scripts do not silently
# fall back to wid embedding.
_RENDER_MODE_ENV_ALIAS = "DVD_PLAYER_RENDER_MODE"
_RENDER_API_ENV_ALIAS = "DVD_PLAYER_RENDER_API"


def render_mode_requested(env: Mapping[str, str] | None = None) -> bool:
    """Return True when the environment selects the GL render API."""
    source = os.environ if env is None else env
    mode = source.get(RENDER_MODE_ENV, "").strip().lower()
    if mode in _RENDER_MODES:
        return True
    legacy = source.get(_RENDER_MODE_ENV_ALIAS, "").strip().lower()
    if legacy in _RENDER_MODES:
        return True
    api = source.get(_RENDER_API_ENV_ALIAS, "").strip().lower()
    return api in ("1", "true", "yes", *_RENDER_MODES)


def render_mode_active(
    engine: object, env: Mapping[str, str] | None = None
) -> bool:
    """Whether MainWindow should build the GL video surface for ``engine``."""
    mode = getattr(engine, "render_mode", None)
    if isinstance(mode, str):
        return mode.strip().lower() in _RENDER_MODES
    active = getattr(engine, "render_active", None)
    if isinstance(active, bool):
        return active
    return render_mode_requested(env)


def wid_embedding_safe() -> bool:
    """Whether the active Qt platform supplies real X11 window ids for mpv.

    Native Wayland ``winId()`` is a wl_surface pointer, not an X Window:
    handing it to mpv's x11 EGL video output makes Xlib abort the process
    with ``BadWindow`` on ``X_DestroyWindow``. Only xcb (or a platform
    without native windows, e.g. offscreen tests) may use the wid path.
    """
    app: QApplication | None = QApplication.instance()
    if app is None:
        return True
    return bool(app.platformName() != "wayland")


def render_surface_required(
    engine: object, env: Mapping[str, str] | None = None
) -> bool:
    """Whether MainWindow must build the GL render surface.

    ``DVD_PLAYER_RENDER=auto``/``render`` always asks for it. On a platform
    where wid embedding is unsafe (native Wayland) the GL surface is the
    only viable path even for ``software``, so the window still attempts
    ``attach_render`` and then degrades to the graceful status instead of
    handing a wl_surface id to Xlib.
    """
    return render_mode_active(engine, env) or not wid_embedding_safe()


def _bytes_proc_loader(
    loader: Callable[[bytes], int | None],
) -> Callable[[str], int | None]:
    """Adapt Qt's bytes-only ``getProcAddress`` to the str seam contract.

    ``RenderContext`` decodes the C ``char *`` name to ``str`` before calling
    its loader; Qt rejects that str, so encode it back to ASCII bytes here.
    """

    def get_proc(name: str) -> int | None:
        return loader(name.encode("ascii"))

    return get_proc


class VideoGLWidget(QOpenGLWidget):
    """GL surface fed by the engine's render API (R6).

    The engine may land in parallel, so every render hook is optional.
    ``attach_render`` returning False is not success: the widget falls back
    to the ordinary ``attach_widget(winId())`` path so a failed GL handshake
    never leaves the surface silently unattached (black frame, no video).
    On success the engine receives the widget's live framebuffer source
    (``defaultFramebufferObject`` as a callable), so libmpv paints into this
    widget's FBO instead of the window backbuffer (FBO 0).

    ``attach_complete(bool)`` fires exactly once per attach chain, after the
    render path, the optional software-GL retry and the wid fallback have all
    settled. MainWindow queues loads until that signal and degrades to a
    clear status when it reports failure.
    """

    attach_complete = pyqtSignal(bool, arguments=())

    def __init__(
        self, engine: DvdPlaybackEngine, parent: QWidget | None = None
    ) -> None:
        """Create the GL surface and keep pointer tracking on."""
        super().__init__(parent)
        self._engine = engine
        self._render_attached = False
        self.setMouseTracking(True)

    def initializeGL(self) -> None:
        """Hand the GL proc-address loader to the engine, once.

        Ordered chain: ``attach_render`` -> one software-GL retry on native
        Wayland -> ``attach_widget(winId())`` when wid embedding is safe.
        ``attach_complete(attached)`` always fires, so MainWindow can release
        queued loads or show the graceful renderer-unavailable status.
        """
        if self._render_attached:
            return
        started = time.monotonic()
        log.info("milestone attach attempt: render api platform=%s", self._platform_name())
        if self._try_attach_render():
            self._render_attached = True
            log.info(
                "milestone attach result: render api attached duration=%.1fms",
                (time.monotonic() - started) * 1000.0,
            )
            self._emit_attach_complete(True)
            return
        self._render_attached = False
        if self._software_retry_feasible():
            os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
            log.warning("render attach failed; retrying once with software GL")
            if self._try_attach_render():
                self._render_attached = True
                log.info(
                    "milestone attach result: software render api attached duration=%.1fms",
                    (time.monotonic() - started) * 1000.0,
                )
                self._emit_attach_complete(True)
                return
        else:
            log.debug(
                "software GL retry not feasible (platform=%s, flag=%r)",
                self._platform_name(),
                os.environ.get("LIBGL_ALWAYS_SOFTWARE"),
            )
        if not wid_embedding_safe():
            log.warning(
                "render attach failed on native Wayland: wid embedding is "
                "unavailable, refusing to pass a Wayland surface id to Xlib"
            )
            log.info(
                "milestone attach result: failed wayland duration=%.1fms",
                (time.monotonic() - started) * 1000.0,
            )
            self._emit_attach_complete(False)
            return
        fallback = getattr(self._engine, "attach_widget", None)
        if not callable(fallback):
            log.warning("render attach refused and no wid fallback exists")
            log.info(
                "milestone attach result: failed duration=%.1fms",
                (time.monotonic() - started) * 1000.0,
            )
            self._emit_attach_complete(False)
            return
        try:
            fallback(int(self.winId()))
        except Exception as exc:  # noqa: BLE001 - wid fallback is best effort
            log.warning("wid fallback after render failure failed: %s", exc)
            log.info(
                "milestone attach result: failed duration=%.1fms",
                (time.monotonic() - started) * 1000.0,
            )
            self._emit_attach_complete(False)
        else:
            log.info("render attach refused; using the wid video path")
            log.info(
                "milestone attach result: wid duration=%.1fms",
                (time.monotonic() - started) * 1000.0,
            )
            self._emit_attach_complete(True)

    def _try_attach_render(self) -> bool:
        """Attempt the render handshake once, swallowing any backend fault."""
        attach = getattr(self._engine, "attach_render", None)
        if not callable(attach):
            return False
        context = self.context()
        loader = getattr(context, "getProcAddress", None)
        get_proc = _bytes_proc_loader(loader) if callable(loader) else None
        fbo = self._fbo_source()
        try:
            return bool(attach(get_proc, fbo=fbo))
        except TypeError:
            # Engines that still expose attach_render(proc) only: keep the
            # wid path reachable rather than failing the handshake outright.
            try:
                return bool(attach(get_proc))
            except Exception as exc:  # noqa: BLE001 - renderer must never kill
                log.warning("render attach failed: %s", exc)
                return False
        except Exception as exc:  # noqa: BLE001 - renderer must never kill the GUI
            log.warning("render attach failed: %s", exc)
            return False

    @staticmethod
    def _platform_name() -> str:
        """Return the active Qt platform name (empty when no app exists)."""
        app: QApplication | None = QApplication.instance()
        return app.platformName() if app is not None else ""

    def _software_retry_feasible(self) -> bool:
        """Software GL is worth one retry only on native Wayland, once."""
        return (
            self._platform_name() == "wayland"
            and os.environ.get("LIBGL_ALWAYS_SOFTWARE") != "1"
        )

    def _emit_attach_complete(self, attached: bool) -> None:
        """Record and broadcast the settled attach result."""
        log.debug(
            "attach chain done: attached=%s render=%s",
            attached,
            self._render_attached,
        )
        self.attach_complete.emit(attached)

    def _fbo_source(self) -> Callable[[], int]:
        """Return a callable that re-reads this widget's FBO every frame.

        QOpenGLWidget paints through its own framebuffer, so libmpv must be
        told that id (not FBO 0). Re-reading per frame follows context
        recreation (resize/device change).
        """
        return lambda: int(self.defaultFramebufferObject())

    def _pixel_ratio(self) -> float:
        """Return the device pixel ratio, clamped to a sane positive value."""
        try:
            ratio = float(self.devicePixelRatioF())
        except (TypeError, ValueError):
            ratio = 1.0
        if not math.isfinite(ratio) or ratio <= 0.0:
            ratio = 1.0
        return ratio

    def paintGL(self) -> None:
        """Drive one update + one render per paint (no-op without a renderer)."""
        update = getattr(self._engine, "update", None)
        if callable(update):
            try:
                update()
            except Exception as exc:  # noqa: BLE001 - never abort the paint loop
                log.debug("render update failed: %s", exc)
        render = getattr(self._engine, "render_frame", None)
        if not callable(render):
            return
        ratio = self._pixel_ratio()
        width = max(1, int(self.width() * ratio))
        height = max(1, int(self.height() * ratio))
        try:
            render(width, height)
        except Exception as exc:  # noqa: BLE001 - never abort the paint loop
            log.debug("render_frame failed: %s", exc)

    def resizeGL(self, width: int, height: int) -> None:
        """Tell the engine the new device-pixel surface size."""
        for name in ("resize_render", "render_resize", "notify_render_size"):
            notify = getattr(self._engine, name, None)
            if not callable(notify):
                continue
            try:
                notify(int(width), int(height))
            except Exception as exc:  # noqa: BLE001 - resize is best effort
                log.debug("%s failed: %s", name, exc)
            return

    def widget_position(self, event: QMouseEvent) -> tuple[int, int]:
        """Map an event to device-pixel widget coordinates for dvdnav."""
        position = event.position()
        try:
            ratio = float(self.devicePixelRatioF())
        except (TypeError, ValueError):
            ratio = 1.0
        if not math.isfinite(ratio) or ratio <= 0.0:
            ratio = 1.0
        return int(position.x() * ratio), int(position.y() * ratio)


def _scrub_preview_size(window_width: float, window_height: float) -> tuple[float, float]:
    """Return the live scrub thumbnail size (>= 320x180, window-scaled)."""
    width = max(1.0, float(window_width))
    height = max(1.0, float(window_height))
    scale = min(width / 1280.0, height / 720.0)
    scale = max(1.0, min(1.5, scale))
    preview_width = max(320.0, 320.0 * scale)
    preview_height = max(180.0, preview_width * 9.0 / 16.0)
    margin = 16.0
    room = max(0.0, width - 2.0 * margin)
    if preview_width > room > 0.0:
        preview_width = room
        preview_height = preview_width * 9.0 / 16.0
    return preview_width, preview_height


def _scrub_preview_rect(
    window_width: float,
    window_height: float,
    anchor_x: float,
    bar_top: float,
) -> QRectF:
    """Place the thumbnail above the seek bar, clamped inside the window."""
    width, height = _scrub_preview_size(window_width, window_height)
    margin = 16.0
    left = float(anchor_x) - width / 2.0
    left = max(margin, min(left, max(margin, window_width - margin - width)))
    gap = 12.0
    top = float(bar_top) - height - gap
    top = max(margin, min(top, max(margin, window_height - height - margin)))
    return QRectF(left, top, width, height)


class ScrubPreviewOverlay(QWidget):
    """Full-window, mouse-transparent live scrub snapshot + timestamp.

    Painted above the video surface (sibling raise order) so the big frame
    is never clipped by the 64 px OSD strip. It exists only between
    scrub press and release.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create a hidden, paint-only overlay."""
        super().__init__(parent)  # type: ignore[arg-type]
        self._image: QImage | None = None
        self._seconds: float | None = None
        self._anchor_x: float | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAutoFillBackground(False)
        self.hide()

    def set_frame(
        self,
        image: QImage,
        seconds: float | None,
        anchor_x: float | None = None,
    ) -> None:
        """Show ``image`` at the scrub anchor with its timestamp."""
        self._image = image
        self._seconds = seconds
        self._anchor_x = anchor_x
        self.show()
        self.raise_()
        self.update()

    def clear(self) -> None:
        """Drop the thumbnail and hide the overlay."""
        self._image = None
        self._seconds = None
        self._anchor_x = None
        self.update()
        self.hide()

    def timestamp_text(self) -> str:
        """Return the thumbnail time readout using the OSD formatter."""
        return Xbox360Osd._fmt_time(self._seconds)

    def _preview_rect(self) -> QRectF:
        """Return the window-space thumbnail rect above the OSD bar."""
        parent = self.parentWidget()
        window_width = float(parent.width()) if parent is not None else float(self.width())
        window_height = (
            float(parent.height()) if parent is not None else float(self.height())
        )
        anchor = self._anchor_x
        if anchor is None:
            anchor = window_width / 2.0
        return _scrub_preview_rect(
            window_width, window_height, float(anchor), window_height - 64.0 + 5.0
        )

    def paintEvent(self, event: QPaintEvent | None) -> None:
        """Paint the snapshot, a green border and the time pill."""
        del event
        image = self._image
        if image is None or image.isNull():
            return
        rect = self._preview_rect()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(rect, 10.0, 10.0)
            painter.save()
            try:
                painter.setClipPath(path)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(0, 0, 0, 235))
                painter.drawRect(rect)
                painter.drawImage(rect, image)
            finally:
                painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QColor(116, 195, 41))
            painter.drawRoundedRect(rect, 10.0, 10.0)

            text = self.timestamp_text()
            font = QFont()
            font.setPointSize(max(12, int(rect.height() * 0.085)))
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            text_width = float(metrics.horizontalAdvance(text)) + 24.0
            text_height = float(metrics.height()) + 10.0
            pill = QRectF(
                rect.left() + 12.0,
                rect.bottom() - text_height - 12.0,
                min(text_width, max(0.0, rect.width() - 24.0)),
                text_height,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 200))
            painter.drawRoundedRect(pill, 7.0, 7.0)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)
        finally:
            painter.end()


class ScrubPreviewOsd(Xbox360Osd):
    """OSD strip whose preview geometry/reporting matches the big overlay."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the strip and add the preview timestamp state."""
        super().__init__(parent)
        self._preview_seconds: float | None = None

    def set_preview(self, image: QImage | None) -> None:
        """Store the snapshot and clear the timestamp with it."""
        if image is None:
            self._preview_seconds = None
        super().set_preview(image)

    def set_preview_time(self, seconds: float | None) -> None:
        """Record the captured frame position for the timestamp label."""
        self._preview_seconds = seconds
        self.update()

    def show_osd(self) -> None:
        """Show the bar and drop any pending preview timestamp."""
        self._preview_seconds = None
        super().show_osd()

    def hide(self) -> None:
        """Hide the bar and drop any pending preview timestamp."""
        self._preview_seconds = None
        super().hide()

    def _paint_preview(self, painter: QPainter) -> None:
        """The big snapshot is painted by :class:`ScrubPreviewOverlay`."""
        del painter

    def _preview_rect(self) -> QRectF:
        """Report the big window-scaled preview rect (painted by overlay)."""
        parent = self.parentWidget()
        window_width = (
            float(parent.width()) if parent is not None else float(self.width())
        )
        window_height = (
            float(parent.height()) if parent is not None else float(self.height())
        )
        track, _hit = self._seek_bar_rect()
        anchor = track.center().x() if self._scrub_x is None else self._scrub_x
        return _scrub_preview_rect(
            window_width,
            window_height,
            float(anchor),
            float(self.y()) + track.top(),
        )


# ════════════════════════════════════════════════════════════════════
# SECTION 6 — MainWindow (input mapping & wiring)
# ════════════════════════════════════════════════════════════════════

# SPEC for SECTION 6 (MainWindow) — contract, do not edit:
#   R6/R7 (render-api addendum): when render_surface_required(engine) is
#     true (DVD_PLAYER_RENDER=auto|render, or any platform where wid
#     embedding is unsafe), self.video is a VideoGLWidget(QOpenGLWidget)
#     whose initializeGL/paintGL/resizeGL bridge to engine.attach_render/
#     update/render_frame/resize_render (all guarded with getattr).
#     attach_render(proc, fbo=…)
#     returns True only when libmpv paints into the widget's own FBO: the
#     widget passes ``lambda: int(defaultFramebufferObject())`` so the live
#     framebuffer is re-read on every frame. Returning False (or raising)
#     triggers one software-GL retry on native Wayland (LIBGL_ALWAYS_SOFTWARE
#     set before the re-init, only when the flag was absent). On xcb the
#     failed handshake may then call engine.attach_widget(int(winId())) once
#     (the wid path is the ordered fallback); on native Wayland wid is NEVER
#     attempted. initializeGL then ALWAYS emits
#     attach_complete(attached); failures are logged, never a silent black
#     frame. MainWindow holds one pending-load slot until that signal: no
#     preview/user load reaches engine.load before it fires. A False result
#     (or an attach-related PlaybackError) clears the queue and shows
#     'renderer unavailable — falling back…', never the raw PlaybackError.
#     A software request on native Wayland is routed through this same GL
#     attempt, then the graceful status when the render handshake fails.
#     paintGL runs exactly one engine.update() followed by one
#     engine.render_frame(width*dpr, height*dpr) per paint, swallowing
#     exceptions so a render fault cannot abort or corrupt the paint loop.
#     Otherwise self.video stays the plain QWidget/wid path. Mouse forwarding keeps the same
#     _on_video_mouse handler; the GL surface additionally scales
#     event.position() by devicePixelRatioF(). OSD/home/grid/help remain
#     sibling widgets above the video (paint-only, never native hacks on
#     the GL path).
#   class MainWindow(QMainWindow) — top-level window:
#     * central QWidget, QVBoxLayout margins 0 spacing 0:
#       - self.video: plain QWidget (autoFillBackground black) stretch=1;
#         receives mouse events; child OSD strip (self.osd) positioned in
#         resizeEvent: self.osd.setGeometry(0, height - 64, width, 64).
#       - bottom bar QHBoxLayout (padding 6): self.source QLineEdit
#         (placeholder '/dev/sr0 | /path/movie.iso | /path/to/VIDEO_TS'),
#         'Browse…' QPushButton, 'Load / Play' QPushButton, 'Eject'
#         QPushButton, self.status QLabel (elided, initial 'IDLE').
#         The bar is HIDDEN BY DEFAULT (widgets stay constructed and
#         reachable for tests, but .hide() is called after it is added to
#         the layout) so the video fills the whole window — console look,
#         no accidental ejects or edits from the noisy strip.
#     * self.setMinimumSize(854, 480) in __init__ (video-only contract).
#   API (signatures exact):
#     __init__(self, engine: DvdPlaybackEngine, source: Optional[str] = None)
#     resizeEvent(self, event) -> None
#     keyPressEvent(self, event: QKeyEvent) -> None
#     _handle_key(self, key: int) -> bool      # True when consumed
#     _poll_gamepad(self) -> None
#     _on_engine_state(self, state: PlayerState) -> None
#     _on_position(self, seconds: float) -> None
#     _on_tracks(self, audio: list, subs: list) -> None
#     _on_menu_active(self, active: bool) -> None
#     _on_engine_error(self, message: str) -> None
#     _on_load_clicked(self) -> None
#     _on_browse_clicked(self) -> None
#     _on_eject_clicked(self) -> None
#     _on_osd_button(self, action: str) -> None
#     _on_video_mouse(self, obj, event: QMouseEvent) -> None
#     _show_resume_prompt(self, disc_hash: str, saved: dict) -> None
#     _on_disc_hash(self, disc_hash: str) -> None
#     _sync_osd(self) -> None
#     _on_scrub_pressed(self, fraction: float) -> None
#     _on_scrub_moved(self, fraction: float) -> None
#     _on_scrub_released(self) -> None
#     _cycle_speed(self, direction: int) -> None
#     _attach_engine(self) -> None
#     _auto_detect_disc(self) -> None
#     _poll_disc_insertion(self) -> None
#     _load_source(self, source: str, quiet: bool = False) -> None
#     closeEvent(self, event: QCloseEvent) -> None
#   Wiring:
#   * engine signals -> handlers: state_changed->_on_engine_state (update
#     self.status with state name + _sync_osd); position_changed->_on_position
#     (_sync_osd); tracks_changed->_on_tracks (cache lists + _sync_osd);
#     menu_active_changed->_on_menu_active (True -> osd.hide() then
#     _sync_osd); error_occurred->
#     _on_engine_error (QMessageBox.critical).
#   * After window.show(): QTimer.singleShot(0, self._attach_engine) ->
#     engine.attach_widget(int(self.video.winId())) inside try/except
#     PlaybackError: log + graceful status 'renderer unavailable — falling
#     back…' (no modal); then when
#     __init__ received a non-empty source: set self.source text and
#     _load_source(source, quiet=False); otherwise _auto_detect_disc().
#     STARTUP ORDER (fix-startup): the first visible frame is the dashboard.
#     showEvent paints HomeMenu from cached state (no list_dvd_devices/
#     disc_present call), then the menu's first_paint signal schedules
#     _startup_after_first_frame through QTimer.singleShot(0). Only that
#     deferred pass shows a hidden GL surface, attaches the engine and runs
#     the device probes, so a spun-down USB drive or slow GL init can never
#     delay the menu. Milestone lines (app start, window shown, first paint,
#     home menu shown, per-device probe duration, attach attempt/result/
#     duration, load request/queued/drained) go to the capped debug log at
#     ~/.cache/dvd-player/dvd-player.log (DVD_PLAYER_LOG_PATH overrides).
#     _attach_engine() is also called at the start of _load_source so a
#     fresh mpv is rebuilt after eject() (attach_widget no-ops while
#     already attached).
#   * Video mouse: install eventFilter on self.video (_on_video_mouse):
#     MouseMove -> when NOT engine.menu_active: osd.show_osd(); when
#     engine.menu_active: engine.menu_mouse_move(pos.x(), pos.y()) only.
#     MouseButtonPress (Left) -> when NOT menu active: osd.show_osd();
#     when menu active: engine.menu_select() only. Inside the menu the
#     menu's own UI is the interface, so pointer activity never reveals
#     the OSD there.
#   * Console scrubbing (OSD seek bar): connect osd.scrub_pressed ->
#     _on_scrub_pressed, osd.scrub_moved -> _on_scrub_moved,
#     osd.scrub_released -> _on_scrub_released. On press/move compute
#     seconds = fraction * engine.duration (skip when duration is None) and
#     throttle seeks to one per 250 ms (SCRUB_THROTTLE_MS). A preview is
#     captured lazily: after a seek, one frame is read only when at least
#     200 ms (SCRUB_CAPTURE_MS) elapsed since the previous capture, so
#     dragging does not capture on every throttle tick. Render mode reads
#     the GL widget's grabFramebuffer() (mpv screenshot-raw is black with
#     vo=libmpv); the wid path falls back to engine.capture_frame() via
#     getattr (absent backend -> no preview), building a QImage from the
#     bgr0 bytes (Format_BGR888, w*4 stride). The snapshot is a BIG
#     (>=320x180, window-scaled) rounded thumbnail painted by
#     ScrubPreviewOverlay above the video, never clipped by the 64 px OSD
#     strip, stamped with the frame position via Xbox360Osd._fmt_time.
#     After the capture RETURNS, re-check _scrub_active and the current
#     fraction: a release during grabFramebuffer/capture_frame drops the
#     frame instead of resurrecting a cleared preview (Loki race).
#     osd.set_preview(image) remains the capture hand-off/test seam.
#     On release: final seek_to for the last fraction (no capture),
#     osd.set_preview(None), scrub_preview.clear() and reset the
#     throttle/capture clocks. The preview exists ONLY between press and
#     release.
#   * _handle_key mapping (return False when unmapped; a consumed key
#     reveals the OSD ONLY while engine.menu_active is FALSE; Qt.Key enum
#     from PyQt6.QtCore):
#       Key_Up/Down/Left/Right -> engine.menu_nav('up'/'down'/'left'/'right')
#       Key_Return/Key_Enter -> menu active: menu_select else play_pause
#       Key_Space -> play_pause
#       Key_Backspace -> menu_back
#       Key_X -> show_root_menu      Key_Y -> show_title_menu
#       Key_S -> stop                Key_F -> _cycle_speed(+1)
#       Key_R -> _cycle_speed(-1)    Key_N -> next_chapter
#       Key_P -> previous_chapter
#       Key_I -> cycle_audio         Key_O -> cycle_subtitle
#       Key_Plus/Key_Equal -> adjust_volume(+5.0)
#       Key_Minus -> adjust_volume(-5.0)
#       Key_E -> eject
#       Key_A -> cycle_aspect() (guarded with getattr)
#       Key_M -> GUIDE: toggle the OSD anywhere (visible -> hide; hidden ->
#       show_osd), including inside the menu. This is the explicit way to
#       summon or dismiss transport controls.
#       Key_Escape -> hide OSD ONLY (never reveals it)
#     OSD-reveal rule: implicit reveal (keys, pointer) happens only while
#     NOT in the menu. Entering the menu (_on_menu_active True) hides the
#     OSD; the DVD menu itself is the interface. Key_M is the only guide
#     toggle at any time.
#       Ctrl+F (handled directly in keyPressEvent, not _handle_key):
#       toggle fullscreen -> showNormal()/showFullScreen() + reveal OSD
#       Ctrl+L (handled directly in keyPressEvent, not _handle_key):
#       open the browse dialog (same ISO dialog / VIDEO_TS folder fallback
#       as _on_browse_clicked) and, when a path is picked, set self.source
#       text and IMMEDIATELY _load_source(picked, quiet=False) — file
#       loading is one shortcut away even with the bar hidden.
#       Ctrl+H (handled directly in keyPressEvent, not _handle_key):
#       toggle HelpOverlay. Opening hides the home/grid overlays and OSD but
#       NEVER touches playback; Esc or Ctrl+H closes it and consumes the
#       key. HelpOverlay.BINDINGS is the authoritative list of keybindings
#       and must stay in sync with the README keybindings table.
#   * _cycle_speed(direction): current = engine.speed; ladder = FF_SPEEDS;
#     direction +1: next step above current (wrap to 2.0 at top? NO: wrap
#     from 16x back to 1.0); direction -1: previous step below current
#     (from 1.0 wrap to 16x); engine.set_speed(step). 1.0 = normal speed.
#   * _poll_gamepad: QTimer(GAMEPAD_POLL_MS); lazy `import inputs`; when
#     available and get_gamepad() non-empty: dpad -> menu_nav, a ->
#     select/play_pause (menu-aware), b -> menu_back, x -> root menu,
#     y -> title menu, start -> play_pause, back -> stop, lb -> prev
#     chapter, rb -> next chapter. Edge-triggered: act only when a control
#     changes vs last poll (keep a tuple snapshot). ImportError -> stop
#     the timer (log once). get_gamepad() raising -> swallow.
#   * _on_load_clicked: text = source.text().strip(); when empty use the
#     first list_dvd_devices() result if any, else QMessageBox.warning
#     ('No source') and return; then _load_source(text, quiet=False).
#   * HOME-MENU BOOT (console behavior — the disc summons the dashboard):
#     after _attach_engine() call _auto_detect_disc() AT MOST ONCE PER APP
#     RUN (it can only observe the startup IDLE state): prime the per-device
#     presence snapshot then OPEN HomeMenu (DVD blade refreshed) — launch
#     NEVER auto-loads a disc. A QTimer (DISC_POLL_MS, always running) calls
#     _poll_disc_insertion(): keep a per-device presence snapshot COMMITTED
#     BEFORE ACTING; on an edge INTO True from any non-True value
#     (False->True or tray-open None->True) WHEN engine.state in
#     {IDLE, STOPPED, EJECTED, ERROR} AND self._loaded_source != device
#     (a disc already loaded is never re-summoned by a transient drive blip)
#     OPEN HomeMenu with the DVD blade refreshed — NEVER _load_source: the
#     user chooses a blade entry. Removal is debounced: on >=2 consecutive
#     non-True polls (~2 * DISC_POLL_MS) for the currently loaded device:
#     status 'disc removed' and clear _loaded_source. _on_eject_clicked
#     clears _loaded_source immediately after eject so closing the tray
#     with a disc re-opens the dashboard.
#   * HomeMenu overlay (A3): full-window NATIVE child window with a
#     frosted translucent veil, shown via _open_home(); mpv renders into
#     video's native X window, so only a native sibling can stack above it.
#     The 2026 veil uses alpha 172 (VEIL_ALPHA): low enough that the live
#     DVD menu is visible through the glass, high enough for text contrast.
#     The veil covers the video while playback keeps running. The DVD blade
#     is ACTIVE from construction (index 0, never -1), so the green
#     border/label and its entry rows paint on the very first frame. On open
#     the DVD blade is refreshed from list_dvd_devices() + _disc_presence; PVD
#     labels are read on a daemon thread through HomeMenu (DiscIdentity.label
#     guarded with getattr) and never block the GUI. DVD entry -> set source
#     text + _load_source(device, quiet=False); USB entry -> resolve
#     VIDEO_TS (dir or ISO) then load; Storage entry -> _browse_for_source()
#     then load (Ctrl+L flow). Escape closes the menu only; G opens it;
#     M keeps toggling the OSD independently. Keys are captured by the menu
#     while it is visible (no engine actions leak through). Enter is
#     edge-triggered: a repeat/double press inside HomeMenu.SELECT_DEBOUNCE_S
#     (0.35 s) is ignored so one blade press starts exactly one load.
#   * LIVE TITLE PREVIEW (behind-the-glass): when the DVD blade is
#     highlighted and a present disc exists, _open_home starts exactly one
#     engine.load(device) per blade-session (self._preview_started guard; a
#     blade away/back or a reopen never reloads). It only boots from an
#     idle-like state {IDLE, STOPPED, EJECTED, ERROR}, so it never interrupts
#     live playback, and it emits no modal dialogs (errors are logged/status
#     only while previewing). HomeMenu.blade_changed -> _sync_home_preview.
#     Insertion/launch use _summon_home(): dashboard only, preview dormant
#     (A1: insertion never loads). ENTER while previewing calls
#     _reveal_preview(): the dashboard hides, the ALREADY-RUNNING menu is
#     revealed with no second load, and focus drops to the video/menu.
#     The deferred resume prompt (disc_hash_ready during preview stashes
#     _preview_hash/_preview_saved) is shown at that Enter moment, never
#     during the quiet boot. Esc from the revealed menu calls
#     _return_to_dashboard(): HomeMenu returns while the menu keeps running.
#     _teardown_preview() clears all preview state on eject, loaded-disc
#     removal (>=2 polls) and app close (stop guarded to preview states).
#   * Platform gate (fix): app.choose_platform decides QT_QPA_PLATFORM
#     before QApplication.main() creates the app — render mode on a Wayland
#     session gets native Wayland; the wid path gets xcb (XWayland) because
#     it needs a real X11 window id. window.py never mutates the environment
#     at import time. If native Wayland survives anyway (no DISPLAY, or an
#     explicit QT_QPA_PLATFORM choice), both initializeGL's wid fallback and
#     _attach_engine refuse wid embedding at runtime instead of passing a
#     wl_surface pointer to Xlib, which aborts the process with an
#     uncatchable X BadWindow error on first load.
#   * HomeMenu presentation (2026 redesign; paint-only, no QSS/assets):
#     structured HomeEntry(title, subtitle, payload) rows (entries() stays
#     the legacy (text, payload) projection). Frosted glassmorphism: a dark
#     translucent veil (VEIL_ALPHA 172) with soft green glows, gradient panel
#     fills, green #74C329 / bright #9FE855 accents, text #EAEEE7/#9EA99E/
#     #606C60 and 1 px hairline borders. PANEL_RADIUS 16 px, ROW_RADIUS 14 px,
#     32 px margins, 20 px blade gaps, 28 px section spacing. Title band 22 pt
#     bold with a fading green rule and an optional "LIVE TITLE PREVIEW" chip;
#     three <=320 px blades, uppercase 16 pt; entry rows 14 pt title + 10 pt
#     subtitle, selected = green glow + 4 px bar + bright text; hover = lighter
#     fill. Empty blades paint a refined frosted card with muted guidance
#     ("No disc — insert one"); a disc whose label is still loading shows a
#     muted "Reading disc…" placeholder, never a blank row. Footer: centered
#     glass key-hint capsules "Enter select · ←/→ blades · Esc back ·
#     Ctrl+H help". Hover is pointer-only; navigation/selection/load/debounce
#     contracts unchanged.
#   * ChapterGrid overlay (C): key C in PLAYING/PAUSED opens it with
#     engine.chapter_list; arrows navigate, Enter schedules ONE coalesced
#     engine.seek_to(chapter time) after the grid's 350 ms settle; Escape or
#     closing cancels the pending seek (no seek after close).
#   * Window title: 'DVD Player — {label}' when engine.disc_label is known
#     (getattr defensive), else 'DVD Player' (updated on state/position/
#     disc-hash changes).
#   * MPRIS (G): MainWindow.start_mpris() creates/registers MprisService on
#     the session bus (duplicate registration non-fatal), stop_mpris()
#     releases it; closeEvent calls stop_mpris(). MprisService maps
#     PlayPause/Play/Pause/Stop -> engine, Next/Previous -> chapters,
#     Seek(offset us) -> seek_to(time_pos+offset), Position via
#     position_changed + 1 s QTimer, Volume via engine.volume/adjust_volume,
#     PlaybackStatus PLAYING->Playing / PAUSED->Paused / else Stopped,
#     Metadata xesam:title = disc_label (getattr default 'DVD'). Bus
#     acquisition is injectable for offscreen tests.
#   * _load_source(source, quiet): the shared load flow. _attach_engine()
#     first (idempotent; re-creates mpv after an eject). problems =
#     check_environment(source): when problems — quiet: set self.status to
#     the joined text (no modal, zero-click flow); not quiet:
#     QMessageBox.warning joined text ('…\nProceeding anyway.') but ONLY
#     when the source exists (a missing source skips the warning so the
#     subsequent critical reads as one clear message). When the source does
#     not exist (os.stat fails / os.path.exists false) and is not a block
#     device (stat.S_ISBLK): quiet -> self.status 'cannot access disc
#     source …'; not quiet -> QMessageBox.critical with the same text;
#     return. The GUI NEVER computes a disc hash here: it then calls
#     _play_source(source) directly so playback starts immediately.
#     _play_source/_start_preview gate on the attach state: while the GL
#     chain is pending they store the newest load in _pending_load (nothing
#     calls engine.load); once attach_complete(True) arrives the window
#     flushes it. After attach_complete(False) loads are refused with the
#     graceful renderer status, so the raw 'engine not attached' error can
#     never reach the user. The slot NEVER outlives teardown: _teardown_preview
#     (eject/disc-removal/close) and closeEvent clear it, _window_closed makes
#     the attach-complete slot a no-op, and a post-close queue attempt is
#     dropped — a late attach_complete can never call engine.load after
#     eject/dispose.
#   * _on_disc_hash(disc_hash): connected to engine.disc_hash_ready (the
#     backend emits it from a worker thread once the hash is known).
#     saved = ResumeStore().get(disc_hash); no entry -> nothing. Show the
#     resume prompt ONLY while playback is still early: engine.state is
#     PlayerState.LOADING, or less than 15 s elapsed since _load_source
#     started (self._load_started_at — a slow hash arrival on USB still
#     gets its prompt). Otherwise playback has moved on: silently skip.
#     When shown, delegate to _show_resume_prompt(disc_hash, saved).
#   * _show_resume_prompt: QMessageBox.question 'Resume playback from
#     mm:ss?' (Yes -> ONLY engine.resume_disc(saved), applied immediately
#     by the backend when PLAYING or staged otherwise; No -> ONLY
#     ResumeStore().forget(hash)). The prompt NEVER loads: _load_source /
#     _play_source is the only load path, since playback already started
#     before the async hash arrived.
#   * _on_osd_button: 'play_pause' -> play_pause; 'stop' -> stop;
#     'ff' -> _cycle_speed(+1); 'rw' -> _cycle_speed(-1); 'prev' ->
#     previous_chapter; 'next' -> next_chapter; 'audio' -> cycle_audio;
#     'subtitle' -> cycle_subtitle; 'menu' -> show_root_menu; 'eject' ->
#     QMessageBox.question(self, "Eject", "Eject the disc?",
#     Yes|No, default Yes) — only Yes triggers _on_eject_clicked(), so an
#     accidental tap or click can never eject the disc.
#   * _on_eject_clicked: engine.eject(); status 'EJECTED'.
#   * _on_browse_clicked: QFileDialog.getOpenFileName('.iso (*.iso)');
#     when canceled try getExistingDirectory ('folder with VIDEO_TS');
#     set self.source text when picked (no load — Ctrl+L loads directly).
#   * closeEvent: engine.dispose(); event.accept().
#   * _sync_osd: osd.set_transport(state=engine.state, speed=engine.speed,
#     time_pos=engine.time_pos, duration=engine.duration,
#     chapter=engine.chapter, chapter_count=(len(engine.chapter_list) when
#     engine.player else None), audio_lang/current subtitle_lang from
#     cached tracks matched to engine player.aid/sid); called from
#     _on_position/_on_engine_state/_on_menu_active/_on_tracks.
class MainWindow(QMainWindow):
    """Top-level window: video surface, transport bar and input wiring."""

    SCRUB_THROTTLE_MS = 250
    SCRUB_CAPTURE_MS = 200

    def __init__(self, engine: DvdPlaybackEngine, source: str | None = None) -> None:
        """Build the window and connect engine signals and timers."""
        super().__init__()
        self.engine = engine
        self.setMinimumSize(854, 480)
        self._initial_source: str | None = source if source else None
        self._pending_source: str | None = None
        self._loaded_source: str | None = None
        self._audio_tracks: list[dict[str, object]] = []
        self._sub_tracks: list[dict[str, object]] = []
        self._gamepad_states: dict[str, int] = {}
        self._gamepad_unavailable = False
        self._disc_presence: dict[str, bool | None] = {}
        self._disc_gone_streak: dict[str, int] = {}
        self._load_started_at = 0.0
        self._auto_detected = False
        self._startup_scheduled = False
        self._startup_done = False
        self._scrub_active = False
        self._scrub_fraction: float | None = None
        self._scrub_last_seek = 0.0
        self._scrub_last_capture = 0.0
        self._mpris: MprisService | None = None
        self._mpris_started = False
        self._window_label: str | None = None
        # Live DVD title preview behind the dashboard (one load per session).
        self._preview_session = False
        self._preview_started = False
        self._preview_running = False
        self._preview_revealed = False
        self._preview_source: str | None = None
        self._preview_hash: str | None = None
        self._preview_saved: dict | None = None  # type: ignore[type-arg]
        self._preview_next_open = True

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # R6: DVD_PLAYER_RENDER=auto/render asks for the GL surface; so does
        # any platform where wid embedding is unsafe (native Wayland), where
        # attach_render is the only alternative to the graceful status.
        self._render_mode = render_surface_required(self.engine)
        self.video: QWidget = (
            VideoGLWidget(self.engine, central)
            if self._render_mode
            else QWidget(central)
        )
        self.video.setAutoFillBackground(True)
        video_palette = self.video.palette()
        video_palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0))
        self.video.setPalette(video_palette)
        self.video.installEventFilter(self)
        layout.addWidget(self.video, 1)
        # The GL surface must not initialize (a possibly slow probe of the
        # driver) before the menu paints. Keep it hidden until the dashboard
        # has produced its first frame; _startup_after_first_frame shows it.
        if isinstance(self.video, VideoGLWidget):
            self.video.hide()
        # Load gate: a GL surface is attached only after initializeGL has
        # settled; until then loads wait in one window-level slot. The wid
        # path attaches synchronously in _attach_engine, so it starts ready.
        self._engine_attached = not isinstance(self.video, VideoGLWidget)
        self._attach_failed = False
        self._pending_load: tuple[str, bool, bool] | None = None
        self._window_closed = False
        if isinstance(self.video, VideoGLWidget):
            self.video.attach_complete.connect(self._on_video_attach_complete)

        bottom = QWidget(central)
        bar = QHBoxLayout(bottom)
        bar.setContentsMargins(6, 6, 6, 6)
        bar.setSpacing(6)
        self.source = QLineEdit(bottom)
        self.source.setPlaceholderText(
            "/dev/sr0 | /path/movie.iso | /path/to/VIDEO_TS"
        )
        browse_button = QPushButton("Browse…", bottom)
        load_button = QPushButton("Load / Play", bottom)
        eject_button = QPushButton("Eject", bottom)
        self.status = QLabel("IDLE", bottom)
        self.status.setMinimumWidth(90)
        bar.addWidget(self.source, 1)
        bar.addWidget(browse_button)
        bar.addWidget(load_button)
        bar.addWidget(eject_button)
        bar.addWidget(self.status)
        layout.addWidget(bottom)
        # Console contract: video only. Keep the widgets constructed (and
        # reachable for tests) but hide the noisy strip by default.
        bottom.hide()
        self.setCentralWidget(central)

        # The OSD floats above the window so it can hug the bottom edge.
        self.osd = ScrubPreviewOsd(self)
        self.osd.hide()
        # Big live scrub snapshot, stacked above the video (never clipped by
        # the 64 px strip). Painted only between scrub press and release.
        self.scrub_preview = ScrubPreviewOverlay(self)
        self.scrub_preview.hide()

        # Full-window launcher and chapter grid overlays (paint-only).
        self.home = HomeMenu(self)
        self.home.hide()
        self.chapter_grid = ChapterGrid(self)
        self.chapter_grid.hide()
        self.help = HelpOverlay(self)
        self.help.hide()
        self.home.entry_selected.connect(self._on_home_selected)
        self.home.browse_requested.connect(self._on_home_browse)
        self.home.close_requested.connect(self._close_home)
        self.home.blade_changed.connect(self._on_home_blade_changed)
        self.home.first_paint.connect(self._on_home_first_paint)
        self.chapter_grid.chapter_chosen.connect(self._on_chapter_chosen)
        self.chapter_grid.close_requested.connect(self._close_chapter_grid)
        self.help.close_requested.connect(self._close_help)

        browse_button.clicked.connect(self._on_browse_clicked)
        load_button.clicked.connect(self._on_load_clicked)
        eject_button.clicked.connect(self._on_eject_clicked)
        self.source.returnPressed.connect(self._on_load_clicked)
        self.osd.button_clicked.connect(self._on_osd_button)
        self.osd.scrub_pressed.connect(self._on_scrub_pressed)
        self.osd.scrub_moved.connect(self._on_scrub_moved)
        self.osd.scrub_released.connect(self._on_scrub_released)

        engine.state_changed.connect(self._on_engine_state)
        engine.position_changed.connect(self._on_position)
        engine.tracks_changed.connect(self._on_tracks)
        engine.menu_active_changed.connect(self._on_menu_active)
        engine.error_occurred.connect(self._on_engine_error)
        disc_hash_ready = getattr(engine, "disc_hash_ready", None)
        if disc_hash_ready is not None:
            disc_hash_ready.connect(self._on_disc_hash)

        self._gamepad_timer = QTimer(self)
        self._gamepad_timer.setInterval(GAMEPAD_POLL_MS)
        self._gamepad_timer.timeout.connect(self._poll_gamepad)
        self._gamepad_timer.start()

        self._disc_timer = QTimer(self)
        self._disc_timer.setInterval(DISC_POLL_MS)
        self._disc_timer.timeout.connect(self._poll_disc_insertion)
        self._disc_timer.start()

        # Startup work is scheduled from showEvent: boot the dashboard, then
        # probe hardware only after its first frame (see showEvent).
        self._update_window_title()

    # ── Qt events ───────────────────────────────────────────────────
    def showEvent(self, event: QShowEvent | None) -> None:
        """Boot the dashboard first; defer every device probe and attach.

        The menu paints from cached (empty) state on this first frame, so a
        spun-down USB optical drive or a slow GL driver can never delay the
        dashboard. The probe/attach pass runs from ``first_paint`` through a
        zero-delay timer, i.e. strictly after the first frame.
        """
        super().showEvent(event)
        log.info(
            "milestone window shown: geom=%sx%s",
            self.width(),
            self.height(),
        )
        if self._startup_scheduled:
            return
        self._startup_scheduled = True
        if not self._auto_detected and self._initial_source is None:
            self._show_boot_home()
            # Safety net for headless platforms that coalesce paints: never
            # leave startup hanging if no paintEvent is ever delivered.
            QTimer.singleShot(250, self._startup_after_first_frame)
        else:
            QTimer.singleShot(0, self._startup_after_first_frame)

    def _on_home_first_paint(self) -> None:
        """The dashboard is on screen: let startup proceed after this frame."""
        if self._startup_scheduled and not self._startup_done:
            log.info("milestone first frame: scheduling deferred startup")
            QTimer.singleShot(0, self._startup_after_first_frame)

    def _startup_after_first_frame(self) -> None:
        """Attach the video surface and probe devices (never before paint)."""
        if self._startup_done:
            return
        self._startup_done = True
        if isinstance(self.video, VideoGLWidget) and not self.video.isVisible():
            # The GL context initializes now, behind the already-painted menu.
            self.video.show()
        self._attach_engine()
        self.start_mpris()

    def _show_boot_home(self) -> None:
        """Paint the dashboard from cached state, without touching devices."""
        self._preview_next_open = False
        try:
            self._open_home(preview=False, probe=False)
        finally:
            self._preview_next_open = True

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Pin the overlays to the window and keep the OSD topmost."""
        super().resizeEvent(event)
        self.osd.setGeometry(0, self.height() - 64, self.width(), 64)
        self.home.setGeometry(0, 0, self.width(), self.height())
        self.chapter_grid.setGeometry(0, 0, self.width(), self.height())
        self.help.setGeometry(0, 0, self.width(), self.height())
        self.scrub_preview.setGeometry(0, 0, self.width(), self.height())
        if self.home.isVisible():
            self.home.raise_()
        if self.chapter_grid.isVisible():
            self.chapter_grid.raise_()
        if self.help.isVisible():
            self.help.raise_()
        if self.scrub_preview.isVisible():
            self.scrub_preview.raise_()
        self.osd.raise_()

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        """Route keys through the console mapping."""
        if event is None:
            return
        if (
            event.key() == Qt.Key.Key_F
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            self.osd.show_osd()
            event.accept()
            return
        if (
            event.key() == Qt.Key.Key_L
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            picked = self._browse_for_source()
            if picked:
                self.source.setText(picked)
                self._load_source(picked, quiet=False)
            event.accept()
            return
        if (
            event.key() == Qt.Key.Key_H
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if self.help.isHidden():
                self._open_help()
            else:
                self._close_help()
            event.accept()
            return
        key = event.key()
        # Esc closes the help reference only; Ctrl+H already toggled it.
        if not self.help.isHidden():
            if key == Qt.Key.Key_Escape.value:
                self._close_help()
            event.accept()
            return
        # Esc from a revealed live menu returns to the dashboard; the DVD
        # menu keeps running behind the frosted veil (no reload).
        if self._preview_revealed and key == Qt.Key.Key_Escape.value:
            self._return_to_dashboard()
            event.accept()
            return
        # Guide G opens the dashboard anywhere; M remains an OSD-only toggle.
        if key == Qt.Key.Key_G.value:
            self._open_home()
            event.accept()
            return
        if key == Qt.Key.Key_M.value:
            if self.osd.isVisible():
                self.osd.hide()
            else:
                self.osd.show_osd()
            if self.osd.isVisible():
                self.osd.raise_()
            event.accept()
            return
        if not self.home.isHidden():
            if key == Qt.Key.Key_Escape.value:
                self._close_home()
            elif (
                key in (Qt.Key.Key_Return.value, Qt.Key.Key_Enter.value)
                and self._preview_running
            ):
                # ENTER reveals the already-booted menu (never loads twice)
                # and only now may the deferred resume prompt appear.
                self._reveal_preview()
            else:
                self.home.handle_key(key)
            event.accept()
            return
        if not self.chapter_grid.isHidden():
            if key in (
                Qt.Key.Key_Escape.value,
                Qt.Key.Key_C.value,
            ):
                self._close_chapter_grid()
            else:
                self.chapter_grid.handle_key(key)
            event.accept()
            return
        if self._handle_key(key):
            event.accept()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, a0: QObject | None, a1: QEvent | None) -> bool:
        """Forward mouse activity on the video surface."""
        if a0 is self.video and isinstance(a1, QMouseEvent):
            self._on_video_mouse(a0, a1)
        return super().eventFilter(a0, a1)  # type: ignore[arg-type]

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Release the engine before the window goes away."""
        self._window_closed = True
        self._pending_load = None  # a late attach must never flush after close
        self._gamepad_timer.stop()
        self._disc_timer.stop()
        self.stop_mpris()
        self._teardown_preview()
        self._close_help()
        self._close_chapter_grid()
        self._close_home()
        self.osd.hide()
        self.osd._hide_timer.stop()
        self.engine.dispose()
        if event is not None:
            event.accept()

    # ── input mapping ───────────────────────────────────────────────
    def _handle_key(self, key: int) -> bool:
        """Dispatch one key; return True when consumed."""
        codes = Qt.Key
        if key == codes.Key_Up.value:
            self.engine.menu_nav("up")
        elif key == codes.Key_Down.value:
            self.engine.menu_nav("down")
        elif key == codes.Key_Left.value:
            self.engine.menu_nav("left")
        elif key == codes.Key_Right.value:
            self.engine.menu_nav("right")
        elif key in (codes.Key_Return.value, codes.Key_Enter.value):
            if self.engine.menu_active:
                self.engine.menu_select()
            else:
                self.engine.play_pause()
        elif key == codes.Key_Space.value:
            self.engine.play_pause()
        elif key == codes.Key_Backspace.value:
            self.engine.menu_back()
        elif key == codes.Key_X.value:
            self.engine.show_root_menu()
        elif key == codes.Key_Y.value:
            self.engine.show_title_menu()
        elif key == codes.Key_S.value:
            self.engine.stop()
        elif key == codes.Key_F.value:
            self._cycle_speed(1)
        elif key == codes.Key_R.value:
            self._cycle_speed(-1)
        elif key == codes.Key_N.value:
            self.engine.next_chapter()
        elif key == codes.Key_P.value:
            self.engine.previous_chapter()
        elif key == codes.Key_I.value:
            self.engine.cycle_audio()
        elif key == codes.Key_O.value:
            self.engine.cycle_subtitle()
        elif key in (codes.Key_Plus.value, codes.Key_Equal.value):
            self.engine.adjust_volume(5.0)
        elif key == codes.Key_Minus.value:
            self.engine.adjust_volume(-5.0)
        elif key == codes.Key_E.value:
            self._on_eject_clicked()
        elif key == codes.Key_M.value:
            if self.osd.isVisible():
                self.osd.hide()
            else:
                self.osd.show_osd()
            return True
        elif key == codes.Key_G.value:
            self._open_home()
            return True
        elif key == codes.Key_A.value:
            cycle_aspect = getattr(self.engine, "cycle_aspect", None)
            if callable(cycle_aspect):
                cycle_aspect()
            return True
        elif key == codes.Key_C.value:
            if self.engine.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                self._open_chapter_grid()
            return True
        elif key == codes.Key_Escape.value:
            self.osd.hide()
            return True
        else:
            return False
        if not self.engine.menu_active:
            self.osd.show_osd()
        return True

    def _cycle_speed(self, direction: int) -> None:
        """Step through the trick-play ladder (2x, 4x, 8x, 16x, 1x)."""
        current = float(self.engine.speed)
        ladder = FF_SPEEDS
        if direction > 0:
            above = [step for step in ladder if step > current + 1e-9]
            target = above[0] if above else 1.0
        else:
            below = [step for step in ladder if step < current - 1e-9]
            if below:
                target = below[-1]
            elif current <= 1.0 + 1e-9:
                target = ladder[-1]  # wrap from normal speed
            else:
                target = 1.0  # step back to normal speed
        self.engine.set_speed(target)

    def _poll_gamepad(self) -> None:
        """Poll an optional gamepad and edge-trigger console actions."""
        try:
            import inputs
        except ImportError:
            self._gamepad_timer.stop()
            if not self._gamepad_unavailable:
                self._gamepad_unavailable = True
                log.info(
                    "gamepad support unavailable: optional 'inputs' package missing"
                )
            return
        try:
            pads = inputs.get_gamepad()
        except Exception:  # noqa: BLE001 - third-party driver errors
            return
        if not pads:
            return
        try:
            pad_iter = iter(pads[0])
        except (TypeError, AttributeError):
            return  # malformed third-party payload: degrade silently
        states: dict[str, int] = {}
        for control in pad_iter:
            code = str(getattr(control, "code", ""))
            try:
                states[code] = int(getattr(control, "state", 0))
            except (TypeError, ValueError):
                continue
        for code, state in states.items():
            if state != self._gamepad_states.get(code, 0):
                self._gamepad_changed(code, state)
        self._gamepad_states = states

    def _gamepad_changed(self, code: str, state: int) -> None:
        """Map one changed gamepad control to an engine action."""
        if code == "ABS_HAT0Y":
            if state < 0:
                self.engine.menu_nav("up")
            elif state > 0:
                self.engine.menu_nav("down")
            else:
                return
        elif code == "ABS_HAT0X":
            if state < 0:
                self.engine.menu_nav("left")
            elif state > 0:
                self.engine.menu_nav("right")
            else:
                return
        elif state != 1:
            return
        elif code in ("BTN_DPAD_UP", "BTN_UP"):
            self.engine.menu_nav("up")
        elif code in ("BTN_DPAD_DOWN", "BTN_DOWN"):
            self.engine.menu_nav("down")
        elif code in ("BTN_DPAD_LEFT", "BTN_LEFT"):
            self.engine.menu_nav("left")
        elif code in ("BTN_DPAD_RIGHT", "BTN_RIGHT"):
            self.engine.menu_nav("right")
        elif code in ("BTN_SOUTH", "BTN_A"):
            if self.engine.menu_active:
                self.engine.menu_select()
            else:
                self.engine.play_pause()
        elif code in ("BTN_EAST", "BTN_B"):
            self.engine.menu_back()
        elif code in ("BTN_WEST", "BTN_X"):
            self.engine.show_root_menu()
        elif code in ("BTN_NORTH", "BTN_Y"):
            self.engine.show_title_menu()
        elif code == "BTN_START":
            self.engine.play_pause()
        elif code in ("BTN_SELECT", "BTN_BACK"):
            self.engine.stop()
        elif code in ("BTN_TL", "BTN_L1"):
            self.engine.previous_chapter()
        elif code in ("BTN_TR", "BTN_R1"):
            self.engine.next_chapter()
        else:
            return
        self.osd.show_osd()

    # ── engine signal handlers ──────────────────────────────────────
    def _on_engine_state(self, state: PlayerState) -> None:
        """Reflect the new state in the status label and OSD."""
        log.debug("state -> %s", state.name)
        self._set_status(state.name)
        self._update_window_title()
        self._sync_osd()

    def _on_position(self, seconds: float) -> None:
        """Repaint the OSD as playback advances."""
        del seconds
        self._update_window_title()
        self._sync_osd()

    def _on_tracks(self, audio: list, subs: list) -> None:  # type: ignore[type-arg]
        """Cache the current track lists for language readouts."""
        self._audio_tracks = [
            dict(item) for item in audio if isinstance(item, dict)
        ]
        self._sub_tracks = [
            dict(item) for item in subs if isinstance(item, dict)
        ]
        self._sync_osd()

    def _on_menu_active(self, active: bool) -> None:
        """Hide the OSD when the menu appears; the menu is the interface."""
        if active:
            self.osd.hide()
        self._sync_osd()

    def _on_engine_error(self, message: str) -> None:
        """Show a critical dialog for backend failures."""
        self._set_status("ERROR")
        if self._preview_running and not self._preview_revealed:
            # No modal may interrupt the behind-the-glass preview.
            log.warning("preview error: %s", message)
            return
        QMessageBox.critical(self, "Playback error", message)

    # ── Help overlay (Ctrl+H) ───────────────────────────────────────
    def _open_help(self) -> None:
        """Show the control reference over the playing video."""
        self.home.hide()
        self.chapter_grid.close_grid()
        self.osd.hide()
        self.help.setGeometry(0, 0, self.width(), self.height())
        self.help.show()
        self.help.raise_()

    def _close_help(self) -> None:
        """Dismiss the control reference only; playback is untouched."""
        self.help.hide()

    # ── HomeMenu overlay (A1/A3) ────────────────────────────────────
    def _open_home(self, preview: bool = True, probe: bool = True) -> None:
        """Show the dashboard over the still-playing video.

        ``probe=False`` is the startup path: it refreshes from the cached
        presence snapshot only, so opening the menu never waits on an ioctl.
        """
        if not preview:
            self._preview_next_open = False
        preview = self._preview_next_open
        self._preview_next_open = True
        devices = (
            list_dvd_devices() if probe else list(self._disc_presence)
        )
        present: dict[str, bool | None] = {}
        for device in devices:
            present[device] = self._disc_presence.get(device)
        if self.home.isHidden() and not self._preview_running:
            # A fresh dashboard starts a fresh blade-session: one preview
            # load is allowed, and only after this point.
            self._preview_started = False
            self._preview_source = None
            self._preview_saved = None
            self._preview_hash = None
        self.home.refresh(devices, present)
        self.home.setGeometry(0, 0, self.width(), self.height())
        self.chapter_grid.close_grid()
        self.home.setGeometry(0, 0, self.width(), self.height())
        self.home.show()
        self.home.raise_()
        self.osd.hide()
        self._preview_revealed = False
        self._preview_session = preview
        self.home.set_preview_live(self._preview_running)
        if preview:
            self._sync_home_preview()
        log.debug(
            "home menu opened: visible=%s geom=%s window=%sx%s devices=%s",
            self.home.isVisible(),
            self.home.geometry(),
            self.width(),
            self.height(),
            devices,
        )

    def _close_home(self) -> None:
        """Dismiss the dashboard only; playback continues untouched."""
        self.home.hide()

    def _summon_home(self) -> None:
        """Insertion/launch summon: dashboard only, preview stays dormant."""
        self._preview_next_open = False
        try:
            self._open_home()
        finally:
            self._preview_next_open = True

    # ── live title preview lifecycle ────────────────────────────────
    def _on_home_blade_changed(self, _index: int) -> None:
        """A blade highlight moved: allow and sync the live preview."""
        self._preview_session = True
        self._sync_home_preview()

    def _sync_home_preview(self) -> None:
        """Boot the highlighted DVD once per blade-session (never twice)."""
        if self.home.isHidden() or not self._preview_session:
            return
        target = self.home.preview_device()
        if target is None:
            return
        if self._preview_started and target == self._preview_source:
            return
        if self._preview_started:
            return  # strictly one load per dashboard session
        if self._loaded_source == target:
            return
        if self.engine.state not in (
            PlayerState.IDLE,
            PlayerState.STOPPED,
            PlayerState.EJECTED,
            PlayerState.ERROR,
        ):
            return  # never interrupt live playback for a preview
        self._start_preview(target)

    def _start_preview(self, source: str) -> None:
        """Load the disc menu behind the dashboard without any dialog."""
        self._preview_session = True
        self._preview_started = True
        self._preview_running = True
        self._preview_source = source
        self._loaded_source = source
        self._pending_source = source
        self._load_started_at = time.monotonic()
        self.home.set_preview_live(True)
        if not self._engine_attached:
            self._queue_load(source, quiet=True, preview=True)
            return
        self._perform_load(source, preview=True)

    def _reveal_preview(self) -> None:
        """ENTER: show the already-running menu, focus it, defer no longer."""
        if not self._preview_running:
            return
        saved = self._preview_saved
        disc_hash = self._preview_hash
        self._preview_saved = None
        self._preview_revealed = True
        self.home.hide()
        self.osd.hide()
        self.video.setFocus(Qt.FocusReason.OtherFocusReason)
        if saved is not None and disc_hash is not None:
            # The resume prompt belongs to the reveal moment, never to the
            # quiet preview boot.
            self._show_resume_prompt(disc_hash, saved)

    def _return_to_dashboard(self) -> None:
        """Esc from a revealed menu: dashboard returns, menu keeps running."""
        self._preview_revealed = False
        self._preview_session = True
        self.home.setGeometry(0, 0, self.width(), self.height())
        self.home.set_preview_live(self._preview_running)
        self.home.show()
        self.home.raise_()
        self.osd.hide()

    def _teardown_preview(self) -> None:
        """Drop all preview state (eject, disc removal or app close)."""
        was_running = self._preview_running
        self._pending_load = None  # a queued load must not outlive teardown
        self._preview_session = False
        self._preview_started = False
        self._preview_running = False
        self._preview_revealed = False
        self._preview_source = None
        self._preview_hash = None
        self._preview_saved = None
        self.home.set_preview_live(False)
        if was_running and self.engine.state in (
            PlayerState.LOADING,
            PlayerState.MENU_ACTIVE,
            PlayerState.PLAYING,
            PlayerState.PAUSED,
        ):
            try:
                self.engine.stop()
            except PlaybackError as exc:
                log.debug("preview stop failed: %s", exc)

    def _on_home_selected(self, path: str) -> None:
        """Load the entry chosen on the DVD/USB blade."""
        log.debug("home select %r", path)
        if self._preview_running and path == self._preview_source:
            # The disc is already booted behind the glass: reveal it.
            self._reveal_preview()
            return
        source = path
        if path not in list_dvd_devices():
            resolved = self._resolve_dvd_source(path)
            if resolved is None:
                self._set_status("no DVD found")
                return
            source = resolved
        self._close_home()
        self.source.setText(source)
        self._load_source(source, quiet=False)

    def _on_home_browse(self) -> None:
        """Storage blade: reuse the Ctrl+L browse flow and load the pick."""
        picked = self._browse_for_source()
        if not picked:
            return
        self._close_home()
        self.source.setText(picked)
        self._load_source(picked, quiet=False)

    def _resolve_dvd_source(self, path: str) -> str | None:
        """Resolve a VIDEO_TS directory or ISO under a selected mount."""
        try:
            candidate = Path(path)
            if candidate.is_file():
                return str(candidate) if candidate.suffix.lower() == ".iso" else None
            if not candidate.is_dir():
                return None
            if (candidate / "VIDEO_TS").is_dir():
                return str(candidate)
            for child in sorted(candidate.iterdir()):
                try:
                    if child.is_dir() and (child / "VIDEO_TS").is_dir():
                        return str(child)
                except OSError:
                    continue
            isos = sorted(
                child for child in candidate.iterdir()
                if child.is_file() and child.suffix.lower() == ".iso"
            )
            if isos:
                return str(isos[0])
        except OSError:
            return None
        return None

    # ── ChapterGrid overlay (C1/C2) ─────────────────────────────────
    def _open_chapter_grid(self) -> None:
        """Show chapter tiles built from the engine's chapter start times."""
        chapters = getattr(self.engine, "chapter_list", [])
        self.chapter_grid.set_chapters(list(chapters))
        self.chapter_grid.setGeometry(0, 0, self.width(), self.height())
        self._close_home()
        self.osd.hide()
        self.chapter_grid.show()
        self.chapter_grid.raise_()

    def _close_chapter_grid(self) -> None:
        """Close the grid, cancelling any pending coalesced seek."""
        self.chapter_grid.close_grid()

    def _on_chapter_chosen(self, seconds: float) -> None:
        """Apply the single settled chapter seek and dismiss the grid."""
        self.engine.seek_to(seconds)
        self.chapter_grid.close_grid()

    # ── MPRIS (G1/G2) ───────────────────────────────────────────────
    def start_mpris(self) -> bool:
        """Create/register the MPRIS bridge; duplicate or no bus is safe."""
        if self._mpris is None:
            self._mpris = MprisService(self.engine, parent=self)
        if self._mpris_started:
            return True
        self._mpris_started = self._mpris.start()
        return self._mpris_started

    def stop_mpris(self) -> None:
        """Release the MPRIS service name/object (idempotent)."""
        if self._mpris is not None:
            self._mpris.stop()
        self._mpris_started = False

    def _update_window_title(self) -> None:
        """Show the disc label in the window title once it is known."""
        label = getattr(self.engine, "disc_label", None)
        text = label if isinstance(label, str) and label else None
        if text == self._window_label:
            return
        self._window_label = text
        if text:
            self.setWindowTitle(f"DVD Player — {text}")
        else:
            self.setWindowTitle("DVD Player")

    # ── buttons ─────────────────────────────────────────────────────
    def _on_load_clicked(self) -> None:
        """Resolve a source and start the shared load flow."""
        text = self.source.text().strip()
        if not text:
            devices = list_dvd_devices()
            if devices:
                text = devices[0]
            else:
                QMessageBox.warning(
                    self,
                    "No source",
                    "No DVD device was found and no source was entered.",
                )
                return
        self._load_source(text, quiet=False)

    def _probe_device(self, device: str, *, milestone: bool) -> bool | None:
        """Probe one drive, logging its duration for startup diagnosis."""
        started = time.monotonic()
        try:
            present = disc_present(device)
        except Exception:  # noqa: BLE001 - hotplug/ioctl weirdness
            present = None
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if milestone:
            log.info(
                "milestone device probe %r: present=%s duration=%.1fms",
                device,
                present,
                elapsed_ms,
            )
        else:
            log.debug(
                "device poll %r: present=%s duration=%.1fms",
                device,
                present,
                elapsed_ms,
            )
        return present

    def _auto_detect_disc(self) -> None:
        """Prime the poller snapshot and open the dashboard (never auto-load)."""
        started = time.monotonic()
        devices = list_dvd_devices()
        for device in devices:
            self._disc_presence[device] = self._probe_device(
                device, milestone=True
            )
        log.info(
            "milestone device probe total: devices=%d duration=%.1fms",
            len(devices),
            (time.monotonic() - started) * 1000.0,
        )
        self._summon_home()

    def _poll_disc_insertion(self) -> None:
        """Summon the dashboard on insertion and report removal."""
        for device in list_dvd_devices():
            present = self._probe_device(device, milestone=False)
            previous = self._disc_presence.get(device)
            # Commit the edge BEFORE acting: the dashboard re-enters the
            # event loop and this poller must then see the edge as consumed
            # instead of firing duplicate menus.
            self._disc_presence[device] = present
            if present is True:
                self._disc_gone_streak[device] = 0
                if (
                    previous is not True
                    and self._loaded_source != device
                    and self.engine.state
                    in (
                        PlayerState.IDLE,
                        PlayerState.STOPPED,
                        PlayerState.EJECTED,
                        PlayerState.ERROR,
                    )
                ):
                    # A1: insertion NEVER loads; it summons the dashboard
                    # (preview stays dormant until the user interacts).
                    self._summon_home()
            elif self._loaded_source == device:
                streak = self._disc_gone_streak.get(device, 0) + 1
                self._disc_gone_streak[device] = streak
                if streak >= 2:
                    self._set_status("disc removed")
                    self._loaded_source = None
                    if self._preview_source == device:
                        self._teardown_preview()

    def _load_source(self, source: str, quiet: bool = False) -> None:
        """Check the source, offer resume state and load it."""
        log.info("milestone load request: %r quiet=%s", source, quiet)
        self._attach_engine()
        self._load_started_at = time.monotonic()
        if source in list_dvd_devices():
            self._disc_presence[source] = True  # avoid a poller double-load
        problems = check_environment(source)
        if problems:
            joined = "\n".join(problems)
            if quiet:
                self._set_status(joined)
            elif os.path.exists(source):
                # A missing source skips the warning: the critical below is
                # the one clear message the user needs.
                QMessageBox.warning(
                    self,
                    "Environment check",
                    joined + "\nProceeding anyway.",
                )
        try:
            info = os.stat(source)
            is_block = stat.S_ISBLK(info.st_mode)
        except (OSError, ValueError):
            is_block = False
        try:
            exists = os.path.exists(source)
        except (OSError, ValueError):
            exists = False
        if not exists and not is_block:
            message = f"cannot access disc source {source!r}"
            if quiet:
                self._set_status(message)
            else:
                QMessageBox.critical(self, "Cannot access source", message)
            return
        self._pending_source = source
        self._play_source(source)

    def _on_video_attach_complete(self, attached: bool) -> None:
        """Release the queued load, or degrade gracefully when unattached."""
        log.debug("video attach complete signal: attached=%s", attached)
        if self._window_closed:
            self._pending_load = None
            log.debug("ignoring attach completion after window close")
            return
        if not attached:
            self._engine_attached = False
            self._attach_failed = True
            pending = self._pending_load
            self._pending_load = None
            log.info("milestone load dropped: attach failed")
            if pending is not None and pending[2]:
                self._preview_running = False
                self.home.set_preview_live(False)
            self._renderer_unavailable()
            return
        self._engine_attached = True
        self._attach_failed = False
        pending = self._pending_load
        self._pending_load = None
        if pending is None:
            return
        source, _quiet, preview = pending
        log.debug("releasing queued load after attach: %r preview=%s", source, preview)
        log.info("milestone load drained: %r preview=%s", source, preview)
        self._perform_load(source, preview=preview)

    def _queue_load(self, source: str, quiet: bool, preview: bool) -> None:
        """Keep only the newest load until the attach chain settles."""
        if self._window_closed:
            log.debug("dropping load queued after window close: %r", source)
            return
        log.debug("load queued until attach: %r preview=%s", source, preview)
        log.info("milestone load queued: %r preview=%s", source, preview)
        self._pending_load = (source, quiet, preview)

    def _renderer_unavailable(self) -> None:
        """Show the graceful renderer fallback status, never a raw error."""
        log.warning("renderer unavailable; suppressing load and showing status")
        self._set_status("renderer unavailable — falling back…")

    def _status_for_load_error(self, exc: PlaybackError) -> None:
        """Map an attach-related PlaybackError to the graceful status."""
        if self._attach_failed or "not attached" in str(exc):
            self._renderer_unavailable()
        else:
            self._set_status(str(exc))

    def _perform_load(self, source: str, preview: bool) -> None:
        """Run one already-authorized load for either the menu or the user."""
        log.info("milestone load started: %r preview=%s", source, preview)
        if preview:
            try:
                self.engine.load(source)
            except PlaybackError as exc:
                self._preview_running = False
                self.home.set_preview_live(False)
                log.debug("preview load failed: %s", exc)
                self._status_for_load_error(exc)
            return
        self._play_source(source)

    def _play_source(self, source: str) -> None:
        """Load ``source``; an illegal-state PlaybackError -> status only."""
        if not self._engine_attached:
            if self._attach_failed:
                self._renderer_unavailable()
            else:
                self._queue_load(source, quiet=False, preview=False)
            return
        self._loaded_source = source
        try:
            self.engine.load(source)
        except PlaybackError as exc:
            self._status_for_load_error(exc)

    def _browse_for_source(self) -> str | None:
        """Pick an ISO image (or VIDEO_TS folder); return None on cancel."""
        path, _selected = QFileDialog.getOpenFileName(
            self, "Select DVD image", "", ".iso (*.iso)"
        )
        if not path:
            path = QFileDialog.getExistingDirectory(
                self, "Select a folder with VIDEO_TS"
            )
        return path or None

    def _on_browse_clicked(self) -> None:
        """Pick an ISO image, falling back to a VIDEO_TS folder."""
        path = self._browse_for_source()
        if path:
            self.source.setText(path)

    def _on_eject_clicked(self) -> None:
        """Eject the disc and show the new state."""
        self._teardown_preview()
        self.engine.eject()
        self._loaded_source = None  # tray-close with a disc may then auto-boot
        self._set_status("EJECTED")

    # ── OSD seek bar scrubbing ──────────────────────────────────────
    def _scrub_seconds(self, fraction: float) -> float | None:
        """Map a 0..1 fraction to seconds, or None without a duration."""
        duration = self.engine.duration
        if duration is None:
            return None
        try:
            seconds_total = float(duration)
            value = float(fraction)
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(seconds_total)
            or seconds_total <= 0.0
            or not math.isfinite(value)
        ):
            return None
        return max(0.0, min(1.0, value)) * seconds_total

    def _scrub_seek(self, fraction: float, *, preview: bool) -> None:
        """Seek to ``fraction`` and optionally capture the preview frame."""
        seconds = self._scrub_seconds(fraction)
        if seconds is None:
            return
        self.engine.seek_to(seconds)
        self._scrub_last_seek = time.monotonic()
        if preview:
            self._refresh_scrub_preview()

    def _on_scrub_pressed(self, fraction: float) -> None:
        """Begin a scrub: seek immediately and show the preview."""
        self._scrub_active = True
        self._scrub_fraction = max(0.0, min(1.0, float(fraction)))
        self._scrub_seek(self._scrub_fraction, preview=True)

    def _on_scrub_moved(self, fraction: float) -> None:
        """Continue a scrub, throttling engine seeks to one per 250 ms."""
        if not self._scrub_active:
            return
        self._scrub_fraction = max(0.0, min(1.0, float(fraction)))
        elapsed = time.monotonic() - self._scrub_last_seek
        if elapsed < self.SCRUB_THROTTLE_MS / 1000.0:
            return
        self._scrub_seek(self._scrub_fraction, preview=True)

    def _on_scrub_released(self) -> None:
        """Finish a scrub: final seek, then drop the preview."""
        self._scrub_active = False
        fraction = self._scrub_fraction
        self._scrub_fraction = None
        if fraction is not None:
            self._scrub_seek(fraction, preview=False)
        self.osd.set_preview(None)
        self.scrub_preview.clear()
        self._scrub_last_seek = 0.0
        self._scrub_last_capture = 0.0

    def _render_scrub_frame(self) -> QImage | None:
        """Return the live GL framebuffer after a render-mode seek."""
        grab = getattr(self.video, "grabFramebuffer", None)
        if not callable(grab):
            return None
        try:
            image = grab()
        except (RuntimeError, SystemError, ValueError):
            return None
        if not isinstance(image, QImage) or image.isNull():
            return None
        return image

    def _wid_scrub_frame(self) -> QImage | None:
        """Return the engine-captured frame for the wid video path."""
        capture = getattr(self.engine, "capture_frame", None)
        if not callable(capture):
            return None
        try:
            frame = capture()
        except (PlaybackError, OSError, RuntimeError, SystemError, ValueError):
            return None
        if not isinstance(frame, tuple) or len(frame) != 3:
            return None
        width, height, data = frame
        if not isinstance(width, int) or not isinstance(height, int):
            return None
        if width <= 0 or height <= 0:
            return None
        if not isinstance(data, (bytes, bytearray, memoryview)):
            return None
        raw = bytes(data)
        if len(raw) < width * height * 4:
            return None
        image = QImage(
            raw,
            width,
            height,
            width * 4,
            QImage.Format.Format_BGR888,
        )
        if image.isNull():
            return None
        return image

    def _refresh_scrub_preview(self) -> None:
        """Capture the current frame and show it as the scrub preview."""
        if not self._scrub_active:
            return
        now = time.monotonic()
        if now - self._scrub_last_capture < self.SCRUB_CAPTURE_MS / 1000.0:
            return
        fraction = self._scrub_fraction
        if fraction is None:
            return
        image = (
            self._render_scrub_frame()
            if self._render_mode
            else self._wid_scrub_frame()
        )
        # A release can arrive while the frame grab runs. Re-check the
        # scrub state and fraction AFTER the capture: never resurrect a
        # preview that the release already cleared.
        if not self._scrub_active or self._scrub_fraction != fraction:
            return
        self._scrub_last_capture = now
        if image is None or image.isNull():
            return
        seconds = self._scrub_seconds(fraction)
        snapshot = image.copy()
        self.osd.set_preview(snapshot)
        self.osd.set_preview_time(seconds)
        self.scrub_preview.set_frame(
            snapshot, seconds, anchor_x=self.osd._scrub_x
        )

    def _on_osd_button(self, action: str) -> None:
        """Route an OSD transport click to the matching engine action."""
        if action == "play_pause":
            self.engine.play_pause()
        elif action == "stop":
            self.engine.stop()
        elif action == "ff":
            self._cycle_speed(1)
        elif action == "rw":
            self._cycle_speed(-1)
        elif action == "prev":
            self.engine.previous_chapter()
        elif action == "next":
            self.engine.next_chapter()
        elif action == "audio":
            self.engine.cycle_audio()
        elif action == "subtitle":
            self.engine.cycle_subtitle()
        elif action == "menu":
            self.engine.show_root_menu()
        elif action == "eject":
            answer = QMessageBox.question(
                self,
                "Eject",
                "Eject the disc?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._on_eject_clicked()

    def _on_video_mouse(self, obj: object, event: QMouseEvent) -> None:
        """Reveal the OSD on pointer activity; forward menu clicks."""
        del obj
        event_type = event.type()
        if event_type == QEvent.Type.MouseMove:
            if not self.engine.menu_active:
                self.osd.show_osd()
            elif isinstance(self.video, VideoGLWidget):
                x, y = self.video.widget_position(event)
                self.engine.menu_mouse_move(x, y)
            else:
                position = event.position()
                self.engine.menu_mouse_move(
                    int(position.x()), int(position.y())
                )
        elif (
            event_type == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            if self.engine.menu_active:
                self.engine.menu_select()
            else:
                self.osd.show_osd()

    # ── resume & OSD sync ───────────────────────────────────────────
    def _on_disc_hash(self, disc_hash: str) -> None:
        """Offer resume state when the backend delivers the disc hash."""
        self._update_window_title()
        saved = ResumeStore().get(disc_hash)
        if not saved:
            return
        if self._preview_running and not self._preview_revealed:
            # Quiet preview: stash the prompt for the ENTER reveal moment.
            self._preview_hash = disc_hash
            self._preview_saved = saved
            return
        elapsed = time.monotonic() - self._load_started_at
        early = self.engine.state is PlayerState.LOADING or elapsed < 15.0
        if not early:
            return
        self._show_resume_prompt(disc_hash, saved)

    def _show_resume_prompt(self, disc_hash: str, saved: dict) -> None:  # type: ignore[type-arg]
        """Offer to resume the already-loading disc; never load it again."""
        position = saved.get("pos")
        try:
            seconds = float(position) if isinstance(position, (int, float)) else 0.0
        except (TypeError, ValueError):
            seconds = 0.0
        if math.isnan(seconds) or math.isinf(seconds):
            seconds = 0.0
        answer = QMessageBox.question(
            self,
            "Resume playback",
            f"Resume playback from {Xbox360Osd._fmt_time(seconds)}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.engine.resume_disc(saved)
        else:
            ResumeStore().forget(disc_hash)

    def _set_status(self, text: str) -> None:
        """Show status text, elided to fit (full text in the tooltip)."""
        self.status.setToolTip(text)
        metrics = self.status.fontMetrics()
        width = max(180, self.status.width() - 4)
        self.status.setText(
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)
        )

    def _sync_osd(self) -> None:
        """Push the engine's current transport facts into the overlay."""
        engine = self.engine
        player = engine.player
        chapter_count: int | None = None
        audio_lang: str | None = None
        subtitle_lang: str | None = None
        if player is not None:
            chapter_count = len(engine.chapter_list)
            aid: object = None
            sid: object = None
            try:
                aid = player.aid
                sid = player.sid
            except (OSError, SystemError, AttributeError, TypeError, ValueError):
                aid = None
                sid = None
            audio_lang = self._track_lang(self._audio_tracks, aid)
            subtitle_lang = self._track_lang(self._sub_tracks, sid)
        self.osd.set_transport(
            state=engine.state,
            speed=engine.speed,
            time_pos=engine.time_pos,
            duration=engine.duration,
            chapter=engine.chapter,
            chapter_count=chapter_count,
            audio_lang=audio_lang,
            subtitle_lang=subtitle_lang,
        )

    @staticmethod
    def _track_lang(tracks: list[dict[str, object]], chosen: object) -> str | None:
        """Return the language of the track whose id equals ``chosen``."""
        for track in tracks:
            if track.get("id") == chosen:
                lang = track.get("lang")
                return lang if isinstance(lang, str) else None
        return None

    def _attach_engine(self) -> None:
        """Bind mpv to the video widget, then honour an initial source."""
        started = time.monotonic()
        has_render = callable(getattr(self.engine, "attach_render", None))
        platform = VideoGLWidget._platform_name()
        log.info(
            "milestone attach attempt: mode=%s platform=%s",
            "render" if isinstance(self.video, VideoGLWidget) else "wid",
            platform,
        )
        if isinstance(self.video, VideoGLWidget) and has_render:
            # The GL context is ready only after show(); initializeGL()
            # performs the attach_render() handshake.
            log.info(
                "milestone attach result: render surface armed duration=%.1fms",
                (time.monotonic() - started) * 1000.0,
            )
            log.debug("render API surface armed for GL attach")
        else:
            if isinstance(self.video, VideoGLWidget):
                log.info("render API unavailable; falling back to wid embedding")
            if not wid_embedding_safe():
                log.error(
                    "wid embedding cannot work on Qt platform %r: set "
                    "DVD_PLAYER_RENDER=auto or use xcb",
                    platform,
                )
                self._attach_failed = True
                self._engine_attached = False
                log.info(
                    "milestone attach result: failed wayland duration=%.1fms",
                    (time.monotonic() - started) * 1000.0,
                )
                self._renderer_unavailable()
                return
            try:
                self.engine.attach_widget(int(self.video.winId()))
            except PlaybackError as exc:
                log.exception("could not attach the video widget: %s", exc)
                self._attach_failed = True
                self._engine_attached = False
                log.info(
                    "milestone attach result: failed duration=%.1fms",
                    (time.monotonic() - started) * 1000.0,
                )
                self._renderer_unavailable()
                return
            self._engine_attached = True
            self._attach_failed = False
            log.info(
                "milestone attach result: wid attached duration=%.1fms",
                (time.monotonic() - started) * 1000.0,
            )
            log.debug("mpv attached to video window %#x", int(self.video.winId()))
        initial = self._initial_source
        self._initial_source = None
        if initial:
            self._auto_detected = True
            self.source.setText(initial)
            self._load_source(initial, quiet=False)
        elif not self._auto_detected:
            self._auto_detected = True
            self._auto_detect_disc()
            log.debug("auto-detect done: home visible=%s", self.home.isVisible())
# ── [AGENT2: SECTION 6 END] ──

# ════════════════════════════════════════════════════════════════════
