"""Smoke tests for the dvd_player backend regions 1-4."""
from __future__ import annotations

import ctypes
import json
import locale
import os
import stat
import subprocess
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest import mock

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
from dvd_player import hardware
from dvd_player import state as state_mod

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

# Truthful warnings: a readable (ACL-accessible) block device must produce
# neither an 'optical' group message nor an 'sg' message, even when sg is
# absent and the user is not in the group.
block_stat = types.SimpleNamespace(st_mode=stat.S_IFBLK | 0o660)
with (
    mock.patch.object(hardware.os, "stat", return_value=block_stat),
    mock.patch.object(hardware.os, "open", return_value=99),
    mock.patch.object(hardware.os, "close"),
    mock.patch.object(hardware, "_sg_loaded", return_value=False),
    mock.patch.object(hardware.ctypes.util, "find_library", return_value="libdvdcss.so.2"),
):
    readable = check_environment("/dev/fake-sr0")
    check(not any("optical" in p.lower() for p in readable))
    check(not any("'sg'" in p for p in readable))

# PermissionError on the probe-open: optical group message AND sg message.
with (
    mock.patch.object(hardware.os, "stat", return_value=block_stat),
    mock.patch.object(hardware.os, "open", side_effect=PermissionError),
    mock.patch.object(hardware, "_sg_loaded", return_value=False),
    mock.patch.object(hardware.ctypes.util, "find_library", return_value="libdvdcss.so.2"),
):
    denied = check_environment("/dev/fake-sr0")
    check(any("optical" in p.lower() for p in denied))
    check(any("'sg'" in p for p in denied))

# Other open failures also allow the sg hint, but a regular file does not.
with (
    mock.patch.object(hardware.os, "stat", return_value=block_stat),
    mock.patch.object(hardware.os, "open", side_effect=OSError(19, "No such device")),
    mock.patch.object(hardware, "_sg_loaded", return_value=False),
    mock.patch.object(hardware.ctypes.util, "find_library", return_value="libdvdcss.so.2"),
):
    broken = check_environment("/dev/fake-sr0")
    check(any("cannot open" in p for p in broken))
    check(any("'sg'" in p for p in broken))

with tempfile.TemporaryDirectory() as tmp:
    plain = Path(tmp) / "movie.iso"
    plain.write_bytes(b"x")
    with mock.patch.object(hardware, "_sg_loaded", return_value=False):
        file_problems = check_environment(str(plain))
    check(not any("'sg'" in p for p in file_problems))

# libdvdcss check is unchanged: still reported when missing.
with mock.patch.object(hardware.ctypes.util, "find_library", return_value=None):
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "movie.iso"
        plain.write_bytes(b"x")
        no_dvdcss = check_environment(str(plain))
    check(any("libdvdcss" in p for p in no_dvdcss))

# ── e. device helpers ──────────────────────────────────────────────
check(disc_present("/dev/nonexistent0") is None)
expect_playback_error(eject_device, "/dev/nonexistent0")

# ── e2. platform dispatch (C1/C2/C3), faked — no real hardware ─────
# Linux parity golden: alias nodes collapse to their real /dev/sr* target.
linux_nodes = {
    "/dev/cdrom": "/dev/sr0",
    "/dev/dvd": "/dev/sr1",
    "/dev/sr0": "/dev/sr0",
    "/dev/sr1": "/dev/sr1",
}
with (
    mock.patch.object(hardware.sys, "platform", "linux"),
    mock.patch.object(hardware.os, "listdir", return_value=["sr0", "sr1"]),
    mock.patch.object(hardware.os.path, "exists", side_effect=lambda p: p in linux_nodes),
    mock.patch.object(hardware.os.path, "realpath", side_effect=lambda p: linux_nodes[p]),
):
    check(hardware.list_dvd_devices() == ["/dev/cdrom", "/dev/dvd"])

for cd_status, want in (
    (hardware.CDS_DISC_OK, True),
    (hardware.CDS_NO_DISC, False),
    (hardware.CDS_TRAY_OPEN, None),
    (hardware.CDS_DRIVE_NOT_READY, None),
):
    with (
        mock.patch.object(hardware.sys, "platform", "linux"),
        mock.patch.object(hardware.os, "open", return_value=9),
        mock.patch.object(hardware.os, "close"),
        mock.patch.object(hardware.fcntl, "ioctl", return_value=cd_status),
    ):
        check(hardware.disc_present("/dev/sr0") == want)

# Eject fallback chain: ioctl succeeds -> no subprocess.
with (
    mock.patch.object(hardware.sys, "platform", "linux"),
    mock.patch.object(hardware.os, "open", return_value=9),
    mock.patch.object(hardware.os, "close"),
    mock.patch.object(hardware.fcntl, "ioctl"),
    mock.patch.object(hardware.subprocess, "run") as run,
):
    hardware.eject_device("/dev/sr0")
    check(not run.called)
# ioctl fails -> the `eject` binary, list form, exactly one argv element.
with (
    mock.patch.object(hardware.sys, "platform", "linux"),
    mock.patch.object(hardware.os, "open", return_value=9),
    mock.patch.object(hardware.os, "close"),
    mock.patch.object(hardware.fcntl, "ioctl", side_effect=OSError(5, "io")),
    mock.patch.object(hardware.shutil, "which", return_value="/usr/bin/eject"),
    mock.patch.object(
        hardware.subprocess, "run",
        return_value=types.SimpleNamespace(returncode=0),
    ) as run,
):
    hardware.eject_device("/dev/sr0")
    check(run.call_args.args[0] == ["/usr/bin/eject", "/dev/sr0"])
# Both fail -> PlaybackError, never a leaked OSError.
with (
    mock.patch.object(hardware.sys, "platform", "linux"),
    mock.patch.object(hardware.os, "open", side_effect=OSError(2, "missing")),
    mock.patch.object(hardware.shutil, "which", return_value=None),
):
    expect_playback_error(hardware.eject_device, "/dev/sr0")

# macOS discovery: only whole disks reported by drutil status.
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(
        hardware.os, "listdir",
        return_value=["disk2", "disk3", "disk0s1", "rdisk2"],
    ),
    mock.patch.object(hardware.os.path, "exists", return_value=True),
    mock.patch.object(hardware.os.path, "realpath", side_effect=lambda p: p),
    mock.patch.object(hardware.shutil, "which", return_value="/usr/bin/drutil"),
    mock.patch.object(
        hardware, "_run_command",
        return_value=types.SimpleNamespace(
            returncode=0, stdout="Name: /dev/disk2\nType: DVD-R\n"
        ),
    ),
):
    check(hardware.list_dvd_devices() == ["/dev/disk2"])
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.os, "listdir", return_value=["disk2"]),
    mock.patch.object(hardware.shutil, "which", return_value=None),
):
    check(hardware.list_dvd_devices() == [])  # drutil absent -> degrade, no raise
# A timed-out drutil probe is swallowed by _run_command -> [] (never raises).
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.os, "listdir", return_value=["disk2"]),
    mock.patch.object(hardware.shutil, "which", return_value="/usr/bin/drutil"),
    mock.patch.object(
        hardware,
        "_subprocess_run",
        side_effect=subprocess.TimeoutExpired(["drutil", "status"], 1),
    ),
):
    check(hardware.list_dvd_devices() == [])

# macOS presence: mount-table hit/miss/unknown.
mounts = types.SimpleNamespace(
    returncode=0, stdout="/dev/disk2 on /Volumes/DVD (cd9660, local)\n"
)
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.os.path, "exists", return_value=True),
    mock.patch.object(hardware.os.path, "realpath", side_effect=lambda p: p),
    mock.patch.object(hardware, "_run_command", return_value=mounts),
):
    check(hardware.disc_present("/dev/disk2") is True)
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.os.path, "exists", return_value=True),
    mock.patch.object(hardware.os.path, "realpath", side_effect=lambda p: p),
    mock.patch.object(
        hardware, "_run_command",
        return_value=types.SimpleNamespace(returncode=0, stdout="/dev/disk3 on /Volumes/X\n"),
    ),
):
    check(hardware.disc_present("/dev/disk2") is False)
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.os.path, "exists", return_value=True),
    mock.patch.object(hardware, "_run_command", return_value=None),
):
    check(hardware.disc_present("/dev/disk2") is None)
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.os.path, "exists", return_value=False),
):
    check(hardware.disc_present("/dev/disk2") is None)

# macOS eject: drutil tray eject; failure becomes PlaybackError.
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.shutil, "which", return_value="/usr/bin/drutil"),
    mock.patch.object(
        hardware, "_run_command",
        return_value=types.SimpleNamespace(returncode=0),
    ) as run,
):
    hardware.eject_device("/dev/disk2")
    check(run.call_args.args[0] == ["/usr/bin/drutil", "tray", "eject"])
with (
    mock.patch.object(hardware.sys, "platform", "darwin"),
    mock.patch.object(hardware.shutil, "which", return_value="/usr/bin/drutil"),
    mock.patch.object(
        hardware, "_run_command",
        return_value=types.SimpleNamespace(returncode=1),
    ),
):
    expect_playback_error(hardware.eject_device, "/dev/disk2")

# Windows discovery: GetLogicalDrives bits -> "D:\\" letters.
class _FakeKernel:
    def __init__(self, mask=0, create=123, ioctl=1):
        self.GetLogicalDrives = mock.Mock(return_value=mask)
        self.CreateFileW = mock.Mock(return_value=create)
        self.DeviceIoControl = mock.Mock(return_value=ioctl)
        self.CloseHandle = mock.Mock(return_value=1)


with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware, "_windows_kernel32", return_value=_FakeKernel(0b11100)),
):
    check(hardware.list_dvd_devices() == ["C:\\", "D:\\", "E:\\"])
with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware, "_windows_kernel32", side_effect=OSError("no kernel32")),
):
    check(hardware.list_dvd_devices() == [])

# Windows presence: VIDEO_TS folder on the drive root; unknown on error.
with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware.os.path, "exists", return_value=True),
    mock.patch.object(
        hardware.os.path, "isdir",
        side_effect=lambda p: p == "D:\\VIDEO_TS",
    ),
):
    check(hardware.disc_present("D:\\") is True)
    check(hardware.disc_present("E:\\") is False)
with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware.os.path, "exists", return_value=False),
):
    check(hardware.disc_present("D:\\") is None)
with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware.os.path, "exists", side_effect=OSError("denied")),
):
    check(hardware.disc_present("D:\\") is None)

# Windows eject: DeviceIoControl on \\.\D:, clean PlaybackError on failure.
kernel = _FakeKernel()
with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware, "_windows_kernel32", return_value=kernel),
):
    hardware.eject_device("D:\\")
    check(kernel.CreateFileW.call_args.args[0] == "\\\\.\\D:")
    check(
        kernel.DeviceIoControl.call_args.args[1]
        == hardware.IOCTL_STORAGE_EJECT_MEDIA
    )
    check(kernel.CloseHandle.called)
kernel = _FakeKernel(ioctl=0)
with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware, "_windows_kernel32", return_value=kernel),
):
    expect_playback_error(hardware.eject_device, "D:\\")
    check(kernel.CloseHandle.called)
kernel = _FakeKernel(create=-1)
with (
    mock.patch.object(hardware.sys, "platform", "win32"),
    mock.patch.object(hardware, "_windows_kernel32", return_value=kernel),
):
    expect_playback_error(hardware.eject_device, "D:\\")
    check(not kernel.CloseHandle.called)

# C3: CONFIG_DIR per OS, RESUME_FILE stays derived (Linux golden values).
with (
    mock.patch.object(state_mod.Path, "home", return_value=Path("/home/alice")),
    mock.patch.dict(os.environ, {}, clear=True),
):
    check(state_mod._config_dir("linux") == Path("/home/alice/.config/dvd_player"))
    check(
        state_mod._config_dir("darwin")
        == Path("/home/alice/Library/Application Support/dvd_player")
    )
    check(
        state_mod._config_dir("win32")
        == Path("/home/alice/AppData/Roaming/dvd_player")
    )
with (
    mock.patch.object(state_mod.Path, "home", return_value=Path("/home/alice")),
    mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg"}, clear=True),
):
    check(state_mod._config_dir("linux") == Path("/xdg/dvd_player"))
with mock.patch.dict(
    os.environ,
    {"APPDATA": "C:\\Users\\alice\\AppData\\Roaming"},
    clear=True,
):
    check(
        state_mod._config_dir("win32")
        == Path("C:\\Users\\alice\\AppData\\Roaming") / "dvd_player"
    )
check(state_mod.RESUME_FILE == state_mod.CONFIG_DIR / "resume.json")

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


# ── f8. render API wrapper + engine attach mode (R1-R5/R8/R9) ──────
import dvd_player.engine as engine_mod
import dvd_player.render as render_mod

# (a) wrapper param structs: OPENGL api, GL proc loader, FBO 0, flip-y.
raw = {
    name: getattr(render_mod.mpv, name)
    for name in (
        "_mpv_render_context_create",
        "_mpv_render_context_update",
        "_mpv_render_context_render",
        "_mpv_render_context_set_parameter",
        "_mpv_render_context_free",
    )
}
created: list[tuple[object, object]] = []
rendered: list[object] = []
updated: list[int] = []
params_set: list[object] = []
freed_ctx: list[object] = []


def fake_create(buf, handle, params):
    created.append((handle, params))
    return 0


def fake_update(_handle):
    updated.append(1)
    return 1


def fake_render(_handle, params):
    rendered.append(params)
    return 0


def fake_set_param(_handle, param):
    params_set.append(param)
    return 0


def fake_free(handle):
    freed_ctx.append(handle)
    return 0


class ProcHandle:
    handle = "mpv-handle"


render_mod.mpv._mpv_render_context_create = fake_create
render_mod.mpv._mpv_render_context_update = fake_update
render_mod.mpv._mpv_render_context_render = fake_render
render_mod.mpv._mpv_render_context_set_parameter = fake_set_param
render_mod.mpv._mpv_render_context_free = fake_free
try:
    proc_calls: list[str] = []

    def gl_proc(name: str) -> int:
        proc_calls.append(name)
        return 0xABCD

    ctx = render_mod.RenderContext(ProcHandle(), gl_proc)
    check(len(created) == 1)
    check(created[0][0] == "mpv-handle")
    params = created[0][1]
    found: dict[int, object] = {}
    index = 0
    while params[index].type_id != 0:
        found[params[index].type_id] = params[index]
        index += 1
    check(ctypes.cast(found[1].data, ctypes.c_char_p).value == b"opengl")
    init = ctypes.cast(
        found[2].data, ctypes.POINTER(render_mod.mpv.MpvOpenGLInitParams)
    ).contents
    check(init.get_proc_address(None, b"glGenFramebuffers") == 0xABCD)
    check(proc_calls == ["glGenFramebuffers"])

    # Qt's getProcAddress returns sip.voidptr, not int. An uncoerced pointer
    # object cannot be converted to the callback's c_void_p restype: libmpv
    # then calls a bogus glGetString and dies in C. The trampoline must
    # return a plain int (or NULL for an unusable loader result).
    class SipLike:
        def __init__(self, value: int) -> None:
            self._value = value

        def __int__(self) -> int:
            return self._value

    # Same trampoline (init.get_proc_address) with a sip-like loader result
    # and with an unusable one.
    ctx._get_proc_address = lambda _name: SipLike(0x1234)
    check(init.get_proc_address(None, b"glGetString") == 0x1234)
    ctx._get_proc_address = lambda _name: object()
    check(init.get_proc_address(None, b"glGetString") is None)

    # Loki: a loader or __int__ that raises anything must yield NULL, never
    # unwind into ctypes (which would return garbage). Non-canonical values
    # (negative, huge, float, bool-as-float) must not reach libmpv.
    class Raising:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        def __int__(self) -> int:
            raise self._exc

    hostile: list[object] = [
        Raising(OverflowError("too big")),
        Raising(ArithmeticError("weird")),
        Raising(RuntimeError("hostile")),
        -1,
        -(1 << 63),
        1 << 64,
        float("inf"),
        float("nan"),
        4096.0,
    ]
    for bad in hostile:
        ctx._get_proc_address = lambda _name, v=bad: v
        check(init.get_proc_address(None, b"glGetString") is None)
    for exc in (OverflowError("x"), ArithmeticError("x"), RuntimeError("x")):
        ctx._get_proc_address = lambda _name, e=exc: (_ for _ in ()).throw(e)
        check(init.get_proc_address(None, b"glGetString") is None)
    ctx._get_proc_address = lambda _name: True
    check(init.get_proc_address(None, b"glGetString") == 1)
    ctx._get_proc_address = gl_proc
    fbo0 = ctypes.cast(found[3].data, ctypes.POINTER(render_mod.mpv.MpvOpenGLFBO)).contents
    check(fbo0.fbo == 0)
    flip = ctypes.cast(found[4].data, ctypes.POINTER(ctypes.c_int)).contents
    check(flip.value == 1)
    check(ctx.update() is True and len(updated) == 1)
    ctx.render(640, 360)
    ctx.render(800, 600)  # R3: resize keeps flip-y and FBO 0
    check(len(rendered) == 2)
    for array, (want_w, want_h) in zip(rendered, ((640, 360), (800, 600))):
        entries: dict[int, object] = {}
        i = 0
        while array[i].type_id != 0:
            entries[array[i].type_id] = array[i]
            i += 1
        frame_fbo = ctypes.cast(
            entries[3].data, ctypes.POINTER(render_mod.mpv.MpvOpenGLFBO)
        ).contents
        check(frame_fbo.fbo == 0)
        check((frame_fbo.w, frame_fbo.h) == (want_w, want_h))
        frame_flip = ctypes.cast(entries[4].data, ctypes.POINTER(ctypes.c_int)).contents
        check(frame_flip.value == 1)
    ctx.set_parameter("flip_y", True)
    check(len(params_set) == 1 and params_set[0].type_id == 4)
    ctx.free()
    ctx.free()  # idempotent
    check(len(freed_ctx) == 1)
    check(ctx.freed is True)
    check(ctx.update() is False and len(updated) == 1)
    ctx.render(10, 10)
    ctx.set_parameter("flip_y", True)
    check(len(rendered) == 2 and len(params_set) == 1)  # no calls after free
finally:
    for name, func in raw.items():
        setattr(render_mod.mpv, name, func)


# (b) engine attach_render with a fake render context: exactly one
# update + render per driver frame, on the GUI thread, never after free.
class FakeRenderPlayer:
    def __init__(self, **kwargs: object) -> None:
        self.options = dict(kwargs)
        self.handle = "fake-mpv"
        self.terminated = 0
        self.played = 0
        self.dvd_device: str | None = None
        self.aid: object = "auto"
        self.sid: object = "auto"
        self.speed = 1.0
        self.pause = False

    def property_observer(self, _name: str):
        return lambda fn: fn

    def play(self, _target: str) -> None:
        self.played += 1

    def terminate(self) -> None:
        self.terminated += 1


class FakeRenderContext:
    fail = False
    instances: list[FakeRenderContext] = []

    def __init__(self, player: object, get_proc_address: object) -> None:
        if FakeRenderContext.fail:
            raise RuntimeError("no current GL context")
        self.player = player
        self.get_proc_address = get_proc_address
        self.updates = 0
        self.renders: list[tuple[int, int]] = []
        self.freed = 0
        self.threads: list[int] = []
        FakeRenderContext.instances.append(self)

    def update(self) -> bool:
        self.updates += 1
        self.threads.append(threading.get_ident())
        return True

    def render(self, width: int, height: int) -> None:
        self.renders.append((width, height))
        self.threads.append(threading.get_ident())

    def free(self) -> None:
        self.freed += 1


players: list[FakeRenderPlayer] = []


def fake_render_mpv(**kwargs: object) -> FakeRenderPlayer:
    player = FakeRenderPlayer(**kwargs)
    players.append(player)
    return player


engine_mod.mpv.MPV = fake_render_mpv
engine_mod.RenderContext = FakeRenderContext
try:
    FakeRenderContext.fail = False
    FakeRenderContext.instances.clear()
    players.clear()
    render_engine = DvdPlaybackEngine()
    holder: list[int] = []

    def get_proc(_name: str) -> int:
        holder.append(1)
        return 0x1234

    check(render_engine.attach_render(get_proc) is True)
    check(len(FakeRenderContext.instances) == 1)
    check("wid" not in players[0].options)
    check(players[0].options["vo"] == "libmpv")
    fake_ctx = FakeRenderContext.instances[0]
    check(render_engine._render_ctx is fake_ctx)
    sizes = ((320, 240), (640, 480), (800, 600))
    for width, height in sizes:
        check(render_engine.update() is True)
        check(render_engine.render_frame(width, height) is True)
    check(fake_ctx.updates == 3)
    check(fake_ctx.renders == list(sizes))
    check(all(tid == threading.get_ident() for tid in fake_ctx.threads))
    off_results: list[bool] = []

    def drive_off_thread() -> None:
        off_results.append(render_engine.update())
        off_results.append(render_engine.render_frame(1, 1))

    worker = threading.Thread(target=drive_off_thread)
    worker.start()
    worker.join(timeout=5.0)
    check(off_results == [False, False])
    check(fake_ctx.updates == 3 and len(fake_ctx.renders) == 3)
    render_engine.dispose()
    check(fake_ctx.freed == 1 and players[0].terminated == 1)
    render_engine.dispose()
    check(fake_ctx.freed == 1)
    check(render_engine._render_ctx is None)
    check(render_engine.update() is False)
    check(render_engine.render_frame(5, 5) is False)
    check(fake_ctx.updates == 3 and len(fake_ctx.renders) == 3)

    # (c) R5: create failure is non-fatal -> wid fallback (recorder).
    FakeRenderContext.fail = True
    FakeRenderContext.instances.clear()
    players.clear()
    fallback_engine = DvdPlaybackEngine()
    fallback_errors: list[str] = []
    fallback_engine.error_occurred.connect(fallback_errors.append)
    check(fallback_engine.attach_render(get_proc, wid=77) is False)
    check(fallback_engine._render_ctx is None)
    check(len(players) == 2)  # failed libmpv core + wid-attached one
    check(players[0].terminated == 1)
    check(players[1].options.get("wid") == "77")
    check("vo" in players[1].options)
    check(fallback_engine._wid == 77)
    check(fallback_errors == [])
    fallback_engine.load("/dev/nonexistent0")
    check(fallback_engine.state is PlayerState.LOADING)
    fallback_engine._hash_worker.join(timeout=30.0)
    fallback_engine.dispose()
finally:
    engine_mod.RenderContext = render_mod.RenderContext
    engine_mod.mpv.MPV = original_mpv  # type: ignore[assignment]

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
        self.played = 0
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
        self.played += 1

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

# ── i. preview seam: load() from IDLE lands in the menu, and a resume
# staged while MENU_ACTIVE applies exactly once on the next PLAYING ──
with tempfile.TemporaryDirectory() as preview_tmp:
    preview_disc = Path(preview_tmp) / "preview"
    (preview_disc / "VIDEO_TS").mkdir(parents=True)
    (preview_disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"PREVIEW" * 40)

    # (a) background preview load: IDLE -> LOADING -> PLAYING -> jump.
    preview_engine = DvdPlaybackEngine()
    preview_engine._resume = ResumeStore(Path(preview_tmp) / "preview_resume.json")
    preview_fake = MenuPlayer()
    preview_engine._player = preview_fake  # type: ignore[assignment]
    seen_states: list[PlayerState] = []
    preview_engine.state_changed.connect(seen_states.append)
    check(preview_engine.state is PlayerState.IDLE)
    preview_engine.load(str(preview_disc))
    check(preview_engine.state is PlayerState.LOADING)
    check(preview_fake.played == 1 and preview_fake.dvd_device == str(preview_disc))
    wait_hash(preview_engine)  # preview: playback starts before hashing finishes
    preview_engine._apply_menu_active(False)  # first-play content started
    check(preview_engine.state is PlayerState.PLAYING)
    pump(0.3)
    check(preview_fake.commands == [("discnav", "menu")])  # landed in menu
    check(preview_engine._menu_jump_done)
    check(seen_states == [PlayerState.LOADING, PlayerState.PLAYING])
    preview_engine._apply_menu_active(True)
    check(preview_engine.state is PlayerState.MENU_ACTIVE)

    # (b) ENTER-with-resume: staged from MENU_ACTIVE, no second load, applied
    # once on MENU_ACTIVE -> PLAYING and then cleared.
    preview_engine.resume_disc({"pos": 42.0, "title": 2, "chapter": 3})
    check(preview_engine.state is PlayerState.MENU_ACTIVE)
    check(preview_fake.time_pos is None)  # not applied inside the menu
    check(preview_engine._pending_resume is not None)
    check(preview_fake.played == 1)  # staging never loads again
    preview_engine._apply_menu_active(False)  # menu selection -> PLAYING
    check(preview_engine.state is PlayerState.PLAYING)
    check(preview_fake.disc_title == 2)
    check(preview_fake.chapter == 3)
    check(preview_fake.time_pos == 42.0)
    check(preview_engine._pending_resume is None)
    # Cleared: a later PLAYING update never re-applies the staging.
    preview_fake.disc_title = None
    preview_fake.chapter = None
    preview_fake.time_pos = None
    preview_engine._apply_menu_active(False)
    check(preview_fake.disc_title is None and preview_fake.chapter is None)
    check(preview_fake.time_pos is None)
    check(len(preview_fake.commands) == 1)  # no extra menu jump
    preview_engine.dispose()

    # (c) preview teardown: stop from the menu, eject terminates the player.
    teardown_engine = DvdPlaybackEngine()
    teardown_engine._resume = ResumeStore(Path(preview_tmp) / "teardown_resume.json")
    teardown_fake = MenuPlayer()
    teardown_engine._player = teardown_fake  # type: ignore[assignment]
    teardown_engine.load(str(preview_disc))
    wait_hash(teardown_engine)
    teardown_engine._apply_menu_active(True)  # preview sits in the menu
    check(teardown_engine.state is PlayerState.MENU_ACTIVE)
    teardown_engine.stop()
    check(teardown_engine.state is PlayerState.STOPPED)
    check(teardown_fake.commands[-1] == ("stop",))
    teardown_engine.eject()
    check(teardown_engine.state is PlayerState.EJECTED)
    check(teardown_fake.terminated == 1)
    check(teardown_engine._player is None)

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
