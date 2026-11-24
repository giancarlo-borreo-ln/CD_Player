"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import logging
import os
from enum import Enum, auto
from pathlib import Path
from typing import ClassVar

log = logging.getLogger("dvd_player")

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
DISC_POLL_MS = 2000  # console-style disc-insertion polling (auto-play)
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
#       IDLE        -> {LOADING, EJECTED}
#       LOADING     -> {MENU_ACTIVE, PLAYING, EJECTED, ERROR}
#       MENU_ACTIVE -> {PLAYING, STOPPED, EJECTED, ERROR}
#       PLAYING     -> {PAUSED, MENU_ACTIVE, STOPPED, EJECTED, ERROR}
#       PAUSED      -> {PLAYING, MENU_ACTIVE, STOPPED, EJECTED, ERROR}
#       STOPPED     -> {IDLE, LOADING, MENU_ACTIVE, EJECTED}
#       EJECTED     -> {IDLE, LOADING}
#       ERROR       -> {LOADING, EJECTED}
#   Eject is legal from EVERY state except EJECTED (console: the tray always
#   opens, even while loading or after an error); eject from EJECTED is the
#   same-state no-op. transition(new): no-op when new == state; raise
#   PlaybackError(f"illegal transition {state} -> {new}") otherwise.
#   reset(): state = IDLE — the DECLARED unvalidated exception (it produces
#   STOPPED->IDLE and EJECTED->IDLE plus emergency resets); transition()
#   itself never bypasses the table.
#   GUI-thread only; mpv-thread callbacks must never call transition
#   directly (see SECTION 4 threading contract).
# ── [AGENT1: SECTION 1 BEGIN] ──
class StateMachine:
    """Validate player state transitions on the GUI thread."""

    _LEGAL: ClassVar[dict[PlayerState, set[PlayerState]]] = {
        PlayerState.IDLE: {PlayerState.LOADING, PlayerState.EJECTED},
        PlayerState.LOADING: {
            PlayerState.MENU_ACTIVE,
            PlayerState.PLAYING,
            PlayerState.EJECTED,
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
        PlayerState.ERROR: {PlayerState.LOADING, PlayerState.EJECTED},
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
