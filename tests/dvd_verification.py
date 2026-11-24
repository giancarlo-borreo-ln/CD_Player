"""Acceptance tests for the SECTION 1-4 state machine / eject reachability.

Read-only audit support: lives in /tmp, imports the repo module by path.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

from PyQt6.QtCore import QCoreApplication

from dvd_player import (
    DiscIdentity,
    DvdPlaybackEngine,
    PlaybackError,
    PlayerState,
    ResumeStore,
    StateMachine,
)

COUNT = 0


def check(condition: bool) -> None:
    global COUNT
    assert condition
    COUNT += 1


def expect_error(fn) -> None:
    global COUNT
    try:
        fn()
    except PlaybackError:
        COUNT += 1
        return
    raise AssertionError("expected PlaybackError")


class FakePlayer:
    def __init__(self) -> None:
        self.terminated = 0

    def terminate(self) -> None:
        self.terminated += 1


# ── 1. _LEGAL table is exactly the SPEC table ──────────────────────
S = PlayerState
expected = {
    S.IDLE: {S.LOADING, S.EJECTED},
    S.LOADING: {S.MENU_ACTIVE, S.PLAYING, S.EJECTED, S.ERROR},
    S.MENU_ACTIVE: {S.PLAYING, S.STOPPED, S.EJECTED, S.ERROR},
    S.PLAYING: {S.PAUSED, S.MENU_ACTIVE, S.STOPPED, S.EJECTED, S.ERROR},
    S.PAUSED: {S.PLAYING, S.MENU_ACTIVE, S.STOPPED, S.EJECTED, S.ERROR},
    S.STOPPED: {S.IDLE, S.LOADING, S.MENU_ACTIVE, S.EJECTED},
    S.EJECTED: {S.IDLE, S.LOADING},
    S.ERROR: {S.LOADING, S.EJECTED},
}
check(StateMachine._LEGAL == expected)

# ── 2. new console-eject reachability is legal ─────────────────────
for origin in (S.PLAYING, S.PAUSED, S.MENU_ACTIVE):
    sm = StateMachine(origin)
    sm.transition(S.EJECTED)
    check(sm.state is S.EJECTED)

# idempotent repeat-eject is not a second transition
sm = StateMachine(S.EJECTED)
sm.transition(S.EJECTED)
check(sm.state is S.EJECTED)

# ── 3. still-illegal transitions must raise ────────────────────────
expect_error(lambda: StateMachine(S.IDLE).transition(S.PLAYING))
expect_error(lambda: StateMachine(S.IDLE).transition(S.STOPPED))
expect_error(lambda: StateMachine(S.MENU_ACTIVE).transition(S.PAUSED))
expect_error(lambda: StateMachine(S.STOPPED).transition(S.PLAYING))
expect_error(lambda: StateMachine(S.ERROR).transition(S.PLAYING))

# ── 4. SPEC trace sequences ────────────────────────────────────────
sm = StateMachine()
for target in (
    S.LOADING,
    S.MENU_ACTIVE,
    S.PLAYING,
    S.PAUSED,
    S.PLAYING,
    S.STOPPED,
    S.LOADING,
):
    sm.transition(target)
    check(sm.state is target)

sm = StateMachine(S.LOADING)
sm.transition(S.ERROR)
sm.transition(S.LOADING)
check(sm.state is S.LOADING)

sm = StateMachine(S.PLAYING)
sm.transition(S.EJECTED)
sm.transition(S.LOADING)
check(sm.state is S.LOADING)

sm = StateMachine(S.MENU_ACTIVE)
sm.transition(S.EJECTED)
check(sm.state is S.EJECTED)
sm = StateMachine(S.PAUSED)
sm.transition(S.EJECTED)
check(sm.state is S.EJECTED)

# ERROR -> ERROR is a no-op, never a double transition, never raises
sm = StateMachine(S.ERROR)
sm.transition(S.ERROR)
check(sm.state is S.ERROR)

# ── 5. engine.eject() reachability + resource reset ────────────────
app = QCoreApplication.instance() or QCoreApplication([])

for origin in (S.PLAYING, S.PAUSED, S.MENU_ACTIVE, S.STOPPED):
    engine = DvdPlaybackEngine()
    engine._sm = StateMachine(origin)  # simulate having reached the state
    fake = FakePlayer()
    engine._player = fake  # type: ignore[assignment]
    engine._wid = 4242
    if origin is S.MENU_ACTIVE:
        engine._menu_active = True
    emitted: list[bool] = []
    engine.menu_active_changed.connect(emitted.append)
    engine.eject()
    check(engine.state is S.EJECTED)
    check(engine._player is None)
    check(engine._wid is None)
    check(fake.terminated == 1)
    if origin is S.MENU_ACTIVE:
        check(engine.menu_active is False)
        check(emitted == [False])

# IDLE -> eject() must not crash and must reach EJECTED (console tray)
engine = DvdPlaybackEngine()
engine.eject()
check(engine.state is S.EJECTED)

# second eject from EJECTED must not crash nor double-terminate
engine = DvdPlaybackEngine()
engine._sm = StateMachine(S.PLAYING)
fake = FakePlayer()
engine._player = fake  # type: ignore[assignment]
engine.eject()
engine.eject()
check(engine.state is S.EJECTED)
check(fake.terminated == 1)

# ERROR -> ERROR through the public _transition helper is a no-op
engine = DvdPlaybackEngine()
engine._sm = StateMachine(S.ERROR)
engine._transition(S.ERROR)
check(engine.state is S.ERROR)

# dispose is idempotent and leaves state alone
engine = DvdPlaybackEngine()
engine._sm = StateMachine(S.PLAYING)
fake = FakePlayer()
engine._player = fake  # type: ignore[assignment]
engine.dispose()
engine.dispose()
check(engine.state is S.PLAYING)
check(fake.terminated == 1)

# ── 6. mpv-thread raw signal is queued to the GUI thread ───────────
engine = DvdPlaybackEngine()
engine._sm = StateMachine(S.LOADING)
worker = threading.Thread(target=lambda: engine._menu_active_raw.emit(True))
worker.start()
worker.join()
check(engine.state is S.LOADING)  # queued: not applied on the mpv thread
app.processEvents()
check(engine.state is S.MENU_ACTIVE)  # applied by the GUI thread

# ── 7. D1 stale label payload is dropped under the epoch guard ─────
engine = DvdPlaybackEngine()
engine._hash_epoch = 3
engine._apply_disc_hash({"ok": True, "hash": "a" * 64, "label": "GOOD", "epoch": 3})
check(engine.disc_label == "GOOD")
engine._apply_disc_hash({"ok": True, "hash": "b" * 64, "label": "OLD", "epoch": 2})
check(engine.disc_label == "GOOD" and engine._disc_hash == "a" * 64)
engine.dispose()

# ── 8. D1 DiscIdentity.label reads only the PVD ────────────────────
with tempfile.TemporaryDirectory() as tmp:
    iso = Path(tmp) / "disc.iso"
    data = bytearray(32768 + 2048)
    data[32768 + 40 : 32768 + 72] = b"VERIFY".ljust(32, b"\x00")
    iso.write_bytes(bytes(data))
    check(DiscIdentity.label(str(iso)) == "VERIFY")
    try:
        DiscIdentity.label("/nonexistent/label")
    except PlaybackError:
        check(True)
    else:
        check(False)

# ── 9. E1 aspect cycle order + non-fatal failure ───────────────────
engine = DvdPlaybackEngine()
fake = FakePlayer()
fake.video_aspect = "-1"
engine._player = fake  # type: ignore[assignment]
values = []
for _ in range(3):
    engine.cycle_aspect()
    values.append(fake.video_aspect)
check(values == ["16:9", "4:3", "-1"])
engine.cycle_aspect()
check(engine.aspect == "16:9")
engine.dispose()

# ── 10. F1/F2 resume schema v2 + language restore gate ────────────
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "resume.json"
    path.write_text(json.dumps({"legacy": {"pos": 1.0, "title": 1,
                                           "chapter": 1, "updated": 1.0}}))
    store = ResumeStore(path)
    check("aid" not in store.get("legacy"))
    store.save("h", 2.0, 1, 1, aid=3, sid=4)
    entry = store.get("h")
    check(entry is not None and entry["aid"] == 3 and entry["sid"] == 4)
    path.write_text(json.dumps({"h": {"pos": 0.0, "title": None, "chapter": None,
                                      "aid": "bad", "sid": None, "updated": 1.0}}))
    check(store.get("h")["aid"] is None)

    engine = DvdPlaybackEngine()
    player = FakePlayer()
    player.aid = 1
    player.sid = 2
    engine._player = player  # type: ignore[assignment]
    engine._sm = StateMachine(PlayerState.PLAYING)
    engine._audio = [{"id": 3}]
    engine._subs = [{"id": 4}]
    engine.resume_disc({"aid": 3, "sid": 4})
    check(player.aid == 3 and player.sid == 4)
    engine.resume_disc({"aid": 99, "sid": 99})
    check(player.aid == 3 and player.sid == 4)
    engine.dispose()

print(f"VERIFICATION OK {COUNT} assertions")
