"""Smoke tests for the dvd_player backend regions 1-4."""
from __future__ import annotations

import json
import locale
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

import mpv
from PyQt6.QtCore import QCoreApplication

from dvd_player import (
    RESUME_MAX_ENTRIES,
    DiscIdentity,
    DvdPlaybackEngine,
    PlaybackError,
    PlayerState,
    ResumeStore,
    StateMachine,
    check_environment,
    disc_present,
    eject_device,
)

COUNT = 0


def check(condition: bool) -> None:
    global COUNT
    assert condition
    COUNT += 1


def expect_playback_error(fn, *args: object) -> None:
    global COUNT
    try:
        fn(*args)
    except PlaybackError:
        COUNT += 1
        return
    raise AssertionError(f"{getattr(fn, '__name__', fn)}{args} did not raise PlaybackError")


# ── a. StateMachine ────────────────────────────────────────────────
sm = StateMachine()
check(sm.state is PlayerState.IDLE)
for target in (
    PlayerState.LOADING,
    PlayerState.PLAYING,
    PlayerState.PAUSED,
    PlayerState.PLAYING,
    PlayerState.STOPPED,
    PlayerState.EJECTED,
):
    sm.transition(target)
    check(sm.state is target)

sm.reset()
check(sm.state is PlayerState.IDLE)
expect_playback_error(sm.transition, PlayerState.PLAYING)  # IDLE -> PLAYING illegal
sm.transition(PlayerState.IDLE)  # same-state no-op
check(sm.state is PlayerState.IDLE)
sm.transition(PlayerState.LOADING)
sm.transition(PlayerState.LOADING)  # same-state no-op
check(sm.state is PlayerState.LOADING)

# ── b. DiscIdentity ────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "disc"
    (root / "VIDEO_TS").mkdir(parents=True)
    ifo = root / "VIDEO_TS" / "VIDEO_TS.IFO"
    ifo.write_bytes(b"FAKEIFO" * 100)

    first = DiscIdentity.compute(str(root))
    second = DiscIdentity.compute(str(root))
    check(len(first) == 64)
    check(first == first.lower())
    check(all(c in "0123456789abcdef" for c in first))
    check(first == second)

    ifo.write_bytes(b"FAKEIFO" * 101)
    check(DiscIdentity.compute(str(root)) != first)

    plain = Path(tmp) / "movie.iso"
    plain.write_bytes(b"x" * 4096)
    check(len(DiscIdentity.compute(str(plain))) == 64)

    empty = Path(tmp) / "empty"
    empty.mkdir()
    expect_playback_error(DiscIdentity.compute, str(empty))

expect_playback_error(DiscIdentity.compute, "/nonexistent/disc")

# ── c. ResumeStore ─────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    store = ResumeStore(Path(tmp) / "nested" / "resume.json")
    check(store.load() == {})
    check(store.get("abc") is None)
    store.save("abc", 42.5, 1, 3)
    entry = store.get("abc")
    check(entry is not None)
    assert entry is not None
    check(float(entry["pos"]) == 42.5)  # type: ignore[arg-type]
    check(entry["title"] == 1)
    check(entry["chapter"] == 3)
    store.forget("abc")
    check(store.get("abc") is None)

    many = ResumeStore(Path(tmp) / "many.json")
    for index in range(RESUME_MAX_ENTRIES + 4):
        many.save(f"hash{index}", float(index), None, None)
        time.sleep(0.002)
    loaded = many.load()
    check(len(loaded) == RESUME_MAX_ENTRIES)
    check(f"hash{RESUME_MAX_ENTRIES + 3}" in loaded)
    check("hash0" not in loaded)

    corrupt = Path(tmp) / "corrupt.json"
    corrupt.write_bytes(b"{broken")
    check(ResumeStore(corrupt).load() == {})

# ── d. check_environment ───────────────────────────────────────────
missing = check_environment("/nonexistent/disc42")
check(isinstance(missing, list))
check(len(missing) == 1)
check("exist" in missing[0].lower())

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "disc"
    (root / "VIDEO_TS").mkdir(parents=True)
    (root / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"IFO")
    problems = check_environment(str(root))
    check(isinstance(problems, list))

    not_dvd = Path(tmp) / "plain"
    not_dvd.mkdir()
    check(any("not a DVD layout" in p for p in check_environment(str(not_dvd))))

# ── e. device helpers ──────────────────────────────────────────────
check(disc_present("/dev/nonexistent0") is None)
expect_playback_error(eject_device, "/dev/nonexistent0")

# ── f. engine offscreen ────────────────────────────────────────────
app = QCoreApplication([])
engine = DvdPlaybackEngine()
check(engine.state is PlayerState.IDLE)
check(engine.menu_active is False)
check(engine.time_pos is None)
check(engine.duration is None)
check(engine.chapter is None)
check(engine.chapter_list == [])
check(engine.disc_title is None)
check(engine.volume is None)
check(engine.speed == 1.0)

errors: list[str] = []
engine.error_occurred.connect(errors.append)
# Unattached: load still raises from _ensure_player (no widget), not from
# the disc hash (which is now never computed on the GUI thread).
expect_playback_error(engine.load, "/dev/nonexistent0")
check(engine.state is PlayerState.ERROR)
check(len(errors) == 1)
check(len(errors[0]) > 0)

engine.menu_nav("up")
engine.menu_select()
engine.menu_back()
engine.menu_mouse_move(10, 20)
engine.play_pause()
engine.stop()
engine.set_speed(2.0)
engine.next_chapter()
engine.previous_chapter()
engine.cycle_audio()
engine.cycle_subtitle()
engine.show_root_menu()
engine.show_title_menu()
engine.set_title(1)
engine.seek_to(10.0)
engine.adjust_volume(5.0)
engine.resume_disc({"pos": 1.0, "title": 1, "chapter": 1})
engine.dispose()
engine.dispose()  # idempotent
check(True)

# ── f2. async disc hash (perf contract: play before hashing) ───────
class FakePlayer:
    def __init__(self) -> None:
        self.dvd_device: str | None = None
        self.played = 0
        self.terminated = 0
        self.disc_title: int | None = None
        self.chapter: int | None = None
        self.time_pos: float | None = None
        self.aid: object = None
        self.sid: object = None
        self.speed: float = 1.0
        self.pause: bool = False
        self.transport_writes: list[tuple[str, object]] = []

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        if name in ("aid", "sid", "speed", "pause"):
            writes = self.__dict__.get("transport_writes")
            if writes is not None:
                writes.append((name, value))

    def play(self, target: str) -> None:
        self.played += 1

    def terminate(self) -> None:
        self.terminated += 1


def wait_hash(eng: DvdPlaybackEngine) -> None:
    worker = eng._hash_worker
    assert worker is not None
    worker.join(timeout=30.0)
    check(not worker.is_alive())
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "disc"
    (root / "VIDEO_TS").mkdir(parents=True)
    (root / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"FAKEIFO" * 50)

    hashing = DvdPlaybackEngine()
    fake = FakePlayer()
    hashing._player = fake  # type: ignore[assignment]
    ready: list[str] = []
    hashing.disc_hash_ready.connect(ready.append)
    hashing.load(str(root))
    # load returns BEFORE the disc is hashed, already playing.
    check(hashing.state is PlayerState.LOADING)
    check(fake.played == 1 and fake.dvd_device == str(root))
    check(hashing._disc_hash is None)
    wait_hash(hashing)
    check(len(ready) == 1)
    check(len(ready[0]) == 64)
    check(all(c in "0123456789abcdef" for c in ready[0]))
    check(hashing._disc_hash == ready[0])
    hashing.dispose()

    # A missing device no longer fails inside load(): playback starts, the
    # hash error is logged only, and the watchdog still surfaces the error.
    missing_engine = DvdPlaybackEngine()
    fake2 = FakePlayer()
    missing_engine._player = fake2  # type: ignore[assignment]
    watch_errors: list[str] = []
    missing_engine.error_occurred.connect(watch_errors.append)
    missing_engine.load("/dev/nonexistent0")  # must NOT raise from the hash
    check(missing_engine.state is PlayerState.LOADING)
    check(fake2.played == 1)
    wait_hash(missing_engine)
    check(len(watch_errors) == 0)
    check(missing_engine._disc_hash is None)
    missing_engine._apply_eof()
    check(missing_engine.state is PlayerState.ERROR)
    check(len(watch_errors) == 1)
    missing_engine.dispose()

    timeout_engine = DvdPlaybackEngine()
    fake3 = FakePlayer()
    timeout_engine._player = fake3  # type: ignore[assignment]
    timeout_errors: list[str] = []
    timeout_engine.error_occurred.connect(timeout_errors.append)
    timeout_engine.load("/dev/nonexistent0")
    check(timeout_engine.state is PlayerState.LOADING)
    timeout_engine._on_load_timeout()
    check(timeout_engine.state is PlayerState.ERROR)
    check(len(timeout_errors) == 1)
    timeout_engine.dispose()

    # ── f3. transport reset on load (no stale audio/speed carryover) ──
    reset_engine = DvdPlaybackEngine()
    stale = FakePlayer()
    stale.aid = 3
    stale.sid = 5
    stale.speed = 2.0
    stale.pause = True
    stale.transport_writes.clear()
    reset_engine._speed = 2.0
    reset_engine._player = stale  # type: ignore[assignment]
    reset_engine.load("/dev/nonexistent0")
    check(reset_engine.state is PlayerState.LOADING)
    check(stale.aid == "auto")
    check(stale.sid == "auto")
    check(stale.speed == 1.0)
    check(stale.pause is False)
    check(reset_engine.speed == 1.0)
    check(("aid", "auto") in stale.transport_writes)
    check(("sid", "auto") in stale.transport_writes)
    check(("speed", 1.0) in stale.transport_writes)
    check(("pause", False) in stale.transport_writes)
    wait_hash(reset_engine)
    reset_engine.dispose()

    class FailingAidPlayer(FakePlayer):
        def __init__(self) -> None:
            self.fail_aid = False
            super().__init__()

        @property
        def aid(self) -> object:
            return self.__dict__.get("_aid")

        @aid.setter
        def aid(self, value: object) -> None:
            if self.__dict__.get("fail_aid"):
                raise mpv.MPVError("aid cannot be reset")
            self.__dict__["_aid"] = value

    # A failing reset is non-fatal: load proceeds and stays LOADING.
    fail_engine = DvdPlaybackEngine()
    fail_player = FailingAidPlayer()
    fail_player.fail_aid = True
    fail_player.sid = 5
    fail_player.speed = 2.0
    fail_player.pause = True
    fail_engine._speed = 2.0
    fail_engine._player = fail_player  # type: ignore[assignment]
    fail_engine.load("/dev/nonexistent0")
    check(fail_engine.state is PlayerState.LOADING)
    check(fail_player.sid == "auto")
    check(fail_player.speed == 1.0)
    check(fail_player.pause is False)
    check(fail_engine.speed == 1.0)
    check(fail_player.played == 1)
    wait_hash(fail_engine)
    fail_engine.dispose()

    # ── f5. seek atomicity: latest-wins settle (C4) ──────────────────
    class SeekPlayer(FakePlayer):
        def __init__(self) -> None:
            self.seek_writes: list[float] = []
            super().__init__()

        @property
        def time_pos(self) -> float | None:
            return self.__dict__.get("_time_pos")

        @time_pos.setter
        def time_pos(self, value: float | None) -> None:
            self.__dict__["_time_pos"] = value
            if value is not None:
                self.seek_writes.append(float(value))

    seek_engine = DvdPlaybackEngine()
    seek_fake = SeekPlayer()
    seek_engine._player = seek_fake  # type: ignore[assignment]
    seek_engine._sm = StateMachine(PlayerState.PLAYING)
    for pos in (10.0, 20.0, 30.0, 40.0, 50.0):
        seek_engine.seek_to(pos)
        time.sleep(0.01)
    check(len(seek_fake.seek_writes) == 1)  # one immediate write only
    check(seek_fake.seek_writes[0] == 10.0)
    check(seek_engine._pending_seek_pos == 50.0)
    deadline = time.time() + 2.0
    while time.time() < deadline and seek_engine._seek_settle.isActive():
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    check(seek_fake.seek_writes == [10.0, 50.0])  # one latest-wins settle
    check(seek_engine._pending_seek_pos is None)
    check(not seek_engine._seek_settle.isActive())
    seek_engine.dispose()

    # eject/dispose stop the settle timer: no late write, no crash.
    stop_engine = DvdPlaybackEngine()
    stop_fake = SeekPlayer()
    stop_engine._player = stop_fake  # type: ignore[assignment]
    stop_engine._sm = StateMachine(PlayerState.PLAYING)
    stop_engine.seek_to(5.0)
    check(stop_engine._seek_settle.isActive())
    stop_engine.eject()
    check(not stop_engine._seek_settle.isActive())
    stop_engine.dispose()
    deadline = time.time() + 0.5
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    check(stop_fake.seek_writes == [5.0])

    # disposed engine: seek_to is a no-op, never re-arms the settle timer.
    dead_engine = DvdPlaybackEngine()
    dead_fake = SeekPlayer()
    dead_engine._player = dead_fake  # type: ignore[assignment]
    dead_engine._sm = StateMachine(PlayerState.PLAYING)
    dead_engine.dispose()
    dead_engine.seek_to(1234.0)
    check(dead_fake.seek_writes == [])
    check(not dead_engine._seek_settle.isActive())
    check(dead_engine._pending_seek_pos is None)
    deadline = time.time() + 0.45
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    check(dead_fake.seek_writes == [])
    check(not dead_engine._seek_settle.isActive())

    # ── f6. render branch B1: unset => software (no probe) ─────────
    import dvd_player as dvd_mod

    captured: dict[str, object] = {}

    class OptionPlayer:
        def __init__(self) -> None:
            self.played = 0
            self.dvd_device: str | None = None
            self.aid: object = "auto"
            self.sid: object = "auto"
            self.speed = 1.0
            self.pause = False
            self.terminated = 0

        def property_observer(self, _name: str):
            return lambda fn: fn

        def play(self, _target: str) -> None:
            self.played += 1

        def terminate(self) -> None:
            self.terminated += 1

    calls: list[dict[str, object]] = []

    def fake_mpv(**kwargs: object) -> OptionPlayer:
        captured.clear()
        captured.update(kwargs)
        calls.append(dict(kwargs))
        return OptionPlayer()

    original_mpv = dvd_mod.mpv.MPV
    dvd_mod.mpv.MPV = fake_mpv  # type: ignore[assignment]
    try:
        for key in ("DVD_PLAYER_RENDER", "DVD_PLAYER_SOFTWARE_RENDER"):
            os.environ.pop(key, None)
        calls.clear()
        opt_engine = DvdPlaybackEngine()
        opt_engine.attach_widget(1)
        check(captured["display_fps_override"] == 25)
        check("autosync" not in captured)
        check(captured["video_sync"] == "audio")
        check(captured["scale"] == "bilinear")
        check(captured["dscale"] == "bilinear")
        check(captured["vo"] == "gpu" and captured["gpu_context"] == "x11egl")
        check(len(calls) == 1)  # default software never probes hardware
        opt_engine.dispose()

        os.environ["DVD_PLAYER_RENDER"] = "software"
        calls.clear()
        sw_engine = DvdPlaybackEngine()
        sw_engine.attach_widget(1)
        check("display_fps_override" in captured and len(calls) == 1)
        sw_engine.dispose()
        os.environ.pop("DVD_PLAYER_RENDER", None)

        # ── f7. B2: auto probes hardware, falls back non-fatally ────
        attempts: list[dict[str, object]] = []

        def flaky_mpv(**kwargs: object) -> OptionPlayer:
            attempts.append(dict(kwargs))
            if len(attempts) == 1:
                raise RuntimeError("no hardware GL context")
            return OptionPlayer()

        dvd_mod.mpv.MPV = flaky_mpv  # type: ignore[assignment]
        os.environ["DVD_PLAYER_RENDER"] = "auto"
        auto_engine = DvdPlaybackEngine()
        auto_engine.attach_widget(1)
        check(len(attempts) == 2)
        hw_attempt, sw_attempt = attempts
        check(hw_attempt["vo"] == "gpu")
        check(hw_attempt["gpu_context"] == "x11egl")
        check("display_fps_override" not in hw_attempt)
        check("scale" not in hw_attempt and "video_sync" not in hw_attempt)
        check(sw_attempt["display_fps_override"] == 25)
        check(sw_attempt["video_sync"] == "audio" and sw_attempt["scale"] == "bilinear")
        auto_engine.load("/dev/nonexistent0")  # playback must still start
        check(auto_engine.state is PlayerState.LOADING)
        check(auto_engine._player is not None and auto_engine._player.played == 1)
        auto_engine._hash_worker.join(timeout=30.0)
        auto_engine.dispose()
    finally:
        os.environ.pop("DVD_PLAYER_RENDER", None)
        os.environ.pop("DVD_PLAYER_SOFTWARE_RENDER", None)
        dvd_mod.mpv.MPV = original_mpv  # type: ignore[assignment]

    # resume_disc staged while already PLAYING applies immediately.
    resume_engine = DvdPlaybackEngine()
    fake4 = FakePlayer()
    resume_engine._player = fake4  # type: ignore[assignment]
    resume_engine._sm = StateMachine(PlayerState.PLAYING)
    resume_engine.resume_disc({"pos": 42.0, "title": 2, "chapter": 3})
    check(fake4.disc_title == 2 and fake4.chapter == 3)
    check(fake4.time_pos == 42.0)
    check(resume_engine._pending_resume is None)
    resume_engine.dispose()


# ── h. D1 disc label + stale epoch guard ───────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    iso = Path(tmp) / "labelled.iso"
    pvd_data = bytearray(32768 + 2048)
    pvd_data[32768 + 40 : 32768 + 72] = b"TONY_DISC".ljust(32, b"\x00")
    iso.write_bytes(bytes(pvd_data))
    check(DiscIdentity.label(str(iso)) == "TONY_DISC")
    expect_playback_error(DiscIdentity.label, "/nonexistent/label")
    (Path(tmp) / "folder").mkdir()
    expect_playback_error(DiscIdentity.label, str(Path(tmp) / "folder"))

    label_engine = DvdPlaybackEngine()
    label_fake = FakePlayer()
    label_engine._player = label_fake  # type: ignore[assignment]
    label_engine.load(str(iso))
    wait_hash(label_engine)
    check(label_engine.disc_label == "TONY_DISC")

    # A stale payload (old epoch) must not overwrite hash or label.
    label_engine._hash_epoch = 7
    label_engine._apply_disc_hash(
        {"ok": True, "hash": "f" * 64, "label": "NEW", "epoch": 7}
    )
    check(label_engine.disc_label == "NEW")
    label_engine._apply_disc_hash(
        {"ok": True, "hash": "e" * 64, "label": "STALE", "epoch": 6}
    )
    check(label_engine.disc_label == "NEW" and label_engine._disc_hash == "f" * 64)
    label_engine.dispose()

# ── i. E1 aspect cycle is ordered and non-fatal ────────────────────
aspect_engine = DvdPlaybackEngine()
aspect_fake = FakePlayer()
aspect_fake.video_aspect = "-1"
aspect_engine._player = aspect_fake  # type: ignore[assignment]
seen: list[object] = []
for _ in range(4):
    aspect_engine.cycle_aspect()
    seen.append(aspect_fake.video_aspect)
check(seen == ["16:9", "4:3", "-1", "16:9"])
check(aspect_engine.aspect == "16:9")
aspect_engine.cycle_aspect()
check(aspect_engine.aspect == "4:3")

bare_aspect = DvdPlaybackEngine()
bare_aspect.cycle_aspect()  # missing player: non-fatal no-op
check(bare_aspect.aspect == "-1")
bare_aspect.dispose()


class BadAspectPlayer(FakePlayer):
    @property
    def video_aspect(self) -> str:
        return "-1"

    @video_aspect.setter
    def video_aspect(self, _value: object) -> None:
        raise mpv.MPVError("aspect unsupported")


bad_engine = DvdPlaybackEngine()
bad_engine._player = BadAspectPlayer()  # type: ignore[assignment]
bad_engine.cycle_aspect()  # failure: non-fatal, no ERROR state
check(bad_engine.state is PlayerState.IDLE and bad_engine.aspect == "-1")
bad_engine.dispose()
aspect_engine.dispose()

# ── j. F1 ResumeStore schema v2 ────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "resume.json"
    legacy = {"pos": 1.5, "title": 1, "chapter": 2, "updated": 123.0}
    path.write_text(json.dumps({"legacy": legacy}), encoding="utf-8")
    store = ResumeStore(path)
    loaded = store.load()
    check(loaded["legacy"] == legacy)  # no aid/sid keys added to legacy
    check("aid" not in loaded["legacy"] and "sid" not in loaded["legacy"])

    store.save("v2", 5.0, 2, 3, aid=4, sid=7)
    entry = store.get("v2")
    check(entry is not None)
    assert entry is not None
    check(entry["aid"] == 4 and entry["sid"] == 7)
    check(entry["pos"] == 5.0 and entry["title"] == 2 and entry["chapter"] == 3)
    raw = json.loads(path.read_text(encoding="utf-8"))
    check(raw["v2"]["aid"] == 4 and raw["v2"]["sid"] == 7)

    path.write_text(
        json.dumps(
            {
                "bad": {
                    "pos": 0.0,
                    "title": None,
                    "chapter": None,
                    "aid": "x",
                    "sid": 2.5,
                    "updated": 1.0,
                },
                "bools": {
                    "pos": 0.0,
                    "title": None,
                    "chapter": None,
                    "aid": True,
                    "sid": False,
                    "updated": 2.0,
                },
            }
        ),
        encoding="utf-8",
    )
    malformed = store.load()
    check(malformed["bad"]["aid"] is None and malformed["bad"]["sid"] is None)
    check(malformed["bools"]["aid"] is None and malformed["bools"]["sid"] is None)

# ── k. F2 save/restore aid/sid with fake tracks ────────────────────
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "resume.json"
    save_engine = DvdPlaybackEngine()
    save_engine._resume = ResumeStore(path)
    saved_player = FakePlayer()
    saved_player.aid = 7
    saved_player.sid = 9
    saved_player.time_pos = 12.0
    saved_player.chapter = 2
    saved_player.disc_title = 1
    save_engine._player = saved_player  # type: ignore[assignment]
    save_engine._sm = StateMachine(PlayerState.PLAYING)
    save_engine._disc_hash = "disc-hash"
    save_engine._save_resume_now()
    saved = ResumeStore(path).get("disc-hash")
    check(saved is not None)
    assert saved is not None
    check(saved["aid"] == 7 and saved["sid"] == 9)
    check(saved["title"] == 1 and saved["chapter"] == 2)
    save_engine.dispose()

    restore_engine = DvdPlaybackEngine()
    restore_player = FakePlayer()
    restore_player.aid = 1
    restore_player.sid = 2
    restore_engine._player = restore_player  # type: ignore[assignment]
    restore_engine._sm = StateMachine(PlayerState.PLAYING)
    restore_engine._audio = [{"id": 7, "lang": "en", "title": None}]
    restore_engine._subs = [{"id": 9, "lang": "fr", "title": None}]
    restore_engine.resume_disc({"pos": 0.0, "title": 1, "chapter": 2, "aid": 7, "sid": 9})
    check(restore_player.aid == 7 and restore_player.sid == 9)

    # Invalid ids are ignored (mpv keeps its current selection).
    restore_player.aid = 1
    restore_player.sid = 2
    restore_engine.resume_disc({"aid": 99, "sid": 99})
    check(restore_player.aid == 1 and restore_player.sid == 2)

    # Track list not known yet: language ids must not be applied.
    restore_player.aid = 1
    restore_player.sid = 2
    restore_engine._audio = []
    restore_engine._subs = []
    restore_engine.resume_disc({"aid": 7, "sid": 9})
    check(restore_player.aid == 1 and restore_player.sid == 2)
    restore_engine.dispose()


# ── h. menu-first load: jump into the menu on first PLAYING ───────
class MenuPlayer:
    def __init__(self, fail: bool = False) -> None:
        self.commands: list[tuple[object, ...]] = []
        self.command_times: list[float] = []
        self.fail = fail
        self.terminated = 0
        self.disc_title: int | None = None
        self.chapter: int | None = None
        self.time_pos: float | None = None
        self.aid: object = None
        self.sid: object = None
        self.speed = 1.0
        self.pause = False
        self.dvd_device: str | None = None

    def command(self, name: str, *args: object) -> None:
        self.commands.append((name, *args))
        self.command_times.append(time.monotonic())
        if self.fail:
            raise mpv.MPVError("discnav unavailable")

    def play(self, target: str) -> None:
        pass

    def terminate(self) -> None:
        self.terminated += 1


def pump(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


# (a) first LOADING->PLAYING arms the jump; exactly one send after >=250 ms.
jump_engine = DvdPlaybackEngine()
jump_fake = MenuPlayer()
jump_engine._player = jump_fake  # type: ignore[assignment]
jump_engine._sm = StateMachine(PlayerState.LOADING)
jump_engine._apply_menu_active(False)
check(jump_engine.state is PlayerState.PLAYING)
armed_at = time.monotonic()
pump(0.15)
check(jump_fake.commands == [])  # settle has not elapsed yet
pump(0.2)
check(jump_fake.commands == [("discnav", "menu")])
check(jump_fake.command_times[0] - armed_at >= 0.24)
check(jump_engine._menu_jump_done)
check(not jump_engine._menu_jump_settle.isActive())
jump_engine._apply_menu_active(False)  # later PLAYING updates never re-jump
pump(0.3)
check(len(jump_fake.commands) == 1)
jump_engine.dispose()

# (b) staged resume wins: zero jumps.
resume_engine = DvdPlaybackEngine()
resume_fake = MenuPlayer()
resume_engine._player = resume_fake  # type: ignore[assignment]
resume_engine._sm = StateMachine(PlayerState.LOADING)
resume_engine._pending_resume = {"pos": 5.0}
resume_engine._apply_menu_active(False)
check(resume_engine.state is PlayerState.PLAYING)
check(resume_engine._pending_resume is None)
check(resume_fake.time_pos == 5.0)
check(resume_engine._menu_jump_done)
pump(0.3)
check(resume_fake.commands == [])
resume_engine.dispose()

# (c) direct LOADING->MENU_ACTIVE needs no jump: flag never armed.
menu_engine = DvdPlaybackEngine()
menu_fake = MenuPlayer()
menu_engine._player = menu_fake  # type: ignore[assignment]
menu_engine._sm = StateMachine(PlayerState.LOADING)
menu_engine._apply_menu_active(True)
check(menu_engine.state is PlayerState.MENU_ACTIVE)
check(not menu_engine._menu_jump_done)
pump(0.3)
check(menu_fake.commands == [])
menu_engine.dispose()

# (d) eject/dispose before the callback -> no send.
eject_engine = DvdPlaybackEngine()
eject_fake = MenuPlayer()
eject_engine._player = eject_fake  # type: ignore[assignment]
eject_engine._sm = StateMachine(PlayerState.LOADING)
eject_engine._apply_menu_active(False)
eject_engine.eject()
check(eject_engine.state is PlayerState.EJECTED)
check(not eject_engine._menu_jump_settle.isActive())
check(eject_engine._menu_jump_done)
pump(0.3)
check(eject_fake.commands == [])

dispose_engine = DvdPlaybackEngine()
dispose_fake = MenuPlayer()
dispose_engine._player = dispose_fake  # type: ignore[assignment]
dispose_engine._sm = StateMachine(PlayerState.LOADING)
dispose_engine._apply_menu_active(False)
dispose_engine.dispose()
check(not dispose_engine._menu_jump_settle.isActive())
pump(0.3)
check(dispose_fake.commands == [])

# (e) load twice resets the flag: exactly one jump per load epoch.
with tempfile.TemporaryDirectory() as menu_tmp:
    disc = Path(menu_tmp) / "disc"
    disc.mkdir()
    twice_engine = DvdPlaybackEngine()
    twice_fake = MenuPlayer()
    twice_engine._player = twice_fake  # type: ignore[assignment]
    twice_engine.load(str(disc))
    wait_hash(twice_engine)
    twice_engine._apply_menu_active(False)
    pump(0.3)
    check(len(twice_fake.commands) == 1)
    twice_engine.eject()
    twice_engine._player = twice_fake  # type: ignore[assignment]
    twice_engine.load(str(disc))
    wait_hash(twice_engine)
    twice_engine._apply_menu_active(False)
    pump(0.3)
    check(len(twice_fake.commands) == 2)
    twice_engine.dispose()

# (f) a raising discnav command stays non-fatal (PLAYING, no ERROR signal).
fail_engine = DvdPlaybackEngine()
fail_fake = MenuPlayer(fail=True)
fail_engine._player = fail_fake  # type: ignore[assignment]
fail_engine._sm = StateMachine(PlayerState.LOADING)
fail_errors: list[str] = []
fail_engine.error_occurred.connect(fail_errors.append)
fail_engine._apply_menu_active(False)
pump(0.3)
check(fail_fake.commands == [("discnav", "menu")])
check(fail_engine.state is PlayerState.PLAYING)
check(fail_errors == [])
fail_engine.dispose()

# (g) settle guarantee: a premature (CoarseTimer-early) callback re-arms
# the remainder and still sends exactly once, never before 250 ms.
early_engine = DvdPlaybackEngine()
early_fake = MenuPlayer()
early_engine._player = early_fake  # type: ignore[assignment]
early_engine._sm = StateMachine(PlayerState.LOADING)
early_engine._apply_menu_active(False)
check(early_engine._menu_jump_settle.isActive())
early_engine._on_menu_jump_settle()  # simulate the timer firing early
check(early_fake.commands == [])
check(not early_engine._menu_jump_done)
check(early_engine._menu_jump_settle.isActive())  # re-armed, not sent
pump(0.3)
check(early_fake.commands == [("discnav", "menu")])
early_armed = early_engine._menu_jump_armed_at
assert early_armed is not None
check(early_fake.command_times[0] - early_armed >= 0.25)
check(early_engine._menu_jump_done)
early_engine.dispose()

# (h) resume_disc() during PLAYING inside the settle window: the resume is
# applied immediately (clearing _pending_resume) yet _resume_requested keeps
# the jump suppressed -> zero jumps.
race_engine = DvdPlaybackEngine()
race_fake = MenuPlayer()
race_engine._player = race_fake  # type: ignore[assignment]
race_engine._sm = StateMachine(PlayerState.LOADING)
race_engine._apply_menu_active(False)  # jump armed
race_engine.resume_disc({"pos": 12.0})
check(race_engine._resume_requested)
check(race_engine._pending_resume is None)  # applied immediately
check(race_fake.time_pos == 12.0)
pump(0.3)
check(race_fake.commands == [])  # jump leaked? no.
check(race_engine._menu_jump_done)
race_engine.dispose()


# (i) ANY exception from discnav is swallowed: the non-fatal menu path can
# never escape the singleShot callback / SIGABRT the process.
class ExplodingMenuPlayer(MenuPlayer):
    def command(self, name: str, *args: object) -> None:
        self.commands.append((name, *args))
        self.command_times.append(time.monotonic())
        raise RuntimeError("discnav exploded")


explode_engine = DvdPlaybackEngine()
explode_fake = ExplodingMenuPlayer()
explode_engine._player = explode_fake  # type: ignore[assignment]
explode_engine._sm = StateMachine(PlayerState.LOADING)
explode_engine._apply_menu_active(False)
pump(0.3)  # jump callback runs and the RuntimeError is swallowed
check(explode_engine.state is PlayerState.PLAYING)
check(len(explode_fake.commands) == 1)
explode_engine._discnav("menu")  # direct call: non-fatal too
check(explode_engine.state is PlayerState.PLAYING)
explode_engine._menu_jump_done = False
explode_engine._menu_jump_armed_at = None
explode_engine._on_menu_jump_settle()  # callback path again, no abort
check(explode_engine.state is PlayerState.PLAYING)
explode_engine.dispose()

# ── g. dvdnav capability probe ─────────────────────────────────────
# Qt's QCoreApplication switches LC_NUMERIC to the user locale; libmpv
# requires "C" for LC_NUMERIC, so restore it before creating mpv.
locale.setlocale(locale.LC_NUMERIC, "C")
probe = mpv.MPV(vo="null", ao="null")
try:
    check(getattr(probe, "dvd_device", "missing") != "missing")
    try:
        probe.loadfile("dvdnav://does-not-exist-xyz")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            app.processEvents()
            if probe.idle_active or probe.eof_reached:
                break
            time.sleep(0.05)
        check(True)
    except mpv.MPVError:
        check(True)
finally:
    probe.terminate()

print(f"SMOKE OK {COUNT} assertions")
