#!/usr/bin/env python3
"""dvd_player — Xbox 360-style DVD player for Arch Linux.

Single-file PyQt6 application: libmpv (dvdnav://) playback engine with DVD
menu navigation, an Xbox 360-style translucent OSD, keyboard/gamepad D-pad
controls, and per-disc resume state.

Usage: uv run dvd-player [/dev/sr0 | /path/to/movie.iso | /path/to/VIDEO_TS]
"""
from __future__ import annotations

# ════════════════════════════════════════════════════════════════════
# SECTION 0 — Imports & framework shim
# ════════════════════════════════════════════════════════════════════
import ctypes
import ctypes.util
import fcntl
import getpass
import grp
import hashlib
import json
import logging
import os
import shutil
import stat
import struct
import subprocess
import sys
import time
from enum import Enum, auto
from pathlib import Path

import mpv  # python-mpv (ctypes bindings over libmpv)

from PyQt6.QtCore import QObject, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
)
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

# python-mpv >= 1.0 has no legacy ``mpv.MPVError``: C-API failures are
# reported as ValueError (ErrorCode.raise_for_ec), a dead core raises
# ShutdownError (a SystemError), and property errors raise AttributeError.
# This alias keeps the legacy name used throughout the engine.
MPVError = ValueError

log = logging.getLogger("dvd_player")

# ── [AGENTS: extra imports appended here if needed] ──
from collections.abc import Callable
from typing import Any, ClassVar

# ════════════════════════════════════════════════════════════════════
# SECTION 1 — Constants, enums, state machine
# ════════════════════════════════════════════════════════════════════

__version__ = "0.1.0"
APP_NAME = "dvd_player"
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / APP_NAME
RESUME_FILE = CONFIG_DIR / "resume.json"
RESUME_MAX_ENTRIES = 16  # cap stored per-disc entries
OSD_TIMEOUT_MS = 4000  # Xbox-style OSD auto-hide delay
GAMEPAD_POLL_MS = 50
LOAD_TIMEOUT_MS = 30000  # LOADING watchdog: must yield MENU_ACTIVE/PLAYING within this

FF_SPEEDS = (2.0, 4.0, 8.0, 16.0)  # console trick-play ladder


class PlayerState(Enum):
    IDLE = auto()
    LOADING = auto()
    MENU_ACTIVE = auto()
    PLAYING = auto()
    PAUSED = auto()
    STOPPED = auto()
    EJECTED = auto()
    ERROR = auto()


class PlaybackError(Exception):
    """User-facing playback failure; str(exc) is safe to show in the GUI."""


# SPEC for SECTION 1 (StateMachine) — contract, do not edit:
#   class StateMachine:
#       __init__(self, initial: PlayerState = PlayerState.IDLE)
#       state -> PlayerState (property, read-only)
#       transition(self, new: PlayerState) -> None
#       reset(self) -> None
#   LEGAL table (use exactly):
#       IDLE        -> {LOADING}
#       LOADING     -> {MENU_ACTIVE, PLAYING, ERROR}
#       MENU_ACTIVE -> {PLAYING, STOPPED, EJECTED, ERROR}
#       PLAYING     -> {PAUSED, MENU_ACTIVE, STOPPED, EJECTED, ERROR}
#       PAUSED      -> {PLAYING, MENU_ACTIVE, STOPPED, EJECTED, ERROR}
#       STOPPED     -> {IDLE, LOADING, MENU_ACTIVE, EJECTED}
#       EJECTED     -> {IDLE, LOADING}
#       ERROR       -> {LOADING}
#   transition(new): no-op when new == state; raise PlaybackError(
#       f"illegal transition {self._state.name} -> {new.name}") otherwise.
#   reset(): state = IDLE (no validation, used on fresh load lifecycle).
#   GUI-thread only; mpv-thread callbacks must never call transition
#   directly (see SECTION 4 threading contract).
# ── [AGENT1: SECTION 1 BEGIN] ──
class StateMachine:
    """Validate player state transitions on the GUI thread."""

    _LEGAL: ClassVar[dict[PlayerState, set[PlayerState]]] = {
        PlayerState.IDLE: {PlayerState.LOADING},
        PlayerState.LOADING: {
            PlayerState.MENU_ACTIVE,
            PlayerState.PLAYING,
            PlayerState.ERROR,
        },
        PlayerState.MENU_ACTIVE: {
            PlayerState.PLAYING,
            PlayerState.STOPPED,
            PlayerState.EJECTED,
            PlayerState.ERROR,
        },
        PlayerState.PLAYING: {
            PlayerState.PAUSED,
            PlayerState.MENU_ACTIVE,
            PlayerState.STOPPED,
            PlayerState.EJECTED,
            PlayerState.ERROR,
        },
        PlayerState.PAUSED: {
            PlayerState.PLAYING,
            PlayerState.MENU_ACTIVE,
            PlayerState.STOPPED,
            PlayerState.EJECTED,
            PlayerState.ERROR,
        },
        PlayerState.STOPPED: {
            PlayerState.IDLE,
            PlayerState.LOADING,
            PlayerState.MENU_ACTIVE,
            PlayerState.EJECTED,
        },
        PlayerState.EJECTED: {PlayerState.IDLE, PlayerState.LOADING},
        PlayerState.ERROR: {PlayerState.LOADING},
    }

    def __init__(self, initial: PlayerState = PlayerState.IDLE) -> None:
        """Create a state machine in ``initial`` (no validation)."""
        self._state = initial

    @property
    def state(self) -> PlayerState:
        """Current state."""
        return self._state

    def transition(self, new: PlayerState) -> None:
        """Move to ``new``; same-state is a no-op, illegal moves raise."""
        if new == self._state:
            return
        if new not in self._LEGAL[self._state]:
            raise PlaybackError(
                f"illegal transition {self._state.name} -> {new.name}"
            )
        self._state = new

    def reset(self) -> None:
        """Return to IDLE without validation."""
        self._state = PlayerState.IDLE
# ── [AGENT1: SECTION 1 END] ──

# ════════════════════════════════════════════════════════════════════
# SECTION 2 — Disc identity & resume store
# ════════════════════════════════════════════════════════════════════

# SPEC for SECTION 2 (DiscIdentity + ResumeStore) — contract, do not edit:
#   class DiscIdentity:
#       @staticmethod compute(source: str) -> str   (lowercase hex sha256)
#       * block device (/dev/sr*, /dev/cdrom, /dev/dvd via stat.S_ISBLK):
#         read ISO9660 PVD at byte offset 32768 (1 sector, 2048 bytes);
#         volume id = bytes 40:72 decoded utf-8, stripped of NULs/spaces;
#         size via ioctl BLKGETSIZE64 (struct 'Q'); sample = 256 KiB read at
#         offset 524288 (best effort, b'' on failure). Digest over
#         volume_id_bytes | str(size) | sample.
#       * regular file: stream sha256 of the whole file, 1 MiB chunks.
#       * directory: hash VIDEO_TS/VIDEO_TS.IFO (fallback VIDEO_TS.BUP),
#         1 MiB chunks; PlaybackError if neither exists ("not a DVD layout").
#       * missing/unreadable source -> PlaybackError with actionable message.
#   class ResumeStore:
#       __init__(self, path: Path = RESUME_FILE)
#       load(self) -> dict   ({} when file missing/corrupt — never raise)
#       get(self, disc_hash: str) -> Optional[dict]
#       save(self, disc_hash: str, pos: float, title: Optional[int],
#            chapter: Optional[int]) -> None   (upsert, set "updated" to
#            time.time(), prune to RESUME_MAX_ENTRIES by newest "updated";
#            atomic write: tmp file in same dir + os.replace; mkdir parents)
#       forget(self, disc_hash: str) -> None
#   Entry schema: {"pos": float, "title": int|None, "chapter": int|None,
#                  "updated": float}
# ── [AGENT1: SECTION 2 BEGIN] ──
def _hash_stream(path: Path) -> str:
    """Return the sha256 hex digest of a file, reading it in 1 MiB chunks."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise PlaybackError(f"cannot read {str(path)!r}: {exc}") from exc
    return digest.hexdigest()


class DiscIdentity:
    """Stable per-disc identifiers used to key resume state."""

    @staticmethod
    def compute(source: str) -> str:
        """Return a lowercase sha256 hex digest for a disc device, ISO or folder."""
        path = Path(source)
        try:
            info = path.stat()
        except (OSError, ValueError) as exc:
            raise PlaybackError(
                f"cannot access disc source {source!r}: {exc}"
            ) from exc
        if stat.S_ISBLK(info.st_mode):
            return DiscIdentity._compute_block(path)
        if stat.S_ISDIR(info.st_mode):
            return DiscIdentity._compute_directory(path)
        if stat.S_ISREG(info.st_mode):
            return _hash_stream(path)
        raise PlaybackError(f"unsupported disc source {source!r}: not a device, file or directory")

    @staticmethod
    def _compute_block(path: Path) -> str:
        """Digest volume id, device size and a data sample of a block device."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise PlaybackError(
                f"cannot open disc device {str(path)!r}: {exc}"
            ) from exc
        try:
            size = struct.unpack(
                "Q", fcntl.ioctl(fd, BLKGETSIZE64, struct.pack("Q", 0))
            )[0]
            volume_id = DiscIdentity._read_pvd(fd)
            try:
                sample = os.pread(fd, 256 * 1024, 524288)
            except OSError:
                sample = b""
        except OSError as exc:
            raise PlaybackError(
                f"cannot read disc device {str(path)!r}: {exc}"
            ) from exc
        finally:
            os.close(fd)
        digest = hashlib.sha256()
        digest.update(volume_id)
        digest.update(str(size).encode("ascii"))
        digest.update(sample)
        return digest.hexdigest()

    @staticmethod
    def _read_pvd(fd: int) -> bytes:
        """Return the ISO9660 volume id (PVD bytes 40:72), stripped of NUL/spaces."""
        try:
            pvd = os.pread(fd, 2048, 32768)
        except OSError:
            return b""
        raw = pvd[40:72].decode("utf-8", "replace").replace("\x00", "").strip()
        return raw.encode("utf-8")

    @staticmethod
    def _compute_directory(path: Path) -> str:
        """Hash VIDEO_TS/VIDEO_TS.IFO, falling back to VIDEO_TS.BUP."""
        video_ts = path / "VIDEO_TS"
        for name in ("VIDEO_TS.IFO", "VIDEO_TS.BUP"):
            candidate = video_ts / name
            if candidate.is_file():
                return _hash_stream(candidate)
        raise PlaybackError(
            f"not a DVD layout: {str(path)!r} has no VIDEO_TS/VIDEO_TS.IFO"
        )


def _entry_updated(entry: dict[str, object]) -> float:
    """Return an entry's ``updated`` timestamp as float (0.0 when absent)."""
    value = entry.get("updated")
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


class ResumeStore:
    """Persist per-disc resume positions in a small JSON file."""

    def __init__(self, path: Path = RESUME_FILE) -> None:
        """Use ``path`` as the backing JSON file."""
        self._path = path

    def load(self) -> dict[str, dict[str, object]]:
        """Return all entries; ``{}`` when the file is missing or corrupt."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def get(self, disc_hash: str) -> dict[str, object] | None:
        """Return the stored entry for ``disc_hash``, if any."""
        return self.load().get(disc_hash)

    def save(
        self,
        disc_hash: str,
        pos: float,
        title: int | None,
        chapter: int | None,
    ) -> None:
        """Upsert an entry, stamp it, and prune the oldest entries."""
        entries = self.load()
        entries[disc_hash] = {
            "pos": float(pos),
            "title": title,
            "chapter": chapter,
            "updated": time.time(),
        }
        if len(entries) > RESUME_MAX_ENTRIES:
            newest = sorted(
                entries.items(), key=lambda item: _entry_updated(item[1]), reverse=True
            )
            entries = dict(newest[:RESUME_MAX_ENTRIES])
        self._write(entries)

    def forget(self, disc_hash: str) -> None:
        """Drop the entry for ``disc_hash`` (no-op when absent)."""
        entries = self.load()
        if entries.pop(disc_hash, None) is None:
            return
        self._write(entries)

    def _write(self, entries: dict[str, dict[str, object]]) -> None:
        """Atomically replace the backing file with ``entries``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(
                json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp, self._path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
# ── [AGENT1: SECTION 2 END] ──

# ════════════════════════════════════════════════════════════════════
# SECTION 3 — Hardware helpers (devices, permissions, eject)
# ════════════════════════════════════════════════════════════════════

CDROM_DRIVE_STATUS = 0x5326  # ioctl: returns one of the CDS_* values below
CDROMEJECT = 0x5309  # ioctl: open the tray
BLKGETSIZE64 = 0x80081272  # ioctl: device size in bytes
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4

# SPEC for SECTION 3 (module-level helpers) — contract, do not edit:
#   list_dvd_devices() -> list[str]
#       Existing node paths from /dev/sr*, /dev/cdrom, /dev/dvd, sorted,
#       deduplicated via os.path.realpath. Never raises.
#   disc_present(device: str) -> Optional[bool]
#       Open O_RDONLY|O_NONBLOCK; ioctl CDROM_DRIVE_STATUS returns int:
#       CDS_DISC_OK -> True; CDS_NO_DISC -> False; CDS_TRAY_OPEN /
#       CDS_DRIVE_NOT_READY -> None; OSError (missing node, ENOMEDIUM,
#       EACCES, ...) -> None. Close the fd in all paths (try/finally).
#   eject_device(device: str) -> None
#       Open O_RDONLY|O_NONBLOCK, ioctl CDROMEJECT. On OSError fall back to
#       running the `eject` binary (subprocess.run list form, NEVER
#       shell=True) when shutil.which("eject"). If both fail -> PlaybackError
#       with a message naming the likely cause (permissions / optical group
#       / drive missing).
#   check_environment(source: str) -> list[str]
#       Human-readable problem strings ([] = all good). Never raises.
#       1. source must exist and be a block device (stat.S_ISBLK), a
#          regular file, or a directory containing VIDEO_TS/ (a dir with
#          neither VIDEO_TS.IFO nor VIDEO_TS.BUP -> "not a DVD layout").
#       2. block device only: probe-open O_RDONLY (EACCES -> message naming
#          the `optical` group); warn when the current user
#          (getpass.getuser()) is not in grp.getgrnam("optical").gr_mem
#          (skip when the group does not exist).
#       3. warn when the sg kernel module is absent: neither /sys/module/sg
#          nor "sg" in Path("/proc/modules") (best effort, no raise).
#       4. warn when libdvdcss is missing: ctypes.util.find_library("dvdcss")
#          is None.
#       Preserve order 1..4. GUI may proceed despite warnings.
# ── [AGENT1: SECTION 3 BEGIN] ──
def list_dvd_devices() -> list[str]:
    """Return existing optical device nodes, deduplicated by real path."""
    candidates = ["/dev/cdrom", "/dev/dvd"]
    try:
        candidates.extend(
            os.path.join("/dev", name)
            for name in os.listdir("/dev")
            if name.startswith("sr")
        )
    except OSError:
        pass
    devices: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(candidates):
        try:
            real = os.path.realpath(candidate)
        except OSError:
            continue
        if not os.path.exists(candidate) or real in seen:
            continue
        seen.add(real)
        devices.append(candidate)
    return devices


def _drive_status(fd: int) -> bool | None:
    """Map a CDROM_DRIVE_STATUS ioctl to True/False/None."""
    try:
        status = int(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0))
    except OSError:
        return None
    if status == CDS_DISC_OK:
        return True
    if status == CDS_NO_DISC:
        return False
    return None


def disc_present(device: str) -> bool | None:
    """Return True when a disc is ready, False when absent, None when unknown."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return _drive_status(fd)
    finally:
        os.close(fd)


def eject_device(device: str) -> None:
    """Open the tray via ioctl, falling back to the ``eject`` binary."""
    ioctl_error: OSError | None = None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        ioctl_error = exc
    else:
        try:
            fcntl.ioctl(fd, CDROMEJECT, 0)
            return
        except OSError as exc:
            ioctl_error = exc
        finally:
            os.close(fd)
    eject_bin = shutil.which("eject")
    if eject_bin is not None:
        try:
            completed = subprocess.run(
                [eject_bin, device],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            ioctl_error = ioctl_error or exc
        else:
            if completed.returncode == 0:
                return
    raise PlaybackError(
        f"cannot eject {device!r}: {ioctl_error}. The drive may be missing, "
        "busy/mounted, or you lack permissions (add the user to the 'optical' group)."
    )


def _check_block_device(device: str) -> list[str]:
    """Report access and 'optical' group problems for a block device."""
    problems: list[str] = []
    try:
        os.close(os.open(device, os.O_RDONLY | os.O_NONBLOCK))
    except PermissionError:
        problems.append(
            f"no read permission on {device}: add the user to the 'optical' group"
        )
    except OSError as exc:
        problems.append(f"cannot open {device}: {exc}")
    try:
        members = grp.getgrnam("optical").gr_mem
    except KeyError:
        members = None
    if members is not None:
        user = getpass.getuser()
        if user not in members:
            problems.append(f"user {user!r} is not in the 'optical' group")
    return problems


def _sg_loaded() -> bool:
    """Return True when the sg kernel module appears to be loaded."""
    try:
        if Path("/sys/module/sg").exists():
            return True
        text = Path("/proc/modules").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return any(line.split(" ", 1)[0] == "sg" for line in text.splitlines())


def check_environment(source: str) -> list[str]:
    """Return human-readable environment problems; ``[]`` means all good."""
    problems: list[str] = []
    try:
        info = os.stat(source)
    except (OSError, ValueError):
        return [f"source {source!r} does not exist or is not accessible"]
    is_block = stat.S_ISBLK(info.st_mode)
    is_file = stat.S_ISREG(info.st_mode)
    is_dir = stat.S_ISDIR(info.st_mode)
    if not (is_block or is_file or is_dir):
        problems.append(
            f"source {source!r} is not a block device, regular file or directory"
        )
    elif is_dir:
        video_ts = Path(source) / "VIDEO_TS"
        if not (
            (video_ts / "VIDEO_TS.IFO").is_file()
            or (video_ts / "VIDEO_TS.BUP").is_file()
        ):
            problems.append(
                f"not a DVD layout: {source!r} has no VIDEO_TS/VIDEO_TS.IFO"
            )
    if is_block:
        problems.extend(_check_block_device(source))
    if not _sg_loaded():
        problems.append(
            "kernel module 'sg' is not loaded: SCSI passthrough may be unavailable"
        )
    if ctypes.util.find_library("dvdcss") is None:
        problems.append(
            "libdvdcss not found: encrypted DVDs cannot be decrypted"
        )
    return problems
# ── [AGENT1: SECTION 3 END] ──

# ════════════════════════════════════════════════════════════════════
# SECTION 4 — DvdPlaybackEngine (libmpv / dvdnav backend)
# ════════════════════════════════════════════════════════════════════

# SPEC for SECTION 4 (DvdPlaybackEngine) — contract, do not edit:
#   class DvdPlaybackEngine(QObject) with signals:
#     state_changed = pyqtSignal(object)          # PlayerState
#     position_changed = pyqtSignal(float)        # seconds
#     menu_active_changed = pyqtSignal(bool)
#     tracks_changed = pyqtSignal(object, object) # (audio_list, sub_list)
#     playback_ended = pyqtSignal()
#     error_occurred = pyqtSignal(str)
#     (private raw signals emitted ONLY from mpv observer callbacks:
#      _menu_active_raw = pyqtSignal(bool)
#      _time_pos_raw = pyqtSignal(float)
#      _tracks_raw = pyqtSignal(object)
#      _eof_raw = pyqtSignal()
#      Connect them to private handlers in __init__; Qt auto-queues
#      cross-thread emissions so handlers always run on the GUI thread.)
#   Public API (signatures exact):
#     __init__(self, parent: Optional[QObject] = None)
#     attach_widget(self, wid: int) -> None
#     load(self, source: str) -> None
#     play_pause(self) -> None
#     stop(self) -> None
#     set_speed(self, mult: float) -> None
#     next_chapter(self) -> None
#     previous_chapter(self) -> None
#     cycle_audio(self) -> None
#     cycle_subtitle(self) -> None
#     menu_nav(self, direction: str) -> None
#     menu_select(self) -> None
#     menu_back(self) -> None
#     menu_mouse_move(self, x: int, y: int) -> None
#     show_root_menu(self) -> None
#     show_title_menu(self) -> None
#     set_title(self, title: int) -> None
#     seek_to(self, seconds: float) -> None
#     adjust_volume(self, delta: float) -> None
#     resume_disc(self, saved: dict) -> None
#     eject(self) -> None
#     dispose(self) -> None
#   Properties (read-only): state, menu_active, time_pos, duration, chapter,
#     chapter_list, speed, disc_title, volume, player
#   Behavior:
#   * __init__: QObject ctor; StateMachine(IDLE); ResumeStore();
#     _player=None, _wid=None, _source=None, _disc_hash=None,
#     _pending_resume=None, _menu_active=False, _speed=1.0, _audio=[],
#     _subs=[]; connect raw signals to private handlers (_apply_menu_active,
#     _apply_time_pos, _apply_tracks, _apply_eof).
#   * attach_widget(wid): idempotent while attached. Create mpv.MPV(
#     wid=str(wid), input_default_bindings=False, input_cursor=False,
#     osc=False, osd_level=0, keep_open=True, idle=True — idle=True keeps
#     the libmpv core alive between loads (idle=False lets the core shut
#     down ~100 ms after creation, so every property read then raises
#     ShutdownError). Assign _wid only AFTER the instance exists; on
#     construction failure reset _wid=None and raise PlaybackError. Register property
#     observers (python-mpv @self._player.property_observer decorator) for
#     'time-pos', 'disc-menu-active', 'track-list', 'eof-reached' that emit
#     the raw signals only (eof observer emits only when value is True).
#   * load(source): allowed from {IDLE, STOPPED, EJECTED, ERROR}, else
#     PlaybackError. transition(LOADING); _ensure_player() — on failure
#     _fail(exc) (LOADING->ERROR + error_occurred emitted) then raise. Compute
#     DiscIdentity.compute(source) (PlaybackError -> transition(ERROR) +
#     emit error_occurred + return). Try player.dvd_device = source; on
#     exception fall back to player.command('loadfile', 'dvdnav://',
#     'replace', f'dvd-device={source}') else player.play('dvdnav://').
#     Store _source/_disc_hash. Any MPVError/OSError/SystemError/AttributeError -> transition(ERROR)
#     + emit error_occurred(str(exc)) + log.exception. LOADING->PLAYING /
#     LOADING->MENU_ACTIVE arrives later via _apply_menu_active.
#   * load watchdog: entering LOADING starts a singleShot
#     QTimer(LOAD_TIMEOUT_MS); every _transition out of LOADING stops it.
#     _on_load_timeout() fires _fail(PlaybackError("playback did not start:
#     disc unreadable or drive error")) when still LOADING, so an unreadable
#     disc always surfaces error_occurred instead of a silent stuck LOADING.
#   * play_pause: PLAYING->PAUSED (player.pause=True); PAUSED->PLAYING
#     (False); MENU_ACTIVE: no-op (A=select in menus). Else no-op.
#   * stop: from {PLAYING, PAUSED, MENU_ACTIVE}: _save_resume_now();
#     player.command('stop'); transition(STOPPED). Else no-op.
#   * set_speed(mult): {PLAYING, PAUSED} only; player.speed = mult;
#     _speed = mult.
#   * next_chapter/previous_chapter: only when state is PLAYING and
#     player.chapter is not None; player.chapter += 1 (prev:
#     max(1, chapter - 1) — DVD chapters are 1-based), guarded by try/except.
#   * cycle_audio: {PLAYING, PAUSED} only; ids = [t['id'] for t in
#     player.track_list if t.get('type') == 'audio']; if ids and player.aid
#     in ids: player.aid = ids[(ids.index(player.aid) + 1) % len(ids)].
#     cycle_subtitle: same shape with type 'sub' and player.sid.
#   * menu_nav(direction in {'up','down','left','right'}): only when
#     _menu_active; player.command('discnav', direction) in try/except.
#     menu_select: discnav 'select' (menu only). menu_back: discnav 'prev'
#     (menu only). menu_mouse_move(x, y): discnav 'mouse_move' x y (menu
#     only, ints).
#   * show_root_menu: player.command('discnav', 'menu').
#   * show_title_menu: player.command('discnav', 'menu') (libmpv exposes no
#     separate VTSM title-menu jump; log.info this fallback once).
#   * set_title(title): {PLAYING, PAUSED} only; player.disc_title = title.
#   * seek_to(seconds): {PLAYING, PAUSED} only; player.time_pos = seconds.
#   * adjust_volume(delta): player.volume = min(150, max(0, volume + delta)).
#   * resume_disc(saved): _pending_resume = dict(saved). Applied by
#     _apply_pending_resume() on the first transition into PLAYING:
#     title (int, not None) -> disc_title = title; chapter (int, not None)
#     -> chapter = chapter; pos (float > 0) -> time_pos = pos; then clear.
#     All best-effort inside try/except (resume must never break playback).
#   * eject: _save_resume_now(); terminate player (set _player=None AND
#     _wid=None so attach_widget() can rebuild mpv for the next load); if
#     _menu_active: clear it and emit menu_active_changed(False); if
#     _source was a block device: eject_device(_source); transition(EJECTED)
#     (legal from PLAYING/PAUSED/MENU_ACTIVE/STOPPED; wrap try/except).
#   * dispose: idempotent; _save_resume_now(); if _player: terminate and
#     _player=None. Does NOT change state.
#   * _ensure_player: raise PlaybackError("engine not attached to a video
#     widget yet (attach_widget first)") when _player is None.
#   * _save_resume_now: when _disc_hash and state in {PLAYING, PAUSED,
#     MENU_ACTIVE}: ResumeStore.save(_disc_hash, time_pos or 0.0,
#     disc_title when not menu else None, chapter).
#   * _apply_menu_active(active) — handler for _menu_active_raw:
#       _menu_active = bool(active)
#       if active and state in {LOADING, PLAYING, PAUSED, STOPPED}: transition(MENU_ACTIVE)
#       elif not active and state == LOADING: transition(PLAYING)
#       elif not active and state == MENU_ACTIVE: transition(PLAYING)
#       if state == PLAYING and _pending_resume: _apply_pending_resume()
#       emit menu_active_changed(_menu_active)
#   * _apply_time_pos(value): when value is not None: emit
#     position_changed(float(value)).
#   * _apply_tracks(value): split into audio/sub lists of dicts with keys
#     {'id','lang','title'} (missing -> None); cache in _audio/_subs;
#     emit tracks_changed(_audio, _subs).
#   * _apply_eof(): LOADING -> _fail(PlaybackError("disc failed to start:
#     end-of-file during load")); {PLAYING, PAUSED} and not _menu_active ->
#     transition(STOPPED) + emit playback_ended(). On real DVDs a title end
#     is followed by dvdnav re-entering the menu, hence STOPPED->MENU_ACTIVE
#     is a legal transition so the state machine follows the disc.
#   * _transition(new): _sm.transition(new) then emit state_changed(state).
#   * Every player call wrapped: on MPVError/OSError/SystemError/AttributeError -> transition(ERROR) +
#     emit error_occurred(str(exc)) + log.exception. StateMachine blocks
#     ERROR->ERROR double transitions (raise PlaybackError inside handler;
#     catch it and only log).
#   * Properties return safe values when _player is None or the libmpv core
#     has shut down: time_pos=None, duration=None, chapter=None,
#     chapter_list=[], disc_title=None, volume=None, speed=_speed,
#     menu_active=_menu_active, state=_sm.state. Every property read is
#     wrapped in try/except (OSError, SystemError, AttributeError,
#     TypeError, ValueError) and degrades to None/[].
#   * Threading contract: ALL public methods GUI-thread-only. mpv observers
#     run on mpv's thread and may ONLY emit raw signals — never touch the
#     StateMachine, never emit public signals directly.
# ── [AGENT1: SECTION 4 BEGIN] ──
class DvdPlaybackEngine(QObject):
    """libmpv/dvdnav backend; all public methods are GUI-thread-only."""

    state_changed = pyqtSignal(object, arguments=())
    position_changed = pyqtSignal(float, arguments=())
    menu_active_changed = pyqtSignal(bool, arguments=())
    tracks_changed = pyqtSignal(object, object, arguments=())
    playback_ended = pyqtSignal(arguments=())
    error_occurred = pyqtSignal(str, arguments=())

    _menu_active_raw = pyqtSignal(bool, arguments=())
    _time_pos_raw = pyqtSignal(float, arguments=())
    _tracks_raw = pyqtSignal(object, arguments=())
    _eof_raw = pyqtSignal(arguments=())

    def __init__(self, parent: QObject | None = None) -> None:
        """Wire raw observer signals and start in IDLE."""
        super().__init__(parent)  # type: ignore[arg-type]
        self._sm = StateMachine(PlayerState.IDLE)
        self._resume = ResumeStore()
        self._player: Any = None
        self._wid: int | None = None
        self._source: str | None = None
        self._disc_hash: str | None = None
        self._pending_resume: dict[str, object] | None = None
        self._menu_active = False
        self._speed = 1.0
        self._audio: list[dict[str, object]] = []
        self._subs: list[dict[str, object]] = []
        self._disposed = False
        self._title_menu_logged = False
        self._menu_active_raw.connect(self._apply_menu_active)
        self._time_pos_raw.connect(self._apply_time_pos)
        self._tracks_raw.connect(self._apply_tracks)
        self._eof_raw.connect(self._apply_eof)
        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.setInterval(LOAD_TIMEOUT_MS)
        self._load_watchdog.timeout.connect(self._on_load_timeout)

    # ── public API ──────────────────────────────────────────────────
    def attach_widget(self, wid: int) -> None:
        """Create the mpv instance bound to the given native window id."""
        if self._wid is not None and self._player is not None:
            return
        try:
            player = mpv.MPV(
                wid=str(wid),
                input_default_bindings=False,
                input_cursor=False,
                osc=False,
                osd_level=0,
                keep_open=True,
                idle=True,  # keep the libmpv core alive between loads
            )
        except Exception as exc:
            self._wid = None
            log.exception("mpv initialization failed")
            raise PlaybackError(f"could not initialize mpv: {exc}") from exc
        self._wid = int(wid)
        self._player = player

        @player.property_observer("time-pos")  # type: ignore[untyped-decorator]
        def _observe_time_pos(_name: str, value: Any) -> None:
            if value is not None:
                self._time_pos_raw.emit(float(value))

        @player.property_observer("disc-menu-active")  # type: ignore[untyped-decorator]
        def _observe_menu(_name: str, value: Any) -> None:
            self._menu_active_raw.emit(bool(value))

        @player.property_observer("track-list")  # type: ignore[untyped-decorator]
        def _observe_tracks(_name: str, value: Any) -> None:
            self._tracks_raw.emit(value)

        @player.property_observer("eof-reached")  # type: ignore[untyped-decorator]
        def _observe_eof(_name: str, value: Any) -> None:
            if value is True:
                self._eof_raw.emit()

    def load(self, source: str) -> None:
        """Open ``source`` (device, ISO or VIDEO_TS folder) through dvdnav."""
        state = self.state
        if state not in (
            PlayerState.IDLE,
            PlayerState.STOPPED,
            PlayerState.EJECTED,
            PlayerState.ERROR,
        ):
            raise PlaybackError(f"cannot load while in state {state.name}")
        self._transition(PlayerState.LOADING)
        self._load_watchdog.start()
        try:
            player = self._ensure_player()
        except PlaybackError as exc:
            self._fail(exc)
            raise
        try:
            disc_hash = DiscIdentity.compute(source)
        except PlaybackError as exc:
            self._fail(exc)
            return
        try:
            try:
                player.dvd_device = source
            except (AttributeError, MPVError, OSError, SystemError):
                try:
                    player.command(
                        "loadfile", "dvdnav://", "replace", f"dvd-device={source}"
                    )
                except (MPVError, OSError, SystemError, AttributeError):
                    player.play("dvdnav://")
        except (MPVError, OSError, SystemError, AttributeError) as exc:
            self._fail(exc)
            return
        self._source = source
        self._disc_hash = disc_hash

    def play_pause(self) -> None:
        """Toggle pause for PLAYING/PAUSED; A selects inside menus."""
        if self.state is PlayerState.PLAYING:
            if self._call(lambda p: setattr(p, "pause", True)):
                self._transition(PlayerState.PAUSED)
        elif self.state is PlayerState.PAUSED and self._call(
            lambda p: setattr(p, "pause", False)
        ):
            self._transition(PlayerState.PLAYING)

    def stop(self) -> None:
        """Save resume state and stop playback."""
        if self.state not in (
            PlayerState.PLAYING,
            PlayerState.PAUSED,
            PlayerState.MENU_ACTIVE,
        ):
            return
        self._save_resume_now()
        if self._call(lambda p: p.command("stop")):
            self._transition(PlayerState.STOPPED)

    def set_speed(self, mult: float) -> None:
        """Set playback speed during PLAYING/PAUSED."""
        if self.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        value = float(mult)
        if self._call(lambda p: setattr(p, "speed", value)):
            self._speed = value

    def next_chapter(self) -> None:
        """Skip to the next DVD chapter while playing."""
        if self.state is not PlayerState.PLAYING or self._player is None:
            return
        try:
            chapter = self._player.chapter
            if chapter is not None:
                self._player.chapter = int(chapter) + 1
        except (MPVError, OSError, SystemError, AttributeError) as exc:
            self._fail(exc)

    def previous_chapter(self) -> None:
        """Skip to the previous DVD chapter while playing."""
        if self.state is not PlayerState.PLAYING or self._player is None:
            return
        try:
            chapter = self._player.chapter
            if chapter is not None:
                self._player.chapter = max(1, int(chapter) - 1)
        except (MPVError, OSError, SystemError, AttributeError) as exc:
            self._fail(exc)

    def cycle_audio(self) -> None:
        """Cycle to the next audio track during PLAYING/PAUSED."""
        if self.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        player = self._player
        if player is None:
            return
        try:
            ids = [
                track["id"]
                for track in player.track_list
                if track.get("type") == "audio"
            ]
            current = player.aid
            if ids and current in ids:
                player.aid = ids[(ids.index(current) + 1) % len(ids)]
        except (MPVError, OSError, SystemError, AttributeError, KeyError, TypeError) as exc:
            self._fail(exc)

    def cycle_subtitle(self) -> None:
        """Cycle to the next subtitle track during PLAYING/PAUSED."""
        if self.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        player = self._player
        if player is None:
            return
        try:
            ids = [
                track["id"]
                for track in player.track_list
                if track.get("type") == "sub"
            ]
            current = player.sid
            if ids and current in ids:
                player.sid = ids[(ids.index(current) + 1) % len(ids)]
        except (MPVError, OSError, SystemError, AttributeError, KeyError, TypeError) as exc:
            self._fail(exc)

    def menu_nav(self, direction: str) -> None:
        """Move the DVD menu highlight in one of four directions."""
        if not self._menu_active or direction not in {"up", "down", "left", "right"}:
            return
        self._call(lambda p: p.command("discnav", direction))

    def menu_select(self) -> None:
        """Activate the highlighted DVD menu item."""
        if not self._menu_active:
            return
        self._call(lambda p: p.command("discnav", "select"))

    def menu_back(self) -> None:
        """Return to the previous DVD menu."""
        if not self._menu_active:
            return
        self._call(lambda p: p.command("discnav", "prev"))

    def menu_mouse_move(self, x: int, y: int) -> None:
        """Forward a menu pointer position to dvdnav."""
        if not self._menu_active:
            return
        px, py = int(x), int(y)
        self._call(lambda p: p.command("discnav", "mouse_move", px, py))

    def show_root_menu(self) -> None:
        """Jump to the disc root menu."""
        self._call(lambda p: p.command("discnav", "menu"))

    def show_title_menu(self) -> None:
        """Jump to the title menu (falls back to the root menu)."""
        if not self._title_menu_logged:
            self._title_menu_logged = True
            log.info("title menu unavailable: falling back to discnav 'menu'")
        self._call(lambda p: p.command("discnav", "menu"))

    def set_title(self, title: int) -> None:
        """Switch to DVD title ``title`` during PLAYING/PAUSED."""
        if self.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        value = int(title)
        self._call(lambda p: setattr(p, "disc_title", value))

    def seek_to(self, seconds: float) -> None:
        """Seek within the current title during PLAYING/PAUSED."""
        if self.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        position = float(seconds)
        self._call(lambda p: setattr(p, "time_pos", position))

    def adjust_volume(self, delta: float) -> None:
        """Add ``delta`` to the volume, clamped to 0..150."""
        player = self._player
        if player is None:
            return
        try:
            current = player.volume
            base = 100.0 if current is None else float(current)
            player.volume = min(150.0, max(0.0, base + float(delta)))
        except (MPVError, OSError, SystemError, AttributeError, TypeError, ValueError) as exc:
            self._fail(exc)

    def resume_disc(self, saved: dict) -> None:  # type: ignore[type-arg]
        """Stage a saved resume entry, applied on the next PLAYING state."""
        self._pending_resume = dict(saved) if saved else None

    def eject(self) -> None:
        """Save state, terminate mpv, eject the disc and enter EJECTED."""
        self._save_resume_now()
        source = self._source
        player = self._player
        self._player = None
        self._wid = None  # allow attach_widget() to rebuild mpv on next load
        if player is not None:
            try:
                player.terminate()
            except (MPVError, OSError, SystemError, AttributeError):
                log.exception("mpv terminate failed during eject")
        if self._menu_active:
            self._menu_active = False
            self.menu_active_changed.emit(False)
        if source is not None:
            try:
                if stat.S_ISBLK(os.stat(source).st_mode):
                    eject_device(source)
            except (OSError, PlaybackError):
                log.exception("could not eject %r", source)
        try:
            self._transition(PlayerState.EJECTED)
        except PlaybackError:
            log.debug("eject transition rejected from %s", self.state.name)

    def dispose(self) -> None:
        """Release mpv once; safe to call repeatedly."""
        if self._disposed:
            return
        self._disposed = True
        self._load_watchdog.stop()
        self._save_resume_now()
        player = self._player
        self._player = None
        if player is not None:
            try:
                player.terminate()
            except (MPVError, OSError, SystemError, AttributeError):
                log.exception("mpv terminate failed during dispose")

    # ── properties ──────────────────────────────────────────────────
    @property
    def state(self) -> PlayerState:
        """Current player state."""
        return self._sm.state

    @property
    def menu_active(self) -> bool:
        """Whether a DVD menu is currently shown."""
        return self._menu_active

    @property
    def time_pos(self) -> float | None:
        """Position within the current title, in seconds."""
        player = self._player
        if player is None:
            return None
        try:
            value = player.time_pos
        except (OSError, SystemError, AttributeError, TypeError, ValueError):
            return None
        return None if value is None else float(value)

    @property
    def duration(self) -> float | None:
        """Duration of the current title, in seconds."""
        player = self._player
        if player is None:
            return None
        try:
            value = player.duration
        except (OSError, SystemError, AttributeError, TypeError, ValueError):
            return None
        return None if value is None else float(value)

    @property
    def chapter(self) -> int | None:
        """Current chapter number, if known."""
        player = self._player
        if player is None:
            return None
        try:
            value = player.chapter
        except (OSError, SystemError, AttributeError, TypeError, ValueError):
            return None
        return None if value is None else int(value)

    @property
    def chapter_list(self) -> list[int]:
        """Chapter start times reported by mpv (empty when unavailable)."""
        player = self._player
        if player is None:
            return []
        try:
            value = player.chapter_list
        except (OSError, SystemError, AttributeError):
            return []
        if not isinstance(value, list):
            return []
        return [int(item) for item in value]

    @property
    def speed(self) -> float:
        """Requested playback speed multiplier."""
        return self._speed

    @property
    def disc_title(self) -> int | None:
        """Current DVD title number, if known."""
        player = self._player
        if player is None:
            return None
        try:
            value = player.disc_title
        except (
            MPVError,
            AttributeError,
            OSError,
            SystemError,
            TypeError,
            ValueError,
        ):
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def volume(self) -> float | None:
        """Current mpv volume (0..150)."""
        player = self._player
        if player is None:
            return None
        try:
            value = player.volume
        except (OSError, SystemError, AttributeError, TypeError, ValueError):
            return None
        return None if value is None else float(value)

    @property
    def player(self) -> Any:
        """Underlying python-mpv instance, if attached."""
        return self._player

    # ── private helpers ─────────────────────────────────────────────
    def _ensure_player(self) -> Any:
        """Return the mpv instance or raise when no widget is attached."""
        if self._player is None:
            raise PlaybackError(
                "engine not attached to a video widget yet (attach_widget first)"
            )
        return self._player

    def _call(self, action: Callable[[Any], None]) -> bool:
        """Run a player action, converting mpv failures into ERROR state."""
        player = self._player
        if player is None:
            return False
        try:
            action(player)
        except (MPVError, OSError, SystemError, AttributeError) as exc:
            self._fail(exc)
            return False
        return True

    def _fail(self, exc: Exception) -> None:
        """Enter ERROR (when legal) and notify listeners."""
        log.exception("playback error: %s", exc)
        try:
            self._transition(PlayerState.ERROR)
        except PlaybackError:
            log.debug("state machine rejected ERROR from %s", self.state.name)
        self.error_occurred.emit(str(exc))

    def _save_resume_now(self) -> None:
        """Persist the current position for the loaded disc, if possible."""
        if self._disc_hash is None:
            return
        if self.state not in (
            PlayerState.PLAYING,
            PlayerState.PAUSED,
            PlayerState.MENU_ACTIVE,
        ):
            return
        try:
            title = None if self._menu_active else self.disc_title
            self._resume.save(
                self._disc_hash,
                self.time_pos or 0.0,
                title,
                self.chapter,
            )
        except OSError as exc:
            log.warning("could not save resume state: %s", exc)

    def _apply_menu_active(self, active: bool) -> None:
        """React to dvdnav menu activation on the GUI thread."""
        self._menu_active = bool(active)
        state = self.state
        if active and state in (
            PlayerState.LOADING,
            PlayerState.PLAYING,
            PlayerState.PAUSED,
            PlayerState.STOPPED,
        ):
            self._transition(PlayerState.MENU_ACTIVE)
        elif not active and state in (PlayerState.LOADING, PlayerState.MENU_ACTIVE):
            self._transition(PlayerState.PLAYING)
        if self.state is PlayerState.PLAYING and self._pending_resume:
            self._apply_pending_resume()
        self.menu_active_changed.emit(self._menu_active)

    def _apply_pending_resume(self) -> None:
        """Best-effort restore of the staged resume entry."""
        saved = self._pending_resume
        self._pending_resume = None
        player = self._player
        if not saved or player is None:
            return
        try:
            title = saved.get("title")
            if isinstance(title, int):
                player.disc_title = title
            chapter = saved.get("chapter")
            if isinstance(chapter, int):
                player.chapter = chapter
            pos = saved.get("pos")
            if isinstance(pos, (int, float)) and float(pos) > 0:
                player.time_pos = float(pos)
        except (MPVError, OSError, SystemError, AttributeError) as exc:
            log.warning("could not apply resume state: %s", exc)

    def _apply_time_pos(self, value: float | None) -> None:
        """Forward a new playback position to listeners."""
        if value is not None:
            self.position_changed.emit(float(value))

    def _apply_tracks(self, value: object) -> None:
        """Split mpv's track list into audio/subtitle summaries."""
        audio: list[dict[str, object]] = []
        subs: list[dict[str, object]] = []
        if isinstance(value, list):
            for track in value:
                if not isinstance(track, dict):
                    continue
                entry: dict[str, object] = {
                    "id": track.get("id"),
                    "lang": track.get("lang"),
                    "title": track.get("title"),
                }
                if track.get("type") == "audio":
                    audio.append(entry)
                elif track.get("type") == "sub":
                    subs.append(entry)
        self._audio = audio
        self._subs = subs
        self.tracks_changed.emit(self._audio, self._subs)

    def _apply_eof(self) -> None:
        """Handle end-of-file on the GUI thread."""
        if self.state is PlayerState.LOADING:
            self._fail(PlaybackError("disc failed to start: end-of-file during load"))
        elif self.state in (PlayerState.PLAYING, PlayerState.PAUSED) and not self._menu_active:
            self._transition(PlayerState.STOPPED)
            self.playback_ended.emit()

    def _on_load_timeout(self) -> None:
        """Watchdog: LOADING must resolve within LOAD_TIMEOUT_MS."""
        if self.state is PlayerState.LOADING:
            self._fail(
                PlaybackError("playback did not start: disc unreadable or drive error")
            )

    def _transition(self, new: PlayerState) -> None:
        """Apply a validated transition and announce it."""
        self._sm.transition(new)
        if new is not PlayerState.LOADING:
            self._load_watchdog.stop()
        self.state_changed.emit(self._sm.state)
# ── [AGENT1: SECTION 4 END] ──

# ════════════════════════════════════════════════════════════════════
# SECTION 5 — Xbox360Osd overlay
# ════════════════════════════════════════════════════════════════════

# SPEC for SECTION 5 (Xbox360Osd) — contract, do not edit:
#   class Xbox360Osd(QWidget) — child overlay strip at the bottom of the
#     video widget (geometry set by MainWindow.resizeEvent). NOT
#     transparent-for-mouse: it intercepts clicks only while visible.
#   Signals:
#     button_clicked = pyqtSignal(str)   # one of the BUTTON names below
#   API (signatures exact):
#     __init__(self, parent: Optional[QWidget] = None)
#     set_transport(self, *, state: PlayerState, speed: float = 1.0,
#                   time_pos: Optional[float] = None,
#                   duration: Optional[float] = None,
#                   chapter: Optional[int] = None,
#                   chapter_count: Optional[int] = None,
#                   audio_lang: Optional[str] = None,
#                   subtitle_lang: Optional[str] = None) -> None
#     show_osd(self) -> None
#     _button_rects(self) -> list[tuple[str, QRectF]]   # hit-testing/tests
#     paintEvent(self, event: QPaintEvent) -> None
#     mousePressEvent(self, event: QMouseEvent) -> None
#     mouseMoveEvent(self, event: QMouseEvent) -> None  # hover repaint
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
#     when chapter_count is not None. Buttons right-aligned, 34 px spacing,
#     14 pt font, base color white, hover color Xbox green
#     QColor(116, 195, 41) via setMouseTracking(True). Draw helper
#     _fmt_time(seconds: float) -> str ('mm:ss' or 'h:mm:ss').
#   * show_osd: show(), raise_(), (re)start a singleShot
#     QTimer(OSD_TIMEOUT_MS) -> hide() (Xbox: any activity resets timeout).
#   * set_transport: store fields; update().
#   * mousePressEvent: hit-test _button_rects(); on hit emit
#     button_clicked(name) and accept the event.
#   * Paint-only: no child widgets, no external assets, no QSS.
# ── [AGENT2: SECTION 5 BEGIN] ──
# ── [AGENT2: SECTION 5 END] ──

# ════════════════════════════════════════════════════════════════════
# SECTION 6 — MainWindow (input mapping & wiring)
# ════════════════════════════════════════════════════════════════════

# SPEC for SECTION 6 (MainWindow) — contract, do not edit:
#   class MainWindow(QMainWindow) — top-level window:
#     * central QWidget, QVBoxLayout margins 0 spacing 0:
#       - self.video: plain QWidget (autoFillBackground black) stretch=1;
#         receives mouse events; child OSD strip (self.osd) positioned in
#         resizeEvent: self.osd.setGeometry(0, height - 64, width, 64).
#       - bottom bar QHBoxLayout (padding 6): self.source QLineEdit
#         (placeholder '/dev/sr0 | /path/movie.iso | /path/to/VIDEO_TS'),
#         'Browse…' QPushButton, 'Load / Play' QPushButton, 'Eject'
#         QPushButton, self.status QLabel (elided, initial 'IDLE').
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
#     _sync_osd(self) -> None
#     _cycle_speed(self, direction: int) -> None
#     _attach_engine(self) -> None
#     closeEvent(self, event: QCloseEvent) -> None
#   Wiring:
#   * engine signals -> handlers: state_changed->_on_engine_state (update
#     self.status with state name + _sync_osd); position_changed->_on_position
#     (_sync_osd); tracks_changed->_on_tracks (cache lists + _sync_osd);
#     menu_active_changed->_on_menu_active (_sync_osd); error_occurred->
#     _on_engine_error (QMessageBox.critical).
#   * After window.show(): QTimer.singleShot(0, self._attach_engine) ->
#     engine.attach_widget(int(self.video.winId())) inside try/except
#     PlaybackError (QMessageBox.critical + status 'ERROR'); then when
#     __init__ received a non-empty source: call _on_load_clicked().
#     _attach_engine() is also called at the start of _on_load_clicked so
#     a fresh mpv is rebuilt after eject() (attach_widget no-ops while
#     already attached).
#   * Video mouse: install eventFilter on self.video (_on_video_mouse):
#     MouseMove -> osd.show_osd(); when engine.menu_active:
#     engine.menu_mouse_move(pos.x(), pos.y()). MouseButtonPress (Left) ->
#     osd.show_osd(); when menu active: engine.menu_select().
#   * _handle_key mapping (return False when unmapped; osd.show_osd() on
#     every consumed key; Qt.Key enum from PyQt6.QtCore):
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
#       Key_E -> eject               Key_Escape -> osd.hide() only
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
#   * _on_load_clicked: self._attach_engine() first (idempotent; re-creates
#     mpv after an eject); text = source.text().strip(); when empty use the
#     first list_dvd_devices() result if any, else QMessageBox.warning
#     ('No source') and return. problems = check_environment(source); when
#     problems: QMessageBox.warning joined text ('…\nProceeding anyway.').
#     DiscIdentity.compute(source) -> on PlaybackError QMessageBox.critical
#     and return. saved = ResumeStore().get(hash); when saved:
#     _show_resume_prompt(hash, saved) else engine.load(source).
#   * _show_resume_prompt: QMessageBox.question 'Resume playback from
#     mm:ss?' (Yes -> engine.load(source) then engine.resume_disc(saved);
#     No -> ResumeStore().forget(hash) then engine.load(source)).
#   * _on_osd_button: 'play_pause' -> play_pause; 'stop' -> stop;
#     'ff' -> _cycle_speed(+1); 'rw' -> _cycle_speed(-1); 'prev' ->
#     previous_chapter; 'next' -> next_chapter; 'audio' -> cycle_audio;
#     'subtitle' -> cycle_subtitle; 'menu' -> show_root_menu; 'eject' ->
#     _on_eject_clicked.
#   * _on_eject_clicked: engine.eject(); status 'EJECTED'.
#   * _on_browse_clicked: QFileDialog.getOpenFileName('.iso (*.iso)');
#     when canceled try getExistingDirectory ('folder with VIDEO_TS');
#     set self.source text when picked.
#   * closeEvent: engine.dispose(); event.accept().
#   * _sync_osd: osd.set_transport(state=engine.state, speed=engine.speed,
#     time_pos=engine.time_pos, duration=engine.duration,
#     chapter=engine.chapter, chapter_count=(len(engine.chapter_list) when
#     engine.player else None), audio_lang/current subtitle_lang from
#     cached tracks matched to engine player.aid/sid); called from
#     _on_position/_on_engine_state/_on_menu_active/_on_tracks.
# ── [AGENT2: SECTION 6 BEGIN] ──
# ── [AGENT2: SECTION 6 END] ──

# ════════════════════════════════════════════════════════════════════
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
    if os.environ.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.info(
            "Wayland session detected: forcing QT_QPA_PLATFORM=xcb for mpv embedding"
        )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    engine = DvdPlaybackEngine()
    window = MainWindow(engine, sys.argv[1] if len(sys.argv) > 1 else None)
    window.show()
    code = app.exec()
    engine.dispose()
    return code


if __name__ == "__main__":
    sys.exit(main())
