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
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPainter,
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

import mpv  # python-mpv (ctypes bindings over libmpv)

log = logging.getLogger("dvd_player")

# ── [AGENTS: extra imports appended here if needed] ──

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
#       MENU_ACTIVE -> {PLAYING, STOPPED, ERROR}
#       PLAYING     -> {PAUSED, MENU_ACTIVE, STOPPED, ERROR}
#       PAUSED      -> {PLAYING, MENU_ACTIVE, STOPPED, ERROR}
#       STOPPED     -> {IDLE, LOADING, EJECTED}
#       EJECTED     -> {IDLE, LOADING}
#       ERROR       -> {LOADING}
#   transition(new): no-op when new == state; raise PlaybackError(
#       f"illegal transition {self._state.name} -> {new.name}") otherwise.
#   reset(): state = IDLE (no validation, used on fresh load lifecycle).
#   GUI-thread only; mpv-thread callbacks must never call transition
#   directly (see SECTION 4 threading contract).
# ── [AGENT1: SECTION 1 BEGIN] ──
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
#   * attach_widget(wid): idempotent (second call is a no-op). Create
#     mpv.MPV(wid=str(wid), input_default_bindings=False, input_cursor=False,
#     osc=False, osd_level=0, keep_open=True, idle=False); register property
#     observers (python-mpv @self._player.property_observer decorator) for
#     'time-pos', 'disc-menu-active', 'track-list', 'eof-reached' that emit
#     the raw signals only (eof observer emits only when value is True).
#   * load(source): allowed from {IDLE, STOPPED, EJECTED, ERROR}, else
#     PlaybackError. _ensure_player(). transition(LOADING). Compute
#     DiscIdentity.compute(source) (PlaybackError -> transition(ERROR) +
#     emit error_occurred + return). Try player.dvd_device = source; on
#     exception fall back to player.command('loadfile', 'dvdnav://',
#     'replace', f'dvd-device={source}') else player.play('dvdnav://').
#     Store _source/_disc_hash. Any mpv.MPVError/OSError -> transition(ERROR)
#     + emit error_occurred(str(exc)) + log.exception. LOADING->PLAYING /
#     LOADING->MENU_ACTIVE arrives later via _apply_menu_active.
#   * play_pause: PLAYING->PAUSED (player.pause=True); PAUSED->PLAYING
#     (False); MENU_ACTIVE: no-op (A=select in menus). Else no-op.
#   * stop: from {PLAYING, PAUSED, MENU_ACTIVE}: _save_resume_now();
#     player.command('stop'); transition(STOPPED). Else no-op.
#   * set_speed(mult): {PLAYING, PAUSED} only; player.speed = mult;
#     _speed = mult.
#   * next_chapter/previous_chapter: only when state is PLAYING and
#     player.chapter is not None; player.chapter += 1 (prev: -= 1),
#     guarded by try/except.
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
#   * eject: _save_resume_now(); terminate player (set _player=None); if
#     _source was a block device: eject_device(_source); transition(EJECTED).
#     Safe from any state except when player calls raise (wrap try/except).
#   * dispose: idempotent; _save_resume_now(); if _player: terminate and
#     _player=None. Does NOT change state.
#   * _ensure_player: raise PlaybackError("engine not attached to a video
#     widget yet (attach_widget first)") when _player is None.
#   * _save_resume_now: when _disc_hash and state in {PLAYING, PAUSED,
#     MENU_ACTIVE}: ResumeStore.save(_disc_hash, time_pos or 0.0,
#     disc_title when not menu else None, chapter).
#   * _apply_menu_active(active) — handler for _menu_active_raw:
#       _menu_active = bool(active)
#       if active and state in {LOADING, PLAYING, PAUSED}: transition(MENU_ACTIVE)
#       elif not active and state == LOADING: transition(PLAYING)
#       elif not active and state == MENU_ACTIVE: transition(PLAYING)
#       if state == PLAYING and _pending_resume: _apply_pending_resume()
#       emit menu_active_changed(_menu_active)
#   * _apply_time_pos(value): when value is not None: emit
#     position_changed(float(value)).
#   * _apply_tracks(value): split into audio/sub lists of dicts with keys
#     {'id','lang','title'} (missing -> None); cache in _audio/_subs;
#     emit tracks_changed(_audio, _subs).
#   * _apply_eof(): when state in {PLAYING, PAUSED} and not _menu_active:
#     transition(STOPPED); emit playback_ended().
#   * _transition(new): _sm.transition(new) then emit state_changed(state).
#   * Every player call wrapped: on mpv.MPVError -> transition(ERROR) +
#     emit error_occurred(str(exc)) + log.exception. StateMachine blocks
#     ERROR->ERROR double transitions (raise PlaybackError inside handler;
#     catch it and only log).
#   * Properties return safe values when _player is None (time_pos=None,
#     duration=None, chapter=None, chapter_list=[], disc_title=None,
#     volume=None, speed=_speed, menu_active=_menu_active, state=_sm.state).
#   * Threading contract: ALL public methods GUI-thread-only. mpv observers
#     run on mpv's thread and may ONLY emit raw signals — never touch the
#     StateMachine, never emit public signals directly.
# ── [AGENT1: SECTION 4 BEGIN] ──
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
#     engine.attach_widget(int(self.video.winId())); then when __init__
#     received a non-empty source: call _on_load_clicked().
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
#   * _on_load_clicked: text = source.text().strip(); when empty use the
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
