"""Formal exam: vectors derived BLIND from the spec (no implementation read),
executed against the real dvd_player module. New-spec deltas: eject legal from
every state except EJECTED; autoplay edge = into True from any non-True value.
"""
import os
import tempfile
import math
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import dvd_player as dp

app = QApplication([])

COUNT = 0


def check(cond, msg):
    global COUNT
    if not cond:
        raise AssertionError(msg)
    COUNT += 1


S = dp.PlayerState
LEGAL = {
    S.IDLE: {S.LOADING, S.EJECTED},
    S.LOADING: {S.MENU_ACTIVE, S.PLAYING, S.EJECTED, S.ERROR},
    S.MENU_ACTIVE: {S.PLAYING, S.STOPPED, S.EJECTED, S.ERROR},
    S.PLAYING: {S.PAUSED, S.MENU_ACTIVE, S.STOPPED, S.EJECTED, S.ERROR},
    S.PAUSED: {S.PLAYING, S.MENU_ACTIVE, S.STOPPED, S.EJECTED, S.ERROR},
    S.STOPPED: {S.IDLE, S.LOADING, S.MENU_ACTIVE, S.EJECTED},
    S.EJECTED: {S.IDLE, S.LOADING},
    S.ERROR: {S.LOADING, S.EJECTED},
}

# ── 1. State machine: full legal closure, all illegal edges, no-op, reset
for origin, targets in LEGAL.items():
    for target in targets:
        sm = dp.StateMachine(origin)
        sm.transition(target)
        check(sm.state is target, f"legal {origin.name}->{target.name} rejected")
ALL = list(S)
for origin in ALL:
    for target in ALL:
        if origin is target:
            continue
        if target in LEGAL[origin]:
            continue
        sm = dp.StateMachine(origin)
        try:
            sm.transition(target)
            check(False, f"illegal {origin.name}->{target.name} accepted")
        except dp.PlaybackError:
            check(sm.state is origin, f"illegal {origin.name}->{target.name} moved state")
for origin in ALL:
    sm = dp.StateMachine(origin)
    sm.transition(origin)
    check(sm.state is origin, f"same-state {origin.name} not a no-op")
sm = dp.StateMachine(S.PLAYING)
sm.reset()
check(sm.state is S.IDLE, "reset() did not reach IDLE")

# ── 2. Engine: load gating, eject reachability, dispose, properties total
for origin in (S.IDLE, S.STOPPED, S.EJECTED, S.ERROR):
    eng = dp.DvdPlaybackEngine()
    eng._sm = dp.StateMachine(origin)
    # Unattached: LOADING is entered, then _ensure_player fails -> ERROR +
    # raise (documented). With a player, a bad source fails -> ERROR, no raise.
    try:
        eng.load("/dev/nonexistent0")
        check(eng.state in (S.LOADING, S.ERROR), f"load from {origin.name} weird")
    except dp.PlaybackError:
        check(eng.state is S.ERROR, f"load from {origin.name} did not reach ERROR")
for origin in (S.LOADING, S.MENU_ACTIVE, S.PLAYING, S.PAUSED):
    eng = dp.DvdPlaybackEngine()
    eng._sm = dp.StateMachine(origin)
    try:
        eng.load("/dev/nonexistent0")
        check(False, f"load from {origin.name} accepted")
    except dp.PlaybackError:
        check(eng.state is origin, f"load from {origin.name} moved state")

for origin in ALL:
    if origin is S.EJECTED:
        continue  # same-state no-op
    eng = dp.DvdPlaybackEngine()
    eng._sm = dp.StateMachine(origin)
    eng.eject()
    check(eng.state is S.EJECTED, f"eject from {origin.name} did not reach EJECTED")
    check(eng.player is None, f"eject from {origin.name} left a player")
    eng.dispose()
    eng.dispose()
    check(eng.state is S.EJECTED, "double dispose changed state")
    check(eng.time_pos is None and eng.duration is None and eng.chapter is None,
          "properties not total after dispose")
    check(eng.chapter_list == [] and eng.volume is None and eng.disc_title is None,
          "properties not total after dispose (2)")

# ── 3. Watchdog + eof-during-load
eng = dp.DvdPlaybackEngine()
eng._sm = dp.StateMachine(S.LOADING)
errors = []
eng.error_occurred.connect(errors.append)
eng._on_load_timeout()
check(eng.state is S.ERROR, "watchdog did not move LOADING->ERROR")
check(len(errors) == 1 and "playback did not start" in errors[0], "watchdog error text")
eng = dp.DvdPlaybackEngine()
eng._sm = dp.StateMachine(S.LOADING)
errors = []
eng.error_occurred.connect(errors.append)
eng._apply_eof()
check(eng.state is S.ERROR, "eof-during-load did not move LOADING->ERROR")
check(len(errors) == 1 and "end-of-file" in errors[0], "eof-during-load error text")

# ── 4. Menu-active transitions incl. STOPPED->MENU_ACTIVE
eng = dp.DvdPlaybackEngine()
for origin, active, want in (
    (S.LOADING, True, S.MENU_ACTIVE),
    (S.LOADING, False, S.PLAYING),
    (S.PLAYING, True, S.MENU_ACTIVE),
    (S.PAUSED, True, S.MENU_ACTIVE),
    (S.STOPPED, True, S.MENU_ACTIVE),
    (S.MENU_ACTIVE, False, S.PLAYING),
):
    eng._sm = dp.StateMachine(origin)
    eng._apply_menu_active(active)
    check(eng.state is want, f"menu {active} from {origin.name} -> {eng.state.name}")
    check(eng.menu_active is active, "menu flag desynced from state")

# ── 5. Resume staging applied at most once (fake player)
class FakePlayer:
    def __init__(self):
        self.disc_title = None
        self.chapter = None
        self.time_pos = None
        self.speed = 1.0
        self.pause = False

eng = dp.DvdPlaybackEngine()
eng._player = FakePlayer()
eng._sm = dp.StateMachine(S.LOADING)
eng.resume_disc({"pos": 90.0, "title": 1, "chapter": 2})
eng._apply_menu_active(False)  # LOADING -> PLAYING applies the stage
check(eng._player.disc_title == 1 and eng._player.chapter == 2
      and eng._player.time_pos == 90.0, "resume stage not applied")
eng._sm.transition(S.PAUSED)
eng._sm.transition(S.PLAYING)  # unpause must NOT re-apply
check(eng._pending_resume is None, "resume stage not cleared")

# ── 6. ResumeStore: roundtrip, prune, corrupt, str path, forget
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "resume.json"
    store = dp.ResumeStore(path)
    store.save("h1", 12.5, 3, 7)
    entry = store.get("h1")
    check(entry is not None and entry["pos"] == 12.5 and entry["title"] == 3
          and entry["chapter"] == 7, "resume roundtrip")
    for i in range(20):
        store.save(f"h{i}", 1.0, None, None)
    data = store.load()
    check(len(data) <= dp.RESUME_MAX_ENTRIES, "prune failed")
    check(store.get("h1") is None, "prune kept an old entry")
    store.forget("h0")
    check(store.get("h0") is None, "forget failed")
    path.write_text("{broken", encoding="utf-8")
    check(store.load() == {}, "corrupt file not degraded")
    check(dp.ResumeStore(str(path)).load() == {}, "str path load raised")

# ── 7. DiscIdentity: determinism + not-a-DVD-layout
with tempfile.TemporaryDirectory() as tmp:
    disc = Path(tmp) / "d"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"IFO" * 512)
    h1 = dp.DiscIdentity.compute(str(disc))
    h2 = dp.DiscIdentity.compute(str(disc))
    check(h1 == h2 and len(h1) == 64 and h1 == h1.lower(), "disc hash determinism")
    broken = Path(tmp) / "b"
    (broken / "VIDEO_TS").mkdir(parents=True)
    try:
        dp.DiscIdentity.compute(str(broken))
        check(False, "no-IFO dir hashed")
    except dp.PlaybackError as exc:
        check("not a DVD layout" in str(exc), "layout error text")
    try:
        dp.DiscIdentity.compute("/nonexistent/xyz")
        check(False, "missing source hashed")
    except dp.PlaybackError:
        pass

# ── 8. OSD: fmt, button order/containment, hit-test, status text
osd = dp.Xbox360Osd()
for value, want in ((None, "00:00"), (float("nan"), "00:00"), (float("inf"), "00:00"),
                    (-5.0, "00:00"), (0.0, "00:00"), (65.0, "01:05"),
                    (3599.0, "59:59"), (3600.0, "1:00:00"), (5400.0, "1:30:00")):
    check(osd._fmt_time(value) == want, f"_fmt_time({value!r}) = {osd._fmt_time(value)!r}")
osd.resize(1280, 64)
rects = osd._button_rects()
check(len(rects) == 10, "not 10 buttons")
names = [n for n, _ in rects]
check(names == [n for n, _ in dp.Xbox360Osd.BUTTONS], f"button order {names}")
for i, (_, r) in enumerate(rects):
    check(r.left() >= 0.0 and r.right() <= 1280 + 1e-9, f"rect {i} outside widget")
    if i:
        check(r.left() >= rects[i - 1][1].right() - 1e-9, f"rects {i-1}/{i} overlap")
osd.show()
osd.resize(1280, 64)
clicks = []
osd.button_clicked.connect(clicks.append)
QTest.mouseClick(osd, Qt.MouseButton.LeftButton, pos=QPoint(int(rects[0][1].center().x()),
                                                            int(rects[0][1].center().y())))
check(clicks == ["prev"], f"first-button click -> {clicks}")
for state, want in ((S.MENU_ACTIVE, "MENU"), (S.PLAYING, "PLAYING"),
                    (S.PAUSED, "PAUSED"), (S.STOPPED, "STOPPED")):
    osd.set_transport(state=state)
    check(osd._status_text() == want, f"status text {state.name}")
osd.set_transport(state=S.PLAYING, speed=4.0)
check(osd._status_text() == "FF 4x", "FF status text")
osd.set_transport(state=S.PLAYING, speed=-2.0)
check(osd._status_text() == "RW 2x", "RW status text")

# ── 9. Speed ladder (via MainWindow._cycle_speed)
engine = dp.DvdPlaybackEngine()
window = dp.MainWindow(engine)
window._auto_detected = True
window._attach_engine = lambda: None
seen = []
engine.set_speed = lambda mult: seen.append(float(mult))
FF = {1.0: 2.0, 2.0: 4.0, 4.0: 8.0, 8.0: 16.0, 16.0: 1.0}
RW = {1.0: 16.0, 2.0: 1.0, 4.0: 2.0, 8.0: 4.0, 16.0: 8.0}
for current, want in FF.items():
    engine._speed = current
    seen.clear()
    window._cycle_speed(1)
    check(seen == [want], f"FF {current} -> {seen}")
for current, want in RW.items():
    engine._speed = current
    seen.clear()
    window._cycle_speed(-1)
    check(seen == [want], f"RW {current} -> {seen}")
window.close()

# ── 10. Autoplay matrix (edge into True from any non-True; state gate)
# NEW CONTRACT (home-menu boot): the insertion edge summons the home menu
# (_open_home), it NEVER loads a source. Removal/state gates unchanged.
window = dp.MainWindow(engine)
window._auto_detected = True
window._attach_engine = lambda: None
loads = []
home_summoned = []
window._load_source = lambda src, quiet=False: loads.append((src, quiet))
window._open_home = lambda: home_summoned.append(True)
orig_ldd, orig_dp = dp.list_dvd_devices, dp.disc_present
dp.list_dvd_devices = lambda: ["/dev/sr0"]
# (previous, present, loaded_source, want_home)
matrix = (
    (False, True, None, True),           # disc inserted -> summon home
    (None, True, None, True),            # tray closed after eject -> summon
    (True, True, None, False),           # steady present -> no summon
    (True, True, "/dev/sr0", False),     # loaded disc blip -> no summon
    (True, None, "/dev/sr0", False),     # blip poll 1: no removal yet
    (False, None, "/dev/sr0", False),
    (True, False, "/dev/sr0", False),
)
for previous, present, loaded, want_home in matrix:
    home_summoned.clear()
    window.status.setText("")
    window._loaded_source = loaded
    dp.disc_present = lambda dev, p=present: p
    window._disc_presence = {"/dev/sr0": previous}
    window._poll_disc_insertion()
    check(bool(home_summoned) is want_home,
          f"edge prev={previous} pres={present} loaded={loaded} -> home={home_summoned}")
    check(loads == [], f"edge prev={previous} pres={present} loaded a source")
# Debounced removal: 2 consecutive non-True polls for the loaded device.
window._loaded_source = "/dev/sr0"
window._disc_gone_streak = {}
window.status.setText("")
dp.disc_present = lambda dev: None
window._disc_presence = {"/dev/sr0": True}
window._poll_disc_insertion()
check(window.status.text() == "", "removal reported after 1 poll")
window._poll_disc_insertion()
check(window.status.text() == "disc removed", "removal not reported after 2 polls")
check(window._loaded_source is None, "removal kept the loaded source")
# State gate.
for blocked in (S.LOADING, S.PLAYING, S.PAUSED, S.MENU_ACTIVE):
    home_summoned.clear()
    engine._sm = dp.StateMachine(blocked)
    window._loaded_source = None
    dp.disc_present = lambda dev: True
    window._disc_presence = {"/dev/sr0": False}
    window._poll_disc_insertion()
    check(home_summoned == [], f"home summoned while {blocked.name}")
    check(loads == [], f"loaded while {blocked.name}")
engine._sm = dp.StateMachine(S.IDLE)
dp.disc_present = lambda dev: (_ for _ in ()).throw(OSError("boom"))
window._poll_disc_insertion()
check(window._disc_presence.get("/dev/sr0") is None, "raising probe not degraded")
window.close()
dp.list_dvd_devices, dp.disc_present = orig_ldd, orig_dp

# ── 11. Ctrl+F fullscreen toggle (offscreen QPA cannot showFullScreen;
#        assert the toggle logic via injected window-state handlers)
window = dp.MainWindow(engine)
window._auto_detected = True
window._attach_engine = lambda: None
calls = []
full = [False]
window.showNormal = lambda: (full.__setitem__(0, False), calls.append("normal"))
window.showFullScreen = lambda: (full.__setitem__(0, True), calls.append("full"))
window.isFullScreen = lambda: full[0]
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtCore import QEvent
for want in ("full", "normal", "full"):
    calls.clear()
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F.value,
                   Qt.KeyboardModifier.ControlModifier)
    window.keyPressEvent(ev)
    check(calls == [want], f"Ctrl+F -> {calls}")
window.close()

print(f"FORMAL EXAM OK {COUNT} assertions")
