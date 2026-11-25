#!/usr/bin/env python3
"""Independent verification for the GUI sections (5-6) of dvd_player.py.

Read-only audit: exercises the code through public/private API with stubbed
engine calls; never touches the repo. Prints VERIFICATION-GUI OK <n>.
"""
from __future__ import annotations

import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

from PyQt6.QtWidgets import QApplication, QMessageBox

import dvd_player as dp

app = QApplication.instance() or QApplication([])
assert isinstance(app, QApplication)

_count = 0


def check(condition: object, message: str) -> None:
    global _count
    if not condition:
        raise AssertionError(message)
    _count += 1


# ── QMessageBox recorders (never block) ────────────────────────────
_warnings: list[tuple[object, ...]] = []
_criticals: list[tuple[object, ...]] = []
_questions: list[tuple[object, ...]] = []
_answer = {"value": QMessageBox.StandardButton.Yes}


def _record_warning(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
    _warnings.append(args)
    return QMessageBox.StandardButton.Ok


def _record_critical(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
    _criticals.append(args)
    return QMessageBox.StandardButton.Ok


def _record_question(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
    _questions.append(args)
    return _answer["value"]


QMessageBox.warning = staticmethod(_record_warning)  # type: ignore[method-assign]
QMessageBox.critical = staticmethod(_record_critical)  # type: ignore[method-assign]
QMessageBox.question = staticmethod(_record_question)  # type: ignore[method-assign]


def make_window() -> tuple[dp.MainWindow, dp.DvdPlaybackEngine]:
    engine = dp.DvdPlaybackEngine()
    engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
    window = dp.MainWindow(engine)
    window._auto_detected = True
    window._attach_engine = lambda: None  # type: ignore[method-assign]
    return window, engine


# ════════════════════════════════════════════════════════════════════
# 1. Trick-play ladder: every (current, direction) target
# ════════════════════════════════════════════════════════════════════
window, engine = make_window()
speed_calls: list[float] = []


def _set_speed(mult: float) -> None:
    speed_calls.append(float(mult))
    engine._speed = float(mult)


engine.set_speed = _set_speed  # type: ignore[method-assign]

FF_TABLE = {
    1.0: 2.0,
    2.0: 4.0,
    4.0: 8.0,
    8.0: 16.0,
    16.0: 1.0,
}
RW_TABLE = {
    1.0: 16.0,
    2.0: 1.0,
    4.0: 2.0,
    8.0: 4.0,
    16.0: 8.0,
}
for current, target in FF_TABLE.items():
    engine._speed = current
    speed_calls.clear()
    window._cycle_speed(1)
    check(speed_calls == [target], f"FF {current} -> {speed_calls}, want {target}")
for current, target in RW_TABLE.items():
    engine._speed = current
    speed_calls.clear()
    window._cycle_speed(-1)
    check(speed_calls == [target], f"RW {current} -> {speed_calls}, want {target}")
check(dp.FF_SPEEDS == (2.0, 4.0, 8.0, 16.0), "FF_SPEEDS changed")
window.close()

# ════════════════════════════════════════════════════════════════════
# 2. _fmt_time safety and formatting
# ════════════════════════════════════════════════════════════════════
fmt_cases: list[tuple[float, str]] = [
    (float("nan"), "00:00"),
    (float("inf"), "00:00"),
    (float("-inf"), "00:00"),
    (-5.0, "00:00"),
    (65.0, "01:05"),
    (5400.0, "1:30:00"),
]
for value, expected in fmt_cases:
    got = dp.Xbox360Osd._fmt_time(value)
    check(got == expected, f"_fmt_time({value!r}) = {got!r}, want {expected!r}")

# ════════════════════════════════════════════════════════════════════
# 3. _button_rects: order, non-overlap, containment
# ════════════════════════════════════════════════════════════════════
osd = dp.Xbox360Osd()
expected_names = [name for name, _ in dp.Xbox360Osd.BUTTONS]
for width in (320, 640, 1280):
    osd.resize(width, 64)
    rects = osd._button_rects()
    names = [name for name, _ in rects]
    check(names == expected_names, f"width {width}: order {names} != {expected_names}")
    for (_, left), (_, right) in zip(rects, rects[1:], strict=False):
        check(
            left.right() <= right.left() + 1e-9,
            f"width {width}: overlapping rects {left.right()} > {right.left()}",
        )
        check(
            all(r.left() >= 0.0 and r.right() <= width + 1e-9 for _, r in rects),
            f"width {width}: buttons are not inside the widget",
        )
osd.resize(1280, 64)
check(osd._button_rects()[0][1].width() >= 34.0, "button width below 34 px at 1280")
_rect_map = dict(osd._button_rects())
check(
    _rect_map["eject"].left() - _rect_map["menu"].right()
    >= 2.0 * (_rect_map["menu"].left() - _rect_map["subtitle"].right()),
    "eject is not separated by an extra BUTTON_GAP",
)
osd.deleteLater()

# ════════════════════════════════════════════════════════════════════
# 4. _status_text variants
# ════════════════════════════════════════════════════════════════════
osd = dp.Xbox360Osd()
for state, expected in (
    (dp.PlayerState.MENU_ACTIVE, "MENU"),
    (dp.PlayerState.PLAYING, "PLAYING"),
    (dp.PlayerState.PAUSED, "PAUSED"),
    (dp.PlayerState.STOPPED, "STOPPED"),
):
    osd.set_transport(state=state, speed=1.0)
    check(osd._status_text() == expected, f"status {state} -> {osd._status_text()}")
osd.set_transport(state=dp.PlayerState.PLAYING, speed=4.0)
check(osd._status_text() == "FF 4x", f"FF 4x -> {osd._status_text()}")
osd.set_transport(state=dp.PlayerState.PLAYING, speed=-2.0)
check(osd._status_text() == "RW 2x", f"RW 2x -> {osd._status_text()}")
osd.deleteLater()

# ════════════════════════════════════════════════════════════════════
# 5. Resume prompt flow (Yes / No / NaN position)
# ════════════════════════════════════════════════════════════════════
window, engine = make_window()
loads: list[str] = []
resumes: list[dict] = []  # type: ignore[type-arg]
forgets: list[str] = []
engine.load = lambda src: loads.append(src)  # type: ignore[method-assign]
engine.resume_disc = lambda saved: resumes.append(saved)  # type: ignore[method-assign]
_orig_forget = dp.ResumeStore.forget
dp.ResumeStore.forget = lambda self, h: forgets.append(h)  # type: ignore[method-assign]

window._pending_source = "/tmp/audit-disc.iso"
_questions.clear()
_answer["value"] = QMessageBox.StandardButton.Yes
window._show_resume_prompt("hash-yes", {"pos": 65.0, "title": 1, "chapter": 2})
check(len(_questions) == 1, "Yes flow: prompt not shown")
check("Resume playback from 01:05?" in str(_questions[0]), f"Yes text: {_questions[0]}")
check(loads == [], f"Yes flow must not load: {loads}")
check(resumes == [{"pos": 65.0, "title": 1, "chapter": 2}], f"Yes flow resume: {resumes}")

loads.clear()
resumes.clear()
_questions.clear()
_answer["value"] = QMessageBox.StandardButton.No
window._show_resume_prompt("hash-no", {"pos": 30.0})
check(forgets == ["hash-no"], f"No flow forget: {forgets}")
check(loads == [], f"No flow must not load: {loads}")
check(resumes == [], f"No flow must not resume: {resumes}")

loads.clear()
_questions.clear()
_answer["value"] = QMessageBox.StandardButton.Yes
window._show_resume_prompt("hash-nan", {"pos": float("nan")})
check(len(_questions) == 1, "NaN flow: prompt not shown")
check("00:00" in str(_questions[0]), f"NaN text: {_questions[0]}")
check(loads == [], f"NaN flow must not load: {loads}")

loads.clear()
_questions.clear()
window._show_resume_prompt("hash-inf", {"pos": float("inf")})
check("00:00" in str(_questions[0]), f"inf text: {_questions[0]}")
dp.ResumeStore.forget = _orig_forget  # type: ignore[method-assign]

# ── async disc hash: early state prompts, late state does not ───────
_orig_fg = dp.ResumeStore.get
dp.ResumeStore.get = lambda self, h: {"pos": 20.0} if h == "known" else None
_questions.clear()
resumes.clear()
engine._sm._state = dp.PlayerState.LOADING
window._on_disc_hash("unknown")
check(_questions == [], "unknown disc hash opened a prompt")
window._on_disc_hash("known")
check(len(_questions) == 1, "early disc hash did not prompt")
check(resumes == [{"pos": 20.0}], f"early Yes did not resume: {resumes}")


class _LatePlayer:
    time_pos = 120.0

    def terminate(self) -> None:
        pass


engine._player = _LatePlayer()  # type: ignore[assignment]
engine._sm._state = dp.PlayerState.PLAYING
window._load_started_at = dp.time.monotonic() - 20.0
_questions.clear()
resumes.clear()
window._on_disc_hash("known")
check(_questions == [], "late disc hash prompted")
check(resumes == [], "late disc hash resumed")
dp.ResumeStore.get = _orig_fg  # type: ignore[method-assign]
window.close()

# ════════════════════════════════════════════════════════════════════
# 6. closeEvent: both timers stopped, dispose exactly once
# ════════════════════════════════════════════════════════════════════
window, engine = make_window()
disposals: list[int] = []
engine.dispose = lambda: disposals.append(1)  # type: ignore[method-assign]
check(window._gamepad_timer.isActive(), "gamepad timer not running")
check(window._disc_timer.isActive(), "disc timer not running")
window.osd.show_osd()
check(window.osd._hide_timer.isActive(), "OSD hide timer did not start")


class _CloseEvent:
    accepted = False

    def accept(self) -> None:
        self.accepted = True


close_event = _CloseEvent()
window.closeEvent(close_event)  # type: ignore[arg-type]
check(close_event.accepted, "closeEvent did not accept the event")
check(not window._gamepad_timer.isActive(), "closeEvent left the gamepad timer running")
check(not window._disc_timer.isActive(), "closeEvent left the disc timer running")
check(disposals == [1], f"dispose calls: {disposals}")
check(
    not window.osd._hide_timer.isActive(),
    "closeEvent left the OSD hide timer running",
)

# ════════════════════════════════════════════════════════════════════
# 7. Disc insertion edge matrix
# ════════════════════════════════════════════════════════════════════
window, engine = make_window()
load_calls: list[tuple[str, bool]] = []
window._load_source = (  # type: ignore[method-assign]
    lambda src, quiet=False: load_calls.append((src, quiet))
)

_orig_devices = dp.list_dvd_devices
_orig_present = dp.disc_present
dp.list_dvd_devices = lambda: ["/dev/sr0"]  # type: ignore[assignment]

# A1: False -> True in IDLE summons HomeMenu and NEVER loads.
dp.disc_present = lambda dev: True  # type: ignore[assignment]
load_calls.clear()
window.home.hide()
window._disc_presence = {"/dev/sr0": False}
window._poll_disc_insertion()
check(load_calls == [], f"insertion edge loaded: {load_calls}")
check(not window.home.isHidden(), "insertion edge did not open HomeMenu")
check(window.home.blade_name() == "DVD", "HomeMenu did not open on the DVD blade")
window._poll_disc_insertion()
check(load_calls == [], "repeated poll loaded the disc")
window._poll_disc_insertion()
check(load_calls == [], "third poll loaded the disc")

# True -> False for the loaded device reports removal (debounced: 2 polls).
window._loaded_source = "/dev/sr0"
window.status.setText("")
dp.disc_present = lambda dev: False  # type: ignore[assignment]
window._poll_disc_insertion()
check(window.status.text() == "", "removal reported after a single poll")
window._poll_disc_insertion()
check(window.status.text() == "disc removed", "removal edge not reported")
check(window._loaded_source is None, "removal did not clear loaded source")

# True -> False for another device must stay silent.
window._loaded_source = "/dev/sr1"
window.status.setText("")
window._disc_presence = {"/dev/sr0": True}
window._poll_disc_insertion()
check(window.status.text() == "", "removal edge for a non-loaded device reported")

# Tray transitions: a loaded disc survives a single-poll None blip (no
# reload), a 2-poll absence debounces to 'disc removed' + clears the loaded
# source, and None->True after that (tray closed, disc present — e.g. after
# an eject) re-opens the dashboard (A1: still never loads).
window._loaded_source = "/dev/sr0"
window._disc_gone_streak = {}
load_calls.clear()
window.status.setText("")
dp.disc_present = lambda dev: None  # type: ignore[assignment]
window._disc_presence = {"/dev/sr0": True}
window._poll_disc_insertion()
check(load_calls == [], "blip poll 1 loaded")
check(window.status.text() == "", "blip poll 1 reported removal")
window._disc_presence = {"/dev/sr0": None}
window._poll_disc_insertion()
check(load_calls == [], "blip poll 2 loaded")
check(window.status.text() == "disc removed", "blip poll 2 did not report removal")
check(window._loaded_source is None, "removal did not clear loaded source")
load_calls.clear()
window.status.setText("")
window.home.hide()
dp.disc_present = lambda dev: True  # type: ignore[assignment]
window._poll_disc_insertion()
check(load_calls == [], f"None->True loaded: {load_calls}")
check(not window.home.isHidden(), "None->True did not open HomeMenu")

# PLAYING / LOADING block the dashboard summon on a real edge.
for state in (dp.PlayerState.PLAYING, dp.PlayerState.LOADING):
    load_calls.clear()
    window.home.hide()
    engine._sm._state = state
    dp.disc_present = lambda dev: True  # type: ignore[assignment]
    window._disc_presence = {"/dev/sr0": False}
    window._poll_disc_insertion()
    check(load_calls == [], f"loaded while {state.name}")
    check(window.home.isHidden(), f"HomeMenu summoned while {state.name}")
engine._sm._state = dp.PlayerState.IDLE
dp.list_dvd_devices = _orig_devices  # type: ignore[assignment]
dp.disc_present = _orig_present  # type: ignore[assignment]
window.close()

# ════════════════════════════════════════════════════════════════════
# 8. _auto_detect_disc primes the snapshot and opens the dashboard
# ════════════════════════════════════════════════════════════════════
window, engine = make_window()
auto_calls: list[tuple[str, bool]] = []
window._load_source = (  # type: ignore[method-assign]
    lambda src, quiet=False: auto_calls.append((src, quiet))
)
dp.list_dvd_devices = lambda: ["/dev/sr0", "/dev/sr1", "/dev/sr2"]  # type: ignore[assignment]
dp.disc_present = lambda dev: dev in ("/dev/sr1", "/dev/sr2")  # type: ignore[assignment]
window.home.hide()
window._auto_detect_disc()
check(auto_calls == [], f"auto detect loaded {auto_calls}")
check(window.source.text() == "", "auto detect set the source text")
check(
    window._disc_presence == {"/dev/sr0": False, "/dev/sr1": True, "/dev/sr2": True},
    f"auto detect did not prime the poller snapshot: {window._disc_presence}",
)
check(not window.home.isHidden(), "auto detect did not open HomeMenu")
window.home.hide()
auto_calls.clear()
dp.disc_present = lambda dev: False  # type: ignore[assignment]
window._auto_detect_disc()
check(auto_calls == [], "auto detect loaded with no disc")
check(not window.home.isHidden(), "launch without a disc did not open HomeMenu")
dp.list_dvd_devices = _orig_devices  # type: ignore[assignment]
dp.disc_present = _orig_present  # type: ignore[assignment]
window.close()

# ════════════════════════════════════════════════════════════════════
# 9. _load_source quiet=True never opens a warning modal
# ════════════════════════════════════════════════════════════════════
import tempfile as _tmp_mod
_tmp_dir = _tmp_mod.TemporaryDirectory()
_real_file = _tmp_mod.NamedTemporaryFile(dir=_tmp_dir.name, delete=False)
_real_file.write(b"x")
_real_file.close()
window, engine = make_window()
loads.clear()
engine.load = lambda src: loads.append(src)  # type: ignore[method-assign]
_orig_env = dp.check_environment
_orig_get = dp.ResumeStore.get
dp.check_environment = lambda source: ["problem x"]  # type: ignore[assignment]
dp.ResumeStore.get = lambda self, h: None  # type: ignore[method-assign]
_warnings.clear()
_criticals.clear()
window.status.setText("")
window._load_source(_real_file.name, quiet=True)
check(_warnings == [], "quiet load opened a warning modal")
check(_criticals == [], "quiet load opened a critical modal")
check(loads == [_real_file.name], f"quiet load: {loads}")
check("problem x" in window.status.toolTip(), "quiet load did not surface warnings")

# Garbage path: synchronous critical, no playback, no hash computation.
loads.clear()
_warnings.clear()
_criticals.clear()
window._load_source("/tmp/audit-iso-does-not-exist.iso", quiet=False)
check(len(_criticals) == 1, "garbage path did not open a critical")
check(loads == [], f"garbage path loaded: {loads}")
_dialogs_ok = "cannot access disc source" in str(_criticals[0])
check(_dialogs_ok, f"garbage critical text: {_criticals[0]}")
window.close()

# ════════════════════════════════════════════════════════════════════
# 10. Legal-state contract at the GUI boundary (known defect)
# ════════════════════════════════════════════════════════════════════
engine._sm._state = dp.PlayerState.IDLE
window.close()
window, engine = make_window()
engine._sm._state = dp.PlayerState.PLAYING
dp.check_environment = lambda source: []  # type: ignore[assignment]
warnings_before = len(_warnings)
criticals_before = len(_criticals)
raised: dp.PlaybackError | None = None
try:
    window._load_source(_real_file.name, quiet=False)
except dp.PlaybackError as exc:
    raised = exc
check(raised is None, "guarded load leaked PlaybackError out of the slot")
check(
    "cannot load while in state PLAYING" in window.status.toolTip(),
    f"unexpected status: {window.status.toolTip()!r}",
)
check(len(_warnings) == warnings_before, "guard path opened a warning")
check(len(_criticals) == criticals_before, "guard path opened a critical dialog")
engine._sm._state = dp.PlayerState.IDLE
window.close()
_tmp_dir.cleanup()

dp.check_environment = _orig_env  # type: ignore[assignment]
dp.ResumeStore.get = _orig_get  # type: ignore[method-assign]
dp.list_dvd_devices = _orig_devices  # type: ignore[assignment]
dp.disc_present = _orig_present  # type: ignore[assignment]

# ════════════════════════════════════════════════════════════════════
# 11. Video-only window, Ctrl+L load, guarded OSD eject
# ════════════════════════════════════════════════════════════════════
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent

window, engine = make_window()
check(
    window.minimumWidth() == 854 and window.minimumHeight() == 480,
    "window minimum size is not 854x480",
)
window.show()
app.processEvents()
check(not window.source.isVisible(), "bottom bar is visible by default")
check(hasattr(window, "source"), "window.source is not reachable while hidden")
window.hide()

browse_calls: list[tuple[str, bool]] = []
window._load_source = (  # type: ignore[method-assign]
    lambda src, quiet=False: browse_calls.append((src, quiet))
)
window._browse_for_source = lambda: "/tmp/audit-ctrl-l.iso"  # type: ignore[method-assign]
ctrl_l = QKeyEvent(
    QEvent.Type.KeyPress,
    Qt.Key.Key_L.value,
    Qt.KeyboardModifier.ControlModifier,
)
window.keyPressEvent(ctrl_l)
check(window.source.text() == "/tmp/audit-ctrl-l.iso", "Ctrl+L did not set source")
check(browse_calls == [("/tmp/audit-ctrl-l.iso", False)], f"Ctrl+L load: {browse_calls}")
window._browse_for_source = lambda: None  # type: ignore[method-assign]
browse_calls.clear()
window.keyPressEvent(ctrl_l)
check(browse_calls == [], "Ctrl+L loaded with no picked path")

ejects: list[int] = []
engine.eject = lambda: ejects.append(1)  # type: ignore[method-assign]
_questions.clear()
_answer["value"] = QMessageBox.StandardButton.No
window._on_osd_button("eject")
check(ejects == [], "OSD eject ignored the No answer")
check(len(_questions) == 1, "OSD eject did not ask for confirmation")
check("Eject the disc?" in str(_questions[0]), f"eject question: {_questions[0]}")
_answer["value"] = QMessageBox.StandardButton.Yes
window._on_osd_button("eject")
check(ejects == [1], f"OSD eject Yes did not eject: {ejects}")
_answer["value"] = QMessageBox.StandardButton.Yes
window.close()

# ════════════════════════════════════════════════════════════════════
# 12. Seek bar scrubbing signals + pressed-only preview wiring
# ════════════════════════════════════════════════════════════════════
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QImage, QMouseEvent

osd_bar = dp.Xbox360Osd()
osd_bar.resize(1280, 64)
osd_bar.set_transport(state=dp.PlayerState.PLAYING, time_pos=30.0, duration=120.0)
check(abs(osd_bar._seek_fraction() - 0.25) < 1e-9, "bar fraction not 30/120")
track, hit = osd_bar._seek_bar_rect()
check(abs(track.left() - 16.0) < 1e-9 and abs(track.width() - 1248.0) < 1e-9, "bar track geometry")
check(abs(track.height() - 6.0) < 1e-9, "bar track is not 6 px")
check(abs(hit.height() - 18.0) < 1e-9 and hit.contains(track.center()), "bar hit rect")
for kwargs in (
    {"time_pos": 30.0, "duration": None},
    {"time_pos": 30.0, "duration": 0.0},
    {"time_pos": None, "duration": 120.0},
):
    osd_bar.set_transport(state=dp.PlayerState.PLAYING, **kwargs)
    check(osd_bar._seek_fraction() == 0.0, f"bar not empty for {kwargs}")
osd_bar.set_transport(state=dp.PlayerState.PLAYING, time_pos=30.0, duration=120.0)

trace: list[tuple[str, float | None]] = []
osd_bar.scrub_pressed.connect(lambda f: trace.append(("press", round(f, 4))))
osd_bar.scrub_moved.connect(lambda f: trace.append(("move", round(f, 4))))
osd_bar.scrub_released.connect(lambda: trace.append(("release", None)))


def _scrub_event(kind: QEvent.Type, x: float, held: bool) -> QMouseEvent:
    button = (
        Qt.MouseButton.NoButton
        if kind == QEvent.Type.MouseMove
        else Qt.MouseButton.LeftButton
    )
    return QMouseEvent(
        kind,
        QPointF(x, hit.center().y()),
        QPointF(x, hit.center().y()),
        button,
        Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


osd_bar.mousePressEvent(
    _scrub_event(QEvent.Type.MouseButtonPress, track.left() + 0.2 * track.width(), True)
)
osd_bar.mouseMoveEvent(
    _scrub_event(QEvent.Type.MouseMove, track.left() + 0.6 * track.width(), True)
)
osd_bar.mouseReleaseEvent(
    _scrub_event(QEvent.Type.MouseButtonRelease, track.left() + 0.6 * track.width(), False)
)
check(
    trace == [("press", 0.2), ("move", 0.6), ("release", None)],
    f"scrub signal fractions: {trace}",
)
osd_bar.mouseMoveEvent(
    _scrub_event(QEvent.Type.MouseMove, track.left() + 0.9 * track.width(), False)
)
check(len(trace) == 3, "unpressed move emitted scrub_moved")
osd_bar.show()
app.processEvents()
osd_bar.set_preview(QImage(32, 18, QImage.Format.Format_RGB32))
check(osd_bar._preview is not None, "set_preview did not store")
osd_bar._scrub_x = 500.0
check(not osd_bar.grab().isNull(), "preview paint failed")
osd_bar.hide()
app.processEvents()
check(osd_bar._preview is None, "hide did not clear preview")
osd_bar.close()


class _BarPlayer:
    duration: float | None = 200.0
    time_pos: float | None = 10.0
    aid: object = None
    sid: object = None
    chapter: int | None = None
    chapter_list: list[int] = []
    volume: float = 100.0


win_bar, eng_bar = make_window()
eng_bar._player = _BarPlayer()
seeks: list[float] = []
captures: list[int] = []
eng_bar.seek_to = lambda s: seeks.append(float(s))  # type: ignore[method-assign]
eng_bar.capture_frame = lambda: (  # type: ignore[attr-defined]
    captures.append(1) or (2, 2, bytes([1, 2, 3, 0]) * 4)
)
preview_calls: list[object] = []
_orig_set_preview = win_bar.osd.set_preview
win_bar.osd.set_preview = lambda img: (  # type: ignore[method-assign]
    preview_calls.append(img),
    _orig_set_preview(img),
)[1]

win_bar._on_scrub_pressed(0.25)
check(seeks == [50.0], f"press seek: {seeks}")
check(len(captures) == 1, "press did not capture")
check(win_bar.osd._preview is not None, "press did not install preview")
win_bar._on_scrub_moved(0.5)
check(seeks == [50.0], f"move not throttled: {seeks}")
win_bar._scrub_last_seek -= 0.2
win_bar._on_scrub_moved(0.5)
check(seeks == [50.0], f"seek inside 250 ms window: {seeks}")
# Seek window elapsed, capture window not: seek without a capture.
win_bar._scrub_last_seek -= 0.1
win_bar._on_scrub_moved(0.5)
check(seeks == [50.0, 100.0], f"throttled move seek: {seeks}")
check(len(captures) == 1, "capture not lazy-guarded")
# Both windows elapsed: one capture follows the seek.
win_bar._scrub_last_seek -= 0.3
win_bar._scrub_last_capture -= 0.2
win_bar._on_scrub_moved(0.75)
check(seeks == [50.0, 100.0, 150.0], f"second throttled move: {seeks}")
check(len(captures) == 2, "lazy capture missing")
win_bar._on_scrub_released()
check(seeks == [50.0, 100.0, 150.0, 150.0], f"release final seek: {seeks}")
check(len(captures) == 2, f"release captured: {captures}")
check(win_bar.osd._preview is None, "release did not clear preview")
check(preview_calls[-1] is None, "release final set_preview not None")
check(len([p for p in preview_calls if p is not None]) == 2, "preview outside press pair")
check(
    not win_bar._scrub_active
    and win_bar._scrub_last_seek == 0.0
    and win_bar._scrub_last_capture == 0.0,
    "scrub not reset",
)
win_bar._on_scrub_moved(0.9)
check(
    seeks == [50.0, 100.0, 150.0, 150.0] and len(preview_calls) == 3,
    "post-release scrub",
)
eng_bar._player = None
seeks.clear()
captures.clear()
win_bar._on_scrub_pressed(0.5)
win_bar._on_scrub_released()
check(seeks == [] and captures == [], "scrub without duration touched engine")

# Render mode: grabFramebuffer is the live frame source; the preview is a
# big (>=320x180) above-video thumbnail with the OSD-formatted timestamp.
eng_bar._player = _BarPlayer()
eng_bar.capture_frame = lambda: (  # type: ignore[attr-defined]
    captures.append(1) or (2, 2, bytes([1, 2, 3, 0]) * 4)
)
render_image = QImage(640, 360, QImage.Format.Format_RGB32)
render_image.fill(0)
grabs: list[int] = []


def _grab_frame() -> QImage:
    grabs.append(1)
    return render_image


win_bar._render_mode = True
win_bar.resize(1280, 720)
win_bar.video.grabFramebuffer = _grab_frame  # type: ignore[method-assign]
seeks.clear()
captures.clear()
preview_calls.clear()
win_bar._on_scrub_pressed(0.25)
check(seeks == [50.0] and grabs == [1], "render press did not grab after seek")
check(captures == [], "render mode called capture_frame")
check(
    preview_calls[-1] is not None
    and preview_calls[-1].width() == 640
    and preview_calls[-1].height() == 360,
    "grabFramebuffer image did not reach set_preview",
)
win_bar._scrub_last_seek -= 0.3
win_bar._scrub_last_capture -= 0.2
win_bar._on_scrub_moved(0.5)
check(seeks == [50.0, 100.0] and grabs == [1, 1], "render throttled seek/grab")
check(win_bar.osd._preview_seconds == 100.0, "render preview timestamp")
check(win_bar.osd._preview_rect().width() >= 320.0, "preview width < 320")
check(win_bar.osd._preview_rect().height() >= 180.0, "preview height < 180")
check(
    win_bar.scrub_preview._preview_rect().width() >= 320.0
    and win_bar.scrub_preview._preview_rect().height() >= 180.0,
    "overlay preview smaller than 320x180",
)
check(win_bar.scrub_preview.timestamp_text() == "01:40", "preview timestamp text")
win_bar._on_scrub_released()
check(
    win_bar.osd._preview is None
    and win_bar.osd._preview_seconds is None
    and win_bar.scrub_preview._image is None,
    "render release did not clear the preview",
)
win_bar._render_mode = False
eng_bar._player = None
win_bar.close()

# ════════════════════════════════════════════════════════════════════
# 12. Ctrl+H HelpOverlay + README keybinding coverage
# ════════════════════════════════════════════════════════════════════
from PyQt6.QtCore import QEvent, Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402

window, engine = make_window()
window.show()
app.processEvents()
engine._sm._state = dp.PlayerState.PLAYING
transport: list[str] = []
engine.play_pause = lambda: transport.append("play_pause")  # type: ignore[method-assign]
engine.stop = lambda: transport.append("stop")  # type: ignore[method-assign]
ctrl_h = QKeyEvent(
    QEvent.Type.KeyPress, Qt.Key.Key_H.value, Qt.KeyboardModifier.ControlModifier
)
window.keyPressEvent(ctrl_h)
check(not window.help.isHidden(), "Ctrl+H did not open help")
check(engine.state is dp.PlayerState.PLAYING, "Ctrl+H changed playback")
check(transport == [], f"Ctrl+H touched transport: {transport}")
window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape.value,
              Qt.KeyboardModifier.NoModifier)
)
check(window.help.isHidden(), "Esc did not close help")
window.keyPressEvent(ctrl_h)
check(not window.help.isHidden(), "Ctrl+H did not reopen help")
window.keyPressEvent(ctrl_h)
check(window.help.isHidden(), "Ctrl+H did not toggle help closed")
check(transport == [], f"help toggle touched transport: {transport}")

_required = {
    "Enter", "Backspace", "Space", "S", "F", "R", "N", "P", "I", "O",
    "+", "-", "E", "M", "G", "C", "A", "Ctrl+F", "Ctrl+L", "Ctrl+H",
    "Esc", "X", "Y", "↑", "↓", "←", "→",
}
_rows = dict(dp.HelpOverlay.bindings())
_listed = {token for keys in _rows for token in keys.split()}
check(_required <= _listed, "help binding table is incomplete")
with open(
    "/home/giancarlo/projects/playground/CD_Player/README.md",
    encoding="utf-8",
) as _readme_file:
    _readme_text = _readme_file.read()
for _keys in _rows:
    for _token in _keys.split():
        check(_token in _readme_text, f"README misses {_token!r}")
window.close()

# D1: HomeMenu opens with the active DVD blade already Xbox-green.
from PyQt6.QtGui import QColor, QImage

launch_menu = dp.HomeMenu()
check(launch_menu._blade == 0, "HomeMenu did not start on DVD")
launch_menu.resize(1280, 720)
launch_menu._dvd_devices = ["/dev/sr0"]
launch_menu._dvd_present = {"/dev/sr0": True}
check(len(launch_menu.entries()) == 1, "active blade entries missing")
launch_image = QImage(1280, 720, QImage.Format.Format_ARGB32)
launch_image.fill(QColor(0, 0, 0))
launch_menu.render(launch_image)
_green = 0
for _y in range(launch_image.height()):
    for _x in range(launch_image.width()):
        _p = launch_image.pixelColor(_x, _y)
        if (
            abs(_p.red() - 116) <= 30
            and abs(_p.green() - 195) <= 30
            and abs(_p.blue() - 41) <= 30
        ):
            _green += 1
check(_green > 100, f"active blade not green at open: {_green} px")
# Brief structure: titled rows + subtitles, guidance and a painted footer.
check(launch_menu.entry_rows()[0].title == "DVD video",
      "DVD row is not titled")
check(launch_menu.entry_rows()[0].subtitle == "/dev/sr0",
      "DVD row lost its device subtitle")
check(launch_menu.footer_text() ==
      "Enter select  ·  ←/→ blades  ·  Esc back  ·  Ctrl+H help",
      f"footer text drifted: {launch_menu.footer_text()!r}")
_footer = launch_menu.footer_rect()
check(0 < _footer.height() and _footer.bottom() <= 720,
      f"footer not painted inside the menu: {_footer}")
launch_menu._dvd_devices = []
launch_menu._dvd_present = {}
check(launch_menu.empty_state_text() == "No disc — insert one",
      "DVD empty-state guidance missing")
check(len(launch_menu.entry_rows()) == 0, "empty blade still has rows")
launch_image.fill(QColor(0, 0, 0))
launch_menu.render(launch_image)
check(not launch_image.isNull(), "empty-state repaint failed")
launch_menu.deleteLater()

# ── Live title preview lifecycle + 2026 veil contract ──────────────
_pv_orig_devices = dp.list_dvd_devices
dp.list_dvd_devices = lambda: ["/dev/sr0"]  # type: ignore[assignment]
pv_engine = dp.DvdPlaybackEngine()
pv_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
pv_loads: list[str] = []
pv_engine.load = lambda src: pv_loads.append(src)  # type: ignore[method-assign]
pv_engine.eject = lambda: None  # type: ignore[method-assign]
pv_window = dp.MainWindow(pv_engine)
pv_window._auto_detected = True
pv_window._attach_engine = lambda: None  # type: ignore[method-assign]
pv_window._disc_presence = {"/dev/sr0": True}
pv_window._open_home()
check(pv_loads == ["/dev/sr0"], f"preview boot count: {pv_loads}")
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right.value,
              Qt.KeyboardModifier.NoModifier)
)
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left.value,
              Qt.KeyboardModifier.NoModifier)
)
check(pv_loads == ["/dev/sr0"], f"blade session reloaded: {pv_loads}")
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return.value,
              Qt.KeyboardModifier.NoModifier)
)
check(pv_loads == ["/dev/sr0"], f"ENTER reloaded: {pv_loads}")
check(pv_window.home.isHidden() and pv_window._preview_revealed,
      "ENTER did not reveal the live menu")
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape.value,
              Qt.KeyboardModifier.NoModifier)
)
check(not pv_window.home.isHidden() and pv_loads == ["/dev/sr0"],
      "Esc did not return to the dashboard without reloading")
pv_window._on_eject_clicked()
check(not pv_window._preview_running, "eject did not tear down preview")
pv_window.close()

# Resume prompt belongs to ENTER, never to the quiet preview boot.
rs_engine = dp.DvdPlaybackEngine()
rs_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
rs_engine.load = lambda src: None  # type: ignore[method-assign]
rs_window = dp.MainWindow(rs_engine)
rs_window._auto_detected = True
rs_window._attach_engine = lambda: None  # type: ignore[method-assign]
rs_window._disc_presence = {"/dev/sr0": True}
rs_window._open_home()
_seen_prompts: list[tuple[str, dict]] = []  # type: ignore[type-arg]
_orig_get = dp.ResumeStore.get
dp.ResumeStore.get = lambda self, h: {"pos": 12.0}  # type: ignore[assignment]
rs_window._show_resume_prompt = (  # type: ignore[method-assign]
    lambda h, saved: _seen_prompts.append((h, saved))
)
rs_window._on_disc_hash("pv-hash")
check(_seen_prompts == [], "resume prompt appeared during preview")
rs_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return.value,
              Qt.KeyboardModifier.NoModifier)
)
check(_seen_prompts == [("pv-hash", {"pos": 12.0})],
      f"resume prompt missing at reveal: {_seen_prompts}")
dp.ResumeStore.get = _orig_get  # type: ignore[assignment]
rs_window.close()
dp.list_dvd_devices = _pv_orig_devices  # type: ignore[assignment]

# 2026 redesign tokens + frosted veil (video visible, not merely black).
redesign_menu = dp.HomeMenu()
redesign_menu.resize(1280, 720)
check(redesign_menu.PANEL_RADIUS == 16.0, "panel radius drifted from 16 px")
check(redesign_menu.VEIL_ALPHA < 210, "veil is too opaque to see the video")
check(redesign_menu.VEIL_ALPHA > 120, "veil is too transparent for text")
check(len(redesign_menu._blade_rects()) == 3, "blade count changed")
check(redesign_menu.PANEL_RADIUS > 10.0, "legacy square panels returned")
veil_image = QImage(1280, 720, QImage.Format.Format_ARGB32)
veil_image.fill(QColor(210, 24, 24))
redesign_menu.render(veil_image)
_veil = veil_image.pixelColor(1275, 715)
check(40 < _veil.red() < 210,
      f"video is not visible through the frosted veil: {_veil.red()}")
redesign_menu.deleteLater()

# R8: platform gate — render API prefers native Wayland, wid keeps xcb,
# non-Wayland and explicit choices are never touched.
from dvd_player import app as dp_app  # noqa: E402

check(dp_app.choose_platform({}) is None, "non-Wayland launch changed platform")
check(
    dp_app.choose_platform({"WAYLAND_DISPLAY": "wayland-0"}) == "xcb",
    "Wayland wid launch did not force xcb",
)
check(
    dp_app.choose_platform(
        {"WAYLAND_DISPLAY": "wayland-0", "DVD_PLAYER_RENDER_API": "1"}
    ) == "wayland",
    "render API alias did not prefer native Wayland",
)
check(
    dp_app.choose_platform(
        {"WAYLAND_DISPLAY": "wayland-0", "DVD_PLAYER_RENDER_MODE": "render"}
    ) == "wayland",
    "render mode did not prefer native Wayland",
)
check(
    dp_app.choose_platform(
        {"WAYLAND_DISPLAY": "wayland-0", "DVD_PLAYER_RENDER_MODE": "wid"}
    ) == "xcb",
    "wid mode on Wayland did not select xcb",
)
check(
    dp_app.choose_platform(
        {"WAYLAND_DISPLAY": "wayland-0", "QT_QPA_PLATFORM": "xcb"}
    ) is None,
    "explicit xcb choice was overridden",
)
check(
    dp_app.choose_platform(
        {"WAYLAND_DISPLAY": "wayland-0", "QT_QPA_PLATFORM": "wayland"}
    ) is None,
    "explicit wayland choice was overridden",
)

app.processEvents()
print(f"VERIFICATION-GUI OK {_count}")
