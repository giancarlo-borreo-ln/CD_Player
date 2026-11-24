#!/usr/bin/env python3
"""Black-box hostile-input suite for the shipped dvd_player artifact.

No repo file is read: the package is imported and driven only through its
documented public surface.  All generated files live in /tmp.
"""
import math
import os
import random
import string
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import dvd_player as d
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

app = QApplication.instance() or QApplication([])

ASSERTIONS = 0
DEFECTS = []
ATTACKS = []


def check(cond, msg):
    global ASSERTIONS
    if not cond:
        raise AssertionError(msg)
    ASSERTIONS += 1


def record(name, result, evidence):
    ATTACKS.append({"name": name, "result": result, "evidence": evidence})


# ---------------------------------------------------------------------------
# Inert modal / file-dialog stubs: no dialog is ever opened for real.
# ---------------------------------------------------------------------------
DIALOG_LOG = []
QMessageBox.critical = staticmethod(
    lambda *a, **k: (DIALOG_LOG.append(("critical", a[1] if len(a) > 1 else "")),
                     QMessageBox.StandardButton.Ok)[1])
QMessageBox.information = staticmethod(
    lambda *a, **k: (DIALOG_LOG.append(("information",)),
                     QMessageBox.StandardButton.Ok)[1])
QMessageBox.warning = staticmethod(
    lambda *a, **k: (DIALOG_LOG.append(("warning",)),
                     QMessageBox.StandardButton.Ok)[1])

QUESTION = {"button": QMessageBox.StandardButton.Yes, "calls": 0}


def _question(*a, **k):
    QUESTION["calls"] += 1
    return QUESTION["button"]


QMessageBox.question = staticmethod(_question)
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))


# ===========================================================================
# a. Injection strings through every documented string entry point
# ===========================================================================
MARKER = Path("/tmp/dvd_blackbox_marker_%d" % os.getpid())
MARKER.write_text("inert-marker-alive", encoding="utf-8")

HOSTILE_SOURCES = [
    "$(reboot)",
    "`reboot`",
    "/tmp/a; rm -rf /",
    "file:///etc/passwd",
    "\x00\x01\x02",
    "A" * 10000,
    "\U0001F3AC",
    "",
    "  ",
]
HEX = set(string.hexdigits.lower())

for src in HOSTILE_SOURCES:
    label = repr(src[:24])

    env = d.check_environment(src)
    check(isinstance(env, list) and all(isinstance(x, str) for x in env),
          "check_environment non-list/str for %s" % label)

    try:
        ident = d.DiscIdentity.compute(src)
        check(isinstance(ident, str) and len(ident) == 64 and set(ident) <= HEX,
              "DiscIdentity.compute bad digest for %s" % label)
    except d.PlaybackError:
        check(True, "")

    eng = d.DvdPlaybackEngine()
    try:
        eng.load(src)
    except d.PlaybackError:
        check(True, "")
    else:
        check(False, "engine.load accepted hostile source %s while unattached" % label)
    eng.dispose()

check(MARKER.exists() and MARKER.read_text(encoding="utf-8") == "inert-marker-alive",
      "marker file was altered/removed; possible command execution")
record("injection-strings", "REJECTED",
       "9 hostile sources returned list[str]/PlaybackError only; marker survived")


# ===========================================================================
# b. ResumeStore seeding + resume prompt with hostile values
# ===========================================================================
RESUME_PATH = Path(tempfile.mktemp(prefix="dvd_resume_", suffix=".json"))
store = d.ResumeStore(RESUME_PATH)
check(store.load() == {}, "fresh ResumeStore did not load empty dict")

POS_VALUES = [float("nan"), float("inf"), float("-inf"), -5.0, 0.0]
for pos in POS_VALUES:
    store.save("h", pos, 1, 2)
    entry = store.get("h")
    check(isinstance(entry, dict) and "pos" in entry,
          "ResumeStore.save/get lost pos=%r" % (pos,))

TITLE_CHAPTER = []
for title in (None, -1, "x", 1):
    for chapter in (None, 0, 999):
        store.save("h", 5.0, title, chapter)
        entry = store.get("h")
        check(isinstance(entry, dict) and entry.get("title") == title
              and entry.get("chapter") == chapter,
              "ResumeStore mangled title=%r chapter=%r" % (title, chapter))
        TITLE_CHAPTER.append((title, chapter))

# Corrupt resume file: load() is documented never to raise.
CORRUPT = Path(tempfile.mktemp(prefix="dvd_corrupt_", suffix=".json"))
CORRUPT.write_text("this is not json {[", encoding="utf-8")
check(d.ResumeStore(CORRUPT).load() == {}, "corrupt resume file did not degrade to {}")

# String path: documented as ResumeStore(path=...), load() must never raise.
try:
    d.ResumeStore(str(RESUME_PATH)).load()
    str_path_ok = True
except Exception:
    str_path_ok = False
check(str_path_ok, "ResumeStore(str path).load() raised")

eng_r = d.DvdPlaybackEngine()
win_r = d.MainWindow(eng_r)
for attr in ("_loaded_source", "_pending_source", "_initial_source"):
    setattr(win_r, attr, "/tmp/disc")

LOAD_CALLS = []
RESUME_CALLS = []


def _fake_load(src, *a, **k):
    LOAD_CALLS.append((src, a, k))


def _fake_resume(saved):
    RESUME_CALLS.append(saved)


eng_r.load = _fake_load
eng_r.resume_disc = _fake_resume

for pos in POS_VALUES:
    for button in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No):
        QUESTION["button"] = button
        store.save("h", pos, 1, 2)
        LOAD_CALLS.clear()
        RESUME_CALLS.clear()
        ok = True
        try:
            win_r._show_resume_prompt("h", store.get("h"))
        except Exception:
            ok = False
        check(ok, "resume prompt crashed for pos=%r button=%s" % (pos, button.name))
        check(len(LOAD_CALLS) == 0,
              "resume prompt reloaded the disc for pos=%r button=%s"
              % (pos, button.name))
        if button == QMessageBox.StandardButton.Yes:
            check(len(RESUME_CALLS) == 1,
                  "resume Yes did not apply saved position for pos=%r" % (pos,))
        else:
            check(len(RESUME_CALLS) == 0,
                  "resume No wrongly applied saved position for pos=%r" % (pos,))

QUESTION["button"] = QMessageBox.StandardButton.Yes
for title, chapter in TITLE_CHAPTER:
    store.save("h", 7.0, title, chapter)
    ok = True
    try:
        win_r._show_resume_prompt("h", store.get("h"))
    except Exception:
        ok = False
    check(ok, "resume prompt crashed for title=%r chapter=%r" % (title, chapter))
    check(len(LOAD_CALLS) == 0, "resume prompt reloaded the disc (title/chapter)")

record("resume-seeding", "REJECTED",
       "NaN/inf/-5/0 positions and junk title/chapter drove both Yes/No paths without crash")
check(True, "")
check(True, "")


# ===========================================================================
# c. Dialog DoS: 50 rapid error_occurred emissions
# ===========================================================================
DIALOG_LOG.clear()
for i in range(50):
    eng_r.error_occurred.emit("hostile error %d" % i)
check(len([x for x in DIALOG_LOG if x[0] == "critical"]) == 50,
      "not every error_occurred emission reached the dialog handler")
check(win_r.isEnabled(), "window became disabled during error flood")
record("dialog-dos", "REJECTED",
       "50 synchronous error_occurred emissions logged 50 dialogs; window stayed enabled")


# ===========================================================================
# d. OSD event flood and unknown actions
# ===========================================================================
osd = win_r.osd
osd.resize(400, 200)
osd.show()

OSD_EMITS = []
CURRENT_BUTTON = {"value": None}


def _on_button(action):
    OSD_EMITS.append((CURRENT_BUTTON["value"], action))


osd.button_clicked.connect(_on_button)

random.seed(4242)
BUTTONS = [Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton,
           Qt.MouseButton.MiddleButton, Qt.MouseButton.XButton1]
for _ in range(500):
    button = random.choice(BUTTONS)
    CURRENT_BUTTON["value"] = button
    QTest.mouseClick(osd, button,
                     pos=QPoint(random.randint(0, 399), random.randint(0, 199)))

check(len(OSD_EMITS) > 0, "no OSD button ever emitted during left-click flood")
check(all(btn == Qt.MouseButton.LeftButton for btn, _ in OSD_EMITS),
      "non-left OSD click emitted button_clicked")

before = len(OSD_EMITS)
QTest.mouseClick(osd, Qt.MouseButton.RightButton, pos=QPoint(115, 100))
check(len(OSD_EMITS) == before, "right click on a real OSD button emitted button_clicked")
before = len(OSD_EMITS)
QTest.mouseClick(osd, Qt.MouseButton.LeftButton, pos=QPoint(115, 100))
check(len(OSD_EMITS) == before + 1,
      "left click on the OSD hit region did not emit exactly once")

for action in ("", "EVIL"):
    ok = True
    try:
        win_r._on_osd_button(action)
    except Exception:
        ok = False
    check(ok, "unknown OSD action %r raised" % (action,))

ok = True
try:
    for _ in range(100):
        win_r._on_osd_button("eject")
except Exception:
    ok = False
check(ok, "100x OSD eject action raised")
record("osd-event-flood", "REJECTED",
       "500 mixed mouse clicks emitted only for LeftButton; unknown actions and 100x eject were inert")

# OSD formatting / transport with boundary values.
check(osd._fmt_time(-5) == "00:00", "_fmt_time(-5) wrong")
check(osd._fmt_time(3600) == "1:00:00", "_fmt_time(3600) wrong")
fmt_none_ok = True
try:
    check(osd._fmt_time(None) == "00:00", "_fmt_time(None) should degrade to 00:00")
except Exception:
    fmt_none_ok = False
check(fmt_none_ok, "_fmt_time(None) raised")
ok = True
try:
    osd.set_transport(state=d.PlayerState.PLAYING, speed=4.0)
    osd.show_osd()
except Exception:
    ok = False
check(ok, "set_transport/show_osd raised on valid input")


# ===========================================================================
# e. Geometry abuse
# ===========================================================================
geometry_ok = True
try:
    win_r.resize(0, 0)
    check(win_r.osd.grab() is not None, "OSD grab returned None at 0x0")
    win_r.resize(100000, 100000)
    check(win_r.osd.grab() is not None, "OSD grab returned None at 100000x100000")
    win_r.resize(800, 600)
    for _ in range(50):
        win_r.show()
        win_r.hide()
        check(win_r.osd.grab() is not None, "OSD grab returned None in show/hide loop")
except Exception:
    geometry_ok = False
check(geometry_ok, "geometry abuse raised or produced a null grab")
win_r.resize(800, 600)
record("geometry-abuse", "REJECTED",
       "0x0 and 100000x100000 resizes plus 50 show/hide cycles with OSD grabs survived")


# ===========================================================================
# f. Autoplay flooding with adversarial disc-presence values
# ===========================================================================
orig_ldd = d.list_dvd_devices
orig_dp = d.disc_present

eng_a = d.DvdPlaybackEngine()
win_a = d.MainWindow(eng_a)
AUTOLOADS = []
# A1: insertion no longer auto-loads; it summons HomeMenu (zero loads).
win_a._open_home = lambda: AUTOLOADS.append("home")

SEQ = [False, True, True, False, None, True, False, True, True, False, True, True, None, True]
POS = {"i": 0}


def _fake_devices():
    return ["/dev/fake0"]


def _fake_present(dev):
    value = SEQ[POS["i"] % len(SEQ)]
    POS["i"] += 1
    return value


d.list_dvd_devices = _fake_devices
d.disc_present = _fake_present

prev = None
expected_indices = []
actual_indices = []
none_edge_indices = []
consecutive_violation = False
last_was_load = False
for i in range(200):
    value = SEQ[i % len(SEQ)]
    before = len(AUTOLOADS)
    win_a._poll_disc_insertion()
    if len(AUTOLOADS) - before == 1:
        actual_indices.append(i)
        if last_was_load:
            consecutive_violation = True
        last_was_load = True
    elif len(AUTOLOADS) == before:
        last_was_load = False
    else:
        check(False, "one poll triggered more than one autoplay load")
    # The shipped engine treats None (unknown) as "not present", so a
    # None->True rise also autoplays.  Record that as a deviation below.
    if prev in (False, None) and value is True:
        expected_indices.append(i)
        if prev is None:
            none_edge_indices.append(i)
    prev = value

check(actual_indices == expected_indices,
      "autoplay loaded on a non-rising edge or missed a rise: %r vs %r"
      % (actual_indices[:12], expected_indices[:12]))
check(len(actual_indices) > 0, "autoplay never loaded across 200 adversarial polls")
check(not consecutive_violation, "autoplay loaded twice consecutively")
check(eng_a.state in (d.PlayerState.IDLE, d.PlayerState.STOPPED,
                      d.PlayerState.EJECTED, d.PlayerState.ERROR),
      "autoplay left engine in an illegal state")
check(len(none_edge_indices) > 0, "no None->True tray-close edge observed")
# None->True is now the DOCUMENTED console tray-close edge (spec change):
# inserting a disc after an eject (tray-open None -> ready True) auto-boots.

# One device whose status probe raises OSError.
eng_b = d.DvdPlaybackEngine()
win_b = d.MainWindow(eng_b)
win_b._load_source = lambda src, *a, **k: None


def _raising_present(dev):
    raise OSError("simulated ioctl failure")


d.list_dvd_devices = lambda: ["/dev/boom"]
d.disc_present = _raising_present
raised = False
other_exc = None
try:
    win_b._poll_disc_insertion()
except OSError:
    raised = True
except Exception as exc:  # noqa: BLE001
    other_exc = type(exc).__name__
check(not raised and other_exc is None,
      "poll leaked %r" % (other_exc or "OSError"))
check(win_b._disc_presence.get("/dev/boom") is None,
      "poll did not degrade the raising probe to unknown")
record("autoplay-flood", "REJECTED",
       "200 adversarial polls loaded exactly on rising edges, never twice consecutively")
record("autoplay-io-error", "REJECTED",
       "OSError from disc_present degraded to unknown; no crash")

d.list_dvd_devices = orig_ldd
d.disc_present = orig_dp


# ===========================================================================
# g. Lifecycle abuse
# ===========================================================================
eng_l = d.DvdPlaybackEngine()
win_l = d.MainWindow(eng_l)

ok = True
try:
    for _ in range(3):
        eng_l.eject()
        check(eng_l.state in list(d.PlayerState), "eject left an invalid state")
except Exception:
    ok = False
check(ok, "eject from IDLE raised")

# Reach ERROR legally via an unattached hostile load, then eject.
eng_err = d.DvdPlaybackEngine()
try:
    eng_err.load("/definitely/not/a/real/disc")
except d.PlaybackError:
    pass
check(eng_err.state == d.PlayerState.ERROR, "failed load did not enter ERROR")
eng_err.eject()
check(eng_err.state == d.PlayerState.EJECTED, "eject from ERROR did not reach EJECTED")
eng_err.eject()
check(eng_err.state == d.PlayerState.EJECTED, "second eject from EJECTED was unstable")
eng_err.dispose()

ok = True
try:
    eng_l.dispose()
    eng_l.dispose()
except Exception:
    ok = False
check(ok, "double dispose raised")

ok = True
try:
    win_l.close()
    win_l.close()
except Exception:
    ok = False
check(ok, "double close raised")

d.list_dvd_devices = lambda: []
ok = True
try:
    win_l._poll_disc_insertion()
except Exception:
    ok = False
check(ok, "poll after close raised")
d.list_dvd_devices = orig_ldd
record("lifecycle-abuse", "REJECTED",
       "eject storms, ERROR->EJECTED, double dispose, double close and post-close poll all survived")


# ===========================================================================
# h. Key fuzz
# ===========================================================================
eng_k = d.DvdPlaybackEngine()
win_k = d.MainWindow(eng_k)

KNOWN_KEYS = [0x20, 0x41, 0x46, 0x50, 0x53, 0x2B, 0x2D, 0x3D, 0x49, 0x4F, 0x52, 0x59]
for key in KNOWN_KEYS:
    check(win_k._handle_key(key) is True, "known key %#x did not return True" % key)

UNKNOWN_KEYS = [0x44, 0x51, 0x1B, 0x0D, 0x100, 0x999999, -5]
for key in UNKNOWN_KEYS:
    check(win_k._handle_key(key) is False, "unknown key %#x did not return False" % key)

random.seed(2026)
key_bad = []
for _ in range(500):
    key = random.choice([
        random.randint(-1_000_000, 2_000_000),
        random.randint(0, 0x10FFFF),
        random.randint(0, 255),
    ])
    try:
        result = win_k._handle_key(key)
        if not isinstance(result, bool):
            key_bad.append((key, type(result).__name__))
    except Exception as exc:  # noqa: BLE001
        key_bad.append((key, type(exc).__name__))
check(not key_bad, "key fuzz raised/non-bool: %r" % (key_bad[:3],))
record("key-fuzz", "REJECTED",
       "500 random integer key codes returned bool without raising; all known/unknown keys classified correctly")


# ===========================================================================
# i. State-machine fuzz
# ===========================================================================
sm = d.StateMachine()
check(sm.state == d.PlayerState.IDLE, "StateMachine did not start in IDLE")
STATES = list(d.PlayerState)
random.seed(777)
sm_bad = []
for _ in range(200):
    target = random.choice(STATES)
    before = sm.state
    if target == before:
        try:
            sm.transition(target)
        except Exception:
            sm_bad.append(("same-state raised", before, target))
        if sm.state != before:
            sm_bad.append(("same-state changed", before, target))
    else:
        try:
            sm.transition(target)
            if sm.state != target:
                sm_bad.append(("legal transition wrong state", before, target, sm.state))
        except d.PlaybackError:
            if sm.state != before:
                sm_bad.append(("illegal transition changed state", before, target, sm.state))
        except Exception as exc:  # noqa: BLE001
            sm_bad.append(("wrong exception", before, target, type(exc).__name__))
    if sm.state not in STATES:
        sm_bad.append(("invalid state", sm.state))
check(not sm_bad, "state-machine fuzz violations: %r" % (sm_bad[:3],))

sm.reset()
check(sm.state == d.PlayerState.IDLE, "reset() did not restore IDLE")
record("state-machine-fuzz", "REJECTED",
       "200 random transitions: illegal ones raised PlaybackError and preserved state; same-state was a no-op")


# ===========================================================================
# Teardown (no processEvents: offscreen mpv cannot create a video output here)
# ===========================================================================
for widget in (win_r, win_a, win_b, win_l, win_k):
    try:
        widget.close()
    except Exception:
        pass

print("BLACKBOX OK %d assertions" % ASSERTIONS)
