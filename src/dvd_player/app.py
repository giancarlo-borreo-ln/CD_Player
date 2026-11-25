"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import locale
import logging
import os
import sys
from collections.abc import Mapping

from PyQt6.QtWidgets import QApplication

from .engine import DvdPlaybackEngine
from .state import APP_NAME, log
from .window import MainWindow, render_mode_requested

# SECTION 7 — main() entry point
# ════════════════════════════════════════════════════════════════════


def choose_platform(env: Mapping[str, str] | None = None) -> str | None:
    """Pick QT_QPA_PLATFORM for this session (None leaves it untouched).

    R8 precedence, highest first:
    1. an explicit ``xcb``/``wayland`` choice in QT_QPA_PLATFORM is honored;
    2. no WAYLAND_DISPLAY -> untouched (native X11/offscreen);
    3. render API requested (DVD_PLAYER_RENDER_MODE/API) -> ``wayland``:
       the GL surface owns its context, so no wid/XWayland is needed;
    4. otherwise (wid embedding, with software fallback) -> ``xcb``.

    wid embedding needs xcb on Wayland (XWayland); the render API can use
    native Wayland, so it is preferred there. This decision must run before
    QApplication exists and is never pre-empted by an import-time mutation:
    a refused wid attach is handled at runtime by ``attach_widget``.
    """
    source = os.environ if env is None else env
    current = source.get("QT_QPA_PLATFORM", "").strip().lower()
    if current.startswith(("xcb", "wayland")):
        return None
    if not source.get("WAYLAND_DISPLAY"):
        return None
    return "wayland" if render_mode_requested(source) else "xcb"


def main() -> int:
    """Application entry point.

    On Wayland without an explicit QT_QPA_PLATFORM, force xcb so libmpv's
    foreign-window (wid) embedding works via XWayland; embedding arbitrary
    native windows is not possible on the native Wayland Qt platform.
    The render API is the exception: it owns its GL surface, so native
    Wayland is preferred there (R8). See choose_platform for precedence.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    platform = choose_platform()
    if platform is not None:
        os.environ["QT_QPA_PLATFORM"] = platform
        log.info(
            "Wayland session detected: forcing QT_QPA_PLATFORM=%s (%s)",
            platform,
            "render API" if platform == "wayland" else "mpv wid embedding",
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
