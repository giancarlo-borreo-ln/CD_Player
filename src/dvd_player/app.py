"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import locale
import logging
import os
import sys

from PyQt6.QtWidgets import QApplication

from .engine import DvdPlaybackEngine
from .state import APP_NAME, log
from .window import MainWindow

# SECTION 7 — main() entry point
# ════════════════════════════════════════════════════════════════════


def main() -> int:
    """Application entry point.

    On Wayland without an explicit QT_QPA_PLATFORM, force xcb so libmpv's
    foreign-window (wid) embedding works via XWayland; embedding arbitrary
    native windows is not possible on the native Wayland Qt platform.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    if (
        os.environ.get("WAYLAND_DISPLAY")
        and not os.environ.get("QT_QPA_PLATFORM", "").startswith("xcb")
    ):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.info(
            "Wayland session detected: forcing QT_QPA_PLATFORM=xcb for mpv embedding"
        )
    app = QApplication(sys.argv)
    # Qt resets LC_NUMERIC at app init; libmpv requires the C locale
    # (prints a warning and may crash with comma-decimal locales).
    locale.setlocale(locale.LC_NUMERIC, "C")
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    engine = DvdPlaybackEngine()
    window = MainWindow(engine, sys.argv[1] if len(sys.argv) > 1 else None)
    window.setWindowTitle("DVD Player")
    window.show()
    code = app.exec()
    engine.dispose()
    return code
