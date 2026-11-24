#!/usr/bin/env python3
"""Black-box attack suite for the async disc-hash contract."""
import os
import subprocess
import sys
import tempfile
import time
import traceback

from PyQt6.QtWidgets import QApplication, QMessageBox

import dvd_player as d
import mpv

app = QApplication.instance() or QApplication([])

N = 0
FAILS = []


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)
    return bool(cond)


def pump(seconds, cond=lambda: False):
    end = time.time() + seconds
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def make_dvd():
    root = tempfile.mkdtemp(prefix="bbdvd_")
    vt = os.path.join(root, "VIDEO_TS")
    os.makedirs(vt)
    with open(os.path.join(vt, "VIDEO_TS.IFO"), "wb") as fh:
        fh.write(b"\0" * 2048)
    return root


class H(mpv.MPV):
    def __init__(self, *a, **k):
        k.pop("wid", None)
        k["vo"] = "null"
        k["ao"] = "null"
        super().__init__(*a, **k)


d.mpv.MPV = H


class FakePlayer:
    """Records property setters / method calls made by the engine."""

    def __init__(self, pos=None):
        object.__setattr__(self, "log", [])
        for k, v in [
            ("disc_title", None),
            ("chapter", None),
            ("time_pos", pos),
            ("duration", 0.0),
            ("pause", False),
            ("eof_reached", False),
            ("idle_active", False),
            ("core_idle", False),
            ("dvd_device", None),
        ]:
            object.__setattr__(self, k, v)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def rec(*a, **k):
            self.log.append(("call", name, a, k))
            return None

        return rec

    def __setattr__(self, name, value):
        self.log.append(("set", name, value))
        object.__setattr__(self, name, value)

    def written(self):
        return {
            x[1]: x[2]
            for x in self.log
            if x[0] == "set" and x[1] in ("disc_title", "chapter", "time_pos")
        }


class RecMB:
    StandardButton = QMessageBox.StandardButton

    class Icon:
        Warning = QMessageBox.Icon.Warning
        Critical = QMessageBox.Icon.Critical
        Question = QMessageBox.Icon.Question
        Information = QMessageBox.Icon.Information

    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, *a, **k):
        cls.calls.append(("question", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
        return cls.answer

    @classmethod
    def critical(cls, *a, **k):
        cls.calls.append(("critical", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def warning(cls, *a, **k):
        cls.calls.append(("warning", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def information(cls, *a, **k):
        cls.calls.append(("information", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
        return QMessageBox.StandardButton.Ok

    def __init__(self, *a, **k):
        pass

    def setIcon(self, *a):
        pass

    def setWindowTitle(self, *a):
        pass

    def setText(self, *a):
        pass

    def setStandardButtons(self, *a):
        pass

    def setDefaultButton(self, *a):
        pass

    def exec(self):
        return QMessageBox.StandardButton.Ok


d.QMessageBox = RecMB
WINDOWS = []  # keep Qt objects alive while worker threads may signal


def make_window(fake=None, attach=True):
    e = d.DvdPlaybackEngine()
    e.attach_widget(0)
    if fake is not None:
        e._player = fake
    w = d.MainWindow(e, None)
    w._auto_detect_disc = lambda: None
    if attach:
        w._attach_engine()
    WINDOWS.append(w)
    return e, w


VIDEO = make_dvd()

# ---------------------------------------------------------------- a. async load
print("== a: async load ==")
e = d.DvdPlaybackEngine()
e.attach_widget(0)
hashes = []
errs = []
e.disc_hash_ready.connect(hashes.append)
e.error_occurred.connect(errs.append)
t0 = time.time()
raised = None
try:
    e.load(VIDEO)
except Exception as ex:  # noqa: BLE001
    raised = ex
dt = time.time() - t0
ok(raised is None, "a: load() raised %r (hash must be async)" % (raised,))
ok(dt < 1.0, "a: load() took %.2fs, expected fast return" % dt)
ok(e.state == d.PlayerState.LOADING, "a: load() state %s, expected LOADING" % e.state)
pump(10.0, lambda: bool(hashes))
ok(len(hashes) == 1, "a: disc_hash_ready count %d, expected 1" % len(hashes))
ok(bool(hashes) and len(hashes[0]) == 64, "a: hash %r is not 64 chars" % (hashes[:1],))
ok(not errs, "a: hash path emitted error_occurred %r" % (errs,))
e.dispose()

# hostile: unreadable/nonexistent DVD dir must fail hash silently, not raise from load
bad = os.path.join(tempfile.mkdtemp(), "missing")
e = d.DvdPlaybackEngine()
e.attach_widget(0)
h2, er2 = [], []
e.disc_hash_ready.connect(h2.append)
e.error_occurred.connect(er2.append)
try:
    e.load(bad)
    ok(True, "")
except Exception as ex:  # noqa: BLE001
    ok(False, "a: load(bad_dir) raised %r (hash failure must be logged only)" % (ex,))
ok(e.state in set(d.PlayerState), "a: bad dir state invalid")
pump(1.0)
ok(not h2, "a: bad dir produced a hash")
e.dispose()

# ------------------------------------------------------- b. hash-flight races
print("== b: races ==")
# b1 eject immediately
e = d.DvdPlaybackEngine()
e.attach_widget(0)
hs = []
e.disc_hash_ready.connect(hs.append)
e.load(VIDEO)
e.eject()
ok(e.state == d.PlayerState.EJECTED, "b1: state after eject %s" % e.state)
exc = None
try:
    pump(1.5)
except Exception as ex:  # noqa: BLE001
    exc = ex
ok(exc is None, "b1: late hash raised %r" % (exc,))
ok(e.state == d.PlayerState.EJECTED, "b1: late hash changed state to %s" % e.state)
ok(e._disc_hash is None or (isinstance(e._disc_hash, str) and len(e._disc_hash) == 64),
   "b1: inconsistent _disc_hash %r" % (e._disc_hash,))
e.dispose()

# b2 dispose immediately
e = d.DvdPlaybackEngine()
e.attach_widget(0)
hs = []
e.disc_hash_ready.connect(hs.append)
e.load(VIDEO)
e.dispose()
exc = None
try:
    pump(1.5)
except Exception as ex:  # noqa: BLE001
    exc = ex
ok(exc is None, "b2: late hash after dispose raised %r" % (exc,))
ok(e.state in set(d.PlayerState), "b2: invalid state %s" % e.state)
ok(e._disc_hash is None or (isinstance(e._disc_hash, str) and len(e._disc_hash) == 64),
   "b2: inconsistent _disc_hash %r" % (e._disc_hash,))

# b3 double load
e = d.DvdPlaybackEngine()
e.attach_widget(0)
hs = []
e.disc_hash_ready.connect(hs.append)
e.load(VIDEO)
ok(e.state == d.PlayerState.LOADING, "b3: first load state %s" % e.state)
raised = None
try:
    e.load(VIDEO)
except Exception as ex:  # noqa: BLE001
    raised = ex
ok(isinstance(raised, d.PlaybackError), "b3: second load did not raise PlaybackError (got %r)" % (raised,))
ok(e.state == d.PlayerState.LOADING, "b3: state after rejected load %s" % e.state)
exc = None
try:
    pump(10.0, lambda: bool(hs))
except Exception as ex:  # noqa: BLE001
    exc = ex
ok(exc is None, "b3: worker signal raised %r" % (exc,))
ok(e.state in set(d.PlayerState), "b3: invalid state %s" % e.state)
ok(bool(hs) and len(hs[0]) == 64, "b3: no valid late hash from first worker: %r" % (hs,))
ok(e._disc_hash == hs[0] if hs else False, "b3: _disc_hash inconsistent with emitted hash")
e.dispose()

# ------------------------------------------------------------- c. resume_disc
print("== c: resume ==")
e = d.DvdPlaybackEngine()
e.attach_widget(0)
f = FakePlayer()
e._player = f
e.load(VIDEO)
ok(pump(3.0, lambda: e.state == d.PlayerState.PLAYING), "c: fake player never reached PLAYING")
f.log.clear()
e.resume_disc({"pos": 33.3, "title": 4, "chapter": 5})
w = f.written()
ok(w.get("disc_title") == 4, "c: immediate disc_title %r" % (w.get("disc_title"),))
ok(w.get("chapter") == 5, "c: immediate chapter %r" % (w.get("chapter"),))
ok(w.get("time_pos") == 33.3, "c: immediate time_pos %r" % (w.get("time_pos"),))
ok(e._pending_resume is None, "c: immediate resume left pending %r" % (e._pending_resume,))
e.dispose()

e = d.DvdPlaybackEngine()
e.attach_widget(0)
f = FakePlayer()
e._player = f
e.load(VIDEO)
ok(e.state == d.PlayerState.LOADING, "c: staged load state %s" % e.state)
e.resume_disc({"pos": 44.4, "title": 6, "chapter": 7})
ok(e._pending_resume is not None, "c: staged resume not stored")
f.log.clear()
ok(pump(3.0, lambda: e.state == d.PlayerState.PLAYING), "c: staged never reached PLAYING")
w = f.written()
ok(w.get("disc_title") == 6, "c: staged disc_title %r" % (w.get("disc_title"),))
ok(w.get("chapter") == 7, "c: staged chapter %r" % (w.get("chapter"),))
ok(w.get("time_pos") == 44.4, "c: staged time_pos %r" % (w.get("time_pos"),))
ok(e._pending_resume is None, "c: staged resume not cleared after playing: %r" % (e._pending_resume,))
e.dispose()

# --------------------------------------------------------------- d. GUI sources
print("== d: GUI garbage sources ==")
e, w = make_window()
loads = []
orig_load = e.load


def record_load(src):
    loads.append(src)
    return orig_load(src)


e.load = record_load
missing = "/tmp/definitely_missing_bb_%d" % os.getpid()
RecMB.calls.clear()
w._load_source(missing)
ok(any(c[0] == "critical" for c in RecMB.calls),
   "d: missing path produced no critical dialog: %r" % (RecMB.calls,))
ok(missing not in loads, "d: engine.load called with garbage source %r" % (missing,))
ok(bool(w.status.text()), "d: missing path left empty status text")

empty_dir = tempfile.mkdtemp(prefix="bbempty_")
RecMB.calls.clear()
loads.clear()
w._load_source(empty_dir)
ok(any(c[0] == "warning" for c in RecMB.calls),
   "d: existing unusable path produced no warning: %r" % (RecMB.calls,))
ok(any("not a DVD layout" in str(c[2]) for c in RecMB.calls if c[0] == "warning"),
   "d: warning did not describe the bad layout: %r" % (RecMB.calls,))
ok(empty_dir in loads, "d: engine.load not called for existing path")
ok(e.state in set(d.PlayerState), "d: invalid state after warning path: %s" % e.state)

# --------------------------------------------------------- e. resume prompt gate
print("== e: prompt gate ==")
SAVED = {"pos": 12.0, "title": 1, "chapter": 2, "updated": 1}
d.ResumeStore.get = lambda self, h: dict(SAVED)
forget_rec = []
d.ResumeStore.forget = lambda self, h: forget_rec.append(h)


def gate_window(pos, state_required=None):
    fake = FakePlayer(pos)
    e, w = make_window(fake)
    e.resume_disc = lambda s: resume_rec.append(s)
    w._load_source(VIDEO)
    try:
        e.disc_hash_ready.disconnect(w._on_disc_hash)
    except Exception:  # noqa: BLE001
        pass
    if state_required is not None:
        pump(3.0, lambda: e.state == state_required)
        fake.time_pos = pos
    return e, w


# LOADING -> prompt
resume_rec = []
e, w = gate_window(None, None)
ok(e.state == d.PlayerState.LOADING, "e: setup state %s not LOADING" % e.state)
RecMB.calls.clear()
RecMB.answer = QMessageBox.StandardButton.Yes
resume_rec.clear()
w._on_disc_hash("a" * 64)
ok(any(c[0] == "question" for c in RecMB.calls), "e: LOADING did not prompt")
ok(resume_rec, "e: Yes did not call resume_disc")
e.dispose()

# PLAYING + time_pos 2.0 -> prompt
resume_rec = []
e, w = gate_window(2.0, d.PlayerState.PLAYING)
ok(e.state == d.PlayerState.PLAYING and e.time_pos == 2.0,
   "e: early setup state=%s time_pos=%r" % (e.state, e.time_pos))
RecMB.calls.clear()
RecMB.answer = QMessageBox.StandardButton.Yes
resume_rec.clear()
w._on_disc_hash("b" * 64)
ok(any(c[0] == "question" for c in RecMB.calls), "e: time_pos=2.0 did not prompt")
e.dispose()

# PLAYING + time_pos 120.0 -> MUST NOT prompt (late: 20 s since load)
resume_rec = []
e, w = gate_window(120.0, d.PlayerState.PLAYING)
ok(e.state == d.PlayerState.PLAYING and e.time_pos == 120.0,
   "e: late setup state=%s time_pos=%r" % (e.state, e.time_pos))
w._load_started_at = time.monotonic() - 20.0  # late per the elapsed gate
RecMB.calls.clear()
RecMB.answer = QMessageBox.StandardButton.Yes
resume_rec.clear()
w._on_disc_hash("c" * 64)
ok(not any(c[0] == "question" for c in RecMB.calls),
   "e: prompt shown at time_pos=120.0 (early-gate ignored)")
e.dispose()

# No -> forget
e, w = gate_window(None, None)
RecMB.calls.clear()
RecMB.answer = QMessageBox.StandardButton.No
resume_rec.clear()
forget_rec.clear()
w._on_disc_hash("d" * 64)
ok(any(c[0] == "question" for c in RecMB.calls), "e: No-case did not prompt")
ok("d" * 64 in forget_rec, "e: No did not forget disc hash: %r" % (forget_rec,))
ok(not resume_rec, "e: No still called resume_disc")
e.dispose()

# ------------------------------------------------ f. worker vs process exit
print("== f: worker vs exit ==")
child = "/tmp/dvd_bb_child_exit.py"
with open(child, "w") as fh:
    fh.write(
        "import sys\n"
        "import dvd_player as d, mpv\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "app=QApplication([])\n"
        "class H(mpv.MPV):\n"
        "    def __init__(self,*a,**k):\n"
        "        k.pop('wid',None); k['vo']='null'; k['ao']='null'\n"
        "        super().__init__(*a,**k)\n"
        "d.mpv.MPV=H\n"
        "e=d.DvdPlaybackEngine(); e.attach_widget(0)\n"
        "e.load(sys.argv[1]); e.dispose()\n"
        "print('CHILD-EXIT')\n"
        "sys.exit(0)\n"
    )
env = dict(os.environ)
env["QT_QPA_PLATFORM"] = "offscreen"
t0 = time.time()
try:
    cp = subprocess.run([sys.executable, child, VIDEO], cwd=os.getcwd(), env=env,
                        capture_output=True, text=True, timeout=15)
    rc, out, err, to = cp.returncode, cp.stdout, cp.stderr, False
except subprocess.TimeoutExpired as ex:  # noqa: BLE001
    rc, out, err, to = None, "", repr(ex), True
dt = time.time() - t0
ok(not to, "f: subprocess hung >15s (daemon worker blocked exit)")
ok(rc == 0, "f: child rc=%r stderr=%s" % (rc, err[-400:]))
ok("CHILD-EXIT" in out, "f: child did not reach clean exit: %r" % (out,))
ok(dt < 15.0, "f: child exit took %.2fs" % dt)

# ----------------------------------------------------- g. rapid cycles x20
print("== g: rapid cycles ==")
exc = None
states = []
try:
    for i in range(20):
        e = d.DvdPlaybackEngine()
        e.attach_widget(0)
        e.load(VIDEO)
        if i % 2:
            e.eject()
        else:
            e.dispose()
        pump(0.05)
        states.append(e.state)
except Exception as ex:  # noqa: BLE001
    exc = ex
ok(exc is None, "g: cycle raised %s: %s" % (type(exc).__name__, exc) if exc else "")
ok(len(states) == 20, "g: only %d/20 cycles completed" % len(states))
ok(all(s in set(d.PlayerState) for s in states), "g: invalid state in %r" % (states,))
ok(all(s in (d.PlayerState.EJECTED, d.PlayerState.PLAYING, d.PlayerState.LOADING,
             d.PlayerState.PAUSED, d.PlayerState.MENU_ACTIVE, d.PlayerState.STOPPED,
             d.PlayerState.IDLE) for s in states),
   "g: unexpected terminal states %r" % (states,))
ok(states.count(d.PlayerState.EJECTED) == 10,
   "g: eject cycles did not end EJECTED: %r" % (states,))

# ------------------------------------------------------------------------ report
print()
if FAILS:
    print("BLACKBOX-ASYNC FAIL %d" % N)
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("BLACKBOX-ASYNC OK %d" % N)
sys.exit(0)
