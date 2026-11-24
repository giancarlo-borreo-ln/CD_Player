"""Loki attack suite: A/V sync seek settling surface. Attack-only, no repo edits."""
import os
import random
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DVD_PLAYER_SOFTWARE_RENDER"] = "1"
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

import mpv  # noqa: E402
import dvd_player as d  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

# ---- silence every modal (constraint) -------------------------------------
for _name in ("information", "warning", "critical", "question", "about"):
    setattr(QMessageBox, _name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
QMessageBox.exec = lambda self, *a, **k: 0

app = QApplication.instance() or QApplication([])

# ---- fixtures --------------------------------------------------------------
os.makedirs("/tmp/loki_dvd/VIDEO_TS", exist_ok=True)
for f in ("VIDEO_TS.IFO", "VTS_01_0.IFO"):
    with open(f"/tmp/loki_dvd/VIDEO_TS/{f}", "wb") as fh:
        fh.write(b"DVDVIDEO-VMG" * 32)
with open("/tmp/vtest.mp4", "wb") as fh:
    fh.write(b"\x00" * 4096)

inits = []          # engine-supplied mpv kwargs per probe
writes = []         # (monotonic, value) time_pos writes across all players
engines = []        # keep alive


class H(mpv.MPV):
    """Headless mpv: null video/audio, records every property write."""

    def __init__(self, *a, **k):
        inits.append(dict(k))
        k["vo"] = "null"
        k["ao"] = "null"
        k.pop("wid", None)
        k.pop("gpu_context", None)
        super().__init__(*a, **k)

    def __setattr__(self, name, value):
        if not name.startswith("_") and name in ("time_pos", "pause"):
            writes.append((time.monotonic(), value))
        object.__setattr__(self, name, value)


d.mpv.MPV = H
PASS = 0
DEFECTS = []


def check(cond, label):
    global PASS
    if not cond:
        raise AssertionError(f"ATTACK SUCCEEDED: {label}")
    PASS += 1


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def new_engine(state=d.PlayerState.PLAYING):
    e = d.DvdPlaybackEngine()
    w = QWidget()
    engines.append((e, w))
    e.attach_widget(int(w.winId()))          # init probe: player is lazy
    e._ensure_player()
    e._player.duration = 600.0
    e._transition(d.PlayerState.LOADING)
    if state is d.PlayerState.MENU_ACTIVE:
        e._transition(d.PlayerState.MENU_ACTIVE)
    elif state is d.PlayerState.PLAYING:
        e._transition(d.PlayerState.PLAYING)
    return e


def settle_timers(e):
    return [t for t in e.findChildren(QTimer)
            if t.isSingleShot() and t.interval() == 350]


def clean(e):
    try:
        e.dispose()
    except Exception:
        pass


# ==========================================================================
# d. option sanity + init probe (first assertion group)
# ==========================================================================
e = new_engine(d.PlayerState.PLAYING)
check(e._player is not None, "init probe did not create a player")
kw = inits[-1]
check(kw.get("display_fps_override") == 25, "software branch lost display_fps_override=25")
check(kw.get("video_sync") == "audio", "software branch lost video_sync=audio")
check("autosync" not in kw, "software branch leaked autosync key")
check(kw.get("vo") is not None and kw.get("wid") is not None,
      "engine no longer requests a real video output/wid (init contract changed)")
clean(e)

# ==========================================================================
# a. seek storm: 200 seeks @5 ms, continuous debounce = one settle window
# ==========================================================================
e = new_engine()
writes.clear()
random.seed(1337)
vals = [random.uniform(0.0, 3600.0) for _ in range(200)]
try:
    for v in vals:
        e.seek_to(v)
        app.processEvents()
        time.sleep(0.005)
    storm_ok = True
except Exception:
    storm_ok = False
check(storm_ok, "seek storm raised an exception")
check(len(writes) <= 2, f"seek storm wrote {len(writes)} time_pos values (expected <=2)")
check(len(writes) >= 1 and abs(writes[0][1] - vals[0]) < 1e-6,
      "first (immediate) write is not the first requested position")
check(e._pending_seek_pos is not None and abs(e._pending_seek_pos - vals[-1]) < 1e-6,
      "pending seek is not the latest requested position")
pump(500)
check(not e._seek_settle.isActive(), "settle timer still active after storm settled")
check(e._pending_seek_pos is None, "pending seek not cleared after settle")
check(len(writes) == 2, f"expected exactly one immediate + one settled write, got {len(writes)}")
check(abs(writes[-1][1] - vals[-1]) < 1e-6,
      "final settled position != LAST requested position")
check(len(settle_timers(e)) == 1, "duplicate 350 ms settle timers exist")
clean(e)

# ==========================================================================
# b. settle races: eject() / dispose() mid-settle
# ==========================================================================
# eject mid-settle
e = new_engine()
writes.clear()
e.seek_to(11.0)
e.seek_to(22.0)               # pending latest-wins
check(e._seek_settle.isActive(), "settle timer not armed before eject race")
mark = len(writes)
try:
    e.eject()
    eject_ok = True
except Exception:
    eject_ok = False
check(eject_ok, "eject() mid-settle raised")
check(e.state is d.PlayerState.EJECTED, "eject() did not reach EJECTED")
check(not e._seek_settle.isActive(), "eject() left settle timer running")
check(e._pending_seek_pos is None, "eject() left a pending seek")
pump(500)
check(len(writes) == mark, "late time_pos write after eject()")
check(not e._seek_settle.isActive(), "settle timer restarted after eject()")
clean(e)

# dispose mid-settle
e = new_engine()
writes.clear()
e.seek_to(33.0)
e.seek_to(44.0)
check(e._seek_settle.isActive(), "settle timer not armed before dispose race")
mark = len(writes)
try:
    e.dispose()
    disp_ok = True
except Exception:
    disp_ok = False
check(disp_ok, "dispose() mid-settle raised")
check(e._disposed, "dispose() did not mark engine disposed")
check(not e._seek_settle.isActive(), "dispose() left settle timer running")
check(e._pending_seek_pos is None, "dispose() left a pending seek")
pump(500)
check(len(writes) == mark, "late time_pos write after dispose()")
before = len(writes)
try:
    e.seek_to(1234.0)
    post_dispose_ok = True
except Exception:
    post_dispose_ok = False
check(post_dispose_ok, "seek_to() after dispose() raised")
check(len(writes) == before, "seek_to() after dispose() wrote time_pos")
if e._seek_settle.isActive():
    DEFECTS.append({
        "severity": "minor",
        "attack": "seek_to() after dispose()",
        "reproduction": "e.seek_to(1); e.dispose(); e.seek_to(1234)",
        "impact": "re-arms the 350 ms settle timer on a disposed engine; settle is inert (no write/crash) but lifecycle guard is incomplete",
    })
pump(500)
check(not e._seek_settle.isActive() and len(writes) == before,
      "post-dispose settle timer did not stop / wrote late")
try:
    e.dispose()
    idem = True
except Exception:
    idem = False
check(idem, "second dispose() raised")
clean(e)

# ==========================================================================
# c. seek vs load / menu states
# ==========================================================================
e = new_engine(d.PlayerState.LOADING)
writes.clear()
e.seek_to(77.0)
e.seek_to(88.0)
check(len(writes) == 0, "seek during LOADING wrote time_pos (must be no-op)")
check(e._pending_seek_pos is None, "seek during LOADING armed a pending seek")
check(not e._seek_settle.isActive(), "seek during LOADING armed settle timer")
clean(e)

e = new_engine(d.PlayerState.MENU_ACTIVE)
writes.clear()
e.seek_to(99.0)
check(len(writes) == 0, "seek during MENU_ACTIVE wrote time_pos (must be no-op)")
check(e._pending_seek_pos is None, "seek during MENU_ACTIVE armed pending seek")
check(not e._seek_settle.isActive(), "seek during MENU_ACTIVE armed settle timer")
# "right after load completes": MENU_ACTIVE -> PLAYING is the documented transition
e._transition(d.PlayerState.PLAYING)
e.seek_to(101.0)
check(len(writes) == 1 and abs(writes[0][1] - 101.0) < 1e-6,
      "seek right after load completion did not apply")
e.seek_to(102.0)
pump(500)
check(abs(writes[-1][1] - 102.0) < 1e-6, "post-load settle did not apply latest value")
clean(e)

# ==========================================================================
# e. staged resume + immediate seek burst ordering
# ==========================================================================
e = new_engine(d.PlayerState.LOADING)
writes.clear()
e.resume_disc({"pos": 42.0})
check(e._pending_resume == {"pos": 42.0}, "resume staging during LOADING was lost")
for v in (10.0, 20.0, 30.0):
    e.seek_to(v)              # illegal in LOADING: must not disturb staging
check(e._pending_resume == {"pos": 42.0}, "seek burst clobbered staged resume")
check(len(writes) == 0 and e._pending_seek_pos is None,
      "seek burst during staging leaked a write/pending seek")
e._transition(d.PlayerState.PLAYING)
e._apply_pending_resume()
check(len(writes) == 1 and abs(writes[0][1] - 42.0) < 1e-6,
      "staged resume was not applied after load")
check(e._pending_resume is None, "staged resume not cleared after application")
pump(500)
check(len(writes) == 1, "settle timer clobbered resume with a stale seek")
clean(e)

e = new_engine()
writes.clear()
e.resume_disc({"pos": 42.0})   # applied immediately in PLAYING
for v in (10.0, 20.0, 30.0):
    e.seek_to(v)
pump(500)
check(len(writes) == 3 and abs(writes[0][1] - 42.0) < 1e-6,
      "resume was not applied before the seek burst")
check(abs(writes[-1][1] - 30.0) < 1e-6,
      "latest seek did not win after resume application")
check(e._pending_resume is None, "resume entry resurrected after settle")
clean(e)

# ==========================================================================
# f. GUI scrub + seek flood through MainWindow
# ==========================================================================
e = new_engine()
mw = d.MainWindow(e)           # source=None: no disc load
recorder = []
real_seek = e.seek_to
e.seek_to = lambda s: (recorder.append(float(s)), real_seek(s))
writes.clear()
random.seed(424242)
try:
    for i in range(120):
        frac = random.random()
        mw._on_scrub_pressed(frac)
        mw._on_scrub_moved(min(1.0, frac + 0.01))
        mw._on_scrub_moved(min(1.0, frac + 0.02))
        if i % 3 == 0:
            e.seek_to(random.uniform(0.0, 600.0))
        if i % 7 == 0:
            mw._on_scrub_released()
        app.processEvents()
        time.sleep(0.002)
    e.seek_to(555.0)
    gui_ok = True
except Exception:
    gui_ok = False
check(gui_ok, "GUI seek/scrub flood raised an exception")
check(len(recorder) >= 120, f"scrub flood produced only {len(recorder)} recorded seeks")
check(len(settle_timers(e)) == 1, "GUI flood created overlapping settle timers")
active = [t for t in e.findChildren(QTimer) if t.isActive() and t.isSingleShot()]
check(len(active) <= 1, "multiple single-shot timers active during GUI flood")
pump(600)
check(not e._seek_settle.isActive(), "settle timer still running after GUI flood")
check(abs(writes[-1][1] - 555.0) < 1e-6,
      "GUI flood final settled position != last requested position")
check(len(writes) <= 1 + (len(recorder) // 1) and len(writes) <= 3,
      f"GUI flood produced {len(writes)} writes (coalescing failed)")
mw.close()
clean(e)

for eng, _w in engines:
    clean(eng)

print(f"LOKI-SYNC OK {PASS}")
