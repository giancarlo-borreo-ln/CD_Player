"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import locale
import os
import stat
import threading
import time
from collections.abc import Callable
from typing import Any

import mpv
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .hardware import eject_device
from .identity import DiscIdentity, ResumeStore, _coerce_track_id
from .state import LOAD_TIMEOUT_MS, PlaybackError, PlayerState, StateMachine, log

# python-mpv >= 1.0 has no legacy ``mpv.MPVError``: C-API failures are
# reported as ValueError (ErrorCode.raise_for_ec), a dead core raises
# ShutdownError (a SystemError), and property errors raise AttributeError.
# This alias keeps the legacy name used throughout the engine.
MPVError = ValueError

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
#     disc_hash_ready = pyqtSignal(str)  # (async DiscIdentity digest)
#     (private raw signals emitted ONLY from mpv observer callbacks and the
#      disc-hash worker:
#      _menu_active_raw = pyqtSignal(bool)
#      _time_pos_raw = pyqtSignal(float)
#      _tracks_raw = pyqtSignal(object)
#      _eof_raw = pyqtSignal()
#      _disc_hash_raw = pyqtSignal(object)  # {"ok":bool,"hash"|"error":str}
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
#     cycle_aspect(self) -> None
#     resume_disc(self, saved: dict) -> None
#     capture_frame(self) -> tuple[int, int, bytes] | None
#     eject(self) -> None
#     dispose(self) -> None
#   Properties (read-only): state, menu_active, time_pos, duration, chapter,
#     chapter_list, speed, disc_title, disc_label, aspect, volume, player
#   Behavior:
#   * __init__: QObject ctor; StateMachine(IDLE); ResumeStore();
#     _player=None, _wid=None, _source=None, _disc_hash=None,
#     _pending_resume=None, _menu_active=False, _speed=1.0, _audio=[],
#     _subs=[], _hash_worker=None, _resume_requested=False,
#     _menu_jump_armed_at=None; connect raw signals to private handlers
#     (_apply_menu_active, _apply_time_pos, _apply_tracks, _apply_eof,
#     _apply_disc_hash).
#   * attach_widget(wid): idempotent while attached. Create mpv.MPV(
#     wid=str(wid), input_default_bindings=False, input_cursor=False,
#     cursor_autohide="no" (mpv's own ~1 s auto-hide would otherwise make the
#     cursor vanish over the video, worst in fullscreen — independent of
#     input_cursor=False),
#     osc=False, osd_level=0, keep_open=True, idle=True — idle=True keeps
#     the libmpv core alive between loads (idle=False lets the core shut
#     down ~100 ms after creation, so every property read then raises
#     ShutdownError). Assign _wid only AFTER the instance exists; on
#     construction failure reset _wid=None and raise PlaybackError. Register property
#     DVD_PLAYER_RENDER selects the VO path (B1/B2): unset or 'software'
#     uses the software branch: vo='gpu', gpu_context='x11egl',
#     LIBGL_ALWAYS_SOFTWARE=1 (llvmpipe), display_fps_override=25 (matches
#     DVD content cadence rather than the display sync),
#     scale='bilinear', dscale='bilinear', gpu_dumb_mode=True
#     (cheapest llvmpipe shaders) and video_sync='audio' (retime against the
#     audio clock; autosync stays at mpv's default 0, so no drift-correction
#     episode after seeks) — fallback for
#     NVIDIA-on-XWayland machines
#     where all hardware GL contexts fail; DVD bitrates render fine in
#     software at near video frame rate. 'auto' first attempts the hardware
#     VO (vo='gpu', gpu_context='x11egl', NO software options); if that init
#     probe raises, log and fall back to the software branch non-fatally so
#     playback still starts. DVD_PLAYER_SOFTWARE_RENDER=1 remains an alias
#     forcing software.
#     observers (python-mpv @self._player.property_observer decorator) for
#     'time-pos', 'disc-menu-active', 'track-list', 'eof-reached' that emit
#     the raw signals only (eof observer emits only when value is True).
#   * load(source): allowed from {IDLE, STOPPED, EJECTED, ERROR}, else
#     PlaybackError. transition(LOADING); _ensure_player() — on failure
#     _fail(exc) (LOADING->ERROR + error_occurred emitted) then raise.
#     Immediately AFTER obtaining the player and BEFORE touching dvd_device,
#     reset carryover transport (aid/sid auto, speed 1.0, pause off) plus
#     _speed = 1.0 — mpv keeps these across load() and DVD track ids are
#     per-title, so a stale aid/sid would select the wrong stream. Each
#     reset is NON-FATAL (only log.debug; never _fail / never leaves
#     LOADING). Set
#     player.dvd_device = source THEN player.play("dvdnav://") — BOTH run in
#     the success path (setting the device alone never starts playback); on
#     failure _fail(exc) + return. Store _source, reset _disc_hash=None, then
#     START the async hash worker: load NEVER reads the disc on the GUI
#     thread, so playback begins immediately. The hash (and a resume prompt)
#     arrives later while PLAYING. Any MPVError/OSError/SystemError/
#     AttributeError -> transition(ERROR) + emit error_occurred(str(exc)) +
#     log.exception. LOADING->PLAYING / LOADING->MENU_ACTIVE arrives later
#     via _apply_menu_active; hash failures are only logged (playback errors
#     surface through mpv/watchdog).
#   * _start_hash_worker(source, epoch): daemon threading.Thread running
#     DiscIdentity.compute(source) plus a best-effort DiscIdentity.label()
#     (label failures yield ""); it emits ONLY _disc_hash_raw with
#     {"ok": True, "hash": str, "label": str, "epoch": int} or
#     {"ok": False, "error": str, "epoch": int} (emit wrapped in try/except
#     RuntimeError for shutdown); kept on _hash_worker. load()/eject()/
#     dispose() bump _hash_epoch so in-flight workers of a previous disc are
#     invalidated (stale/out-of-order payloads are dropped in the handler).
#   * _apply_disc_hash(payload) — GUI-thread handler for _disc_hash_raw:
#     payloads whose epoch != current _hash_epoch are DROPPED (stale-hash
#     guard). On ok -> _disc_hash = hash; cache _disc_label from the
#     payload's "label"; emit disc_hash_ready(hash). On error -> log.warning
#     only (no state change, no error_occurred).

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
#     _menu_active; player.command('discnav', direction) via _discnav,
#     which is NON-FATAL: ANY exception (not just mpv errors) is logged
#     at debug and swallowed, so the non-fatal menu path NEVER propagates
#     into the Qt singleShot callback / event loop (no ERROR, no abort).
#     menu_select: discnav 'select' (menu only, non-fatal). menu_back:
#     discnav 'prev' (menu only, non-fatal). menu_mouse_move(x, y):
#     discnav 'mouse_move' x y (menu only, ints, non-fatal).
#   * show_root_menu: player.command('discnav', 'menu') via _discnav
#     (non-fatal: an unavailable discnav never errors the engine).
#   * show_title_menu: player.command('discnav', 'menu') via _discnav
#     (non-fatal; libmpv exposes no separate VTSM title-menu jump;
#     log.info this fallback once).
#   * menu-first load: on the FIRST transition into PLAYING after load
#     (the LOADING->PLAYING branch of _apply_menu_active, i.e. first-play
#     content started — never waiting for a menu to appear naturally),
#     immediately jump INTO the disc menu via _discnav('menu') so the
#     first-play trailers are skipped. A 250 ms singleShot settle QTimer
#     is armed and time.monotonic() is recorded at arm time; Qt's
#     CoarseTimer may fire early, so the callback re-checks
#     elapsed >= 0.25 s and, when short, re-arms the remaining time
#     (single-shot) and returns WITHOUT sending — the settle guarantee is
#     never violated. The callback also re-checks state == PLAYING, not
#     disposed, _resume_requested False and _pending_resume is None
#     before sending (abort otherwise). A resume wins: the jump is
#     suppressed at trigger time when _resume_requested is True or
#     _pending_resume is not None (resume_disc marks _resume_requested
#     even when it applies immediately) and re-checked in the callback
#     (skip + mark done). Exactly once per load epoch: _menu_jump_done is
#     reset in load() and set BEFORE sending; eject()/dispose() set it
#     too. A menu-less disc needs no special case (discnav failures are
#     already non-fatal). A disc that starts menu-active
#     (LOADING->MENU_ACTIVE) needs no jump and the flag is simply never
#     armed.
#   * set_title(title): {PLAYING, PAUSED} only; player.disc_title = title.
#   * cycle_aspect(): cycle player.video_aspect '-1' -> '16:9' -> '4:3' ->
#     '-1' and cache the current value in _aspect (exposed via aspect).
#     Missing player / mpv failure is NON-FATAL (log only, never ERROR /
#     error_occurred).
#   * seek_to(seconds): {PLAYING, PAUSED} only. Latest-wins settling:
#     when no settle is pending, apply player.time_pos = seconds via _call
#     and start the 350 ms singleShot _seek_settle; while it is active,
#     store seconds in _pending_seek_pos and restart the timer (at most one
#     immediate write per burst). _on_seek_settle applies the pending
#     position via _call and clears it (at most one final write).
#     eject()/dispose() stop _seek_settle (and clear _pending_seek_pos) so
#     no write lands on a dead player; on a disposed engine seek_to is a
#     no-op (returns before the state guard, never re-arms the timer).
#   * seek atomicity: seek bursts are coalesced by _seek_settle (350 ms,
#     singleShot QTimer created in __init__); an intermediate seek never
#     overwrites a newer target, and the burst produces at most one
#     immediate write plus one latest-wins settle write.
#   * adjust_volume(delta): player.volume = min(150, max(0, volume + delta)).
#   * resume_disc(saved): _resume_requested = True (unconditionally, in
#     EVERY staging mode) and _pending_resume = dict(saved). The flag is
#     reset False in load() and is what suppresses the menu-first jump
#     even when the resume applies immediately and clears _pending_resume.
#     Applied AT MOST ONCE by _apply_pending_resume(): IMMEDIATELY when
#     staged while state
#     is PLAYING and not _menu_active and _player is not None (the async
#     hash/prompt often arrives after playback started), otherwise on the
#     first transition into PLAYING after staging (the stage is cleared
#     BEFORE applying, so a later PAUSED->PLAYING unpause never re-applies
#     it):
#     title (int, not None) -> disc_title = title; chapter (int, not None)
#     -> chapter = chapter; pos (float > 0) -> time_pos = pos; then aid/sid
#     (int, restored ONLY once the track list is known: _audio non-empty)
#     -> player.aid/sid when the id exists in the cached track ids (invalid
#     ids are ignored); then clear. All best-effort inside try/except
#     (resume must never break playback).
#   * capture_frame: GUI-thread seek-thumbnail helper. Returns
#     (width, height, bgr0_bytes) from mpv 'screenshot-raw' for the current
#     frame, or None when unavailable. NON-FATAL: every failure is swallowed
#     (never raises, never changes state, never emits signals).
#   * eject: _save_resume_now(); terminate player (set _player=None AND
#     _wid=None so attach_widget() can rebuild mpv for the next load); if
#     _menu_active: clear it and emit menu_active_changed(False); if
#     _source was a block device: eject_device(_source); transition(EJECTED)
#     (legal from EVERY state except EJECTED — console tray semantics; the
#     transition is wrapped in try/except and a rejected transition only
#     logs; the load watchdog is disarmed by the non-LOADING transition).
#   * dispose: idempotent; stops the load watchdog and bumps _hash_epoch
#     (invalidates any in-flight disc-hash worker); _save_resume_now(); if
#     _player: terminate and _player=None. Does NOT change state.
#   * _ensure_player: raise PlaybackError("engine not attached to a video
#     widget yet (attach_widget first)") when _player is None.
#   * _save_resume_now: when _disc_hash and state in {PLAYING, PAUSED,
#     MENU_ACTIVE}: ResumeStore.save(_disc_hash, time_pos or 0.0,
#     disc_title when not menu else None, chapter, aid=player.aid,
#     sid=player.sid) — string/None mpv values become None (v2 fields).
#   * _apply_menu_active(active) — handler for _menu_active_raw:
#       _menu_active = bool(active)
#       if active and state in {LOADING, PLAYING, PAUSED, STOPPED}: transition(MENU_ACTIVE)
#       elif not active and state == LOADING: transition(PLAYING) + arm
#           the menu-first jump (see the menu-first load bullet; armed
#           BEFORE the resume block below so a staged resume wins)
#       elif not active and state == MENU_ACTIVE: transition(PLAYING)
#       if state == PLAYING and _pending_resume: _apply_pending_resume()
#       emit menu_active_changed(_menu_active)
#     Invariant: menu_active == (state == MENU_ACTIVE) after each handler
#     completes; eject() clears both.
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
#     catch it and only log). ERROR is entered only from {LOADING,
#     MENU_ACTIVE, PLAYING, PAUSED}; failures while IDLE/STOPPED/EJECTED
#     surface via error_occurred + GUI status without a state change.
#   * Residual-state rule: every public operation EXCEPT load is a NO-OP
#     outside its declared pre-state (never raises, never signals); load
#     raises PlaybackError outside {IDLE, STOPPED, EJECTED, ERROR}.
#   * Properties return safe values when _player is None or the libmpv core
#     has shut down: time_pos=None, duration=None, chapter=None,
#     chapter_list=[], disc_title=None, volume=None, speed=_speed,
#     menu_active=_menu_active, state=_sm.state. Every property read is
#     wrapped in try/except (OSError, SystemError, AttributeError,
#     TypeError, ValueError) and degrades to None/[]. chapter-list entries
#     may be floats or {'title':..., 'time':...} maps (mpv >= 0.36); the
#     property extracts the time from either shape.
#   * Threading contract: ALL public methods GUI-thread-only. mpv observers
#     run on mpv's thread and may ONLY emit raw signals — never touch the
#     StateMachine, never emit public signals directly. The disc-hash worker
#     thread may ONLY emit _disc_hash_raw (and nothing else).
# ── [AGENT1: SECTION 4 BEGIN] ──
class DvdPlaybackEngine(QObject):
    """libmpv/dvdnav backend; all public methods are GUI-thread-only."""

    state_changed = pyqtSignal(object, arguments=())
    position_changed = pyqtSignal(float, arguments=())
    menu_active_changed = pyqtSignal(bool, arguments=())
    tracks_changed = pyqtSignal(object, object, arguments=())
    playback_ended = pyqtSignal(arguments=())
    error_occurred = pyqtSignal(str, arguments=())
    disc_hash_ready = pyqtSignal(str, arguments=())

    _menu_active_raw = pyqtSignal(bool, arguments=())
    _time_pos_raw = pyqtSignal(float, arguments=())
    _tracks_raw = pyqtSignal(object, arguments=())
    _eof_raw = pyqtSignal(arguments=())
    _disc_hash_raw = pyqtSignal(object, arguments=())

    def __init__(self, parent: QObject | None = None) -> None:
        """Wire raw observer signals and start in IDLE."""
        super().__init__(parent)  # type: ignore[arg-type]
        self._sm = StateMachine(PlayerState.IDLE)
        self._resume = ResumeStore()
        self._player: Any = None
        self._wid: int | None = None
        self._source: str | None = None
        self._disc_hash: str | None = None
        self._disc_label: str | None = None
        self._pending_resume: dict[str, object] | None = None
        self._menu_active = False
        self._speed = 1.0
        self._aspect = "-1"
        self._audio: list[dict[str, object]] = []
        self._subs: list[dict[str, object]] = []
        self._disposed = False
        self._title_menu_logged = False
        self._hash_worker: Any = None
        self._hash_epoch = 0  # stale-hash guard: workers carry their epoch
        self._pending_seek_pos: float | None = None
        self._menu_jump_done = False  # exactly-once menu-first jump per load
        self._menu_jump_armed_at: float | None = None  # monotonic arm timestamp
        self._resume_requested = False  # a resume wins over the menu jump
        self._menu_active_raw.connect(self._apply_menu_active)
        self._time_pos_raw.connect(self._apply_time_pos)
        self._tracks_raw.connect(self._apply_tracks)
        self._eof_raw.connect(self._apply_eof)
        self._disc_hash_raw.connect(self._apply_disc_hash)
        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.setInterval(LOAD_TIMEOUT_MS)
        self._load_watchdog.timeout.connect(self._on_load_timeout)
        self._seek_settle = QTimer(self)
        self._seek_settle.setSingleShot(True)
        self._seek_settle.setInterval(350)
        self._seek_settle.timeout.connect(self._on_seek_settle)
        self._menu_jump_settle = QTimer(self)
        self._menu_jump_settle.setSingleShot(True)
        self._menu_jump_settle.setInterval(250)
        self._menu_jump_settle.timeout.connect(self._on_menu_jump_settle)

    # ── public API ──────────────────────────────────────────────────
    def attach_widget(self, wid: int) -> None:
        """Create the mpv instance bound to the given native window id."""
        if self._wid is not None and self._player is not None:
            return
        # Qt may reset LC_NUMERIC after app init; libmpv requires the C
        # locale (warns, and may crash, with comma-decimal locales).
        locale.setlocale(locale.LC_NUMERIC, "C")
        try:
            options: dict[str, Any] = {
                "wid": str(wid),
                "input_default_bindings": False,
                "input_cursor": False,
                # mpv auto-hides the cursor on its own (~1 s) regardless of
                # input_cursor; keep it visible over the video in fullscreen.
                "cursor_autohide": "no",
                "osc": False,
                "osd_level": 0,
                "keep_open": True,
                "idle": True,  # keep the libmpv core alive between loads
            }
            # B1/B2: software by default; DVD_PLAYER_RENDER=auto probes the
            # hardware VO first and falls back non-fatally to software.
            render = os.environ.get("DVD_PLAYER_RENDER")
            software = render != "auto"
            player: Any = None
            if render == "auto":
                # Init probe: hardware VO only (no software-only options).
                # A failed probe must never prevent playback from starting.
                hardware_options = dict(options)
                hardware_options["vo"] = "gpu"
                hardware_options["gpu_context"] = "x11egl"
                try:
                    player = mpv.MPV(**hardware_options)
                except Exception as exc:  # noqa: BLE001 - probe fallback
                    log.warning(
                        "hardware render init failed; falling back to software: %s",
                        exc,
                    )
                    software = True
            if software:
                # NVIDIA-on-XWayland: every hardware GL context fails there
                # (Mesa has no NVIDIA DRI driver, NVIDIA EGL dmabuf import
                # fails, GLX is indirect); llvmpipe via x11egl renders fine
                # and DVD bitrates are light enough for software decode+scale.
                os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
                options["vo"] = "gpu"
                options["gpu_context"] = "x11egl"
                # llvmpipe on a 144 Hz display: cap presentation to DVD rate
                # and pick the cheapest scalers/shader path.
                options["display_fps_override"] = 25
                options["scale"] = "bilinear"
                options["dscale"] = "bilinear"
                options["gpu_dumb_mode"] = True
                # retime against the audio clock; mpv's own autosync (0) is
                # intentionally left at its default — no drift correction
                # episode after seeks.
                options["video_sync"] = "audio"
                player = mpv.MPV(**options)
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
        self._menu_jump_settle.stop()
        self._menu_jump_done = False
        self._menu_jump_armed_at = None
        self._resume_requested = False
        self._load_watchdog.start()
        try:
            player = self._ensure_player()
        except PlaybackError as exc:
            self._fail(exc)
            raise
        self._reset_carryover_transport(player)
        try:
            player.dvd_device = source
            player.play("dvdnav://")
        except (AttributeError, MPVError, OSError, SystemError) as exc:
            self._fail(exc)
            return
        self._source = source
        self._disc_hash = None
        self._disc_label = None
        self._hash_epoch += 1  # invalidate any in-flight worker of a previous disc
        self._start_hash_worker(source, self._hash_epoch)

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
        self._discnav(direction)

    def menu_select(self) -> None:
        """Activate the highlighted DVD menu item."""
        if not self._menu_active:
            return
        self._discnav("select")

    def menu_back(self) -> None:
        """Return to the previous DVD menu."""
        if not self._menu_active:
            return
        self._discnav("prev")

    def menu_mouse_move(self, x: int, y: int) -> None:
        """Forward a menu pointer position to dvdnav."""
        if not self._menu_active:
            return
        px, py = int(x), int(y)
        self._discnav("mouse_move", px, py)

    def show_root_menu(self) -> None:
        """Jump to the disc root menu."""
        self._discnav("menu")

    def show_title_menu(self) -> None:
        """Jump to the title menu (falls back to the root menu)."""
        if not self._title_menu_logged:
            self._title_menu_logged = True
            log.info("title menu unavailable: falling back to discnav 'menu'")
        self._discnav("menu")

    def set_title(self, title: int) -> None:
        """Switch to DVD title ``title`` during PLAYING/PAUSED."""
        if self.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        value = int(title)
        self._call(lambda p: setattr(p, "disc_title", value))

    def cycle_aspect(self) -> None:
        """Cycle video aspect auto -> 16:9 -> 4:3 -> auto (non-fatal)."""
        player = self._player
        if player is None:
            return
        cycle = ("-1", "16:9", "4:3")
        try:
            current = getattr(player, "video_aspect", None)
        except (MPVError, OSError, SystemError, AttributeError):
            current = None
        if not isinstance(current, str) or current not in cycle:
            current = self._aspect if self._aspect in cycle else "-1"
        nxt = cycle[(cycle.index(current) + 1) % len(cycle)]
        try:
            player.video_aspect = nxt
        except (
            MPVError,
            OSError,
            SystemError,
            AttributeError,
            TypeError,
            ValueError,
        ) as exc:
            log.info("aspect switch unavailable: %s", exc)
            return
        self._aspect = nxt

    def seek_to(self, seconds: float) -> None:
        """Seek within the current title during PLAYING/PAUSED."""
        if self._disposed:
            return
        if self.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        position = float(seconds)
        if self._seek_settle.isActive():
            # Latest wins: coalesce intermediate seeks into one final apply.
            self._pending_seek_pos = position
            self._seek_settle.start()
            return
        self._pending_seek_pos = None
        self._call(lambda p: setattr(p, "time_pos", position))
        self._seek_settle.start()

    def _on_seek_settle(self) -> None:
        """Apply the most recent coalesced seek once the burst settles."""
        position = self._pending_seek_pos
        if position is None:
            return
        self._pending_seek_pos = None
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
        """Stage a saved resume entry, applied now when already PLAYING."""
        self._resume_requested = True
        self._pending_resume = dict(saved) if saved else None
        if (
            self.state is PlayerState.PLAYING
            and not self._menu_active
            and self._player is not None
        ):
            self._apply_pending_resume()

    def capture_frame(self) -> tuple[int, int, bytes] | None:
        """Return (width, height, bgr0_bytes) of the current video frame, or
        None when unavailable. NEVER raises and NEVER changes state."""
        player = self._player
        if player is None or self._disposed:
            return None
        try:
            raw = player.command("screenshot-raw")
            if not isinstance(raw, dict):
                return None
            if raw.get("format") != "bgr0":
                return None
            width = int(raw.get("w", 0))
            height = int(raw.get("h", 0))
            if width <= 0 or height <= 0:
                return None
            data = bytes(raw.get("data", b""))
            stride = int(raw.get("stride", width * 4))
            row_bytes = width * 4
            if stride == row_bytes:
                packed = data
            else:
                packed = b"".join(
                    data[offset : offset + row_bytes]
                    for offset in range(0, stride * height, stride)
                )
            if len(packed) < row_bytes * height:
                return None
            return width, height, packed
        except (
            MPVError,
            OSError,
            SystemError,
            AttributeError,
            TypeError,
            ValueError,
        ):
            return None

    def eject(self) -> None:
        """Save state, terminate mpv, eject the disc and enter EJECTED."""
        self._save_resume_now()
        self._hash_epoch += 1  # invalidate any in-flight disc-hash worker
        self._seek_settle.stop()
        self._pending_seek_pos = None
        self._menu_jump_settle.stop()
        self._menu_jump_done = True
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
        self._hash_epoch += 1  # invalidate any in-flight disc-hash worker
        self._seek_settle.stop()
        self._pending_seek_pos = None
        self._menu_jump_settle.stop()
        self._menu_jump_done = True
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
        times: list[int] = []
        for item in value:
            raw = item.get("time") if isinstance(item, dict) else item
            if raw is None:
                continue
            try:
                times.append(int(raw))
            except (TypeError, ValueError):
                continue
        return times

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
    def disc_label(self) -> str | None:
        """PVD volume id of the loaded disc, once the hash worker reports it."""
        return self._disc_label

    @property
    def aspect(self) -> str:
        """Current aspect setting requested by cycle_aspect()."""
        return self._aspect

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

    def _reset_carryover_transport(self, player: Any) -> None:
        """Clear transport state mpv carries over from the previous title/disc."""
        for name, value in (
            ("aid", "auto"),
            ("sid", "auto"),
            ("speed", 1.0),
            ("pause", False),
        ):
            try:
                setattr(player, name, value)
            except (
                MPVError,
                OSError,
                SystemError,
                AttributeError,
                TypeError,
                ValueError,
            ) as exc:
                log.debug("could not reset mpv %s on load: %s", name, exc)
        self._speed = 1.0

    def _start_hash_worker(self, source: str, epoch: int) -> None:
        """Compute the disc hash off the GUI thread; only raw signals cross."""

        def _work() -> None:
            payload: dict[str, object]
            try:
                digest = DiscIdentity.compute(source)
            except Exception as exc:  # noqa: BLE001 - errors travel in the payload
                payload = {"ok": False, "error": str(exc), "epoch": epoch}
            else:
                # The label is metadata only: failing to read it must never
                # invalidate a successful hash (directory sources have no PVD).
                try:
                    label = DiscIdentity.label(source)
                except (PlaybackError, OSError, ValueError) as exc:
                    log.debug("disc label unavailable for %r: %s", source, exc)
                    label = ""
                payload = {
                    "ok": True,
                    "hash": digest,
                    "label": label,
                    "epoch": epoch,
                }
            try:
                self._disc_hash_raw.emit(payload)
            except RuntimeError:
                log.debug("disc hash signal dropped: engine already destroyed")

        worker = threading.Thread(target=_work, daemon=True, name="disc-hash")
        self._hash_worker = worker
        worker.start()

    def _discnav(self, *args: object) -> None:
        """Send a dvdnav command; ANY failure is non-fatal and only logged."""
        player = self._player
        if player is None:
            return
        try:
            player.command("discnav", *args)
        except Exception as exc:  # noqa: BLE001 - menu path must never propagate
            log.debug("discnav %s unavailable: %s", args, exc)

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
                aid=self._current_track_id("aid"),
                sid=self._current_track_id("sid"),
            )
        except OSError as exc:
            log.warning("could not save resume state: %s", exc)

    def _current_track_id(self, name: str) -> int | None:
        """Return mpv's int aid/sid, or None for 'auto'/missing values."""
        player = self._player
        if player is None:
            return None
        try:
            value = getattr(player, name)
        except (
            MPVError,
            OSError,
            SystemError,
            AttributeError,
            TypeError,
            ValueError,
        ):
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

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
            if state is PlayerState.LOADING:
                self._arm_menu_jump()
        if self.state is PlayerState.PLAYING and self._pending_resume:
            self._apply_pending_resume()
        self.menu_active_changed.emit(self._menu_active)

    def _arm_menu_jump(self) -> None:
        """Arm the once-per-load jump into the menu after first-play starts."""
        if self._menu_jump_done or self._menu_jump_settle.isActive():
            return
        if self._resume_requested or self._pending_resume is not None:
            # Resume wins: never jump into the menu for this load.
            self._menu_jump_done = True
            return
        self._menu_jump_settle.setInterval(250)
        self._menu_jump_armed_at = time.monotonic()
        self._menu_jump_settle.start()

    def _on_menu_jump_settle(self) -> None:
        """Send the menu-first jump once the first-play content settled."""
        if self._menu_jump_done:
            return
        if self._disposed or self.state is not PlayerState.PLAYING:
            return
        if self._resume_requested or self._pending_resume is not None:
            self._menu_jump_done = True
            return
        armed_at = self._menu_jump_armed_at
        if armed_at is not None:
            remaining = 0.25 - (time.monotonic() - armed_at)
            if remaining > 0.0:
                # CoarseTimer may fire up to ~13 ms early: re-arm the
                # remainder so the 250 ms settle guarantee always holds.
                self._menu_jump_settle.setInterval(max(1, int(remaining * 1000) + 1))
                self._menu_jump_settle.start()
                return
        self._menu_jump_done = True
        self._discnav("menu")

    def _apply_disc_hash(self, payload: object) -> None:
        """Apply the worker's hash result on the GUI thread (stale epochs dropped)."""
        if not isinstance(payload, dict):
            return
        if payload.get("epoch") != self._hash_epoch:
            log.debug("stale disc hash dropped (epoch %s)", payload.get("epoch"))
            return
        if payload.get("ok"):
            value = payload.get("hash")
            if isinstance(value, str):
                self._disc_hash = value
                label = payload.get("label")
                self._disc_label = label if isinstance(label, str) else None
                self.disc_hash_ready.emit(value)
            return
        log.warning("could not identify disc: %s", payload.get("error"))

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
            # Language memory needs the disc's track list; only restore ids
            # that actually exist, otherwise leave mpv's current choice.
            if self._audio:
                audio_ids = [track.get("id") for track in self._audio]
                aid = _coerce_track_id(saved.get("aid"))
                if aid is not None and aid in audio_ids:
                    player.aid = aid
                sub_ids = [track.get("id") for track in self._subs]
                sid = _coerce_track_id(saved.get("sid"))
                if sid is not None and sid in sub_ids:
                    player.sid = sid
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
