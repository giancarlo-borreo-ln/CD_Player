"""Black-box contract test: load() resets carryover transport non-fatally.

Public API only (README-level). Fake mpv player records every transport write.
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)

import dvd_player as d

N = 0
FAILS = []
BOX = []


def check(cond, msg):
    global N
    N += 1
    if not cond:
        FAILS.append(msg)


class _Any:
    """Permissive mpv member: callable, decorator, and truthy."""

    def __call__(self, *a, **kw):
        return lambda f: f

    def __bool__(self):
        return True


class RecorderPlayer:
    """Fake python-mpv player. Records sets/calls; optional aid setter failure."""

    def __init__(self, **kw):
        object.__setattr__(self, "_store", {"aid": "auto", "sid": "auto",
                                           "speed": 1.0, "pause": False})
        object.__setattr__(self, "obs", {})
        object.__setattr__(self, "sets", [])
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "bad_aid", False)

    def __getattr__(self, name):
        if name == "property_observer":
            def po(prop):
                def deco(fn):
                    self.obs[prop] = fn
                    return fn
                return deco
            return po
        if name == "observe_property":
            def op(prop, fn, *a):
                self.obs[prop] = fn
            return op
        if name in ("play", "stop", "command", "seek", "set_property", "quit"):
            def method(*a, **kw):
                self.calls.append((name, a, kw))
                return None
            return method
        return _Any()

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self.sets.append((name, value))
        if name == "aid" and self.bad_aid:
            raise ValueError("carryover aid rejected")
        if name in ("aid", "sid", "speed", "pause"):
            self._store[name] = value
        else:
            object.__setattr__(self, name, value)


class QMRecorder:
    calls = []

    @classmethod
    def _rec(cls, kind, *a, **k):
        cls.calls.append((kind, a, k))

    @classmethod
    def warning(cls, *a, **k):
        cls._rec("warning", *a, **k)

    @classmethod
    def information(cls, *a, **k):
        cls._rec("information", *a, **k)

    @classmethod
    def question(cls, *a, **k):
        cls._rec("question", *a, **k)
        return 0

    @classmethod
    def critical(cls, *a, **k):
        cls._rec("critical", *a, **k)


d.mpv.MPV = RecorderPlayer
d.QMessageBox = QMRecorder


def make_video_ts():
    root = tempfile.mkdtemp(prefix="bbreset_", dir="/tmp")
    vts = os.path.join(root, "VIDEO_TS")
    os.makedirs(vts)
    for name, magic in (("VIDEO_TS.IFO", b"DVDVIDEO-VMG"),
                        ("VTS_01_0.IFO", b"DVDVIDEO-VTS"),
                        ("VTS_01_1.VOB", b"\x00fake")):
        with open(os.path.join(vts, name), "wb") as fh:
            fh.write(magic)
    return root


def new_engine():
    eng = d.DvdPlaybackEngine()
    eng.attach_widget(1)
    return eng, eng.player


def aid_writes(p):
    return [v for (k, v) in p.sets if k == "aid"]


SRC = make_video_ts()
try:
    check(d.DiscIdentity.compute(SRC), "temp VIDEO_TS must yield a disc hash")

    # ---------------- a. carryover reset before play ----------------
    e, p = new_engine()
    p.aid = 7
    p.sid = 9
    p.speed = 4.0
    p.pause = True
    e._speed = 4.0
    check(p._store["aid"] == 7, "precondition: aid=7")
    check(p._store["sid"] == 9, "precondition: sid=9")
    check(e._speed == 4.0, "precondition: engine speed=4.0")
    p.sets.clear()
    p.calls.clear()
    e.load(SRC)
    check(e.state is d.PlayerState.LOADING, "a: load ends in LOADING")
    check(p._store["aid"] == "auto", "a: aid reset to auto")
    check(p._store["sid"] == "auto", "a: sid reset to auto")
    check(p._store["speed"] == 1.0, "a: player speed reset to 1.0")
    check(p._store["pause"] is False, "a: pause reset to False")
    check(e.speed == 1.0, "a: engine.speed reset to 1.0")
    check(any(c[0] == "play" and c[1] == ("dvdnav://",) for c in p.calls),
          "a: load reaches play(dvdnav://)")
    names = [k for (k, _v) in p.sets]
    play_idx = next(i for i, c in enumerate(p.calls) if c[0] == "play")
    check(names.index("aid") < names.index("dvd_device") if "dvd_device" in names else True,
          "a: reset precedes source assignment")
    check("aid" in names and "sid" in names and "speed" in names
          and "pause" in names and play_idx >= 0, "a: all four transport writes issued")

    # ---------------- b. raising aid setter is non-fatal ----------------
    QMRecorder.calls.clear()
    e2, p2 = new_engine()
    p2.bad_aid = True
    raised = None
    try:
        e2.load(SRC)
    except Exception as exc:  # noqa: BLE001 - recording unexpected escape
        raised = exc
    check(raised is None, "b: reset setter failure does not escape load")
    check(e2.state is d.PlayerState.LOADING, "b: engine stays LOADING, not ERROR")
    check(any(c[0] == "play" for c in p2.calls), "b: play step still reached")
    check(not any(k in ("warning", "critical") for (k, _a, _k) in QMRecorder.calls),
          "b: no modal error from non-fatal reset")

    # ---------------- c. staged resume applies after reset ----------------
    e3, p3 = new_engine()
    e3.resume_disc({"pos": 33.0, "title": 1, "chapter": 2})
    e3.load(SRC)
    check(e3.state is d.PlayerState.LOADING, "c: loaded before menu")
    cb = p3.obs.get("disc-menu-active")
    check(cb is not None, "c: menu-active observer registered")
    cb("disc-menu-active", True)
    check(e3.state is d.PlayerState.MENU_ACTIVE, "c: menu-active true -> MENU_ACTIVE")
    cb("disc-menu-active", False)
    check(e3.state is d.PlayerState.PLAYING, "c: menu-active false -> PLAYING")
    check(e3._pending_resume is None, "c: staged resume consumed")
    vals = dict(p3.sets)
    check(vals.get("disc_title") == 1, "c: resume title applied")
    check(vals.get("chapter") == 2, "c: resume chapter applied")
    check(vals.get("time_pos") == 33.0, "c: resume position applied")
    idx = {k: i for i, (k, _v) in enumerate(p3.sets)}
    check(idx.get("time_pos", -1) > idx.get("pause", -1),
          "c: resume writes occur after transport reset")
    check(p3._store["aid"] == "auto" and p3._store["speed"] == 1.0,
          "c: reset values persist through resume")

    # ---------------- d. double load: reset exactly once ----------------
    e4, p4 = new_engine()
    e4.load(SRC)
    first_aid = len(aid_writes(p4))
    check(first_aid == 1, "d: first load reset ran exactly once")
    second = None
    try:
        e4.load(SRC)
    except d.PlaybackError as exc:
        second = exc
    check(isinstance(second, d.PlaybackError), "d: second load raises PlaybackError")
    check(len(aid_writes(p4)) == 1, "d: failed second load does not rerun reset")
    check(e4.state is d.PlayerState.LOADING, "d: state stays LOADING after rejection")

    # ---------------- e. rapid load/dispose cycles ----------------
    loaded = disposed = reset_once = enum_ok = 0
    for _ in range(15):
        try:
            ec, pc = new_engine()
            ec.load(SRC)
            if ec.state is d.PlayerState.LOADING:
                loaded += 1
            if len(aid_writes(pc)) == 1:
                reset_once += 1
            ec.dispose()
            ec.dispose()
            if ec.player is None and ec._disposed and not ec._load_watchdog.isActive():
                disposed += 1
            if isinstance(ec.state, d.PlayerState) and ec.state is not d.PlayerState.ERROR:
                enum_ok += 1
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"e: cycle crashed: {type(exc).__name__}: {exc}")
            N += 1
    check(loaded == 15, "e: all 15 loads reached LOADING")
    check(reset_once == 15, "e: reset ran once per cycle")
    check(disposed == 15, "e: every dispose released player and stopped watchdog")
    check(enum_ok == 15, "e: every cycle ends in a valid non-ERROR state")
finally:
    shutil.rmtree(SRC, ignore_errors=True)

if FAILS:
    print("BLACKBOX-RESET FAIL")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print(f"BLACKBOX-RESET OK {N}")
