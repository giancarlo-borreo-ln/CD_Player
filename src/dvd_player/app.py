"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations  # noqa: I001 - import order below is intentional

import locale
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from . import _win_dll  # noqa: F401 - registers DLL dirs before mpv loads

from PyQt6.QtWidgets import QApplication

from .engine import DvdPlaybackEngine
from .state import APP_NAME, log
from .window import MainWindow, attach_mode_requested, render_mode_requested

# SECTION 7 — main() entry point
# ════════════════════════════════════════════════════════════════════

# Startup milestones are appended here so a slow launch (spun-down USB
# optical drive, GL attach retry) can be diagnosed after the fact. The
# file is capped so it never grows without bound.
DEBUG_LOG_MAX_LINES = 200
_LOG_HANDLER_FLAG = "_dvd_player_debug_handler"


def debug_log_path() -> Path:
    """Return the milestone log path (``DVD_PLAYER_LOG_PATH`` overrides)."""
    override = os.environ.get("DVD_PLAYER_LOG_PATH", "").strip()
    if override:
        return Path(override)
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return cache / "dvd-player" / "dvd-player.log"


class _CappedFileHandler(logging.FileHandler):
    """Append-only handler that trims the oldest lines past ``max_lines``."""

    def __init__(self, filename: str, max_lines: int) -> None:
        """Open ``filename`` for appending and count the current lines."""
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(filename, mode="a", encoding="utf-8")
        self._max_lines = max_lines
        self._line_count = self._count_lines()

    def _count_lines(self) -> int:
        try:
            with open(self.baseFilename, encoding="utf-8", errors="replace") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def emit(self, record: logging.LogRecord) -> None:
        """Append one record, trimming the file when it exceeds the cap."""
        super().emit(record)
        self._line_count += 1
        if self._line_count > self._max_lines:
            self._trim()

    def _trim(self) -> None:
        self.acquire()
        try:
            self.flush()
            path = Path(self.baseFilename)
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return
            keep = lines[-self._max_lines:]
            path.write_text("\n".join(keep) + "\n", encoding="utf-8")
            self._line_count = len(keep)
            if self.stream is not None:
                self.stream.close()
            self.stream = self._open()
        finally:
            self.release()


def configure_debug_log(path: str | os.PathLike[str] | None = None) -> Path:
    """Attach the capped milestone log and return its resolved path.

    Safe to call more than once: an existing handler is replaced only when
    the target path changes (tests use a temp path per run). A read-only
    cache directory must never prevent the app from starting.
    """
    target = Path(path) if path is not None else debug_log_path()
    existing = next(
        (
            handler
            for handler in log.handlers
            if getattr(handler, _LOG_HANDLER_FLAG, False)
        ),
        None,
    )
    if isinstance(existing, _CappedFileHandler) and existing.baseFilename == str(target):
        return target
    if existing is not None:
        log.removeHandler(existing)
        existing.close()
    try:
        handler = _CappedFileHandler(str(target), DEBUG_LOG_MAX_LINES)
    except OSError as exc:
        log.warning("cannot open debug log %s: %s", target, exc)
        return target
    setattr(handler, _LOG_HANDLER_FLAG, True)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.info("milestone app start: log=%s", target)
    return target


def choose_platform(env: Mapping[str, str] | None = None) -> str | None:
    """Pick QT_QPA_PLATFORM for this session (None leaves it untouched).

    R8 precedence, highest first:
    1. any non-empty QT_QPA_PLATFORM is honored verbatim (xcb, wayland,
       offscreen, minimal, vnc, foo, ...): the user/CI override wins;
    2. no WAYLAND_DISPLAY -> untouched (native X11/offscreen);
    3. DVD_PLAYER_ATTACH=render, or a render API request
       (DVD_PLAYER_RENDER_MODE/API) -> ``wayland``: the GL surface owns its
       context, so no wid/XWayland is needed;
    4. otherwise (DVD_PLAYER_ATTACH=wid or auto with wid embedding, with
       software fallback) -> ``xcb``.

    wid embedding needs xcb on Wayland (XWayland); the render API can use
    native Wayland, so it is preferred there. This decision must run before
    QApplication exists and is never pre-empted by an import-time mutation:
    a refused wid attach is handled at runtime by the attach matrix.
    """
    source = os.environ if env is None else env
    if source.get("QT_QPA_PLATFORM", "").strip():
        return None
    if not source.get("WAYLAND_DISPLAY"):
        return None
    if attach_mode_requested(source) == "render":
        return "wayland"
    if attach_mode_requested(source) == "wid":
        return "xcb"
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
    configure_debug_log()
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
