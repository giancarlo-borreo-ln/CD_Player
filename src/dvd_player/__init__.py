"""dvd_player — Xbox 360-style DVD player for Arch Linux.

Modern src-layout package. The public surface mirrors the historical
single-file module, so ``import dvd_player as dp`` keeps working: every
name (classes, constants, helpers, imported Qt/stdlib modules) is
re-exported here.
"""
from __future__ import annotations

import ctypes
import fcntl
import getpass
import grp
import hashlib
import json
import locale
import logging
import math
import os
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable as Callable
from enum import Enum as Enum
from enum import auto as auto
from pathlib import Path as Path
from typing import Any as Any
from typing import ClassVar as ClassVar

import mpv
from PyQt6.QtCore import (
    QEvent as QEvent,
)
from PyQt6.QtCore import (
    QObject as QObject,
)
from PyQt6.QtCore import (
    QRectF as QRectF,
)
from PyQt6.QtCore import (
    Qt as Qt,
)
from PyQt6.QtCore import (
    QTimer as QTimer,
)
from PyQt6.QtCore import (
    pyqtClassInfo as pyqtClassInfo,
)
from PyQt6.QtCore import (
    pyqtProperty as pyqtProperty,
)
from PyQt6.QtCore import (
    pyqtSignal as pyqtSignal,
)
from PyQt6.QtCore import (
    pyqtSlot as pyqtSlot,
)
from PyQt6.QtDBus import (
    QDBusAbstractAdaptor as QDBusAbstractAdaptor,
)
from PyQt6.QtDBus import (
    QDBusConnection as QDBusConnection,
)
from PyQt6.QtGui import (
    QCloseEvent as QCloseEvent,
)
from PyQt6.QtGui import (
    QColor as QColor,
)
from PyQt6.QtGui import (
    QFont as QFont,
)
from PyQt6.QtGui import (
    QFontMetricsF as QFontMetricsF,
)
from PyQt6.QtGui import (
    QImage as QImage,
)
from PyQt6.QtGui import (
    QKeyEvent as QKeyEvent,
)
from PyQt6.QtGui import (
    QLinearGradient as QLinearGradient,
)
from PyQt6.QtGui import (
    QMouseEvent as QMouseEvent,
)
from PyQt6.QtGui import (
    QPainter as QPainter,
)
from PyQt6.QtGui import (
    QPainterPath as QPainterPath,
)
from PyQt6.QtGui import (
    QPaintEvent as QPaintEvent,
)
from PyQt6.QtGui import (
    QPalette as QPalette,
)
from PyQt6.QtGui import (
    QResizeEvent as QResizeEvent,
)
from PyQt6.QtWidgets import (
    QApplication as QApplication,
)
from PyQt6.QtWidgets import (
    QFileDialog as QFileDialog,
)
from PyQt6.QtWidgets import (
    QHBoxLayout as QHBoxLayout,
)
from PyQt6.QtWidgets import (
    QLabel as QLabel,
)
from PyQt6.QtWidgets import (
    QLineEdit as QLineEdit,
)
from PyQt6.QtWidgets import (
    QMainWindow as QMainWindow,
)
from PyQt6.QtWidgets import (
    QMessageBox as QMessageBox,
)
from PyQt6.QtWidgets import (
    QPushButton as QPushButton,
)
from PyQt6.QtWidgets import (
    QVBoxLayout as QVBoxLayout,
)
from PyQt6.QtWidgets import (
    QWidget as QWidget,
)

from .app import main as main
from .engine import DvdPlaybackEngine as DvdPlaybackEngine
from .engine import MPVError as MPVError
from .hardware import (
    BLKGETSIZE64 as BLKGETSIZE64,
)
from .hardware import (
    CDROM_DRIVE_STATUS as CDROM_DRIVE_STATUS,
)
from .hardware import (
    CDROMEJECT as CDROMEJECT,
)
from .hardware import (
    CDS_DISC_OK as CDS_DISC_OK,
)
from .hardware import (
    CDS_DRIVE_NOT_READY as CDS_DRIVE_NOT_READY,
)
from .hardware import (
    CDS_NO_DISC as CDS_NO_DISC,
)
from .hardware import (
    CDS_TRAY_OPEN as CDS_TRAY_OPEN,
)
from .hardware import (
    _check_block_device as _check_block_device,
)
from .hardware import (
    _drive_status as _drive_status,
)
from .hardware import (
    _sg_loaded as _sg_loaded,
)
from .hardware import (
    check_environment as check_environment,
)
from .hardware import (
    disc_present as disc_present,
)
from .hardware import (
    eject_device as eject_device,
)
from .hardware import (
    list_dvd_devices as list_dvd_devices,
)
from .identity import (
    DiscIdentity as DiscIdentity,
)
from .identity import (
    ResumeStore as ResumeStore,
)
from .identity import (
    _coerce_track_id as _coerce_track_id,
)
from .identity import (
    _entry_updated as _entry_updated,
)
from .identity import (
    _hash_stream as _hash_stream,
)
from .mpris import (
    MprisService as MprisService,
)
from .mpris import (
    _MprisPlayerAdaptor as _MprisPlayerAdaptor,
)
from .mpris import (
    _MprisRootAdaptor as _MprisRootAdaptor,
)
from .osd import Xbox360Osd as Xbox360Osd
from .state import (
    APP_NAME as APP_NAME,
)
from .state import (
    CONFIG_DIR as CONFIG_DIR,
)
from .state import (
    DISC_POLL_MS as DISC_POLL_MS,
)
from .state import (
    FF_SPEEDS as FF_SPEEDS,
)
from .state import (
    GAMEPAD_POLL_MS as GAMEPAD_POLL_MS,
)
from .state import (
    LOAD_TIMEOUT_MS as LOAD_TIMEOUT_MS,
)
from .state import (
    OSD_TIMEOUT_MS as OSD_TIMEOUT_MS,
)
from .state import (
    RESUME_FILE as RESUME_FILE,
)
from .state import (
    RESUME_MAX_ENTRIES as RESUME_MAX_ENTRIES,
)
from .state import (
    PlaybackError as PlaybackError,
)
from .state import (
    PlayerState as PlayerState,
)
from .state import (
    StateMachine as StateMachine,
)
from .state import (
    __version__ as __version__,
)
from .state import (
    log as log,
)
from .window import (
    ChapterGrid as ChapterGrid,
)
from .window import (
    HelpOverlay as HelpOverlay,
)
from .window import (
    HomeMenu as HomeMenu,
)
from .window import (
    MainWindow as MainWindow,
)
from .window import (
    _sd_mounts as _sd_mounts,
)

__all__ = [
    "APP_NAME",
    "BLKGETSIZE64",
    "CDROMEJECT",
    "CDROM_DRIVE_STATUS",
    "CDS_DISC_OK",
    "CDS_DRIVE_NOT_READY",
    "CDS_NO_DISC",
    "CDS_TRAY_OPEN",
    "CONFIG_DIR",
    "DISC_POLL_MS",
    "FF_SPEEDS",
    "GAMEPAD_POLL_MS",
    "LOAD_TIMEOUT_MS",
    "OSD_TIMEOUT_MS",
    "RESUME_FILE",
    "RESUME_MAX_ENTRIES",
    "Any",
    "Callable",
    "ChapterGrid",
    "ClassVar",
    "DiscIdentity",
    "DvdPlaybackEngine",
    "Enum",
    "HelpOverlay",
    "HomeMenu",
    "MPVError",
    "MainWindow",
    "MprisService",
    "Path",
    "PlaybackError",
    "PlayerState",
    "QApplication",
    "QCloseEvent",
    "QColor",
    "QDBusAbstractAdaptor",
    "QDBusConnection",
    "QEvent",
    "QFileDialog",
    "QFont",
    "QFontMetricsF",
    "QHBoxLayout",
    "QImage",
    "QKeyEvent",
    "QLabel",
    "QLineEdit",
    "QLinearGradient",
    "QMainWindow",
    "QMessageBox",
    "QMouseEvent",
    "QObject",
    "QPaintEvent",
    "QPainter",
    "QPainterPath",
    "QPalette",
    "QPushButton",
    "QRectF",
    "QResizeEvent",
    "QTimer",
    "QVBoxLayout",
    "QWidget",
    "Qt",
    "ResumeStore",
    "StateMachine",
    "Xbox360Osd",
    "_MprisPlayerAdaptor",
    "_MprisRootAdaptor",
    "__version__",
    "_check_block_device",
    "_coerce_track_id",
    "_drive_status",
    "_entry_updated",
    "_hash_stream",
    "_sd_mounts",
    "_sg_loaded",
    "auto",
    "check_environment",
    "ctypes",
    "disc_present",
    "eject_device",
    "fcntl",
    "getpass",
    "grp",
    "hashlib",
    "json",
    "list_dvd_devices",
    "locale",
    "log",
    "logging",
    "main",
    "math",
    "mpv",
    "os",
    "pyqtClassInfo",
    "pyqtProperty",
    "pyqtSignal",
    "pyqtSlot",
    "shutil",
    "stat",
    "struct",
    "subprocess",
    "sys",
    "threading",
    "time",
]

# Only these facade names are forwarded to submodules. Everything else
# (dunders, importlib machinery such as __spec__/__loader__/__path__,
# unknown attributes) is handled by types.ModuleType alone — forwarding
# those made importlib.reload() recurse into the submodules.
_PUBLIC_NAMES = frozenset(__all__)


_SUBMODULES = (
    "dvd_player.state",
    "dvd_player.identity",
    "dvd_player.hardware",
    "dvd_player.engine",
    "dvd_player.osd",
    "dvd_player.mpris",
    "dvd_player.window",
    "dvd_player.app",
)


class _DvdPlayerModule(types.ModuleType):
    """Module type that keeps legacy global monkeypatching working.

    The application used to be one module, so suites patched
    ``dvd_player.X`` (``QMessageBox``, ``list_dvd_devices``,
    ``ResumeStore`` …) and every function saw the change. Forward
    attribute assignment to submodules that define the same name so
    that contract still holds after the split. Only registered public
    names are forwarded; importlib machinery and dunders are left to
    :class:`types.ModuleType` so ``importlib.reload`` stays stable.
    """

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name not in globals().get("_PUBLIC_NAMES", ()):
            return
        for submodule in _SUBMODULES:
            module = sys.modules.get(submodule)
            if module is not None and hasattr(module, name):
                setattr(module, name, value)


setattr(  # noqa: B010 - mypy forbids direct __class__ assignment
    sys.modules[__name__], "__class__", _DvdPlayerModule
)
